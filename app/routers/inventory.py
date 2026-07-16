"""Inventory upload (CSV).

Full-replace semantics: upload wipes the inventory_snapshot table and inserts
the rows from the CSV. After the wipe + reload, every PENDING/READY defective
item is re-evaluated so new stock levels are reflected immediately.
"""
from __future__ import annotations

import csv
import io
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
    """Upload JST inventory CSV. Expected columns (case-insensitive, flexible order):
        part_code, part_name, on_hand_qty, location

    Full replace semantics: rows in CSV become the new snapshot, then every
    defective item is re-evaluated against the fresh stock.
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
    qty_col = col("on_hand_qty", "qty", "库存", "在库数量", "stock")
    loc_col = col("location", "位置", "库位", "warehouse")
    if not code_col or not qty_col:
        raise HTTPException(
            400,
            f"need part_code ({code_col or '?'}) and qty ({qty_col or '?'}) columns; "
            f"got headers: {reader.fieldnames}",
        )

    rows = []
    for r in reader:
        code = (r.get(code_col) or "").strip()
        if not code:
            continue
        try:
            qty = int(float(r.get(qty_col) or 0))
        except ValueError:
            continue
        rows.append((
            code,
            (r.get(name_col) or "").strip() or None if name_col else None,
            qty,
            (r.get(loc_col) or "").strip() or None if loc_col else None,
        ))

    if not rows:
        raise HTTPException(400, "no valid rows")

    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("TRUNCATE inventory_snapshot")
            await conn.executemany(
                """
                INSERT INTO inventory_snapshot (part_code, part_name, on_hand_qty, location)
                VALUES ($1, $2, $3, $4)
                """,
                rows,
            )
            await conn.execute(
                """
                INSERT INTO audit_log (user_id, action, entity_type, details)
                VALUES ($1, 'upload_inventory', 'inventory_snapshot', $2::jsonb)
                """,
                user["id"], f'{{"rows": {len(rows)}}}',
            )

    # Re-evaluate every PENDING/READY defective against the fresh stock
    # in a single SQL round-trip (was O(N) per-item before).
    flip = await reevaluate_all_pending_ready()
    status_flip = {"to_pending": flip["to_pending"], "to_ready": flip["to_ready"]}
    reevaluated = flip["no_change"] + flip["to_pending"] + flip["to_ready"]

    return {
        "inserted": len(rows),
        "reevaluated": reevaluated,
        "status_flip": status_flip,
    }


@router.get("/summary")
async def summary(user: dict = Depends(require_role("admin", "repair"))):
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*)::int AS sku_count,
                COALESCE(SUM(on_hand_qty), 0)::int AS total_units,
                MAX(updated_at) AS last_updated
            FROM inventory_snapshot
            """
        )
    return dict(row) if row else {"sku_count": 0, "total_units": 0, "last_updated": None}


@router.get("/preview/{part_code}")
async def preview_one(
    part_code: str,
    user: dict = Depends(require_role("admin", "repair", "returns")),
):
    """Look up a part_code (use this for ad-hoc repair queries)."""
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
    d = dict(row)
    d["updated_at"] = d["updated_at"].isoformat() if d["updated_at"] else None
    return d
