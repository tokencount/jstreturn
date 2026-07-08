"""Defective items CRUD."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.auth import current_user, require_role
from app.db import pool
from app.matcher import evaluate_status, list_with_parts

router = APIRouter(prefix="/api/defectives", tags=["defectives"])


class PartIn(BaseModel):
    part_code: str = Field(..., min_length=1, max_length=80)
    part_name: Optional[str] = None
    qty: int = Field(..., gt=0)


class DefectiveIn(BaseModel):
    pallet_no: str = Field(..., min_length=1, max_length=80)
    product_name: Optional[str] = None
    location: Optional[str] = None
    sku: str = Field(..., min_length=1, max_length=80)
    qty: int = Field(..., gt=0)
    parts: list[PartIn] = Field(..., min_length=1)


class DefectivePatch(BaseModel):
    """Patch a defective_item's header fields (parts not edited here;
    use /api/defectives/{id}/parts for that). All fields optional;
    only provided fields are updated.
    """
    pallet_no: Optional[str] = Field(None, min_length=1, max_length=80)
    product_name: Optional[str] = None
    location: Optional[str] = None
    sku: Optional[str] = Field(None, min_length=1, max_length=80)
    qty: Optional[int] = Field(None, gt=0)


@router.post("")
async def create_defective(
    payload: DefectiveIn,
    user: dict = Depends(require_role("returns", "admin")),
):
    async with pool().acquire() as conn:
        async with conn.transaction():
            di_id = await conn.fetchval(
                """
                INSERT INTO defective_items (pallet_no, product_name, sku, qty, location, created_by)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
                """,
                payload.pallet_no, payload.product_name, payload.sku, payload.qty, payload.location, user["id"],
            )
            for p in payload.parts:
                await conn.execute(
                    """
                    INSERT INTO defective_parts (defective_id, part_code, part_name, qty)
                    VALUES ($1, $2, $3, $4)
                    """,
                    di_id, p.part_code, p.part_name, p.qty,
                )
            await conn.execute(
                """
                INSERT INTO audit_log (user_id, action, entity_type, entity_id, details)
                VALUES ($1, 'create', 'defective_item', $2, $3::jsonb)
                """,
                user["id"], di_id, json.dumps(payload.model_dump()),
            )

    status = await evaluate_status(di_id)
    return {"id": di_id, "status": status}


@router.get("")
async def list_defectives(
    status: Optional[str] = Query(None, pattern="^(PENDING|READY|COMPLETED)$"),
    limit: int = Query(100, ge=1, le=500),
    user: dict = Depends(current_user),
):
    return await list_with_parts(status_filter=status, limit=limit)


@router.get("/{defective_id}")
async def get_defective(defective_id: int, user: dict = Depends(current_user)):
    items = await list_with_parts()
    for it in items:
        if it["id"] == defective_id:
            return it
    raise HTTPException(404, "not found")


@router.patch("/{defective_id}")
async def patch_defective(
    defective_id: int,
    payload: DefectivePatch,
    user: dict = Depends(require_role("returns", "repair", "admin")),
):
    """Update editable header fields on a defective_item.

    Returns the updated row. Records each changed field in audit_log.
    Only admin can change sku (sku is a workflow-bearing field).
    """
    import json as _json
    from app.matcher import evaluate_status as _eval

    # Build dynamic UPDATE based on what was provided.
    fields = []
    values: list = []
    idx = 1
    body = payload.model_dump(exclude_unset=True)
    if not body:
        raise HTTPException(400, "no fields to update")
    # sku is admin-only because changing sku invalidates matches.
    if "sku" in body and user["role"] != "admin":
        raise HTTPException(403, "sku change requires admin")
    for k in ("pallet_no", "product_name", "location", "sku", "qty"):
        if k in body:
            fields.append(f"{k} = ${idx}")
            values.append(body[k])
            idx += 1
    values.append(defective_id)
    set_clause = ", ".join(fields) + f", updated_at = NOW()"

    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE defective_items SET {set_clause} WHERE id = ${idx} RETURNING id, pallet_no, product_name, location, sku, qty, status",
            *values,
        )
        if row is None:
            raise HTTPException(404, "not found")
        # Audit: log which fields changed.
        await conn.execute(
            """
            INSERT INTO audit_log (user_id, action, entity_type, entity_id, details)
            VALUES ($1, 'patch', 'defective_item', $2, $3::jsonb)
            """,
            user["id"], defective_id,
            _json.dumps({"fields": list(body.keys())}),
        )

    # Re-evaluate status since sku/qty changes can flip READY/PENDING.
    try:
        status = await _eval(defective_id)
    except Exception:
        status = row["status"]

    return {**dict(row), "status": status}


@router.put("/{defective_id}/parts")
async def put_parts(
    defective_id: int,
    parts: list["PartIn"],
    user: dict = Depends(require_role("returns", "repair", "admin")),
):
    """Replace the entire parts list for a defective_item.

    Used by the edit modal on READY/PENDING/HISTORY rows. After
    replacing, re-evaluate status since part changes can flip
    READY/PENDING.
    """
    from app.matcher import evaluate_status as _eval
    if not parts:
        raise HTTPException(400, "need at least 1 part")

    async with pool().acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM defective_items WHERE id=$1", defective_id)
        if not exists:
            raise HTTPException(404, "not found")
        async with conn.transaction():
            await conn.execute("DELETE FROM defective_parts WHERE defective_id=$1", defective_id)
            for p in parts:
                await conn.execute(
                    "INSERT INTO defective_parts (defective_id, part_code, part_name, qty) VALUES ($1, $2, $3, $4)",
                    defective_id, p.part_code, p.part_name, p.qty,
                )
            await conn.execute(
                """
                INSERT INTO audit_log (user_id, action, entity_type, entity_id, details)
                VALUES ($1, 'put_parts', 'defective_item', $2, $3::jsonb)
                """,
                user["id"], defective_id,
                json.dumps({"count": len(parts)}),
            )

    try:
        status = await _eval(defective_id)
    except Exception:
        status = None

    return {"id": defective_id, "parts": len(parts), "status": status}


@router.post("/{defective_id}/complete")
async def complete(
    defective_id: int,
    user: dict = Depends(require_role("repair", "admin")),
):
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM defective_items WHERE id=$1",
            defective_id,
        )
    if row is None:
        raise HTTPException(404, "not found")
    if row["status"] == "COMPLETED":
        raise HTTPException(400, "already completed")
    if row["status"] != "READY":
        raise HTTPException(400, f"cannot complete: status is {row['status']}")

    async with pool().acquire() as conn:
        await conn.execute(
            """
            UPDATE defective_items
            SET status='COMPLETED', completed_by=$1, completed_at=now()
            WHERE id=$2
            """,
            user["id"], defective_id,
        )
        await conn.execute(
            """
            INSERT INTO audit_log (user_id, action, entity_type, entity_id)
            VALUES ($1, 'complete', 'defective_item', $2)
            """,
            user["id"], defective_id,
        )
    return {"id": defective_id, "status": "COMPLETED"}


@router.get("/_/ready")
async def list_ready(user: dict = Depends(current_user)):
    return await list_with_parts(status_filter="READY")


@router.get("/_/pending")
async def list_pending(user: dict = Depends(current_user)):
    return await list_with_parts(status_filter="PENDING")


@router.post("/bulk")
async def bulk_action(
    payload: dict,
    user: dict = Depends(require_role("repair", "admin", "returns")),
):
    """Apply a bulk action to a set of defective items.

    payload:
      ids: list[int]                 — required
      action:                          — required
        "recompute"                  re-evaluate status via inventory
        "mark_complete"              mark READY → COMPLETED  (require role repair/admin)
        "set_sku"        { sku }     change sku            (admin only)
        "set_location"   { location } change 仓位          (admin only)
        "set_product_name" { product_name }                (admin only)
        "set_product_name" { product_name }                (admin only)
        "delete"                    remove                (admin only)
      reason: str (optional) — recorded in audit_log
    """
    ids = payload.get("ids") or []
    action = (payload.get("action") or "").strip()
    reason = (payload.get("reason") or "").strip()
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "ids must be a non-empty list")
    if not action:
        raise HTTPException(400, "action is required")

    pool_ = pool()
    successes = []
    failures = []

    async with pool_.acquire() as conn:
        # Pre-flight: lock existing rows
        for did in ids:
            try:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        "SELECT id, status, sku, product_name FROM defective_items WHERE id=$1",
                        did,
                    )
                    if row is None:
                        failures.append({"id": did, "error": "not found"})
                        continue

                    if action == "recompute":
                        # Recompute via matcher
                        pass  # handled below outside transaction
                        # NOTE: matcher.evaluate_status acquires pool, so do this AFTER
                        # releasing the row's transaction
                    elif action == "mark_complete":
                        if user["role"] not in ("repair", "admin"):
                            raise HTTPException(403, "needs repair/admin")
                        if row["status"] != "READY":
                            failures.append({"id": did, "error": f"status is {row['status']}"})
                            continue
                        await conn.execute(
                            """
                            UPDATE defective_items
                            SET status='COMPLETED', completed_by=$1, completed_at=now()
                            WHERE id=$2
                            """,
                            user["id"], did,
                        )
                        await conn.execute(
                            """
                            INSERT INTO audit_log (user_id, action, entity_type, entity_id, details)
                            VALUES ($1, 'bulk_complete', 'defective_item', $2, $3::jsonb)
                            """,
                            user["id"], did, f'{{"reason":"{reason}"}}',
                        )
                    elif action == "set_sku":
                        if user["role"] != "admin":
                            raise HTTPException(403, "admin only")
                        new_sku = (payload.get("sku") or "").strip()
                        if not new_sku:
                            raise HTTPException(400, "sku required")
                        await conn.execute(
                            "UPDATE defective_items SET sku=$1 WHERE id=$2",
                            new_sku, did,
                        )
                        await conn.execute(
                            """
                            INSERT INTO audit_log (user_id, action, entity_type, entity_id, details)
                            VALUES ($1, 'bulk_set_sku', 'defective_item', $2, $3::jsonb)
                            """,
                            user["id"], did, f'{{"sku":"{new_sku}","reason":"{reason}"}}',
                        )
                    elif action == "set_location":
                        if user["role"] != "admin":
                            raise HTTPException(403, "admin only")
                        new_loc = (payload.get("location") or "").strip() or None
                        await conn.execute(
                            "UPDATE defective_items SET location=$1 WHERE id=$2",
                            new_loc, did,
                        )
                        await conn.execute(
                            """
                            INSERT INTO audit_log (user_id, action, entity_type, entity_id, details)
                            VALUES ($1, 'bulk_set_location', 'defective_item', $2, $3::jsonb)
                            """,
                            user["id"], did, f'{{"location":"{new_loc or ""}","reason":"{reason}"}}',
                        )
                    elif action == "set_product_name":
                        if user["role"] != "admin":
                            raise HTTPException(403, "admin only")
                        new_pn = (payload.get("product_name") or "").strip() or None
                        await conn.execute(
                            "UPDATE defective_items SET product_name=$1 WHERE id=$2",
                            new_pn, did,
                        )
                        await conn.execute(
                            """
                            INSERT INTO audit_log (user_id, action, entity_type, entity_id, details)
                            VALUES ($1, 'bulk_set_product_name', 'defective_item', $2, $3::jsonb)
                            """,
                            user["id"], did, f'{{"product_name":"{new_pn or ""}","reason":"{reason}"}}',
                        )
                    elif action == "delete":
                        if user["role"] != "admin":
                            raise HTTPException(403, "admin only")
                        await conn.execute("DELETE FROM defective_items WHERE id=$1", did)
                        await conn.execute(
                            """
                            INSERT INTO audit_log (user_id, action, entity_type, entity_id, details)
                            VALUES ($1, 'bulk_delete', 'defective_item', $2, $3::jsonb)
                            """,
                            user["id"], did, f'{{"reason":"{reason}"}}',
                        )
                    else:
                        failures.append({"id": did, "error": f"unknown action {action!r}"})
                        continue
                successes.append({"id": did, "action": action})
            except HTTPException:
                raise
            except Exception as e:
                failures.append({"id": did, "error": str(e) or "失败"})

        # For recompute, do it OUTSIDE the per-id transactions to avoid pool reuse.
        if action == "recompute":
            new_failures = []
            for did in ids:
                if any(f["id"] == did and "error" in f for f in failures):
                    continue
                try:
                    new_status = await evaluate_status(did)
                    successes.append({"id": did, "action": "recompute", "status": new_status})
                except Exception as e:
                    failures.append({"id": did, "error": str(e) or "recompute failed"})
            # Update successes result with status (latest wins)
            # Rewrite specific recompute entries with status
            seen = set()
            new_successes = []
            for s in successes:
                if s.get("action") == "recompute":
                    if s["id"] in seen:
                        continue
                    seen.add(s["id"])
                    for retry in successes:
                        if retry.get("id") == s["id"] and "status" in retry:
                            s = retry
                            break
                    new_successes.append(s)
                else:
                    new_successes.append(s)
            successes = new_successes

    return {
        "applied": action,
        "succeeded": len([s for s in successes if s.get("action") == action or action == "recompute"]),
        "failed": len(failures),
        "successes": successes,
        "failures": failures,
    }


@router.get("/filter")
async def filter_list(
    user: dict = Depends(current_user),
    status: Optional[str] = Query(None, pattern="^(PENDING|READY|COMPLETED)$"),
    q: Optional[str] = Query(None, description="substring against pallet_no/sku/product_name/location"),
    sku: Optional[str] = None,
    pallet: Optional[str] = None,
    location: Optional[str] = Query(None, description="substring against 次品仓位"),
    is_pending: Optional[bool] = Query(None, description="if true, only those with at least one missing part"),
    limit: int = Query(500, ge=1, le=2000),
):
    """Filter-driven list. Used by the UI bulk-edit panel.

    Notes: status='PENDING' already returns items with at least one missing
    part, so the dedicated is_pending flag is mostly redundant.
    """
    where = []
    args = []

    if status:
        args.append(status)
        where.append(f"di.status = ${len(args)}")
    if q:
        like = f"%{q}%"
        args.append(like)
        iq = len(args)
        where.append(
            f"(di.pallet_no ILIKE ${iq} OR di.sku ILIKE ${iq} "
            f"OR di.product_name ILIKE ${iq} OR di.location ILIKE ${iq})"
        )
    if sku:
        args.append(sku)
        where.append(f"di.sku = ${len(args)}")
    if pallet:
        args.append(pallet)
        where.append(f"di.pallet_no = ${len(args)}")
    if location:
        like = f"%{location}%"
        args.append(like)
        where.append(f"di.location ILIKE ${len(args)}")
    if is_pending:
        where.append(
            "EXISTS (SELECT 1 FROM defective_parts dp "
            "LEFT JOIN inventory_snapshot i ON i.part_code = dp.part_code "
            "WHERE dp.defective_id=di.id AND COALESCE(i.on_hand_qty,0) < dp.qty)"
        )

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
            di.location, di.created_at, di.completed_at,
            u_creator.name AS created_by_name,
            u_completer.name AS completed_by_name,
            pa.parts
        FROM defective_items di
        LEFT JOIN users u_creator ON u_creator.id = di.created_by
        LEFT JOIN users u_completer ON u_completer.id = di.completed_by
        LEFT JOIN parts_agg pa ON pa.defective_id = di.id
        {where}
        ORDER BY di.created_at DESC
        LIMIT ${placeholder}
    """.replace("{where}", ("WHERE " + " AND ".join(where)) if where else "").replace(
        "${placeholder}", f"${len(args)+1}"
    )
    args.append(limit)

    async with pool().acquire() as conn:
        rows = await conn.fetch(sql, *args)
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("parts"), str):
            d["parts"] = json.loads(d["parts"])
        d["created_at"] = d["created_at"].isoformat() if d["created_at"] else None
        d["completed_at"] = d["completed_at"].isoformat() if d["completed_at"] else None
        out.append(d)
    return out