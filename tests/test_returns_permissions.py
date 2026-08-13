"""Returns / repair / admin permission matrix — P3 final.

The P3 final matrix (per Cc's latest clarification):

  | op                              | returns | repair | admin |
  |---------------------------------|---------|--------|-------|
  | POST   /api/defectives          | ✓       | ✗      | ✓     |
  | PATCH  /api/defectives/{id}     | ✓       | ✗      | ✓     |
  | DELETE /api/defectives/{id}     | ✓       | ✗      | ✓     |
  | PUT    /api/defectives/{id}/parts | ✓     | ✗      | ✓     |
  | POST   /api/defectives/{id}/complete | ✗   | ✓      | ✓     |
  | POST   /api/defectives/bulk mark_complete | ✗ | ✓ | ✓ |
  | POST   /api/defectives/bulk recompute | ✓ | ✗     | ✓     |
  | POST   /api/defectives/bulk delete / set_* | ✓ | ✗ | ✓ |
  | POST   /api/inventory/upload           | ✗    | ✗ | ✓ |
  | POST   /api/imports/defectives         | ✓    | ✗ | ✓ |
  | GET    /api/exports/purchase           | ✓    | ✓ | ✓ |
  | POST/PATCH/DELETE /api/users/*         | ✗    | ✗ | ✓ |

Audit log writes should always include `actor_role` so the audit trail
mirrors the matrix.

Tests construct a fake asyncpg pool + FastAPI TestClient (no real DB) by
patching `app.db.pool` and `app.auth.require_role`. The patched
``require_role`` honours the requested role: it raises 403 for
non-matches so admin-only endpoints (users / inventory / imports) behave
correctly under each role.
"""
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.routers.defectives as defectives_mod
import app.routers.users as users_mod
from app.auth import current_user


# ---------------------------------------------------------------------------
# Mock asyncpg pool + DB primitives
# ---------------------------------------------------------------------------

class _FakeConn:
    """Minimal asyncpg connection stand-in."""

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


def make_pool_with(conn: _FakeConn) -> MagicMock:
    pool = MagicMock()

    class _PoolCM:
        async def __aenter__(self_inner):
            return conn

        async def __aexit__(self_inner, *exc):
            return None

    def _acquire(*_args, **_kwargs):
        return _PoolCM()

    pool.acquire = MagicMock(side_effect=_acquire)
    return pool


# ---------------------------------------------------------------------------
# App fixture with dependency overrides + pool patch
# ---------------------------------------------------------------------------

def _build_app(role: str, user_id: int = 100, active_user: bool = True):
    """Build a FastAPI app with the auth dependency overridden for the
    given role and ``app.db.pool`` patched to a Mock pool so endpoint
    code that calls pool() doesn't blow up.
    """
    app = FastAPI()
    app.include_router(defectives_mod.router)
    app.include_router(users_mod.router)

    fake_user = {
        "id": user_id,
        "name": f"test-{role}",
        "role": role,
        "active": active_user,
        "telegram_id": None,
    }

    # Override current_user so endpoints that resolve it directly pick up
    # our fake user.
    app.dependency_overrides[current_user] = lambda: fake_user

    # Replace require_role in the relevant modules. The patched version
    # honours the requested role: if the role doesn't match it raises
    # HTTP 403 just like the real implementation, otherwise returns the
    # fake user. This makes 403 tests for non-admin users against
    # /api/users/* behave correctly even when the endpoint's nested
    # Depends(Depends(...)) pattern would otherwise bypass our override.
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
    import app.routers.inventory as _inv_mod  # noqa: F401
    import app.routers.exports as _exp_mod  # noqa: F401

    _auth_mod.require_role = _realistic_require_role
    _users_mod.require_role = _realistic_require_role
    _def_mod.require_role = _realistic_require_role

    return app, fake_user


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mk_item(status: str = "PENDING", sku: str = "SKU-X"):
    """Return a fake row like defective_items."""
    return {
        "id": 42,
        "business_date": "2026-08-14",
        "pallet_no": "PLT-X",
        "product_name": None,
        "location": None,
        "sku": sku,
        "qty": 1,
        "status": status,
        "created_by": 1,
        "created_at": None,
        "completed_by": None,
        "completed_at": None,
    }


def _audit_calls_with(conn, action_substring: str):
    """Return the list of execute() call arg-tuple-lists whose SQL mentions action_substring."""
    return [
        c.args for c in conn.execute.call_args_list
        if len(c.args) >= 2 and isinstance(c.args[0], str)
        and action_substring in c.args[0]
    ]


