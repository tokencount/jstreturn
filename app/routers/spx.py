"""SPX shipment processing: upload, lookup, pick-list.

Workflow:
  1. Upload SPX Excel → rows parsed & stored in spx_shipments
  2. Scan Tracking No. → return SKUs + our location + employee-entered location
  3. Admin print pick-list → filter by date, show all AWBs ready for picking
  4. JST fetcher for new-pick locations (拣货仓位 / 主仓 / exclude 配件)
"""
from __future__ import annotations

import io
import json
import logging
import re
import uuid
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import openpyxl
from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.auth import require_role
from app.db import pool

router = APIRouter(prefix="/api/spx", tags=["spx"])
log = logging.getLogger("jstreturn.spx")
KLT = ZoneInfo("Asia/Kuala_Lumpur")

# ---------------------------------------------------------------------------
# SKU parsing helpers
# ---------------------------------------------------------------------------

# Matches an order-variant suffix, e.g. ST5822PK-001 / ST5822PK-WHT1
# → base=ST5822PK.  The source order suffix is 1–4 alphanumeric characters.
SEQ_PATTERN = re.compile(r"^(.+?)-([A-Za-z0-9]{1,4})$")


def parse_sku(raw: str) -> tuple[str, int]:
    """Return (sku, qty) from a raw cell.

    Rules:
    - 'SKU*2'  → qty=2
    - 'SKU'    → qty=1
    - Trailing whitespace and surrounding newlines stripped.
    """
    raw = raw.strip()
    m = re.match(r"^(.+?)\s*\*\s*(\d+)$", raw)
    if m:
        return m.group(1).strip(), int(m.group(2))
    return raw, 1


def base_sku(sku: str) -> str:
    """Strip a trailing ``-xxxx`` order-variant suffix; return the base SKU."""
    m = SEQ_PATTERN.match(sku)
    if m:
        return m.group(1)
    return sku


def inventory_candidates(sku: str) -> list[str]:
    """Return inventory codes in matching priority for an order SKU.

    Some SPX orders use an ``HE-`` prefixed variant while JST stores the
    sellable pick SKU without that prefix, e.g. ``HE-AG7421GR-010`` maps to
    ``AG7421GR``.  That prefix fallback is only valid after stripping an
    order suffix, so a normal non-variant ``HE-*`` SKU is never rewritten.
    """
    base = base_sku(sku)
    candidates = [sku]
    if base != sku:
        candidates.append(base)
        if base.upper().startswith("HE-") and len(base) > 3:
            candidates.append(base[3:])
    return candidates


async def inventory_match_sku(conn, sku: str) -> str:
    """Return the SKU used for inventory lookup, preserving the order SKU.

    A suffixed order SKU falls back to its base only when the exact code is
    unavailable and the base code exists in an inventory source.  Callers
    store this separately as ``matched_sku``; ``sku`` remains the original
    accessory/order SKU for traceability.
    """
    async def has_parts_stock(candidate: str) -> bool:
        """The parts snapshot is the only source with an actual quantity."""
        return bool(await conn.fetchval(
            """
            SELECT EXISTS(
                SELECT 1 FROM inventory_snapshot
                WHERE UPPER(TRIM(part_code)) = UPPER(TRIM($1))
                  AND on_hand_qty > 0
            )
            """,
            candidate,
        ))

    async def has_base_inventory(candidate: str) -> bool:
        """A base SKU may resolve from either the stock snapshot or catalogue."""
        return bool(await conn.fetchval(
            """
            SELECT EXISTS(
                SELECT 1 FROM spx_all_sku_inventory
                WHERE UPPER(TRIM(sku)) = UPPER(TRIM($1))
            ) OR EXISTS(
                SELECT 1 FROM inventory_snapshot
                WHERE UPPER(TRIM(part_code)) = UPPER(TRIM($1))
                  AND on_hand_qty > 0
            )
            """,
            candidate,
        ))

    # All-SKU is a location catalogue, not a quantity source.  Do not let an
    # exact catalogue row suppress the ``-001/-002/-003`` fallback.
    candidates = inventory_candidates(sku)
    if await has_parts_stock(candidates[0]):
        return sku
    for candidate in candidates[1:]:
        if await has_base_inventory(candidate):
            return candidate
    return sku


