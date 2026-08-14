"""Defective items CRUD."""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.auth import current_user, require_role
from app.db import pool
from app.matcher import (
    ALLOWED_PAGE_SIZES,
    DEFAULT_PAGE_SIZE,
    count_by_status,
    evaluate_status,
    list_with_parts,
    list_with_parts_paged,
)

router = APIRouter(prefix="/api/defectives", tags=["defectives"])


class PartIn(BaseModel):
    part_code: str = Field(..., min_length=1, max_length=80)
    part_name: Optional[str] = None
    qty: int = Field(..., gt=0)


class DefectiveIn(BaseModel):
    business_date: date = Field(default_factory=date.today)
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
    business_date: Optional[date] = None
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
                INSERT INTO defective_items (business_date, pallet_no, product_name, sku, qty, location, created_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id
                """,
                payload.business_date, payload.pallet_no, payload.product_name, payload.sku, payload.qty, payload.location, user["id"],
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
                user["id"], di_id, json.dumps(payload.model_dump(mode="json")),
            )

    status = await evaluate_status(di_id)
    return {"id": di_id, "status": status}


@router.get("")
async def list_defectives(
    status: Optional[str] = Query(None, pattern="^(PENDING|READY|COMPLETED)$"),
    limit: int = Query(100, ge=1, le=200_000),
    offset: int = Query(0, ge=0, le=10_000_000),
    user: dict = Depends(current_user),
):
    """List defective items.

    Pagination: server-side ``limit`` + ``offset`` with a stable
    ORDER BY (created_at DESC, id DESC). The response is the bare
    list for backwards-compat — consumers that need accurate totals
    should call ``/_/ready`` or ``/_/pending`` (which return
    ``{items, total, limit, offset}``), or call ``/_/count`` for a
    fast standalone COUNT.

    Cap behaviour (updated 2026-08-14 scope bump): the general list
    ceiling is raised to ``200_000`` so a single bulk-export-style
    call can pull the whole catalog. The paginated READY/PENDING
    endpoints live at ``/_/ready`` and ``/_/pending`` with their own
    whitelist-driven defaults (100/200/500/2000, default 500) and are
    NOT affected by this ceiling — those are user-facing paged views,
    while this endpoint is the bulk-fetch path.

    Note: the response is the JSON-serialised list returned to the
    caller. With ``limit=200_000`` the response payload can be large;
    the SQL itself still applies ``LIMIT/OFFSET`` so the database only
    ships the requested slice. Accurate totals are soley available
    via ``/_/count`` (true ``COUNT(*)``) — never derived from a
    200k-row page.
    """
    return await list_with_parts(status_filter=status, limit=limit, offset=offset)


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
    user: dict = Depends(require_role("returns", "admin")),
):
    """Update editable header fields on a defective_item.

    Returns the updated row. Records each changed field in audit_log.
    returns / admin can edit every header field (including sku) on every
    status. repair users get a strict read-only role on this endpoint —
    their workflow is the dedicated repair view, which only exposes the
    «完成» button against READY items. (Front-end hides the edit affordances
    for repair too, but we enforce the matrix server-side regardless.)
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
    for k in ("business_date", "pallet_no", "product_name", "location", "sku", "qty"):
        if k in body:
            fields.append(f"{k} = ${idx}")
            values.append(body[k])
            idx += 1
    values.append(defective_id)
    set_clause = ", ".join(fields) + f", updated_at = NOW()"

    async with pool().acquire() as conn:
        current = await conn.fetchrow(
            "SELECT status FROM defective_items WHERE id=$1", defective_id
        )
        if current is None:
            raise HTTPException(404, "not found")
        # returns / admin can patch on every status, including COMPLETED.
        # The flow gate is on /complete (which requires repair/admin), not on
        # header edits — returns still needs to be able to fix mistyped
        # SKUs after the fact.
        row = await conn.fetchrow(
            f"UPDATE defective_items SET {set_clause} WHERE id = ${idx} RETURNING id, business_date, pallet_no, product_name, location, sku, qty, status",
            *values,
        )
        if row is None:
            raise HTTPException(404, "not found")
        # Audit: log which fields changed (include actor role for forensics).
        await conn.execute(
            """
            INSERT INTO audit_log (user_id, action, entity_type, entity_id, details)
            VALUES ($1, 'patch', 'defective_item', $2, $3::jsonb)
            """,
            user["id"], defective_id,
            _json.dumps({"fields": list(body.keys()), "actor_role": user["role"]}),
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
    parts: list[PartIn],
    user: dict = Depends(require_role("returns", "admin")),
):
    """Replace the entire parts list for a defective_item.

    returns / admin only. repair is excluded because their workflow is
    to mark READY items complete via the dedicated repair view; parts
    edits are a returns/admin responsibility.
    """
    from app.matcher import evaluate_status as _eval
    if not parts:
        raise HTTPException(400, "need at least 1 part")

    async with pool().acquire() as conn:
        current = await conn.fetchrow("SELECT status FROM defective_items WHERE id=$1", defective_id)
        if not current:
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
                json.dumps({"count": len(parts), "actor_role": user["role"]}),
            )

    try:
        status = await _eval(defective_id)
    except Exception:
        status = None

    return {"id": defective_id, "parts": len(parts), "status": status}


