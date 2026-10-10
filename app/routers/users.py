"""User management — admin only."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import current_user, require_role
from app.db import pool

router = APIRouter(prefix="/api/users", tags=["users"])

ROLES = ("returns", "repair", "admin")


# Admin gate for every endpoint below. We instantiate the Depends at
# module import time (not via a factory returning Depends(...)) so that
# FastAPI's dependency_overrides can correctly target the closure when
# unit-testing role gates.
admin_required = Depends(require_role("admin"))


async def deactivate_employee_accounts(dingtalk_user_id: str, *, actor_id: int | None = None, source: str = "dingtalk") -> list[int]:
    """Revoke every local account bound to one exact DingTalk employee ID.

    Shared by the admin action and the future verified DingTalk offboarding
    adapter. Soft deactivation keeps audit/history but invalidates sessions.
    """
    employee_id = dingtalk_user_id.strip()
    if not employee_id:
        raise HTTPException(400, "DingTalk employee ID required")
    async with pool().acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                "SELECT id FROM users WHERE dingtalk_user_id=$1 AND active=TRUE FOR UPDATE",
                employee_id,
            )
            ids = [row["id"] for row in rows]
            if ids:
                await conn.execute(
                    "UPDATE users SET active=FALSE WHERE dingtalk_user_id=$1 AND active=TRUE",
                    employee_id,
                )
                for account_id in ids:
                    await conn.execute(
                        "INSERT INTO audit_log (user_id, action, entity_type, entity_id, details) "
                        "VALUES ($1, 'deactivate_employee', 'user', $2, $3::jsonb)",
                        actor_id, account_id, json.dumps({"source": source, "dingtalk_user_id": employee_id}),
                    )
    return ids


class EmployeeOffboardIn(BaseModel):
    dingtalk_user_id: str = Field(..., min_length=1, max_length=128)


@router.post("/_/deactivate-employee")
async def deactivate_employee(payload: EmployeeOffboardIn, actor: dict = admin_required):
    ids = await deactivate_employee_accounts(payload.dingtalk_user_id, actor_id=actor["id"], source="admin")
    return {"deactivated": len(ids), "account_ids": ids}


@router.get("")
async def list_users(user: dict = admin_required):
    """List users (admin only)."""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, role, active, telegram_id, dingtalk_user_id, must_change_password, created_at
            FROM users
            ORDER BY id
            """
        )
    out = []
    for r in rows:
        d = dict(r)
        d["created_at"] = d["created_at"].isoformat() if d["created_at"] else None
        out.append(d)
    return out


class UserIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    role: str = Field(..., pattern="^(returns|repair|admin)$")
    telegram_id: int | None = None
    dingtalk_user_id: str | None = Field(default=None, max_length=128)
    active: bool = True


@router.post("")
async def create_user(
    payload: UserIn,
    actor: dict = admin_required,
):
    """Create a new user (admin only)."""
    if payload.role not in ROLES:
        raise HTTPException(400, f"role must be one of {ROLES}")

    async with pool().acquire() as conn:
        # If a user with this name exists (case-insensitive), update; else insert.
        existing = await conn.fetchrow(
            "SELECT id, dingtalk_user_id FROM users WHERE LOWER(name) = LOWER($1)",
            payload.name,
        )
        dingtalk_user_id = (payload.dingtalk_user_id or "").strip() or (existing.get("dingtalk_user_id") if existing else None)
        if payload.role != "admin" and not dingtalk_user_id:
            raise HTTPException(400, "DingTalk employee ID required for staff accounts")
        if existing:
            row = await conn.fetchrow(
                """
                UPDATE users
                SET role = $1, active = $2, telegram_id = $3,
                    dingtalk_user_id = COALESCE($4, dingtalk_user_id)
                WHERE id = $5
                RETURNING id, name, role, active, telegram_id, dingtalk_user_id, must_change_password, created_at
                """,
                payload.role, payload.active, payload.telegram_id,
                dingtalk_user_id, existing["id"],
            )
        else:
            row = await conn.fetchrow(
                """
                INSERT INTO users (name, role, active, telegram_id, dingtalk_user_id)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, name, role, active, telegram_id, dingtalk_user_id, must_change_password, created_at
                """,
                payload.name, payload.role, payload.active, payload.telegram_id,
                dingtalk_user_id,
            )
        await conn.execute(
            """
            INSERT INTO audit_log (user_id, action, entity_type, entity_id, details)
            VALUES ($1, 'create_or_update', 'user', $2, $3::jsonb)
            """,
            actor["id"], row["id"],
            json.dumps({"name": payload.name, "role": payload.role}),
        )

    d = dict(row)
    d["created_at"] = d["created_at"].isoformat() if d["created_at"] else None
    return d


class UserUpdate(BaseModel):
    role: str | None = Field(default=None, pattern="^(returns|repair|admin)$")
    active: bool | None = None
    telegram_id: int | None = None
    dingtalk_user_id: str | None = Field(default=None, max_length=128)


