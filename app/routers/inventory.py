"""Inventory upload (CSV).

Full-replace semantics: upload wipes the inventory_snapshot table and inserts
the rows from the CSV. After the wipe + reload, every PENDING/READY defective
item is re-evaluated so new stock levels are reflected immediately.

The CSV columns are flexible. The required columns are:

    SKU        — part_code (alias: part_code / 编码 / 配件编码 / jst_code / SKU)
    数量       — on_hand_qty (alias: qty / 库存 / 在库数量 / stock / 数量)
    仓位       — location (alias: location / 位置 / 库位 / warehouse / 仓位)

Optional: part_name (alias: 名称 / 配件名称 / desc).

Multiple locations for the same part_code are accepted — either as multiple
CSV rows with the same part_code (each row contributes its qty at its
location) or as a single row with a single location. The aggregate
``inventory_snapshot.on_hand_qty`` is the SUM across locations; the
``inventory_locations`` child table stores the per-(part_code, location)
breakdown that the UI uses to display "哪里有货" beside the part_code.
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.auth import require_role
from app.db import pool
from app.matcher import reevaluate_all_pending_ready

router = APIRouter(prefix="/api/inventory", tags=["inventory"])


class InventoryRow(BaseModel):
    part_code: str
    part_name: Optional[str] = None
    on_hand_qty: int
    location: Optional[str] = None


@router.post("/upload")
async def upload_csv(
    file: UploadFile = File(...),
    user: dict = Depends(require_role("admin")),
):
    """Upload JST inventory CSV. Expected columns (case-insensitive, flexible
    order, multiple Chinese/English aliases):

        SKU        part_code   (aliases: part_code / 编码 / 配件编码 / jst_code / SKU)
        数量       on_hand_qty (aliases: qty / 库存 / 在库数量 / stock / 数量)
        仓位       location    (aliases: location / 位置 / 库位 / warehouse / 仓位)
        part_name  part_name   (aliases: 名称 / 配件名称 / desc) — optional

    Multiple rows with the same part_code are allowed: each row contributes
    its (location, qty) to the per-location breakdown, and the SUM across
    locations becomes ``inventory_snapshot.on_hand_qty``.

    Full-replace semantics: snapshot + locations are wiped, the CSV becomes
    the new state, then every defective item is re-evaluated against the
    fresh stock.
    """
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "must be .csv")

    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("gb18030", errors="replace")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(400, "empty CSV")
    field_map = {f.lower().strip(): f for f in reader.fieldnames}

    def col(*names: str) -> Optional[str]:
        for n in names:
            if n in field_map:
                return field_map[n]
        return None

    code_col = col("part_code", "编码", "配件编码", "jst_code", "sku")
    name_col = col("part_name", "名称", "配件名称", "desc")
    qty_col = col("on_hand_qty", "qty", "库存", "在库数量", "stock", "数量")
    loc_col = col("location", "位置", "库位", "warehouse", "仓位")
    if not code_col or not qty_col:
        raise HTTPException(
            400,
            f"need part_code ({code_col or '?'}) and qty ({qty_col or '?'}) columns; "
            f"got headers: {reader.fieldnames}",
        )

    # Parse into per-location rows first; aggregate happens server-side.
    location_rows: list[tuple[str, Optional[str], int, Optional[str]]] = []
    # Track the first non-empty part_name seen per part_code so the aggregate
    # row has a sensible name even if only one of N rows carries it.
    name_by_code: dict[str, str] = {}
    for r in reader:
        code = (r.get(code_col) or "").strip()
        if not code:
            continue
        try:
            qty = int(float(r.get(qty_col) or 0))
        except ValueError:
            continue
        if qty < 0:
            # Negative quantities are nonsensical for a stock snapshot; skip
            # rather than poison the aggregate.
            continue
        loc_value = (r.get(loc_col) or "").strip() if loc_col else ""
        # Empty / whitespace-only location string is treated as "(default)".
        # The UI/API still render an empty string for display, but the
        # uniqueness constraint (part_code, location) requires *some* key.
        loc_key = loc_value if loc_value else ""
        name_value = (r.get(name_col) or "").strip() if name_col else ""
        if name_value and code not in name_by_code:
            name_by_code[code] = name_value
        location_rows.append((code, loc_key or None, qty, loc_value or None))

    if not location_rows:
        raise HTTPException(400, "no valid rows")

    # Aggregate per part_code for the inventory_snapshot row.
    aggregate: dict[str, dict] = {}
    for code, _loc_key, qty, _loc_display in location_rows:
        if code not in aggregate:
            aggregate[code] = {
                "part_code": code,
                "part_name": name_by_code.get(code),
                "on_hand_qty": 0,
                "location": None,  # populated below with first non-empty
            }
        aggregate[code]["on_hand_qty"] += qty
    # Pick the first non-empty location per part_code for the aggregate
    # ``location`` column (backward compat — old consumers still read it).
    first_loc_by_code: dict[str, str] = {}
    for code, _loc_key, _qty, loc_display in location_rows:
        if loc_display and code not in first_loc_by_code:
            first_loc_by_code[code] = loc_display
    for code, loc in first_loc_by_code.items():
        aggregate[code]["location"] = loc

    snapshot_rows = [
        (agg["part_code"], agg["part_name"], agg["on_hand_qty"], agg["location"])
        for agg in aggregate.values()
    ]

    # Collapse duplicate (part_code, location) rows into one SUM. Two CSVs
    # sometimes repeat the same bucket (e.g. fresh export on top of a stale
    # one) — taking the SUM keeps the upload idempotent against dups.
    location_breakdown: dict[tuple[str, str], int] = defaultdict(int)
    location_display: dict[tuple[str, str], str] = {}
    for code, _loc_key, qty, loc_display in location_rows:
        key = (code, (loc_display or ""))
        location_breakdown[key] += qty
        if loc_display and key not in location_display:
            location_display[key] = loc_display
    breakdown_rows = [
        (code, location_display.get((code, loc), loc), qty)
        for (code, loc), qty in location_breakdown.items()
    ]

    async with pool().acquire() as conn:
        async with conn.transaction():
            # Full-replace both the aggregate and the per-location breakdown.
            # Re-insert into ``inventory_snapshot`` so the aggregate rows
            # match the breakdown; if a part_code has no rows in the new CSV
            # it disappears entirely (matches old behaviour).
            await conn.execute("TRUNCATE inventory_snapshot, inventory_locations")
            await conn.executemany(
                """
                INSERT INTO inventory_snapshot (part_code, part_name, on_hand_qty, location)
                VALUES ($1, $2, $3, $4)
                """,
                snapshot_rows,
            )
            await conn.executemany(
                """
                INSERT INTO inventory_locations (part_code, location, qty)
                VALUES ($1, $2, $3)
                """,
                breakdown_rows,
            )
            await conn.execute(
                """
                INSERT INTO audit_log (user_id, action, entity_type, details)
                VALUES ($1, 'upload_inventory', 'inventory_snapshot', $2::jsonb)
                """,
                user["id"],
                f'{{"rows": {len(snapshot_rows)}, "locations": {len(breakdown_rows)}}}',
            )

    # Re-evaluate every PENDING/READY defective against the fresh stock
    # in a single SQL round-trip (was O(N) per-item before).
    flip = await reevaluate_all_pending_ready()
    status_flip = {"to_pending": flip["to_pending"], "to_ready": flip["to_ready"]}
    reevaluated = flip["no_change"] + flip["to_pending"] + flip["to_ready"]

    return {
        "inserted": len(snapshot_rows),
        "locations": len(breakdown_rows),
        "reevaluated": reevaluated,
        "status_flip": status_flip,
    }


@router.get("/summary")
async def summary(user: dict = Depends(require_role("admin", "repair"))):
    async with pool().acquire() as conn:
        inv_row = await conn.fetchrow(
            """
            SELECT
                COUNT(*)::int AS sku_count,
                COALESCE(SUM(on_hand_qty), 0)::int AS total_units,
                MAX(updated_at) AS last_updated
            FROM inventory_snapshot
            """
        )
        # Per-pallet status counts so the page can show READY/PENDING split.
        counts_row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE status='PENDING')::int AS pending_items,
                COUNT(*) FILTER (WHERE status='PENDING')::int +
                COUNT(*) FILTER (WHERE status='READY')::int AS open_items
            FROM defective_items
            """
        )
        # Distinct pallet count still in PENDING.
        pallet_row = await conn.fetchrow(
            """
            SELECT COUNT(DISTINCT pallet_no)::int AS pending_pallets
            FROM defective_items WHERE status='PENDING'
            """
        )
    return {
        "sku_count": inv_row["sku_count"] if inv_row else 0,
        "total_units": inv_row["total_units"] if inv_row else 0,
        "last_updated": inv_row["last_updated"].isoformat() if inv_row and inv_row["last_updated"] else None,
        "pending_items": counts_row["pending_items"] if counts_row else 0,
        "open_items": counts_row["open_items"] if counts_row else 0,
        "pending_pallets": pallet_row["pending_pallets"] if pallet_row else 0,
    }


@router.get("/preview/{part_code}")
async def preview_one(
    part_code: str,
    user: dict = Depends(require_role("admin", "repair", "returns")),
):
    """Look up a part_code (use this for ad-hoc repair queries).

    Returns the aggregate row plus the per-location breakdown so the caller
    can show "哪几个仓位有多少" alongside the aggregate qty.
    """
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT part_code, part_name, on_hand_qty, location, updated_at
            FROM inventory_snapshot WHERE part_code = $1
            """,
            part_code,
        )
        if row is None:
            raise HTTPException(404, f"no inventory for {part_code!r}")
        locations = await conn.fetch(
            """
            SELECT COALESCE(NULLIF(location, ''), '') AS location, qty::int AS qty
            FROM inventory_locations
            WHERE part_code = $1
            ORDER BY qty DESC, location ASC
            """,
            part_code,
        )
    d = dict(row)
    d["updated_at"] = d["updated_at"].isoformat() if d["updated_at"] else None
    d["inventory_locations"] = [
        {"location": r["location"] or "", "qty": int(r["qty"] or 0)}
        for r in locations
    ]
    return d
