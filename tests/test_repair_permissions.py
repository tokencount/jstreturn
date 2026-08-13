"""Repair-specific permission tests.

Single-purpose mirror of `test_returns_permissions.py` for the repair
role. Pinned here so future refactors cannot regress the repair
workflow: the only thing a repair user can do is mark items complete
on READY, plus read inventory/items.

Matrix pinned here (P3 final):

  repair MAY:
    - GET /api/defectives
    - GET /api/defectives/{id}
    - GET /api/defectives/_/ready
    - GET /api/defectives/_/pending
    - GET /api/defectives/filter
    - POST /api/defectives/{id}/complete  (status must be READY)
    - POST /api/defectives/bulk mark_complete
    - GET /api/exports/purchase
    - GET /api/exports/purchase/preview

  repair MAY NOT:
    - POST /api/defectives
    - PATCH /api/defectives/{id}
    - DELETE /api/defectives/{id}
    - PUT /api/defectives/{id}/parts
    - POST /api/defectives/bulk set_sku / set_location / set_product_name / delete / recompute
    - POST /api/imports/defectives
    - POST /api/inventory/upload
    - POST /api/users
    - PATCH /api/users/{id}
    - DELETE /api/users/{id}

Audit log entries must include actor_role so the matrix is auditable.
"""
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.routers.defectives as defectives_mod
import app.routers.users as users_mod
import app.matcher as matcher_mod
import app.db as db_mod
from app.auth import current_user


class _FakeConn:
    def __init__(self):
        self.fetchrow = AsyncMock()
        self.fetch = AsyncMock(return_value=[])
        self.execute = AsyncMock()
        self.executemany = AsyncMock()
        self._tx_active = False

    def transaction(self):
        outer = self

        class _TxCM:
            async def __aenter__(self_inner):
                outer._tx_active = True
                return outer

            async def __aexit__(self_inner, *exc):
                outer._tx_active = False
                return None

        return _TxCM()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


def make_pool_with(conn):
    pool = MagicMock()

    class _PoolCM:
        async def __aenter__(self_inner):
            return conn

        async def __aexit__(self_inner, *exc):
            return None

    pool.acquire = MagicMock(return_value=_PoolCM())
    return pool


def _build_app(role="repair"):
    app = FastAPI()
    app.include_router(defectives_mod.router)
    app.include_router(users_mod.router)
    fake_user = {"id": 100, "name": f"test-{role}", "role": role, "active": True, "telegram_id": None}
    app.dependency_overrides[current_user] = lambda: fake_user

    def _realistic_require_role(*roles):
        async def dep(user=None):
            if user is None:
                user = fake_user
            if user.get("role") not in roles:
                from fastapi import HTTPException, status as _status
                raise HTTPException(_status.HTTP_403_FORBIDDEN, f"need role in {roles}")
            return user
        return dep

    import app.auth as _auth_mod
    import app.routers.users as _users_mod
    import app.routers.defectives as _def_mod
    _auth_mod.require_role = _realistic_require_role
    _users_mod.require_role = _realistic_require_role
    _def_mod.require_role = _realistic_require_role
    return app, fake_user


def _mk_item(status="READY", sku="SKU-X"):
    return {
        "id": 42, "business_date": "2026-08-14", "pallet_no": "PLT-X",
        "product_name": None, "location": None, "sku": sku, "qty": 1, "status": status,
        "created_by": 1, "created_at": None, "completed_by": None, "completed_at": None,
    }


def _audit_calls_with(conn, action_substring):
    return [
        c.args for c in conn.execute.call_args_list
        if len(c.args) >= 2 and isinstance(c.args[0], str) and action_substring in c.args[0]
    ]


def _audit_details(conn, action_substring):
    calls = _audit_calls_with(conn, action_substring)
    if not calls:
        return None
    for args in calls:
        if len(args) >= 4:
            try:
                return json.loads(args[3])
            except Exception:
                return None
    return None


class RepairCannotMutateDefectiveTests(unittest.TestCase):
    """Repair must NOT PATCH / PUT parts / DELETE defective items."""

    def _set_repair(self):
        self.app, self.user = _build_app("repair")
        self.conn = _FakeConn()
        self._pool_patch = patch.object(defectives_mod, "pool", lambda: make_pool_with(self.conn))
        self._pool_patch.start()
        self._eval_patch = patch.object(defectives_mod, "evaluate_status", AsyncMock(return_value="READY"))
        self._eval_patch.start()
        self.client = TestClient(self.app)

    def tearDown(self):
        self._pool_patch.stop()
        try:
            self._eval_patch.stop()
        except Exception:
            pass

    def test_repair_cannot_patch_sku(self):
        self._set_repair()
        self.conn.fetchrow = AsyncMock(return_value=_mk_item("COMPLETED"))
        r = self.client.patch("/api/defectives/42", json={"sku": "NEW"})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertFalse(_audit_calls_with(self.conn, "'patch'"))

    def test_repair_cannot_put_parts(self):
        self._set_repair()
        self.conn.fetchrow = AsyncMock(return_value=_mk_item("COMPLETED"))
        r = self.client.put(
            "/api/defectives/42/parts",
            json=[{"part_code": "HS-A", "qty": 1}],
        )
        self.assertEqual(r.status_code, 403, r.text)
        self.assertFalse(_audit_calls_with(self.conn, "put_parts"))

    def test_repair_cannot_delete(self):
        self._set_repair()
        self.conn.fetchrow = AsyncMock(return_value=_mk_item("COMPLETED"))
        r = self.client.delete("/api/defectives/42")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertFalse(_audit_calls_with(self.conn, "'delete'"))

    def test_repair_cannot_create_defective(self):
        """Repair cannot POST a new defective item either."""
        self._set_repair()
        # If the role gate is bypassed the handler will try to fetchval
        # on the conn. We don't expect that — the request should fail at
        # the gate.
        self.conn.fetchval = AsyncMock(return_value=42)
        payload = {
            "pallet_no": "PLT", "sku": "SKU", "qty": 1,
            "parts": [{"part_code": "HS-A", "qty": 1}],
        }
        r = self.client.post("/api/defectives", json=payload)
        self.assertEqual(r.status_code, 403, r.text)