def _audit_details(conn, action_substring: str):
    """Return the parsed JSON of the audit_log details for the first matching call."""
    calls = _audit_calls_with(conn, action_substring)
    if not calls:
        return None
    # INSERT INTO audit_log stores details as the 4th positional arg
    # (user_id, action, entity_id, details::jsonb).
    for args in calls:
        if len(args) >= 4:
            try:
                return json.loads(args[3])
            except Exception:
                return None
    return None


# ---------------------------------------------------------------------------
# Tests: returns can PATCH / PUT / DELETE on every status (matrix gate)
# ---------------------------------------------------------------------------

class ReturnsPatchPermissionsTests(unittest.TestCase):
    """returns can PATCH sku on every status."""

    def _set_role(self, role):
        self.app, self.user = _build_app(role)
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

    def test_returns_can_patch_sku_on_pending(self):
        self._set_role("returns")
        self.conn.fetchrow = AsyncMock(side_effect=[_mk_item("PENDING", "OLD-SKU"), _mk_item("PENDING", "NEW-SKU")])
        r = self.client.patch("/api/defectives/42", json={"sku": "NEW-SKU"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["sku"], "NEW-SKU")
        details = _audit_details(self.conn, "'patch'")
        self.assertIsNotNone(details)
        self.assertIn("sku", details["fields"])
        self.assertEqual(details["actor_role"], "returns")

    def test_returns_can_patch_sku_on_completed(self):
        self._set_role("returns")
        self.conn.fetchrow = AsyncMock(side_effect=[_mk_item("COMPLETED", "OLD-SKU"), _mk_item("COMPLETED", "NEW-SKU")])
        r = self.client.patch("/api/defectives/42", json={"sku": "NEW-SKU"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_returns_can_patch_sku_on_ready(self):
        self._set_role("returns")
        self.conn.fetchrow = AsyncMock(side_effect=[_mk_item("READY", "OLD-SKU"), _mk_item("READY", "NEW-SKU")])
        r = self.client.patch("/api/defectives/42", json={"sku": "NEW-SKU"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_admin_can_patch_sku_on_completed(self):
        self._set_role("admin")
        self.conn.fetchrow = AsyncMock(side_effect=[_mk_item("COMPLETED", "OLD-SKU"), _mk_item("COMPLETED", "NEW-SKU")])
        r = self.client.patch("/api/defectives/42", json={"sku": "NEW-SKU"})
        self.assertEqual(r.status_code, 200, r.text)


class RepairCannotPatchPermissionsTests(unittest.TestCase):
    """Repair CANNOT PATCH (matrix gate)."""

    def _set_role(self, role):
        self.app, self.user = _build_app(role)
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

    def test_repair_CANNOT_patch_sku_on_completed(self):
        self._set_role("repair")
        # The role gate runs BEFORE the DB so we don't even need
        # fetchrow to be set. If the route bypasses the gate, the test
        # would still fail because the conn would not be properly set.
        # We pre-set fetchrow to None so the route returns 404 if it
        # accidentally bypasses the role gate.
        self.conn.fetchrow = AsyncMock(return_value=None)
        r = self.client.patch("/api/defectives/42", json={"sku": "NEW-SKU"})
        self.assertEqual(r.status_code, 403, r.text)
        # No audit log entry for patch should be created.
        self.assertFalse(_audit_calls_with(self.conn, "'patch'"))


class ReturnsPartsPermissionsTests(unittest.TestCase):
    """returns can PUT parts on every status."""

    def _set_role(self, role):
        self.app, self.user = _build_app(role)
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

    def test_returns_can_put_parts_on_completed(self):
        self._set_role("returns")
        self.conn.fetchrow = AsyncMock(return_value=_mk_item("COMPLETED"))
        r = self.client.put(
            "/api/defectives/42/parts",
            json=[{"part_code": "HS-A", "part_name": "x", "qty": 1}],
        )
        self.assertEqual(r.status_code, 200, r.text)
        details = _audit_details(self.conn, "put_parts")
        self.assertIsNotNone(details)
        self.assertEqual(details["actor_role"], "returns")

    def test_returns_can_put_parts_on_pending(self):
        self._set_role("returns")
        self.conn.fetchrow = AsyncMock(return_value=_mk_item("PENDING"))
        r = self.client.put(
            "/api/defectives/42/parts",
            json=[{"part_code": "HS-A", "qty": 2}],
        )
        self.assertEqual(r.status_code, 200, r.text)


class RepairCannotPartsPermissionsTests(unittest.TestCase):
    """Repair CANNOT PUT parts (matrix gate)."""

    def _set_role(self, role):
        self.app, self.user = _build_app(role)
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

    def test_repair_CANNOT_put_parts_on_completed(self):
        self._set_role("repair")
        self.conn.fetchrow = AsyncMock(return_value=_mk_item("COMPLETED"))
        r = self.client.put(
            "/api/defectives/42/parts",
            json=[{"part_code": "HS-A", "qty": 1}],
        )
        self.assertEqual(r.status_code, 403, r.text)
        self.assertFalse(_audit_calls_with(self.conn, "put_parts"))


class ReturnsDeletePermissionsTests(unittest.TestCase):
    """returns can DELETE on every status."""

    def _set_role(self, role):
        self.app, self.user = _build_app(role)
        self.conn = _FakeConn()
        self._pool_patch = patch.object(defectives_mod, "pool", lambda: make_pool_with(self.conn))
        self._pool_patch.start()
        self.client = TestClient(self.app)

    def tearDown(self):
        self._pool_patch.stop()

    def test_returns_can_delete_pending(self):
        self._set_role("returns")
        self.conn.fetchrow = AsyncMock(return_value=_mk_item("PENDING", "SKU-P"))
        r = self.client.delete("/api/defectives/42")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["deleted"], True)
        details = _audit_details(self.conn, "'delete'")
        self.assertIsNotNone(details)
        self.assertEqual(details["actor_role"], "returns")
        self.assertEqual(details["previous_status"], "PENDING")

    def test_returns_can_delete_completed(self):
        self._set_role("returns")
        self.conn.fetchrow = AsyncMock(return_value=_mk_item("COMPLETED", "SKU-C"))
        r = self.client.delete("/api/defectives/42")
        self.assertEqual(r.status_code, 200, r.text)

    def test_returns_can_delete_ready(self):
        self._set_role("returns")
        self.conn.fetchrow = AsyncMock(return_value=_mk_item("READY", "SKU-R"))
        r = self.client.delete("/api/defectives/42")
        self.assertEqual(r.status_code, 200, r.text)

    def test_admin_can_delete_completed(self):
        self._set_role("admin")
        self.conn.fetchrow = AsyncMock(return_value=_mk_item("COMPLETED"))
        r = self.client.delete("/api/defectives/42")
        self.assertEqual(r.status_code, 200, r.text)

    def test_delete_returns_404_when_missing(self):
        self._set_role("returns")
        self.conn.fetchrow = AsyncMock(return_value=None)
        r = self.client.delete("/api/defectives/9999")
        self.assertEqual(r.status_code, 404)


class RepairCannotDeletePermissionsTests(unittest.TestCase):
    """Repair CANNOT DELETE (matrix gate)."""

    def _set_role(self, role):
        self.app, self.user = _build_app(role)
        self.conn = _FakeConn()
        self._pool_patch = patch.object(defectives_mod, "pool", lambda: make_pool_with(self.conn))
        self._pool_patch.start()
        self.client = TestClient(self.app)

    def tearDown(self):
        self._pool_patch.stop()

    def test_repair_CANNOT_delete_completed(self):
        self._set_role("repair")
        self.conn.fetchrow = AsyncMock(return_value=_mk_item("COMPLETED"))
        r = self.client.delete("/api/defectives/42")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertFalse(_audit_calls_with(self.conn, "'delete'"))

    def test_repair_CANNOT_delete_pending(self):
        self._set_role("repair")
        self.conn.fetchrow = AsyncMock(return_value=_mk_item("PENDING"))
        r = self.client.delete("/api/defectives/42")
        self.assertEqual(r.status_code, 403, r.text)


class ReturnsBulkPermissionsTests(unittest.TestCase):
    """returns can run every bulk action EXCEPT mark_complete."""

    def _set_role(self, role):
        self.app, self.user = _build_app(role)
        self.conn = _FakeConn()
        self._pool_patch = patch.object(defectives_mod, "pool", lambda: make_pool_with(self.conn))
        self._pool_patch.start()
        self.client = TestClient(self.app)

    def tearDown(self):
        self._pool_patch.stop()

    def _set_pre_flight(self, item):
        async def _side_effect(query, *args, **kwargs):
            return item
        self.conn.fetchrow = AsyncMock(side_effect=_side_effect)

    def test_returns_bulk_set_sku(self):
        self._set_role("returns")
        self._set_pre_flight(_mk_item("PENDING", "OLD-SKU"))
        r = self.client.post(
            "/api/defectives/bulk",
            json={"ids": [42], "action": "set_sku", "sku": "NEW-SKU"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        details = _audit_details(self.conn, "bulk_set_sku")
        self.assertIsNotNone(details)
        self.assertEqual(details["actor_role"], "returns")
        self.assertEqual(details["sku"], "NEW-SKU")

    def test_returns_bulk_set_location(self):
        self._set_role("returns")
        self._set_pre_flight(_mk_item("PENDING"))
        r = self.client.post(
            "/api/defectives/bulk",
            json={"ids": [42], "action": "set_location", "location": "H5-99-9"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        details = _audit_details(self.conn, "bulk_set_location")
        self.assertIsNotNone(details)
        self.assertEqual(details["actor_role"], "returns")
        self.assertEqual(details["location"], "H5-99-9")

    def test_returns_bulk_set_product_name(self):
        self._set_role("returns")
        self._set_pre_flight(_mk_item("PENDING"))
        r = self.client.post(
            "/api/defectives/bulk",
            json={"ids": [42], "action": "set_product_name", "product_name": "Foo"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        details = _audit_details(self.conn, "bulk_set_product_name")
        self.assertIsNotNone(details)
        self.assertEqual(details["actor_role"], "returns")
        self.assertEqual(details["product_name"], "Foo")

    def test_returns_bulk_delete(self):
        self._set_role("returns")
        self._set_pre_flight(_mk_item("PENDING"))
        r = self.client.post(
            "/api/defectives/bulk",
            json={"ids": [42], "action": "delete"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        details = _audit_details(self.conn, "bulk_delete")
        self.assertIsNotNone(details)
        self.assertEqual(details["actor_role"], "returns")

    def test_returns_bulk_recompute(self):
        self._set_role("returns")
        self._set_pre_flight(_mk_item("PENDING"))
        r = self.client.post(
            "/api/defectives/bulk",
            json={"ids": [42], "action": "recompute"},
        )
        self.assertEqual(r.status_code, 200, r.text)

    def test_returns_bulk_mark_complete_blocked(self):
        """mark_complete is still repair/admin only."""
        self._set_role("returns")
        self._set_pre_flight(_mk_item("READY"))
        r = self.client.post(
            "/api/defectives/bulk",
            json={"ids": [42], "action": "mark_complete"},
        )
        # The new bulk handler raises 403 directly on the action gate.
        self.assertEqual(r.status_code, 403, r.text)


class RepairCannotBulkPermissionsTests(unittest.TestCase):
    """Repair CANNOT run bulk set_sku / set_location / set_product_name / delete / recompute."""

    def _set_role(self, role):
        self.app, self.user = _build_app(role)
        self.conn = _FakeConn()
        self._pool_patch = patch.object(defectives_mod, "pool", lambda: make_pool_with(self.conn))
        self._pool_patch.start()
        self.client = TestClient(self.app)

    def tearDown(self):
        self._pool_patch.stop()

    def _set_pre_flight(self, item):
        async def _side_effect(query, *args, **kwargs):
            return item
        self.conn.fetchrow = AsyncMock(side_effect=_side_effect)

    def test_repair_CANNOT_bulk_set_sku(self):
        self._set_role("repair")
        self._set_pre_flight(_mk_item("PENDING"))
        r = self.client.post(
            "/api/defectives/bulk",
            json={"ids": [42], "action": "set_sku", "sku": "NEW-SKU"},
        )
        self.assertEqual(r.status_code, 403, r.text)
        self.assertFalse(_audit_calls_with(self.conn, "bulk_set_sku"))

    def test_repair_CANNOT_bulk_delete(self):
        self._set_role("repair")
        self._set_pre_flight(_mk_item("PENDING"))
        r = self.client.post(
            "/api/defectives/bulk",
            json={"ids": [42], "action": "delete"},
        )
        self.assertEqual(r.status_code, 403, r.text)
        self.assertFalse(_audit_calls_with(self.conn, "bulk_delete"))

    def test_repair_CANNOT_bulk_recompute(self):
        self._set_role("repair")
        self._set_pre_flight(_mk_item("PENDING"))
        r = self.client.post(
            "/api/defectives/bulk",
            json={"ids": [42], "action": "recompute"},
        )
        self.assertEqual(r.status_code, 403, r.text)

    def test_repair_CAN_bulk_mark_complete(self):
        self._set_role("repair")
        self._set_pre_flight(_mk_item("READY"))
        r = self.client.post(
            "/api/defectives/bulk",
            json={"ids": [42], "action": "mark_complete"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # The repair user should succeed since the item is READY.
        self.assertGreaterEqual(body["succeeded"], 1)
        details = _audit_details(self.conn, "bulk_complete")
        self.assertIsNotNone(details)
        self.assertEqual(details["actor_role"], "repair")


class UsersPermissionsTests(unittest.TestCase):
    """returns / repair CANNOT touch /api/users; admin only."""

    def _set_role(self, role):
        self.app, self.user = _build_app(role)
        self.conn = _FakeConn()
        self._pool_patch = patch.object(users_mod, "pool", lambda: make_pool_with(self.conn))
        self._pool_patch.start()
        self.client = TestClient(self.app)

    def tearDown(self):
        self._pool_patch.stop()

    def test_returns_cannot_list_users(self):
        self._set_role("returns")
        r = self.client.get("/api/users")
        self.assertEqual(r.status_code, 403, r.text)

    def test_returns_cannot_create_user(self):
        self._set_role("returns")
        r = self.client.post("/api/users", json={"name": "x", "role": "returns"})
        self.assertEqual(r.status_code, 403, r.text)

    def test_returns_cannot_patch_user(self):
        self._set_role("returns")
        r = self.client.patch("/api/users/1", json={"role": "admin"})
        self.assertEqual(r.status_code, 403, r.text)

    def test_returns_cannot_delete_user(self):
        self._set_role("returns")
        r = self.client.delete("/api/users/1")
        self.assertEqual(r.status_code, 403, r.text)

    def test_repair_cannot_create_user(self):
        self._set_role("repair")
        r = self.client.post("/api/users", json={"name": "x", "role": "repair"})
        self.assertEqual(r.status_code, 403, r.text)

    def test_repair_cannot_patch_user(self):
        self._set_role("repair")
        r = self.client.patch("/api/users/1", json={"role": "admin"})
        self.assertEqual(r.status_code, 403, r.text)

    def test_admin_can_list_users(self):
        self._set_role("admin")
        self.conn.fetch = AsyncMock(return_value=[])
        r = self.client.get("/api/users")
        self.assertEqual(r.status_code, 200, r.text)


class CompletePermissionsTests(unittest.TestCase):
    """/complete still requires repair or admin; returns is forbidden."""

    def _set_role(self, role):
        self.app, self.user = _build_app(role)
        self.conn = _FakeConn()
        self._pool_patch = patch.object(defectives_mod, "pool", lambda: make_pool_with(self.conn))
        self._pool_patch.start()
        self.client = TestClient(self.app)

    def tearDown(self):
        self._pool_patch.stop()

    def test_returns_cannot_complete(self):
        self._set_role("returns")
        self.conn.fetchrow = AsyncMock(return_value=_mk_item("READY"))
        r = self.client.post("/api/defectives/42/complete")
        self.assertEqual(r.status_code, 403, r.text)

    def test_repair_can_complete(self):
        self._set_role("repair")
        self.conn.fetchrow = AsyncMock(return_value=_mk_item("READY"))
        r = self.client.post("/api/defectives/42/complete")
        self.assertEqual(r.status_code, 200, r.text)
        # audit_log 'complete' row written
        self.assertTrue(_audit_calls_with(self.conn, "'complete'"))

    def test_admin_can_complete(self):
        self._set_role("admin")
        self.conn.fetchrow = AsyncMock(return_value=_mk_item("READY"))
        r = self.client.post("/api/defectives/42/complete")
        self.assertEqual(r.status_code, 200, r.text)


class AuditLogActorRoleTests(unittest.TestCase):
    """Audit log entries carry actor_role so the audit trail mirrors the matrix."""

    def _set_role(self, role):
        self.app, self.user = _build_app(role)
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

    def test_patch_audit_mentions_returns(self):
        self._set_role("returns")
        self.conn.fetchrow = AsyncMock(side_effect=[_mk_item("PENDING", "X"), _mk_item("PENDING", "Y")])
        r = self.client.patch("/api/defectives/42", json={"sku": "Y"})
        self.assertEqual(r.status_code, 200, r.text)
        details = _audit_details(self.conn, "'patch'")
        self.assertIsNotNone(details)
        self.assertEqual(details["actor_role"], "returns")

    def test_delete_audit_mentions_returns_previous_state(self):
        self._set_role("returns")
        self.conn.fetchrow = AsyncMock(return_value=_mk_item("COMPLETED", "SKU-C"))
        r = self.client.delete("/api/defectives/42")
        self.assertEqual(r.status_code, 200, r.text)
        details = _audit_details(self.conn, "'delete'")
        self.assertIsNotNone(details)
        self.assertEqual(details["actor_role"], "returns")
        self.assertEqual(details["previous_status"], "COMPLETED")
        self.assertEqual(details["previous_sku"], "SKU-C")


if __name__ == "__main__":
    unittest.main()
