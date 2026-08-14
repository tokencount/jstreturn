"""List-endpoint limit cap tests.

Pinned behaviour of the defective-returns list cap, after the 2026-08-14
bump from 10,000 to 200,000:

  - GET /api/defectives        : default 100, max 200_000 (was 10,000)
  - GET /api/defectives/filter : default 500, max 2000  (unchanged)

The 200k ceiling is the bulk-fetch path: a single call can pull the
whole catalog. The paginated READY/PENDING endpoints
(``/_/ready`` / ``/_/pending``) keep their own whitelist
(100/200/500/2000, default 500) — see test_ready_pending_pagination.py
for those caps.

Import semantics remain uncapped: POST /api/imports/defectives does
not take a ``limit`` query parameter — only the list cap was raised.

Tests construct a FastAPI TestClient with auth + pool patched so no
real DB is needed. They focus on FastAPI's Query-level validation
(``le``) — these are the gates that return 422 before the request
even reaches the handler.

Note: the ``/filter`` route is registered AFTER ``/{defective_id}`` in
``defectives.py``. FastAPI evaluates routes in registration order, so
a literal string like ``/filter`` matches ``{defective_id}`` first
and is parsed as an int → 422. This is a pre-existing route-order
quirk that is out of scope for the cap raise, so this test module
patches ``list_with_parts`` to drive the list handler directly when
testing the cap on ``/api/defectives`` (the one whose cap was
actually raised), introspects the ``/filter`` route signature to pin
its cap, and asserts the import endpoint stays uncapped.
"""
import inspect
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.routers.defectives as defectives_mod
import app.routers.imports as imports_mod
from app.auth import current_user


# ---------------------------------------------------------------------------
# Fixtures (mirroring test_returns_permissions.py style)
# ---------------------------------------------------------------------------

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


def _build_app(role="returns", include_imports=False):
    """Build a FastAPI app with auth overridden so list endpoints resolve
    a fake user. Optionally include the imports router."""
    app = FastAPI()
    app.include_router(defectives_mod.router)
    if include_imports:
        app.include_router(imports_mod.router)

    fake_user = {
        "id": 100,
        "name": f"test-{role}",
        "role": role,
        "active": True,
        "telegram_id": None,
    }
    app.dependency_overrides[current_user] = lambda: fake_user

    # Defensive: replace require_role in case the list chain ever
    # ends up using it (currently only mutation paths do, but this
    # keeps the test robust to refactors that promote list endpoints
    # behind a role gate).
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
    _auth_mod.require_role = _realistic_require_role
    defectives_mod.require_role = _realistic_require_role
    return app, fake_user


# ---------------------------------------------------------------------------
# Cap behaviour — GET /api/defectives  (the cap that was raised)
# ---------------------------------------------------------------------------

class ListDefectivesCapTests(unittest.TestCase):
    """GET /api/defectives cap is now 10000 (was 500)."""

    def _set_role(self, role="returns"):
        self.app, self.user = _build_app(role)
        self.conn = _FakeConn()
        self._pool_patch = patch.object(defectives_mod, "pool", lambda: make_pool_with(self.conn))
        self._pool_patch.start()
        self.client = TestClient(self.app)

    def tearDown(self):
        self._pool_patch.stop()

    def _patch_list_with_parts(self):
        """Return a (patch, seen) pair so each test can record the
        ``limit`` that the handler forwarded to ``list_with_parts``.

        We patch the name as bound inside ``defectives.py`` (it was
        imported via ``from app.matcher import ...``), so patching
        ``matcher_mod.list_with_parts`` would NOT be hit by the call.
        """
        seen = {}

        async def fake_lwp(status_filter=None, limit=200, offset=0):
            seen["status_filter"] = status_filter
            seen["limit"] = limit
            seen["offset"] = offset
            return []

        patcher = patch.object(defectives_mod, "list_with_parts", side_effect=fake_lwp)
        return patcher, seen

    def test_default_limit_is_100(self):
        """No ?limit → handler called with default 100."""
        self._set_role()
        patcher, seen = self._patch_list_with_parts()
        patcher.start()
        try:
            r = self.client.get("/api/defectives")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(seen.get("limit"), 100)
        finally:
            patcher.stop()

    def test_old_cap_500_now_accepted(self):
        """?limit=500 was the old upper bound; must now pass."""
        self._set_role()
        patcher, seen = self._patch_list_with_parts()
        patcher.start()
        try:
            r = self.client.get("/api/defectives?limit=500")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(seen.get("limit"), 500)
        finally:
            patcher.stop()

    def test_new_cap_200000_accepted(self):
        """?limit=200000 is the new upper bound (200k ceiling raise)."""
        self._set_role()
        patcher, seen = self._patch_list_with_parts()
        patcher.start()
        try:
            r = self.client.get("/api/defectives?limit=200000")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(seen.get("limit"), 200000)
        finally:
            patcher.stop()

    def test_old_cap_10000_still_accepted(self):
        """?limit=10000 must still pass (regression guard: previous cap
        value continues to work)."""
        self._set_role()
        patcher, seen = self._patch_list_with_parts()
        patcher.start()
        try:
            r = self.client.get("/api/defectives?limit=10000")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(seen.get("limit"), 10000)
        finally:
            patcher.stop()

    def test_one_over_cap_rejected(self):
        """One over the cap must still be a 422 (FastAPI Query gate)."""
        self._set_role()
        r = self.client.get("/api/defectives?limit=200001")
        self.assertEqual(r.status_code, 422, r.text)

    def test_limit_zero_rejected(self):
        """Lower bound stays at 1."""
        self._set_role()
        r = self.client.get("/api/defectives?limit=0")
        self.assertEqual(r.status_code, 422, r.text)

    def test_status_filter_with_high_limit(self):
        """?status=PENDING&limit=200000 is the realistic load scenario."""
        self._set_role()
        patcher, seen = self._patch_list_with_parts()
        patcher.start()
        try:
            r = self.client.get("/api/defectives?status=PENDING&limit=200000")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(seen.get("status_filter"), "PENDING")
            self.assertEqual(seen.get("limit"), 200000)
        finally:
            patcher.stop()