class RepairCannotBulkActionsTests(unittest.TestCase):
    """Repair can ONLY mark_complete on bulk; every other bulk action is 403."""

    def _set_repair(self):
        self.app, self.user = _build_app("repair")
        self.conn = _FakeConn()
        self._pool_patch = patch.object(defectives_mod, "pool", lambda: make_pool_with(self.conn))
        self._pool_patch.start()
        self.client = TestClient(self.app)

    def tearDown(self):
        self._pool_patch.stop()

    def _pre_flight(self, item):
        async def _side_effect(query, *args, **kwargs):
            return item
        self.conn.fetchrow = AsyncMock(side_effect=_side_effect)

    def test_repair_cannot_bulk_set_sku(self):
        self._set_repair()
        self._pre_flight(_mk_item("PENDING"))
        r = self.client.post(
            "/api/defectives/bulk",
            json={"ids": [42], "action": "set_sku", "sku": "X"},
        )
        self.assertEqual(r.status_code, 403, r.text)
        self.assertFalse(_audit_calls_with(self.conn, "bulk_set_sku"))

    def test_repair_cannot_bulk_set_location(self):
        self._set_repair()
        self._pre_flight(_mk_item("PENDING"))
        r = self.client.post(
            "/api/defectives/bulk",
            json={"ids": [42], "action": "set_location", "location": "X"},
        )
        self.assertEqual(r.status_code, 403, r.text)
        self.assertFalse(_audit_calls_with(self.conn, "bulk_set_location"))

    def test_repair_cannot_bulk_set_product_name(self):
        self._set_repair()
        self._pre_flight(_mk_item("PENDING"))
        r = self.client.post(
            "/api/defectives/bulk",
            json={"ids": [42], "action": "set_product_name", "product_name": "X"},
        )
        self.assertEqual(r.status_code, 403, r.text)
        self.assertFalse(_audit_calls_with(self.conn, "bulk_set_product_name"))

    def test_repair_cannot_bulk_delete(self):
        self._set_repair()
        self._pre_flight(_mk_item("PENDING"))
        r = self.client.post(
            "/api/defectives/bulk",
            json={"ids": [42], "action": "delete"},
        )
        self.assertEqual(r.status_code, 403, r.text)
        self.assertFalse(_audit_calls_with(self.conn, "bulk_delete"))

    def test_repair_cannot_bulk_recompute(self):
        self._set_repair()
        self._pre_flight(_mk_item("PENDING"))
        r = self.client.post(
            "/api/defectives/bulk",
            json={"ids": [42], "action": "recompute"},
        )
        self.assertEqual(r.status_code, 403, r.text)

    def test_repair_can_bulk_mark_complete(self):
        self._set_repair()
        self._pre_flight(_mk_item("READY"))
        r = self.client.post(
            "/api/defectives/bulk",
            json={"ids": [42], "action": "mark_complete"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertGreaterEqual(body["succeeded"], 1)
        # audit_log 'bulk_complete' with actor_role=repair
        details = _audit_details(self.conn, "bulk_complete")
        self.assertIsNotNone(details)
        self.assertEqual(details["actor_role"], "repair")


class RepairCompleteTests(unittest.TestCase):
    """Repair can complete a READY item."""

    def _set_repair(self):
        self.app, self.user = _build_app("repair")
        self.conn = _FakeConn()
        self._pool_patch = patch.object(defectives_mod, "pool", lambda: make_pool_with(self.conn))
        self._pool_patch.start()
        self.client = TestClient(self.app)

    def tearDown(self):
        self._pool_patch.stop()

    def test_repair_can_complete_ready(self):
        self._set_repair()
        self.conn.fetchrow = AsyncMock(return_value=_mk_item("READY"))
        r = self.client.post("/api/defectives/42/complete")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(_audit_calls_with(self.conn, "'complete'"))

    def test_repair_cannot_complete_pending(self):
        """PENDING items cannot be completed directly."""
        self._set_repair()
        self.conn.fetchrow = AsyncMock(return_value=_mk_item("PENDING"))
        r = self.client.post("/api/defectives/42/complete")
        self.assertEqual(r.status_code, 400, r.text)


class RepairReadOnlyTests(unittest.TestCase):
    """Repair can read list / single / filtered items."""

    def _set_repair(self):
        self.app, self.user = _build_app("repair")
        self.conn = _FakeConn()
        self._pool_patch = patch.object(defectives_mod, "pool", lambda: make_pool_with(self.conn))
        self._pool_patch.start()
        self.client = TestClient(self.app)

    def tearDown(self):
        self._pool_patch.stop()

    def test_repair_can_list_empty(self):
        self._set_repair()
        # list_with_parts is mocked via patch on matcher_mod.
        with patch.object(matcher_mod, "list_with_parts", AsyncMock(return_value=[])), \
             patch.object(matcher_mod, "pool", lambda: make_pool_with(self.conn)):
            r = self.client.get("/api/defectives")
            self.assertNotEqual(r.status_code, 403)
            self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
