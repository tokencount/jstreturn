"""Inventory upload (CSV).

Full-replace semantics: upload wipes the inventory_snapshot table and inserts
the rows from the CSV. After the wipe + reload, every PENDING/READY defective
item is re-evaluated so new stock levels are reflected immediately.

The CSV columns are flexible. The required columns are:

    SKU        — part_code (alias: part_code / 编码 / 配件编码 / jst_code / SKU)
    数量       — on_hand_qty (alias: qty / 库存 / 在库数量 / stock / 数量)
    仓位       — location (alias: location / 位置 / 库位 / warehouse / 仓位)

Optional: part_name (alias: 名称 / 配件名称 / desc) and image_url
(alias: image / 图片 / 图片链接).

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
import asyncio
from collections import OrderedDict, defaultdict
from time import monotonic
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from PIL import Image, ImageOps
from starlette.concurrency import run_in_threadpool

from app.auth import require_role
from app.db import pool
from app.matcher import reevaluate_all_pending_ready

router = APIRouter(prefix="/api/inventory", tags=["inventory"])

# Hosts emitted by JST inventory exports.  Keep this an explicit allowlist:
# the image endpoint fetches upstream content and must never become a general
# proxy.  JST currently returns both its OSS URLs and marketplace/CDN URLs.
INVENTORY_IMAGE_HOSTS = frozenset({
    "jst-yikan-picspace.oss-ap-southeast-1.aliyuncs.com",
    "jst-yikan-picspace-new.oss-ap-southeast-1.aliyuncs.com",
    "p16-oec-sg.ibyteimg.com",
    "p16-oec-va.ibyteimg.com",
    "p16-oec-general.tiktokcdn.com",
    "p19-oec-sg.ibyteimg.com",
    "cf.shopee.com.my",
    "cf.shopee.ph",
    "s-cf-tw.shopeesz.com",
    "cbu01.alicdn.com",
    "s.alicdn.com",
    "my-live.slatic.net",
    "sg-test-11.slatic.net",
})
MAX_IMAGE_BYTES = 10 * 1024 * 1024
# The shipment scanner often revisits the same popular SKU.  JST image hosts
# are comparatively slow to establish a new connection, so keep a bounded
# process-local LRU cache in front of the upstream request.  Browser cache
# headers remain the first layer; this covers different staff browsers too.
IMAGE_CACHE_TTL_SECONDS = 6 * 60 * 60
IMAGE_CACHE_MAX_ITEMS = 512
# Hard byte limit matters more than item count: JST originals vary widely in
# size.  Keep scanner warm-cache safely inside the existing Render instance.
IMAGE_CACHE_MAX_BYTES = 64 * 1024 * 1024
_image_cache: OrderedDict[str, tuple[float, bytes, str]] = OrderedDict()
_image_cache_bytes = 0
# Keep the inexpensive SKU -> source URL mapping separately.  Previously a
# thumbnail cache hit still ran the normalized-SKU SQL query first.  With a
# half-million-row image catalogue that lookup dominated scanner latency.
IMAGE_URL_CACHE_MAX_ITEMS = 8_192
_image_url_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
_image_client: httpx.AsyncClient | None = None


def _cached_image(image_url: str) -> tuple[bytes, str] | None:
    global _image_cache_bytes
    cached = _image_cache.get(image_url)
    if not cached:
        return None
    expires_at, content, content_type = cached
    if expires_at <= monotonic():
        _image_cache.pop(image_url, None)
        _image_cache_bytes -= len(content)
        return None
    _image_cache.move_to_end(image_url)
    return content, content_type


def _store_cached_image(image_url: str, content: bytes, content_type: str) -> None:
    global _image_cache_bytes
    # An oversized single source image is still served, but never retained.
    if len(content) > IMAGE_CACHE_MAX_BYTES:
        return
    old = _image_cache.pop(image_url, None)
    if old:
        _image_cache_bytes -= len(old[1])
    while _image_cache and (
        len(_image_cache) >= IMAGE_CACHE_MAX_ITEMS
        or _image_cache_bytes + len(content) > IMAGE_CACHE_MAX_BYTES
    ):
        _, (_, evicted, _) = _image_cache.popitem(last=False)
        _image_cache_bytes -= len(evicted)
    _image_cache[image_url] = (monotonic() + IMAGE_CACHE_TTL_SECONDS, content, content_type)
    _image_cache.move_to_end(image_url)
    _image_cache_bytes += len(content)


def _image_sku_key(part_code: str) -> str:
    return part_code.strip().upper()


def _cached_image_url(part_code: str) -> str | None:
    key = _image_sku_key(part_code)
    cached = _image_url_cache.get(key)
    if not cached:
        return None
    expires_at, image_url = cached
    if expires_at <= monotonic():
        _image_url_cache.pop(key, None)
        return None
    _image_url_cache.move_to_end(key)
    return image_url


def _store_cached_image_url(part_code: str, image_url: str) -> None:
    key = _image_sku_key(part_code)
    _image_url_cache[key] = (monotonic() + IMAGE_CACHE_TTL_SECONDS, image_url)
    _image_url_cache.move_to_end(key)
    while len(_image_url_cache) > IMAGE_URL_CACHE_MAX_ITEMS:
        _image_url_cache.popitem(last=False)


def _clear_image_caches() -> None:
    """Invalidate source mappings when a stock/image upload changes URLs."""
    global _image_cache_bytes
    _image_cache.clear()
    _image_cache_bytes = 0
    _image_url_cache.clear()


def _image_http_client() -> httpx.AsyncClient:
    global _image_client
    if _image_client is None or _image_client.is_closed:
        # Reuse TCP/TLS connections to JST instead of paying a connection
        # setup cost for every SKU image in one scanned waybill.
        _image_client = httpx.AsyncClient(timeout=10.0, follow_redirects=False)
    return _image_client


def _make_thumbnail(content: bytes) -> tuple[bytes, str]:
    """Create a scanner-friendly WebP preview without blocking the event loop."""
    with Image.open(io.BytesIO(content)) as source:
        image = ImageOps.exif_transpose(source)
        image.thumbnail((400, 400), Image.Resampling.LANCZOS)
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA" if "transparency" in image.info else "RGB")
        output = io.BytesIO()
        image.save(output, format="WEBP", quality=78, method=4)
        return output.getvalue(), "image/webp"


async def _load_image(image_url: str, thumbnail: bool) -> tuple[bytes, str, str]:
    """Return a proxied image and keep both scanner and warm-up paths identical."""
    parsed = urlparse(image_url)
    if parsed.scheme != "https" or parsed.hostname not in INVENTORY_IMAGE_HOSTS:
        raise ValueError("unsupported image host")
    cache_key = f"{image_url}|thumb" if thumbnail else image_url
    cached = _cached_image(cache_key)
    if cached:
        content, content_type = cached
        return content, content_type, "HIT"
    upstream = await _image_http_client().get(image_url)
    if upstream.status_code != 200:
        raise ValueError("image upstream unavailable")
    content_type = upstream.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    content = upstream.content
    if not content_type.startswith("image/") or len(content) > MAX_IMAGE_BYTES:
        raise ValueError("invalid image response")
    if thumbnail:
        content, content_type = await run_in_threadpool(_make_thumbnail, content)
    _store_cached_image(cache_key, content, content_type)
    return content, content_type, "MISS"


async def prewarm_image_thumbnails(part_codes: list[str]) -> None:
    """Warm scanner thumbnails after a shipment wave upload.

    This is intentionally best-effort and bounded: a bad JST image must not
    fail an order upload, and one wave cannot evict the whole process cache.
    """
    normalized = sorted({_image_sku_key(code) for code in part_codes if code and code.strip()})[:200]
    if not normalized:
        return
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            WITH wanted AS (
                SELECT DISTINCT UPPER(TRIM(code)) AS sku
                FROM UNNEST($1::TEXT[]) AS requested(code)
            ), images AS (
                SELECT UPPER(TRIM(part_code)) AS sku, image_url, 0 AS source_priority
                FROM inventory_snapshot
                WHERE COALESCE(image_url, '') <> ''
                UNION ALL
                SELECT UPPER(TRIM(part_code)) AS sku, image_url, 1 AS source_priority
                FROM inventory_image_catalog
                WHERE COALESCE(image_url, '') <> ''
            )
            SELECT DISTINCT ON (images.sku) images.sku, images.image_url
            FROM images JOIN wanted USING (sku)
            ORDER BY images.sku, images.source_priority
            """,
            normalized,
        )
    semaphore = asyncio.Semaphore(4)

    async def warm(row) -> None:
        try:
            async with semaphore:
                _store_cached_image_url(row["sku"], row["image_url"])
                await _load_image(row["image_url"], thumbnail=True)
        except (httpx.HTTPError, ValueError, OSError):
            # A later scan can retry a temporarily unavailable JST image.
            return

    await asyncio.gather(*(warm(row) for row in rows))