def decode_items_json(value) -> list[dict]:
    """Normalize asyncpg JSONB output (string by default) to a list."""
    if value is None:
        return []
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise ValueError("items_json must be a list")
    return value


def parse_create_time(value: str) -> Optional[datetime]:
    """Parse common SPX timestamps and interpret naive values as Malaysia time."""
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        for fmt in (
            "%d/%m/%Y %H:%M:%S",
            "%d/%m/%Y %H:%M",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
            "%d-%m-%Y %H:%M:%S",
            "%d-%m-%Y %H:%M",
        ):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    return parsed.replace(tzinfo=KLT) if parsed.tzinfo is None else parsed


async def resolve_location(conn, sku: str) -> Optional[str]:
    """Look up location for a SKU.

    1. Try exact SKU
    2. Try base SKU (sku without -001/-002/... suffix)
    3. Return None if neither found.
    """
    row = await conn.fetchrow(
        "SELECT location FROM inventory_snapshot WHERE part_code = $1 AND on_hand_qty > 0 LIMIT 1",
        sku,
    )
    if row:
        return row["location"] or ""

    base = base_sku(sku)
    if base != sku:
        row = await conn.fetchrow(
            "SELECT location FROM inventory_snapshot WHERE part_code = $1 AND on_hand_qty > 0 LIMIT 1",
            base,
        )
        if row:
            return row["location"] or ""

    return None


async def resolve_parts_sku_details(conn, sku: str) -> Optional[dict]:
    """Return parts-inventory image/location, trying exact then base SKU."""
    row = await conn.fetchrow(
        """SELECT location, COALESCE(image_url, '') AS image_url
           FROM inventory_snapshot
           WHERE UPPER(TRIM(part_code)) = UPPER(TRIM($1))
           LIMIT 1""",
        sku,
    )
    if row:
        return dict(row)

    base = base_sku(sku)
    if base != sku:
        row = await conn.fetchrow(
            """SELECT location, COALESCE(image_url, '') AS image_url
               FROM inventory_snapshot
               WHERE UPPER(TRIM(part_code)) = UPPER(TRIM($1))
               LIMIT 1""",
            base,
        )
        if row:
            return dict(row)
    return None


async def resolve_all_sku_details(conn, sku: str) -> Optional[dict]:
    """Read SPX SKU details from the daily unified inventory snapshot."""
    row = await conn.fetchrow(
        """SELECT location, COALESCE(image_url, '') AS image_url
           FROM inventory_snapshot
           WHERE UPPER(TRIM(part_code)) = UPPER(TRIM($1))
             AND on_hand_qty > 0
           LIMIT 1""",
        sku,
    )
    if row:
        return dict(row)

    base = base_sku(sku)
    if base != sku:
        row = await conn.fetchrow(
            """SELECT location, COALESCE(image_url, '') AS image_url
               FROM inventory_snapshot
               WHERE UPPER(TRIM(part_code)) = UPPER(TRIM($1))
                 AND on_hand_qty > 0
               LIMIT 1""",
            base,
        )
        if row:
            return dict(row)
    return None


async def resolve_original_sku_image(conn, sku: str) -> str:
    """Return an image only for the exact order SKU.

    A matched SKU can be a stock replacement, so its image must not be used
    in shipment lookup: staff need to see the product printed on the waybill.
    """
    row = await conn.fetchrow(
        """SELECT COALESCE(image_url, '') AS image_url
           FROM inventory_snapshot
           WHERE UPPER(TRIM(part_code)) = UPPER(TRIM($1))
           ORDER BY on_hand_qty DESC
           LIMIT 1""",
        sku,
    )
    return str(row["image_url"] or "") if row else ""


