"""Read-only, whole-pallet READY pick preview (never paginated)."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from app.auth import current_user
from app.db import pool
from app.matcher import _rebuild_plan_with_breakdown

router = APIRouter(prefix="/api/pallet-pick", tags=["pallet-pick"])


@router.get("/{pallet_no}")
async def pallet_pick_preview(pallet_no: str, user: dict = Depends(current_user)):
    pallet_no = pallet_no.strip()
    if not pallet_no or len(pallet_no) > 80:
        raise HTTPException(422, "invalid pallet number")

    # Keep the same global reservation calculation as the READY screen.
    await _rebuild_plan_with_breakdown()
    async with pool().acquire() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            rows = await conn.fetch(
                """
                SELECT di.id, di.business_date, di.pallet_no, di.location,
                       di.sku, di.product_name, di.qty, di.status,
                       dp.id AS part_id, dp.part_code, dp.part_name,
                       dp.qty AS part_qty, i.part_name AS stock_part_name,
                       i.location AS stock_location, i.updated_at AS stock_updated_at
                FROM defective_items di
                LEFT JOIN defective_parts dp ON dp.defective_id = di.id
                LEFT JOIN inventory_snapshot i ON i.part_code = dp.part_code
                WHERE di.pallet_no = $1
                ORDER BY di.id, dp.id
                """, pallet_no,
            )
            codes = sorted({r["part_code"] for r in rows if r["status"] == "READY" and r["part_code"]})
            locations = await conn.fetch(
                """
                SELECT part_code, location, qty
                FROM inventory_locations
                WHERE part_code = ANY($1::text[]) AND qty > 0
                ORDER BY part_code, qty DESC, location
                """, codes,
            ) if codes else []

    if not rows:
        raise HTTPException(404, "pallet not found")
    counts = Counter()
    items = {}
    pick = {}
    for row in rows:
        rid = row["id"]
        if rid not in items:
            status = str(row["status"])
            counts[status] += 1
            if status == "READY":
                items[rid] = {
                    "id": rid, "business_date": row["business_date"].isoformat() if row["business_date"] else None,
                    "sku": row["sku"], "product_name": row["product_name"],
                    "qty": int(row["qty"]), "location": row["location"], "parts": [],
                }
            else:
                items[rid] = None
        if items[rid] is None or row["part_id"] is None:
            continue
        code = row["part_code"]
        qty = int(row["part_qty"])
        items[rid]["parts"].append({"part_code": code, "part_name": row["part_name"], "qty": qty})
        if code not in pick:
            pick[code] = {
                "part_code": code, "part_name": row["part_name"] or row["stock_part_name"],
                "qty": 0, "item_ids": [], "locations": [],
                "stock_location": row["stock_location"] or "",
                "stock_updated_at": row["stock_updated_at"].isoformat() if row["stock_updated_at"] else None,
            }
        pick[code]["qty"] += qty
        if rid not in pick[code]["item_ids"]:
            pick[code]["item_ids"].append(rid)

    for row in locations:
        pick[row["part_code"]]["locations"].append({"location": row["location"], "qty": int(row["qty"])})

    ready = [item for item in items.values() if item is not None]
    parts = sorted(pick.values(), key=lambda p: (p["locations"][0]["location"] if p["locations"] else p["stock_location"], p["part_code"]))
    signature = hashlib.sha256(json.dumps({"ready": ready, "parts": parts}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {
        "pallet_no": pallet_no,
        "counts": {status: counts[status] for status in ("READY", "PENDING", "COMPLETED")},
        "ready_items": ready,
        "pick_parts": parts,
        "signature": signature,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