class InventoryRow(BaseModel):
    part_code: str
    part_name: Optional[str] = None
    image_url: Optional[str] = None
    on_hand_qty: int
    location: Optional[str] = None


class InventoryImageCatalogRow(BaseModel):
    """One exact JST product SKU image, including zero-stock products."""

    part_code: str
    image_url: str


@router.post("/image-catalog/upload")
async def upload_image_catalog(
    rows: list[InventoryImageCatalogRow],
    user: dict = Depends(require_role("admin")),
):
    """Upsert product images without changing the sellable inventory snapshot.

    JST's stock export omits zero-stock products, while the product catalogue
    still provides their exact-SKU images.  This side catalogue is therefore
    deliberately additive: it never deletes inventory rows or quantities.
    """
    normalized: dict[str, str] = {}
    for row in rows:
        code = row.part_code.strip()
        image_url = row.image_url.strip()
        if code and image_url:
            normalized[code] = image_url
    if not normalized:
        return {"upserted": 0, "submitted": 0, "unchanged": 0}

    async with pool().acquire() as conn:
        changed = await conn.fetchval(
            """
            WITH incoming AS (
                SELECT * FROM UNNEST($1::TEXT[], $2::TEXT[])
                    AS row(part_code, image_url)
            ), written AS (
                INSERT INTO inventory_image_catalog (part_code, image_url, updated_at)
                SELECT part_code, image_url, NOW() FROM incoming
                ON CONFLICT (part_code) DO UPDATE
                SET image_url = EXCLUDED.image_url, updated_at = NOW()
                WHERE inventory_image_catalog.image_url
                    IS DISTINCT FROM EXCLUDED.image_url
                RETURNING 1
            )
            SELECT COUNT(*) FROM written
            """,
            list(normalized), list(normalized.values()),
        )
    upserted = int(changed or 0)
    if upserted:
        _clear_image_caches()
    return {
        "upserted": upserted,
        "submitted": len(normalized),
        "unchanged": len(normalized) - upserted,
    }


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
        image_url  image_url   (aliases: image / 图片 / 图片链接) — optional

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
    image_col = col("image_url", "image", "图片", "图片链接")
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
    image_by_code: dict[str, str] = {}
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
        image_value = (r.get(image_col) or "").strip() if image_col else ""
        if name_value and code not in name_by_code:
            name_by_code[code] = name_value
        if image_value and code not in image_by_code:
            image_by_code[code] = image_value
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
                "image_url": image_by_code.get(code),
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
        (agg["part_code"], agg["part_name"], agg["on_hand_qty"], agg["location"], agg["image_url"])
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
                INSERT INTO inventory_snapshot (part_code, part_name, on_hand_qty, location, image_url)
                VALUES ($1, $2, $3, $4, $5)
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
    _clear_image_caches()
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
            SELECT part_code, part_name, image_url, on_hand_qty, location, updated_at
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