# ---------------------------------------------------------------------------
# Excel parser
# ---------------------------------------------------------------------------

# Column indices (0-based) in the SPX report
COL_REPORT_TIME   = 0   # 'Report Download Time'
COL_TRACKING      = 0   # 'Tracking No.'
COL_ITEM_IN_PARCEL = 28 # 'Item in Parcel'
COL_NO_OF_ITEMS   = 29  # 'No. of item in Parcel'
COL_ITEM_LIST     = 30  # 'Item List'
COL_CREATE_TIME   = 4   # 'Create Time'


def _col_idx(ws, name: str) -> int:
    """Find column index by header name (case-insensitive)."""
    for col in ws.iter_cols(min_row=1, max_row=1):
        for cell in col:
            if str(cell.value or "").strip().lower() == name.lower():
                return cell.column - 1
    raise ValueError(f"Column not found: {name!r}")


def _parse_item_in_parcel(raw: str) -> list[tuple[str, int, str]]:
    """Parse 'Item in Parcel' cell → list of (sku, qty, employee_location).

    Format per line in cell: 'SKU\n位置'
    Multiple lines separated by \n.
    Returns [(sku, qty, employee_location_or_empty), ...]
    """
    lines = [ln.strip() for ln in (raw or "").split("\n") if ln.strip()]
    result = []
    i = 0
    while i < len(lines):
        sku, qty = parse_sku(lines[i])
        loc = ""
        # If next non-empty line doesn't look like a SKU (no letters, just a location pattern),
        # treat it as the employee-entered location.
        if i + 1 < len(lines):
            next_line = lines[i + 1]
            # A location line typically has digits/hyphens but no typical SKU letters
            # or is just 6+ chars with hyphens. Be permissive: if it's not a SKU with qty marker.
            if not re.search(r"[A-Za-z]{2,}", next_line) and re.search(r"[\d\w]{4,}", next_line):
                loc = next_line
                i += 1
        result.append((sku, qty, loc))
        i += 1
    return result


def _col(ws, name: str) -> int:
    try:
        return _col_idx(ws, name)
    except ValueError:
        return -1


def _normalize_header(value) -> str:
    """Normalize exported Excel headers for reliable matching."""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").strip().lower())


SPX_HEADER_ALIASES = {
    "tracking": {
        "trackingno", "trackingnumber", "trackingid", "awb", "awbno",
        "运单号", "物流单号", "追踪号",
    },
    "parcel": {
        "iteminparcel", "itemsinparcel", "itemlist", "sku", "skulist",
        "包裹商品", "包裹内商品", "商品sku", "商品",
    },
    "create_time": {
        "createtime", "createdtime", "ordertime", "下单时间", "创建时间",
    },
}


def _find_spx_header(ws) -> tuple[int, dict[str, Optional[int]]] | None:
    """Find a usable SPX header row, tolerating titles and header variants."""
    for row_number, row in enumerate(
        ws.iter_rows(min_row=1, max_row=50, values_only=True), start=1
    ):
        normalized = [_normalize_header(value) for value in row]
        found = {
            key: next((i for i, value in enumerate(normalized) if value in aliases), None)
            for key, aliases in SPX_HEADER_ALIASES.items()
        }
        if found["tracking"] is not None and found["parcel"] is not None:
            return row_number, found
    return None


