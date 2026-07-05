"""Auth routes — login / logout / me."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.auth import (
    SESSION_COOKIE,
    current_user,
    login_token_for,
    make_session,
)
from app.db import pool

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
async def login(payload: dict, response: Response):
    """Login with name + token (v1).

    Body: {"name": "zhangsan", "token": "abc123..."}
    """
    name = (payload.get("name") or "").strip()
    token = (payload.get("token") or "").strip()
    if not name or not token:
        raise HTTPException(400, "name and token required")

    expected = login_token_for(name)
    # Constant-time-ish compare (good enough for v1)
    if not (len(token) == len(expected) and all(a == b for a, b in zip(token, expected))):
        raise HTTPException(401, "invalid credentials")

    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, role FROM users WHERE LOWER(name)=LOWER($1) AND active=TRUE",
            name,
        )
    if row is None:
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