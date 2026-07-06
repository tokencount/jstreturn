"""Auth routes — login / logout / me.

v1 self-bootstrapping:
- Each user has a token = sha256(SESSION_SECRET + name)[:16]
- First login for a name CREATES the user row automatically (since SQLite/the
  data store may not pre-seed users). Default role = 'returns' unless the
  caller passes 'role' in the body AND there are no users yet (so the FIRST
  login in a fresh database becomes admin).
- An existing admin can change roles via /api/users/{id}.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from app.auth import (
    SESSION_COOKIE,
    current_user,
    login_token_for,
    make_session,
)
from app.db import pool

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    name: str
    token: str


@router.post("/login")
async def login(payload: LoginIn, response: Response):
    name = (payload.name or "").strip()
    token = (payload.token or "").strip()
    if not name or not token:
        raise HTTPException(400, "name and token required")

    expected = login_token_for(name)
    # Constant-time-ish compare
    if not (len(token) == len(expected) and all(a == b for a, b in zip(token, expected))):
        raise HTTPException(401, "invalid credentials")

    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, role, active FROM users WHERE LOWER(name)=LOWER($1)",
            name,
        )

        if row is None:
            # Bootstrapping: first-ever user becomes admin.
            count = await conn.fetchval("SELECT COUNT(*) FROM users")
            role = "admin" if count == 0 else "returns"
            row = await conn.fetchrow(
                """
                INSERT INTO users (name, role, active)
                VALUES ($1, $2, TRUE)
                ON CONFLICT (name) DO NOTHING
                RETURNING id, name, role, active
                """,
                name, role,
            )
            if row is None:
                # Race: another request created them; re-read.
                row = await conn.fetchrow(
                    "SELECT id, name, role, active FROM users WHERE LOWER(name)=LOWER($1)",
                    name,
                )

    if row is None or not row["active"]:
        raise HTTPException(401, "user not found or inactive")

    session = make_session(row["id"])
    response.set_cookie(
        SESSION_COOKIE,
        session,
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="lax",
        secure=True,
    )
    return {"id": row["id"], "name": row["name"], "role": row["role"]}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@router.get("/me")
async def me(user: dict = Depends(current_user)):
    return user
