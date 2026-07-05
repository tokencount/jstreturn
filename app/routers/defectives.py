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
    sku: str = Field(..., min_length=1, max_length=80)
    qty: int = Field(..., gt=0)
    parts: list[PartIn] = Field(..., min_length=1)


@router.post("")
async def create_defective(
    payload: DefectiveIn,
    user: dict = Depends(require_role("returns", "admin")),
):
    async with pool().acquire() as conn:
        async with conn.transaction():
            di_id = await conn.fetchval(
                """
                INSERT INTO defective_items (pallet_no, product_name, sku, qty, created_by)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
                """,
                payload.pallet_no, payload.product_name, payload.sku, payload.qty, user["id"],
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