def _find_fixed_layout_start(ws) -> Optional[int]:
    """Find the first data row in SPX's stable 31-column export layout.

    Some regional SPX exports translate or omit the header labels while
    retaining the documented column positions (tracking=1, create time=5,
    item in parcel=29). Use that layout only when both required data cells
    are populated, so unrelated workbooks are not silently accepted.
    """
    if ws.max_column is not None and ws.max_column <= COL_ITEM_IN_PARCEL:
        return None
    for row_number, row in enumerate(
        ws.iter_rows(min_row=1, max_row=100, values_only=True), start=1
    ):
        if len(row) <= COL_ITEM_IN_PARCEL:
            continue
        tracking = str(row[COL_TRACKING] or "").strip()
        parcel = str(row[COL_ITEM_IN_PARCEL] or "").strip()
        normalized_tracking = _normalize_header(tracking)
        if (
            tracking
            and parcel
            and bool(re.search(r"\d", tracking))
            and not bool(re.search(r"[\u4e00-\u9fff]", tracking))
            and normalized_tracking not in SPX_HEADER_ALIASES["tracking"]
            and normalized_tracking not in {"reportdownloadtime", "报表下载时间"}
        ):
            return row_number
    return None


def parse_spx_xlsx(raw: bytes) -> list[dict]:
    """Parse SPX Excel workbook → list of shipment dicts.

    Returns [ {
        tracking_no, create_time, items: [(sku, qty, employee_location), ...]
    }, ... ]
    """
    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    try:
        # Some SPX exports contain a broken worksheet dimension of A1:A1 even
        # though the XML contains the full table. In read-only mode openpyxl
        # trusts that marker and otherwise exposes only the first cell.
        for sheet in wb.worksheets:
            if sheet.max_row == 1 and sheet.max_column == 1:
                sheet.reset_dimensions()
        selected = next(
            ((ws, match) for ws in wb.worksheets if (match := _find_spx_header(ws)) is not None),
            None,
        )
        if selected is not None:
            ws, (header_row_number, col_map) = selected
            data_start_row = header_row_number + 1
            tracking_col = col_map["tracking"]
            parcel_col = col_map["parcel"]
            time_col = col_map.get("create_time")
        else:
            fixed = next(
                ((ws, start) for ws in wb.worksheets if (start := _find_fixed_layout_start(ws)) is not None),
                None,
            )
            if fixed is None:
                raise ValueError(
                    "required SPX columns not found: need Tracking No. and Item in Parcel/SKU"
                )
            ws, data_start_row = fixed
            tracking_col = COL_TRACKING
            parcel_col = COL_ITEM_IN_PARCEL
            time_col = COL_CREATE_TIME

        rows = []
        for row in ws.iter_rows(min_row=data_start_row, values_only=True):
            if not row or all(v is None for v in row):
                continue
            tracking = str(row[tracking_col] or "").strip()
            if not tracking:
                continue
            create_time = ""
            if time_col is not None and time_col < len(row) and row[time_col]:
                create_time = str(row[time_col])
            items = []
            if parcel_col < len(row) and row[parcel_col]:
                items = _parse_item_in_parcel(str(row[parcel_col]))
            if items:
                rows.append({
                    "tracking_no": tracking,
                    "create_time": create_time,
                    "items": items,
                })
        return rows
    finally:
        wb.close()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ShipmentItemOut(BaseModel):
    sku: str
    matched_sku: str
    qty: int
    employee_location: str
    our_location: Optional[str] = None  # None = not in stock
    image_url: str = ""


class ShipmentOut(BaseModel):
    tracking_no: str
    create_time: str
    items: list[ShipmentItemOut]


class UploadResult(BaseModel):
    total_rows: int
    saved_rows: int
    batch_id: str
    batch_name: str


class PickListItem(BaseModel):
    tracking_no: str
    create_time: str
    sku: str
    matched_sku: str
    qty: int
    our_location: Optional[str] = None
    employee_location: str = ""


class PickListOut(BaseModel):
    batch_ids: list[str]
    batch_name: str
    items: list[PickListItem]
    total: int


class SpxBatchOut(BaseModel):
    id: str
    name: str
    uploaded_at: str
    shipment_count: int
    item_count: int


class AllSkuRow(BaseModel):
    sku: str
    location: str
    image_url: str = ""
    on_hand_qty: int = 0