@router.get("/image/{part_code}")
async def image_proxy(
    part_code: str,
    thumbnail: bool = Query(False, description="Return a 400px WebP scanner preview"),
    user: dict = Depends(require_role("admin", "repair", "returns")),
):
    """Serve JST part images through the app's own origin.

    Some client browsers intermittently fail to render marketplace/CDN URLs
    directly. Only the known hosts emitted by JST are allowed, so this
    endpoint cannot become a general-purpose SSRF proxy.
    """
    image_url = _cached_image_url(part_code)
    lookup_cache_status = "HIT" if image_url else "MISS"
    if not image_url:
        async with pool().acquire() as conn:
            image_url = await conn.fetchval(
                """
                SELECT image_url FROM (
                    SELECT image_url, 0 AS source_priority
                    FROM inventory_snapshot
                    WHERE UPPER(TRIM(part_code)) = UPPER(TRIM($1))
                    UNION ALL
                    SELECT image_url, 1 AS source_priority
                    FROM inventory_image_catalog
                    WHERE UPPER(TRIM(part_code)) = UPPER(TRIM($1))
                ) images
                WHERE COALESCE(image_url, '') <> ''
                ORDER BY source_priority
                LIMIT 1
                """,
                part_code,
            )
        if image_url:
            _store_cached_image_url(part_code, image_url)
    if not image_url:
        raise HTTPException(404, "image not found")

    try:
        content, content_type, cache_status = await _load_image(image_url, thumbnail)
    except httpx.HTTPError as exc:
        raise HTTPException(502, "image upstream unavailable") from exc
    except ValueError as exc:
        raise HTTPException(400 if str(exc) == "unsupported image host" else 502, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, "image thumbnail conversion failed") from exc
    return Response(
        content=content,
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=86400",
            "X-Image-Cache": cache_status,
            "X-Image-Lookup-Cache": lookup_cache_status,
        },
    )