@router.delete("/{defective_id}")
async def delete_defective(
    defective_id: int,
    user: dict = Depends(require_role("returns", "admin")),
):
    """Hard-delete a defective_item and its parts.

    returns / admin only. repair is excluded; their workflow runs the
    COMPLETED transition, not history cleanup.
    """
    async with pool().acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT id, status, sku, pallet_no FROM defective_items WHERE id=$1",
                defective_id,
            )
            if existing is None:
                raise HTTPException(404, "not found")
            # defensive: cascade deletes parts via FK ON DELETE CASCADE
            await conn.execute(
                "DELETE FROM defective_items WHERE id=$1",
                defective_id,
            )
            await conn.execute(
                """
                INSERT INTO audit_log (user_id, action, entity_type, entity_id, details)
                VALUES ($1, 'delete', 'defective_item', $2, $3::jsonb)
                """,
                user["id"], defective_id,
                json.dumps({
                    "actor_role": user["role"],
                    "previous_status": existing["status"],
                    "previous_sku": existing["sku"],
                    "previous_pallet_no": existing["pallet_no"],
                }),
            )
    return {"id": defective_id, "deleted": True}


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
async def list_ready(
    page_size: int = Query(
        DEFAULT_PAGE_SIZE,
        ge=1,
        le=max(ALLOWED_PAGE_SIZES),
        description=(
            "Page size for the READY tab. Must be one of: 100, 200, 500, 2000. "
            "Default 500. The READY and PENDING tabs maintain independent "
            "pagination state, so a smaller/larger page size on READY does "
            "not affect PENDING."
        ),
    ),
    offset: int = Query(0, ge=0, le=10_000_000),
    user: dict = Depends(current_user),
):
    """Paginated READY tab listing with accurate total.

    Response shape: ``{items: [...], total: int, limit: int, offset: int}``.
    ``total`` is a fresh COUNT(*) against ``defective_items WHERE status='READY'``
    — independent of the page slice so the UI can render accurate
    ``X / Y`` counts and ``page N of M`` indicators.
    """
    if page_size not in ALLOWED_PAGE_SIZES:
        raise HTTPException(
            422,
            f"page_size must be one of {sorted(ALLOWED_PAGE_SIZES)}; got {page_size}",
        )
    return await list_with_parts_paged("READY", page_size, offset)


@router.get("/_/pending")
async def list_pending(
    page_size: int = Query(
        DEFAULT_PAGE_SIZE,
        ge=1,
        le=max(ALLOWED_PAGE_SIZES),
        description=(
            "Page size for the PENDING tab. Must be one of: 100, 200, 500, 2000. "
            "Default 500. Independent from the READY tab."
        ),
    ),
    offset: int = Query(0, ge=0, le=10_000_000),
    user: dict = Depends(current_user),
):
    """Paginated PENDING tab listing with accurate total.

    Same response shape as ``/_/ready``. The two tabs hold independent
    pagination state in the front-end — switching to READY and back to
    PENDING restores the user's last seen page on each tab.
    """
    if page_size not in ALLOWED_PAGE_SIZES:
        raise HTTPException(
            422,
            f"page_size must be one of {sorted(ALLOWED_PAGE_SIZES)}; got {page_size}",
        )
    return await list_with_parts_paged("PENDING", page_size, offset)


