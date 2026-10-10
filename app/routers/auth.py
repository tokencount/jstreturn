"""Auth routes — login, first-login password change, logout, me."""
from __future__ import annotations

import hmac
import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from app.auth import (
    SESSION_COOKIE,
    current_user,
    hash_password,
    login_token_for,
    make_session,
    verify_password,
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

    # Constant-time compare for temporary credentials.

    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, role, active, password_hash, must_change_password, session_version FROM users WHERE LOWER(name)=LOWER($1)",
            name,
        )

        if row is None:
            if not hmac.compare_digest(token, login_token_for(name)):
                raise HTTPException(401, "invalid credentials")
            # Only the first-ever admin may bootstrap via login. Employee
            # accounts must be created by admin so they can be linked to a
            # DingTalk identity before receiving a temporary credential.
            count = await conn.fetchval("SELECT COUNT(*) FROM users") or 0
            if int(count) != 0:
                raise HTTPException(401, "account not registered")
            try:
                row = await conn.fetchrow(
                    """
                    INSERT INTO users (name, role, active)
                    VALUES ($1, $2, TRUE)
                    RETURNING id, name, role, active, password_hash, must_change_password, session_version
                    """,
                    name, "admin",
                )
            except asyncpg.UniqueViolationError:
                # Race: another request created them; re-read.
                row = await conn.fetchrow(
                    "SELECT id, name, role, active, password_hash, must_change_password, session_version FROM users WHERE LOWER(name)=LOWER($1)",
                    name,
                )

    if row is None or not row["active"]:
        raise HTTPException(401, "user not found or inactive")
    if row["password_hash"]:
        valid = verify_password(token, row["password_hash"])
    else:
        valid = hmac.compare_digest(token, login_token_for(row["name"]))
    if not valid:
        raise HTTPException(401, "invalid credentials")

    session = make_session(row["id"], row["session_version"])
    response.set_cookie(
        SESSION_COOKIE,
        session,
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="lax",
        secure=True,
    )
    return {"id": row["id"], "name": row["name"], "role": row["role"],
            "must_change_password": row["must_change_password"]}


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


@router.post("/change-password")
async def change_password(payload: ChangePasswordIn, response: Response, user: dict = Depends(current_user)):
    if not 10 <= len(payload.new_password) <= 128:
        raise HTTPException(400, "new password must be 10-128 characters")
    if payload.new_password == payload.current_password:
        raise HTTPException(400, "new password must differ from current password")
    async with pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT name, password_hash FROM users WHERE id=$1 AND active=TRUE FOR UPDATE",
                user["id"],
            )
            if row is None:
                raise HTTPException(401, "user not found or inactive")
            if row["password_hash"]:
                valid = verify_password(payload.current_password, row["password_hash"])
            else:
                valid = hmac.compare_digest(payload.current_password, login_token_for(row["name"]))
            if not valid:
                raise HTTPException(401, "invalid current password")
            await conn.execute(
                "UPDATE users SET password_hash=$1, must_change_password=FALSE, session_version=session_version+1 WHERE id=$2",
                hash_password(payload.new_password), user["id"],
            )
            await conn.execute(
                "INSERT INTO audit_log (user_id, action, entity_type, entity_id) "
                "VALUES ($1, 'change_password', 'user', $1)",
                user["id"],
            )
    response.set_cookie(
        SESSION_COOKIE, make_session(user["id"], user.get("session_version", 0) + 1),
        max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax", secure=True,
    )
    return {"ok": True}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@router.get("/me")
async def me(user: dict = Depends(current_user)):
    return user