class AllSkuImport(BaseModel):
    rows: list[AllSkuRow]


# ---------------------------------------------------------------------------
# DB: create table if not exists
# ---------------------------------------------------------------------------

SPX_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS public.spx_shipments (
    id              BIGSERIAL PRIMARY KEY,
    tracking_no     TEXT NOT NULL,
    create_time     TIMESTAMPTZ,
    items_json      JSONB NOT NULL,   -- [{sku, matched_sku, qty, employee_location}]
    uploaded_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (tracking_no)
);
CREATE INDEX IF NOT EXISTS idx_spx_tracking ON public.spx_shipments (tracking_no);
CREATE INDEX IF NOT EXISTS idx_spx_uploaded ON public.spx_shipments (uploaded_at);

CREATE TABLE IF NOT EXISTS public.spx_upload_batches (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_filename TEXT NOT NULL DEFAULT ''
);
ALTER TABLE public.spx_shipments ADD COLUMN IF NOT EXISTS batch_id TEXT;
CREATE INDEX IF NOT EXISTS idx_spx_batch ON public.spx_shipments (batch_id);
"""

ALL_SKU_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS public.spx_all_sku_inventory (
    sku         TEXT PRIMARY KEY,
    location    TEXT NOT NULL,
    image_url   TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_spx_all_sku_location ON public.spx_all_sku_inventory (location);
"""


async def ensure_spx_table():
    async with pool().acquire() as conn:
        await conn.execute(SPX_TABLE_SQL)
        # Old imports predate upload batches. Keep them usable as one
        # historical batch per upload date, without guessing an individual
        # file boundary that was never recorded.
        await conn.execute(
            """
            INSERT INTO spx_upload_batches (id, name, uploaded_at)
            SELECT 'legacy-' || day_key,
                   '历史批次 ' || day_label,
                   min(uploaded_at)
            FROM (
                SELECT uploaded_at,
                       to_char(uploaded_at AT TIME ZONE 'Asia/Kuala_Lumpur', 'YYYYMMDD') AS day_key,
                       to_char(uploaded_at AT TIME ZONE 'Asia/Kuala_Lumpur', 'YYYY-MM-DD') AS day_label
                FROM spx_shipments
                WHERE batch_id IS NULL
            ) legacy
            GROUP BY day_key, day_label
            ON CONFLICT (id) DO NOTHING
            """
        )
        await conn.execute(
            """
            UPDATE spx_shipments
            SET batch_id = 'legacy-' || to_char(uploaded_at AT TIME ZONE 'Asia/Kuala_Lumpur', 'YYYYMMDD')
            WHERE batch_id IS NULL
            """
        )


async def ensure_all_sku_table():
    async with pool().acquire() as conn:
        await conn.execute(ALL_SKU_TABLE_SQL)


@router.post("/all-sku/import")
async def import_all_sku(
    payload: AllSkuImport = Body(...),
    user: dict = Depends(require_role("admin")),
):
    """Atomically replace the separate new-goods SKU/location catalogue."""
    normalized = {}
    for row in payload.rows:
        sku = row.sku.strip()
        location = row.location.strip()
        if sku and location:
            normalized[sku] = (location, row.image_url.strip())
    if not normalized:
        raise HTTPException(400, "all-SKU import is empty")
    await ensure_all_sku_table()
    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM spx_all_sku_inventory")
            await conn.executemany(
                "INSERT INTO spx_all_sku_inventory (sku, location, image_url) VALUES ($1, $2, $3)",
                [(sku, loc, image) for sku, (loc, image) in normalized.items()],
            )
    return {"ok": True, "count": len(normalized)}


