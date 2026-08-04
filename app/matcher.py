"""Inventory allocation and status calculation.

READY means stock has been reserved for the whole defective item.  Stock is
never counted twice.  Pallets with the highest projected repairable ratio are
allocated first; an item only receives stock when every required part can be
reserved.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from fractions import Fraction
import json
from typing import Iterable, Mapping, Optional

from app.db import pool


def _demands(parts: Iterable[Mapping]) -> dict[str, int]:
    """Merge duplicate part codes belonging to one defective item."""
    out: dict[str, int] = defaultdict(int)
    for part in parts:
        code = str(part["part_code"]).strip()
        if code:
            out[code] += int(part["qty"])
    return dict(out)


def _allocate_within_pallet(items: list[dict], stock: dict[str, int]) -> list[int]:
    """Greedily maximise complete items; partial reservations are forbidden."""
    remaining = dict(stock)
    waiting = list(items)
    ready: list[int] = []
    while waiting:
        feasible = [
            item for item in waiting
            if item["demands"] and all(remaining.get(code, 0) >= qty for code, qty in item["demands"].items())
        ]
        if not feasible:
            break

        # Prefer the item consuming the smallest share of scarce stock. This
        # normally completes more SKUs than input/id order alone.
        def cost(item: dict):
            scarcity = sum(
                Fraction(qty, max(remaining.get(code, 0), 1))
                for code, qty in item["demands"].items()
            )
            return (scarcity, sum(item["demands"].values()), item["created_at"], item["id"])

        chosen = min(feasible, key=cost)
        for code, qty in chosen["demands"].items():
            remaining[code] -= qty
        ready.append(chosen["id"])
        waiting.remove(chosen)
    return ready


def allocate_by_pallet(items: Iterable[Mapping], inventory: Mapping[str, int]) -> set[int]:
    """Return item ids that receive a complete stock reservation.

    Pallets are ranked using a projection against the same starting inventory:
    repairable ratio, then repairable count, then oldest item, then pallet no.
    Once ranked, real inventory is deducted pallet by pallet.
    """
    stock = {str(k): max(int(v), 0) for k, v in inventory.items()}
    grouped: dict[str, list[dict]] = defaultdict(list)
    for raw in items:
        created = raw.get("created_at") or datetime.max.replace(tzinfo=timezone.utc)
        grouped[str(raw.get("pallet_no") or "")].append({
            "id": int(raw["id"]),
            "created_at": created,
            "demands": _demands(raw.get("parts") or []),
        })

    ranked = []
    for pallet, pallet_items in grouped.items():
        projected = _allocate_within_pallet(pallet_items, stock)
        oldest = min(item["created_at"] for item in pallet_items)
        ratio = Fraction(len(projected), len(pallet_items))
        ranked.append((pallet, pallet_items, ratio, len(projected), oldest))

    ranked.sort(key=lambda row: (-row[2], -row[3], row[4], row[0]))

    remaining = dict(stock)
    ready: set[int] = set()
    for _pallet, pallet_items, _ratio, _count, _oldest in ranked:
        allocated = _allocate_within_pallet(pallet_items, remaining)
        allocated_set = set(allocated)
        ready.update(allocated_set)
        for item in pallet_items:
            if item["id"] in allocated_set:
                for code, qty in item["demands"].items():
                    remaining[code] -= qty
    return ready


async def reevaluate_all_pending_ready() -> dict:
    """Rebuild every READY reservation using pallet-priority allocation."""
    async with pool().acquire() as conn:
        async with conn.transaction():
            # Serialise allocation runs so concurrent imports/edits cannot
            # publish two different reservation plans.
            await conn.execute("SELECT pg_advisory_xact_lock(74687201)")
            item_rows = await conn.fetch(
                """
                SELECT di.id, di.pallet_no, di.status, di.created_at,
                       COALESCE(json_agg(json_build_object(
                           'part_code', dp.part_code, 'qty', dp.qty
                       ) ORDER BY dp.id) FILTER (WHERE dp.id IS NOT NULL), '[]'::json) AS parts
                FROM defective_items di
                LEFT JOIN defective_parts dp ON dp.defective_id = di.id
                WHERE di.status IN ('PENDING', 'READY')
                GROUP BY di.id, di.pallet_no, di.status, di.created_at
                """
            )
            inventory_rows = await conn.fetch(
                "SELECT part_code, on_hand_qty FROM inventory_snapshot"
            )

            items = []
            for row in item_rows:
                item = dict(row)
                if isinstance(item["parts"], str):
                    item["parts"] = json.loads(item["parts"])
                items.append(item)
            ready_ids = allocate_by_pallet(
                items,
                {row["part_code"]: row["on_hand_qty"] for row in inventory_rows},
            )

            flip = {"to_pending": 0, "to_ready": 0, "no_change": 0}
            updates = []
            for item in items:
                new_status = "READY" if item["id"] in ready_ids else "PENDING"
                current = item["status"]
                if new_status == current:
                    flip["no_change"] += 1
                elif new_status == "READY":
                    flip["to_ready"] += 1
                else:
                    flip["to_pending"] += 1
                if new_status != current:
                    updates.append((new_status, item["id"]))
            if updates:
                await conn.executemany(
                    "UPDATE defective_items SET status=$1 WHERE id=$2 AND status != 'COMPLETED'",
                    updates,
                )
            return flip


async def evaluate_status(defective_id: int) -> str:
    """Rebuild the global plan, then return one item's resulting status."""
    await reevaluate_all_pending_ready()
    async with pool().acquire() as conn:
        status = await conn.fetchval(
            "SELECT status FROM defective_items WHERE id=$1", defective_id
        )
    return status or "PENDING"


async def list_with_parts(status_filter: Optional[str] = None, limit: int = 200):
    """List defectives, refreshing the global reservation plan first."""
    if status_filter in ("PENDING", "READY"):
        await reevaluate_all_pending_ready()
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
            di.id, di.business_date, di.pallet_no, di.product_name, di.sku, di.qty, di.status,
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
    async with pool().acquire() as conn:
        rows = await conn.fetch(sql.format(where=where), *args)
    out = []
    for row in rows:
        item = dict(row)
        if isinstance(item.get("parts"), str):
            item["parts"] = json.loads(item["parts"])
        item["created_at"] = item["created_at"].isoformat() if item["created_at"] else None
        item["business_date"] = item["business_date"].isoformat() if item["business_date"] else None
        item["completed_at"] = item["completed_at"].isoformat() if item["completed_at"] else None
        out.append(item)
    return out


async def summary_counts() -> dict:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT status, COUNT(*)::int AS n FROM defective_items GROUP BY status"
        )
    out = {"PENDING": 0, "READY": 0, "COMPLETED": 0}
    for row in rows:
        out[row["status"]] = row["n"]
    return out