@router.get("/_/count")
async def list_count(
    status: Optional[str] = Query(None, pattern="^(PENDING|READY|COMPLETED)$"),
    user: dict = Depends(current_user),
):
    """Accurate count for one (or all) defective-item statuses.

    Used by the front-end tab badges so the displayed counts reflect
    the entire database, not just the items currently on the page.
    Pass ``?status=PENDING`` (or READY / COMPLETED) to scope the count;
    omit the param to get a dict of all three. The implementation runs
    a single COUNT(*) per status — cheap and stable across pagination.
    """
    if status is not None:
        n = await count_by_status(status)
        return {"status": status, "total": n}
    out = {s: await count_by_status(s) for s in ("PENDING", "READY", "COMPLETED")}
    return {"status": None, "totals": out}


@router.post("/bulk")
async def bulk_action(
    payload: dict,
    user: dict = Depends(require_role("returns", "repair", "admin")),
):
    """Apply a bulk action to a set of defective items.

    payload:
      ids: list[int]                 — required
      action:                          — required
        "recompute"                  re-evaluate status via inventory
                                     (returns + admin)
        "mark_complete"              mark READY → COMPLETED
                                     (repair + admin) — the only bulk action
                                     available to repair users
        "set_sku"        { sku }     change sku            (returns + admin)
        "set_location"   { location } change 仓位           (returns + admin)
        "set_product_name" { product_name }                (returns + admin)
        "delete"                    remove                 (returns + admin)
      reason: str (optional) — recorded in audit_log

    The coarse-grained `require_role(...)` keeps the route open for the
    three roles; per-action checks below enforce the matrix. Per-action
    gates run BEFORE any DB work so a forbidden action is rejected with
    a clear 403 even before the per-id loop opens a transaction.
    """
    ids = payload.get("ids") or []
    action = (payload.get("action") or "").strip()
    reason = (payload.get("reason") or "").strip()
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "ids must be a non-empty list")
    if not action:
        raise HTTPException(400, "action is required")

    role = user.get("role")
    # Per-action matrix. repair only gets mark_complete; returns +
    # admin can do everything except mark_complete (which stays
    # repair+admin only — Cc doesn't want returns flipping items to
    # COMPLETED via the bulk endpoint).
    if action == "mark_complete":
        if role not in ("repair", "admin"):
            raise HTTPException(403, "mark_complete requires repair/admin")
    elif action in ("recompute", "delete", "set_sku", "set_location", "set_product_name"):
        if role not in ("returns", "admin"):
            raise HTTPException(403, f"{action} requires returns/admin")
    else:
        raise HTTPException(400, f"unknown action {action!r}")

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
                            user["id"], did, json.dumps({"reason": reason, "actor_role": user["role"]}),
                        )
                    elif action == "set_sku":
                        new_sku = (payload.get("sku") or "").strip()
                        if not new_sku:
                            failures.append({"id": did, "error": "sku required"})
                            continue
                        await conn.execute(
                            "UPDATE defective_items SET sku=$1 WHERE id=$2",
                            new_sku, did,
                        )
                        await conn.execute(
                            """
                            INSERT INTO audit_log (user_id, action, entity_type, entity_id, details)
                            VALUES ($1, 'bulk_set_sku', 'defective_item', $2, $3::jsonb)
                            """,
                            user["id"], did, json.dumps({"sku": new_sku, "reason": reason, "actor_role": user["role"]}),
                        )
                    elif action == "set_location":
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
                            user["id"], did, json.dumps({"location": new_loc, "reason": reason, "actor_role": user["role"]}),
                        )
                    elif action == "set_product_name":
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
                            user["id"], did, json.dumps({"product_name": new_pn, "reason": reason, "actor_role": user["role"]}),
                        )
                    elif action == "delete":
                        await conn.execute("DELETE FROM defective_items WHERE id=$1", did)
                        await conn.execute(
                            """
                            INSERT INTO audit_log (user_id, action, entity_type, entity_id, details)
                            VALUES ($1, 'bulk_delete', 'defective_item', $2, $3::jsonb)
                            """,
                            user["id"], did, json.dumps({"reason": reason, "actor_role": user["role"]}),
                        )
                    else:
                        # Unreachable: validated above.
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
    q: Optional[str] = Query(None, description="substring against pallet_no/sku/product_name/location/part_code/part_name"),
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
            f"OR di.product_name ILIKE ${iq} OR di.location ILIKE ${iq} "
            f"OR EXISTS (SELECT 1 FROM defective_parts dp_search "
            f"WHERE dp_search.defective_id = di.id "
            f"AND (dp_search.part_code ILIKE ${iq} OR dp_search.part_name ILIKE ${iq})))"
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
            di.id, di.business_date, di.pallet_no, di.product_name, di.sku, di.qty, di.status,
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
    business_date: Optional[date] = None