@router.patch("/{user_id}")
async def update_user(
    user_id: int,
    payload: UserUpdate,
    actor: dict = admin_required,
):
    """Update role / active / telegram_id (admin only). Cannot delete the last admin."""
    if payload.role is None and payload.active is None and payload.telegram_id is None and "dingtalk_user_id" not in payload.model_fields_set:
        raise HTTPException(400, "nothing to update")

    async with pool().acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id, role, active, telegram_id, dingtalk_user_id FROM users WHERE id=$1",
            user_id,
        )
        if existing is None:
            raise HTTPException(404, "user not found")

        new_role = payload.role if payload.role is not None else existing["role"]
        new_active = payload.active if payload.active is not None else existing["active"]
        new_tid = payload.telegram_id if payload.telegram_id is not None else existing["telegram_id"]
        new_dingtalk_id = ((payload.dingtalk_user_id or "").strip() or None)
        if "dingtalk_user_id" not in payload.model_fields_set:
            new_dingtalk_id = existing.get("dingtalk_user_id")
        if new_role != "admin" and not new_dingtalk_id:
            raise HTTPException(400, "DingTalk employee ID required for staff accounts")

        # protect last admin from demotion/deactivation
        if existing["role"] == "admin" and (new_role != "admin" or not new_active):
            admin_count = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE role='admin' AND active=TRUE"
            )
            if admin_count <= 1:
                raise HTTPException(400, "cannot demote/deactivate the last admin")

        row = await conn.fetchrow(
            """
            UPDATE users
            SET role = $1, active = $2, telegram_id = $3, dingtalk_user_id = $4
            WHERE id = $5
            RETURNING id, name, role, active, telegram_id, dingtalk_user_id, must_change_password, created_at
            """,
            new_role, new_active, new_tid, new_dingtalk_id, user_id,
        )

        await conn.execute(
            """
            INSERT INTO audit_log (user_id, action, entity_type, entity_id, details)
            VALUES ($1, 'update', 'user', $2, $3::jsonb)
            """,
            actor["id"], user_id,
            json.dumps({"role": new_role, "active": new_active, "dingtalk_user_id": new_dingtalk_id}),
        )

    d = dict(row)
    d["created_at"] = d["created_at"].isoformat() if d["created_at"] else None
    return d


@router.delete("/{user_id}", status_code=200)
async def deactivate_user(
    user_id: int,
    actor: dict = admin_required,
):
    """Soft-deactivate a user (admin only). The user cannot log in afterwards."""
    if user_id == actor["id"]:
        raise HTTPException(400, "cannot delete your own account")

    async with pool().acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id, role, active, name FROM users WHERE id=$1",
            user_id,
        )
        if existing is None:
            raise HTTPException(404, "user not found")
        if not existing["active"]:
            return {"id": user_id, "name": existing["name"], "active": False, "noop": True}

        if existing["role"] == "admin":
            admin_count = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE role='admin' AND active=TRUE"
            )
            if admin_count <= 1:
                raise HTTPException(400, "cannot deactivate the last admin")

        await conn.execute("UPDATE users SET active=FALSE WHERE id=$1", user_id)

        await conn.execute(
            """
            INSERT INTO audit_log (user_id, action, entity_type, entity_id)
            VALUES ($1, 'deactivate', 'user', $2)
            """,
            actor["id"], user_id,
        )

    return {"id": user_id, "name": existing["name"], "active": False}


@router.post("/{user_id}/reset-password")
async def reset_password(user_id: int, actor: dict = admin_required):
    """Invalidate the personal password and require the initial token again."""
    async with pool().acquire() as conn:
        row = await conn.fetchrow("SELECT id, name, active FROM users WHERE id=$1", user_id)
        if row is None:
            raise HTTPException(404, "user not found")
        if not row["active"]:
            raise HTTPException(400, "inactive account")
        await conn.execute(
            "UPDATE users SET password_hash=NULL, must_change_password=TRUE, "
            "session_version=session_version+1 WHERE id=$1",
            user_id,
        )
        await conn.execute(
            "INSERT INTO audit_log (user_id, action, entity_type, entity_id) "
            "VALUES ($1, 'reset_password', 'user', $2)",
            actor["id"], user_id,
        )
    return {"id": user_id, "must_change_password": True}


# Token helper endpoint — admin-only, valid only before personal password setup.
@router.get("/token-for/{name}")
async def token_for(name: str, actor: dict = admin_required):
    """Return a temporary login token for an active account awaiting setup."""
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT name, active, must_change_password FROM users WHERE LOWER(name)=LOWER($1)",
            name,
        )
    if row is None or not row["active"]:
        raise HTTPException(404, "active user not found")
    if not row["must_change_password"]:
        raise HTTPException(400, "password already set; reset it first")
    from app.auth import login_token_for
    return {"name": row["name"], "token": login_token_for(row["name"])}
