"""User management — admin only."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import current_user, require_role
from app.db import pool

router = APIRouter(prefix="/api/users", tags=["users"])

ROLES = ("returns", "repair", "admin")


def _require_admin():
    return Depends(require_role("admin"))


@router.get("")
async def list_users(user: dict = Depends(_require_admin)):
    """List users (admin only)."""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, role, active, telegram_id, created_at
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
    active: bool = True


@router.post("")
async def create_user(
    payload: UserIn,
    actor: dict = Depends(_require_admin),
):
    """Create a new user (admin only)."""
    if payload.role not in ROLES:
        raise HTTPException(400, f"role must be one of {ROLES}")

    async with pool().acquire() as conn:
        # If a user with this name exists (case-insensitive), update; else insert.
        existing = await conn.fetchrow(
            "SELECT id FROM users WHERE LOWER(name) = LOWER($1)",
            payload.name,
        )
        if existing:
            row = await conn.fetchrow(
                """
                UPDATE users
                SET role = $1, active = $2, telegram_id = $3
                WHERE id = $4
                RETURNING id, name, role, active, telegram_id, created_at
                """,
                payload.role, payload.active, payload.telegram_id, existing["id"],
            )
        else:
            row = await conn.fetchrow(
                """
                INSERT INTO users (name, role, active, telegram_id)
                VALUES ($1, $2, $3, $4)
                RETURNING id, name, role, active, telegram_id, created_at
                """,
                payload.name, payload.role, payload.active, payload.telegram_id,
            )
        await conn.execute(
            """
            INSERT INTO audit_log (user_id, action, entity_type, entity_id, details)
            VALUES ($1, 'create_or_update', 'user', $2, $3::jsonb)
            """,
            actor["id"], row["id"],
            f'{{"name":"{payload.name}","role":"{payload.role}"}}',
        )

    d = dict(row)
    d["created_at"] = d["created_at"].isoformat() if d["created_at"] else None
    return d


class UserUpdate(BaseModel):
    role: str | None = Field(default=None, pattern="^(returns|repair|admin)$")
    active: bool | None = None
    telegram_id: int | None = None


@router.patch("/{user_id}")
async def update_user(
    user_id: int,
    payload: UserUpdate,
    actor: dict = Depends(_require_admin),
):
    """Update role / active / telegram_id (admin only). Cannot delete the last admin."""
    if payload.role is None and payload.active is None and payload.telegram_id is None:
        raise HTTPException(400, "nothing to update")

    async with pool().acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id, role, active, telegram_id FROM users WHERE id=$1",
            user_id,
        )
        if existing is None:
            raise HTTPException(404, "user not found")

        new_role = payload.role if payload.role is not None else existing["role"]
        new_active = payload.active if payload.active is not None else existing["active"]
        new_tid = payload.telegram_id if payload.telegram_id is not None else existing["telegram_id"]

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
            SET role = $1, active = $2, telegram_id = $3
            WHERE id = $4
            RETURNING id, name, role, active, telegram_id, created_at
            """,
            new_role, new_active, new_tid, user_id,
        )

        await conn.execute(
            """
            INSERT INTO audit_log (user_id, action, entity_type, entity_id, details)
            VALUES ($1, 'update', 'user', $2, $3::jsonb)
            """,
            actor["id"], user_id,
            f'{{"role":"{new_role}","active":{str(new_active).lower()}}}',
        )

    d = dict(row)
    d["created_at"] = d["created_at"].isoformat() if d["created_at"] else None
    return d


@router.delete("/{user_id}", status_code=200)
async def deactivate_user(
    user_id: int,
    actor: dict = Depends(_require_admin),
):
    """Soft-deactivate a user (admin only). The user cannot log in afterwards."""
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


# Token helper endpoint — admin-only, lets admin look up or compute a token.
@router.get("/token-for/{name}")
async def token_for(name: str, actor: dict = Depends(_require_admin)):
    """Return the login token for a given name. Admin only.

    Token = sha256(SESSION_SECRET + name)[:16].
    """
    from app.auth import login_token_for
    return {"name": name, "token": login_token_for(name)}