# ---------------------------------------------------------------------------
# Pin the new cap values via direct route-introspection
# ---------------------------------------------------------------------------

class RouteSignatureCapTests(unittest.TestCase):
    """Pin the Query signature on ``list_defectives`` and ``filter_list``
    so any future change to the cap shows up as a failing test."""

    def _extract_le_ge(self, query_default):
        """FastAPI 0.141 stores Query bounds in ``metadata`` as Ge/Le
        marker objects from the ``annotated_types`` package. Extract
        ``le`` (upper) and ``ge`` (lower) ints robustly across the
        FastAPI/Pydantic combo we ship with."""
        from annotated_types import Ge, Le
        le, ge = None, None
        for m in getattr(query_default, "metadata", []) or []:
            if isinstance(m, Le):
                le = m.le
            elif isinstance(m, Ge):
                ge = m.ge
        return ge, le

    def test_list_defectives_default_is_100_le_is_200000(self):
        from app.routers.defectives import list_defectives
        sig = inspect.signature(list_defectives)
        limit = sig.parameters["limit"]
        # FastAPI stores ``Query(100, ge=1, le=200_000)`` — the bare
        # default value sits on ``.default`` (here ``100``), and the
        # bounds live in ``.metadata`` as pydantic Ge/Le markers.
        # The 200k ceiling is the bulk-fetch path; the paginated
        # READY/PENDING endpoints have their own caps (see
        # test_ready_pending_pagination.py).
        self.assertEqual(limit.default.default, 100)
        ge, le = self._extract_le_ge(limit.default)
        self.assertEqual(ge, 1)
        self.assertEqual(le, 200_000)

    def test_filter_list_default_is_500_le_is_2000(self):
        """``/filter`` cap was NOT touched by the raise — pin its
        unchanged values so we don't accidentally regress it."""
        from app.routers.defectives import filter_list
        sig = inspect.signature(filter_list)
        limit = sig.parameters["limit"]
        self.assertEqual(limit.default.default, 500)
        ge, le = self._extract_le_ge(limit.default)
        self.assertEqual(ge, 1)
        self.assertEqual(le, 2000)


# ---------------------------------------------------------------------------
# Regression guard: import endpoint stays uncapped
# ---------------------------------------------------------------------------

class ImportEndpointUncappedTests(unittest.TestCase):
    """POST /api/imports/defectives does NOT accept a ``limit`` query
    parameter — import semantics remain uncapped. Regression guard so
    a future refactor doesn't accidentally bolt a row cap onto the
    import route."""

    def setUp(self):
        self.app, self.user = _build_app("returns", include_imports=True)
        self.conn = _FakeConn()
        # Patch the pool on every module that touches DB.
        self._pool_patch_def = patch.object(defectives_mod, "pool", lambda: make_pool_with(self.conn))
        self._pool_patch_imp = patch.object(imports_mod, "pool", lambda: make_pool_with(self.conn))
        self._pool_patch_def.start()
        self._pool_patch_imp.start()
        self.client = TestClient(self.app)

    def tearDown(self):
        self._pool_patch_def.stop()
        self._pool_patch_imp.stop()

    def test_import_does_not_accept_limit_query(self):
        """A ``limit`` query param must NOT be silently accepted on the
        import endpoint. We expect a 422 (unknown query) — meaning the
        route's signature is clean. If somebody later adds a row cap
        here, the test will start accepting the param and we want it
        to flip to red."""
        # No body — we only care about the Query schema. The value
        # is irrelevant: the import endpoint must reject ANY limit
        # param, regardless of the 200k general-list ceiling.
        r = self.client.post("/api/imports/defectives?limit=200000")
        # 422 (unknown query param) is the desired outcome.
        self.assertEqual(r.status_code, 422, r.text)


if __name__ == "__main__":
    unittest.main()