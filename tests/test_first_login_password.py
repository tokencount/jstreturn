"""First login is restricted until the initial token is replaced by a password."""

import asyncio
from unittest.mock import patch

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import app.auth as auth_core
import app.routers.auth as auth_routes
import app.routers.users as users_routes


class FakeDb:
    def __init__(self):
        self.user = {"id": 7, "name": "worker", "role": "returns", "active": True,
                     "telegram_id": None, "password_hash": None, "must_change_password": True,
                     "session_version": 0}

    def acquire(self):
        return self

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def fetchrow(self, query, *args):
        if "LOWER(name)" in query:
            return dict(self.user) if args[0].lower() == self.user["name"] else None
        if "WHERE id=$1" in query:
            return dict(self.user) if args[0] == self.user["id"] and self.user["active"] else None
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        if "COUNT(*) FROM users" in query:
            return 1
        raise AssertionError(query)

    async def execute(self, query, *args):
        if "SET password_hash" in query:
            self.user["password_hash"] = args[0]
            self.user["must_change_password"] = False
            self.user["session_version"] += 1


def test_first_login_requires_password_change_and_invalidates_initial_token(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-secret-not-production")
    db = FakeDb()
    app = FastAPI()
    app.include_router(auth_routes.router)

    @app.get("/protected")
    async def protected(user: dict = Depends(auth_core.current_user)):
        return {"ok": True}

    with patch.object(auth_routes, "pool", lambda: db), patch("app.db.pool", lambda: db):
        with TestClient(app, base_url="https://testserver") as client:
            token = auth_core.login_token_for("worker")
            login = client.post("/api/auth/login", json={"name": "worker", "token": token})
            assert login.status_code == 200
            assert login.json()["must_change_password"] is True
            old_cookie = client.cookies.get(auth_core.SESSION_COOKIE)
            assert client.get("/protected").status_code == 403
            assert client.get("/api/auth/me").json()["must_change_password"] is True
            assert client.post("/api/auth/change-password", json={
                "current_password": "wrong", "new_password": "new-password-123",
            }).status_code == 401
            changed = client.post("/api/auth/change-password", json={
                "current_password": token, "new_password": "new-password-123",
            })
            assert changed.status_code == 200
            assert db.user["password_hash"] != "new-password-123"
            assert client.get("/protected").status_code == 200
            with TestClient(app, base_url="https://testserver") as stale_client:
                stale_client.cookies.set(auth_core.SESSION_COOKIE, old_cookie)
                assert stale_client.get("/protected").status_code == 401
            assert client.post("/api/auth/login", json={"name": "worker", "token": token}).status_code == 401
            assert client.post("/api/auth/login", json={
                "name": "worker", "token": "new-password-123",
            }).status_code == 200
            assert client.post("/api/auth/login", json={
                "name": "unregistered", "token": auth_core.login_token_for("unregistered"),
            }).status_code == 401


def test_admin_reset_revokes_existing_session_and_restores_initial_token(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-secret-not-production")
    db = FakeDb()
    db.user["password_hash"] = auth_core.hash_password("old-password-123")
    db.user["must_change_password"] = False
    original = auth_core.make_session(db.user["id"], db.user["session_version"])

    async def fetchrow(query, *args):
        if "WHERE LOWER(name)" in query or "WHERE id=$1" in query:
            return dict(db.user)
        raise AssertionError(query)

    async def execute(query, *args):
        if "SET password_hash=NULL" in query:
            db.user["password_hash"] = None
            db.user["must_change_password"] = True
            db.user["session_version"] += 1

    db.fetchrow = fetchrow
    db.execute = execute
    with patch.object(users_routes, "pool", lambda: db):
        result = asyncio.run(users_routes.reset_password(7, actor={"id": 1}))
        assert result["must_change_password"] is True
        assert db.user["session_version"] == 1
        assert auth_core.read_session(original) == (7, 0)
        issued = asyncio.run(users_routes.token_for("worker", actor={"id": 1}))
        assert issued["token"] == auth_core.login_token_for("worker")