@router.get("/all-sku")
async def list_all_sku(
    q: str = "",
    limit: int = Query(200, ge=1, le=1000),
    user: dict = Depends(require_role("admin", "repair")),
):
    term = q.strip()
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT part_code AS sku, location, COALESCE(image_url, '') AS image_url,
                   on_hand_qty, updated_at
            FROM inventory_snapshot
            WHERE ($1 = '' OR part_code ILIKE '%' || $1 || '%' OR location ILIKE '%' || $1 || '%')
            ORDER BY part_code
            LIMIT $2
            """,
            term, limit,
        )
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM inventory_snapshot WHERE ($1 = '' OR part_code ILIKE '%' || $1 || '%' OR location ILIKE '%' || $1 || '%')",
            term,
        )
    return {"count": int(total or 0), "items": [dict(row) for row in rows]}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/upload")
async def upload_spx(
    file: UploadFile = File(...),
    user: dict = Depends(require_role("admin", "returns")),
):
    """Upload SPX Excel file.

    Parses Tracking No. + Item in Parcel (SKU + employee location),
    saves to spx_shipments table, and returns a summary.
    """
    if not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(400, "must be .xlsx")

    await ensure_spx_table()
    await ensure_all_sku_table()

    raw = await file.read()
    try:
        rows = parse_spx_xlsx(raw)
    except Exception as exc:
        log.exception("SPX parse error")
        raise HTTPException(400, f"parse error: {exc}") from exc

    if not rows:
        raise HTTPException(400, "no shipment rows found")

    batch_id = str(uuid.uuid4())
    batch_time = datetime.now(KLT)
    batch_name = f"{batch_time:%Y-%m-%d %H:%M} · {file.filename}"
    saved = 0
    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO spx_upload_batches (id, name, uploaded_at, source_filename) VALUES ($1, $2, $3, $4)",
                batch_id, batch_name, batch_time, file.filename,
            )
            for row in rows:
                items_json = [
                    {
                        "sku": sku,
                        "matched_sku": await inventory_match_sku(conn, sku),
                        "qty": qty,
                        "employee_location": loc,
                    }
                    for sku, qty, loc in row["items"]
                ]
                create_ts = None
                if row["create_time"]:
                    create_ts = parse_create_time(row["create_time"])
                result = await conn.execute(
                    """
                    INSERT INTO spx_shipments (tracking_no, create_time, items_json, batch_id)
                    VALUES ($1, $2, $3::jsonb, $4)
                    ON CONFLICT (tracking_no) DO UPDATE SET
                        create_time = EXCLUDED.create_time,
                        items_json = EXCLUDED.items_json,
                        batch_id = EXCLUDED.batch_id,
                        uploaded_at = NOW()
                    """,
                    row["tracking_no"], create_ts,
                    json.dumps(items_json, ensure_ascii=False), batch_id,
                )
                if result.startswith("INSERT") or result.startswith("UPDATE"):
                    saved += 1

    return UploadResult(total_rows=len(rows), saved_rows=saved, batch_id=batch_id, batch_name=batch_name)


@router.get("/lookup/{tracking_no}", response_model=ShipmentOut)
async def lookup_tracking(
    tracking_no: str,
    user: dict = Depends(require_role("admin", "repair")),
):
    """Scan a Tracking No. → return SKUs, our location, employee location."""

    await ensure_spx_table()
    await ensure_all_sku_table()
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """SELECT tracking_no,
                      COALESCE(create_time, uploaded_at) AS effective_time,
                      items_json
               FROM spx_shipments
               WHERE UPPER(TRIM(tracking_no)) = UPPER(TRIM($1))""",
            tracking_no,
        )

    if not row:
        raise HTTPException(404, f"Tracking {tracking_no!r} not found")

    items_out = []
    async with pool().acquire() as conn:
        for item in decode_items_json(row["items_json"]):
            sku = item.get("sku", "")
            # Match against the current inventory snapshot at read time so
            # existing waves receive new suffix/prefix replacements too.
            matched_sku = await inventory_match_sku(conn, sku)
            all_sku = await resolve_all_sku_details(conn, matched_sku)
            parts_sku = await resolve_parts_sku_details(conn, matched_sku)
            original_image_url = await resolve_original_sku_image(conn, sku)
            our_loc = ((all_sku or {}).get("location")
                       or (parts_sku or {}).get("location")
                       or "无库存")
            items_out.append(ShipmentItemOut(
                sku=sku,
                matched_sku=matched_sku,
                qty=item.get("qty", 1),
                employee_location=item.get("employee_location", ""),
                our_location=our_loc,
                image_url=original_image_url,
            ))

    return ShipmentOut(
        tracking_no=row["tracking_no"],
        create_time=str(row["effective_time"] or ""),
        items=items_out,
    )


@router.get("/batches", response_model=list[SpxBatchOut])
async def list_batches(
    user: dict = Depends(require_role("admin")),
):
    """List upload waves that can be turned into a pick list."""
    await ensure_spx_table()
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT b.id, b.name, b.uploaded_at,
                   COUNT(s.id)::int AS shipment_count,
                   COALESCE(SUM(jsonb_array_length(s.items_json)), 0)::int AS item_count
            FROM spx_upload_batches b
            LEFT JOIN spx_shipments s ON s.batch_id = b.id
            GROUP BY b.id, b.name, b.uploaded_at
            HAVING COUNT(s.id) > 0
            ORDER BY b.uploaded_at DESC
            """
        )
    return [
        SpxBatchOut(
            id=row["id"], name=row["name"], uploaded_at=str(row["uploaded_at"]),
            shipment_count=row["shipment_count"], item_count=row["item_count"],
        )
        for row in rows
    ]


