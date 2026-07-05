"""Simple session auth using signed cookies.

Login: username + password (bcrypt-style: we use a pre-shared secret per user for v1).
Session: signed cookie containing user_id.

For Telegram login (WebApp initData), use verify_telegram_initdata().
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from typing import Optional

from fastapi import Cookie, Depends, HTTPException, Request, status

SESSION_COOKIE = "jstreturn_session"
SESSION_TTL = 60 * 60 * 24 * 30  # 30 days


def _b64(data: bytes) -> str:
    return urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload: bytes) -> bytes:
    secret = os.environ["SESSION_SECRET"].encode()
    return hmac.new(secret, payload, hashlib.sha256).digest()


def make_session(user_id: int) -> str:
    payload = json.dumps({"u": user_id, "t": int(time.time())}).encode()
    sig = _sign(payload)
    return f"{_b64(payload)}.{_b64(sig)}"


def read_session(token: str) -> Optional[int]:
    try:
        body, sig = token.split(".", 1)
        payload = _b64d(body)
        expected_sig = _sign(payload)
        actual_sig = _b64d(sig)
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None
        data = json.loads(payload)
        if time.time() - data["t"] > SESSION_TTL:
            return None
        return int(data["u"])
    except Exception:
        return None


def login_token_for(name: str) -> str:
    """v1: each user has a pre-shared token; first login bootstraps them.

    Real production would use bcrypt-hashed passwords in users.password_hash.
    """
    return hashlib.sha256(
        os.environ["SESSION_SECRET"].encode() + name.encode()
    ).hexdigest()[:16]


async def current_user(request: Request) -> dict:
    """Dependency: return user row from DB. Raises 401 if not logged in."""
    from app.db import pool

    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not logged in")
    uid = read_session(token)
    if uid is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid session")
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, telegram_id, name, role, active FROM users WHERE id=$1",
            uid,
        )
    if row is None or not row["active"]:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user not found")
    return dict(row)


def require_role(*roles: str):
    async def dep(user: dict = Depends(current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"need role in {roles}")
        return user
    return dep


def verify_telegram_initdata(init_data: str, bot_token: str) -> Optional[dict]:
    """Verify Telegram WebApp initData and return user dict.

    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    try:
        params = dict(p.split("=", 1) for p in init_data.split("&"))
    except ValueError:
        return None
    check_string_parts = []
    for k in sorted(params.keys()):
        if k != "hash":
            check_string_parts.append(f"{k}={params[k]}")
    check_string = "\n".join(check_string_parts)
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, params.get("hash", "")):
        return None
    import urllib.parse
    user_json = urllib.parse.unquote(params.get("user", "{}"))
    return json.loads(user_json)