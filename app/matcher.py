"""Status calculation: PENDING / READY / COMPLETED.

Single source of truth: a SQL view that joins defective_items + defective_parts
to inventory_snapshot and tells you, per defective_id, whether all parts are
sufficient.

Status transitions:
  PENDING   -> READY         when every part qty <= on_hand_qty
  READY     -> COMPLETED    when repair marks it complete
  COMPLETED is terminal
"""
from __future__ import annotations

import json
from typing import Optional

from app.db import pool


async def evaluate_status(defective_id: int) -> str:
    """Compute current status for a defective_item and persist if changed.

    Returns the status (PENDING/READY).
    """
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                di.id,
                di.status AS current,
                BOOL_AND(COALESCE(i.on_hand_qty, 0) >= dp.qty) AS all_ready
            FROM defective_items di
            JOIN defective_parts dp ON dp.defective_id = di.id
            LEFT JOIN inventory_snapshot i ON i.part_code = dp.part_code
            WHERE di.id = $1
            GROUP BY di.id, di.status
            """,
            defective_id,
        )
        if row is None:
            return "PENDING"
        if row["current"] == "COMPLETED":
            return "COMPLETED"
        new_status = "READY" if row["all_ready"] else "PENDING"
        if new_status != row["current"]:
            await conn.execute(
                "UPDATE defective_items SET status=$1 WHERE id=$2 AND status != 'COMPLETED'",
                new_status, defective_id,
            )
        return new_status


async def list_with_parts(status_filter: Optional[str] = None, limit: int = 200):
    """List defectives. If filtering by PENDING/READY, recompute status first
    so inventory changes from manual CSV upload are immediately reflected.
    """
    if status_filter in ("PENDING", "READY"):
        async with pool().acquire() as conn:
            ids = await conn.fetch(
                "SELECT id FROM defective_items WHERE status IN ('PENDING','READY')"
            )
        for r in ids:
            await evaluate_status(r["id"])
    sql = """
        WITH parts_agg AS (
            SELECT
                dp.defective_id,
                json_agg(json_build_object(
                    'part_code', dp.part_code,
                    'part_name', dp.part_name,
                    'need', dp.qty,
                    'have', COALESCE(i.on_hand_qty, 0),
                    'short', GREATEST(dp.qty - COALESCE(i.on_hand_qty, 0), 0)
                ) ORDER BY dp.id) AS parts
            FROM defective_parts dp
            LEFT JOIN inventory_snapshot i ON i.part_code = dp.part_code
            GROUP BY dp.defective_id
        )
        SELECT
            di.id, di.pallet_no, di.product_name, di.sku, di.qty, di.status,
            di.location,
            di.created_at, di.completed_at,
            u_creator.name AS created_by_name,
            u_completer.name AS completed_by_name,
            pa.parts
        FROM defective_items di
        LEFT JOIN users u_creator ON u_creator.id = di.created_by
        LEFT JOIN users u_completer ON u_completer.id = di.completed_by
        LEFT JOIN parts_agg pa ON pa.defective_id = di.id
        {where}
        ORDER BY
            CASE di.status WHEN 'READY' THEN 0 WHEN 'PENDING' THEN 1 ELSE 2 END,
            di.created_at DESC
        LIMIT $1
    """
    where = ""
    args: list = [limit]
    if status_filter:
        where = "WHERE di.status = $2"
        args.append(status_filter)
    sql = sql.format(where=where)
    async with pool().acquire() as conn:
        rows = await conn.fetch(sql, *args)
    out = []
    for r in rows:
        d = dict(r)
        # asyncpg returns jsonb as str sometimes; normalise
        if isinstance(d.get("parts"), str):
            d["parts"] = json.loads(d["parts"])
        d["created_at"] = d["created_at"].isoformat() if d["created_at"] else None
        d["completed_at"] = d["completed_at"].isoformat() if d["completed_at"] else None
        out.append(d)
    return out


async def summary_counts() -> dict:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT status, COUNT(*)::int AS n FROM defective_items GROUP BY status"
        )
    out = {"PENDING": 0, "READY": 0, "COMPLETED": 0}
    for r in rows:
        out[r["status"]] = r["n"]
    return out