@router.get("/pick-list", response_model=PickListOut)
async def pick_list(
    batch_ids: list[str] = Query(..., min_length=1),
    user: dict = Depends(require_role("admin")),
):
    """Print one merged pick-list for one or more SPX upload waves."""

    await ensure_spx_table()
    await ensure_all_sku_table()
    selected_ids = list(dict.fromkeys(batch_id.strip() for batch_id in batch_ids if batch_id.strip()))
    if not selected_ids:
        raise HTTPException(400, "at least one batch is required")
    async with pool().acquire() as conn:
        batches = await conn.fetch(
            "SELECT id, name FROM spx_upload_batches WHERE id = ANY($1::text[]) ORDER BY uploaded_at DESC",
            selected_ids,
        )
        if len(batches) != len(selected_ids):
            raise HTTPException(404, "batch not found")
        rows = await conn.fetch(
            """
            SELECT tracking_no,
                   COALESCE(create_time, uploaded_at) AS effective_time,
                   items_json
            FROM spx_shipments
            WHERE batch_id = ANY($1::text[])
            ORDER BY uploaded_at, tracking_no
            """,
            selected_ids,
        )

    items_out: list[PickListItem] = []
    async with pool().acquire() as conn:
        for row in rows:
            for item in decode_items_json(row["items_json"]):
                sku = item.get("sku", "")
                # Always use the current inventory match, including for
                # waves uploaded before a replacement rule was added.
                matched_sku = await inventory_match_sku(conn, sku)
                all_sku = await resolve_all_sku_details(conn, matched_sku)
                parts_sku = await resolve_parts_sku_details(conn, matched_sku)
                our_loc = ((all_sku or {}).get("location")
                           or (parts_sku or {}).get("location")
                           or "无库存")
                items_out.append(PickListItem(
                    tracking_no=row["tracking_no"],
                    create_time=str(row["effective_time"] or ""),
                    sku=sku,
                    matched_sku=matched_sku,
                    qty=item.get("qty", 1),
                    our_location=our_loc,
                    employee_location=item.get("employee_location", ""),
                ))

    return PickListOut(
        batch_ids=selected_ids,
        batch_name="、".join(row["name"] for row in batches),
        items=items_out,
        total=len(items_out),
    )
