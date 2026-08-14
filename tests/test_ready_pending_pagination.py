"""READY and PENDING independent pagination regression tests.

Pinned behaviour for the 2026-08-14 pagination refactor (and the
same-day scope bump on the general list cap from 10,000 to 200,000):

  * /api/defectives/_/ready and /api/defectives/_/pending
      - independent pagination state in the front-end (separate
        pageSize + page index per tab)
      - page-size whitelist: 100, 200, 500, 2000; default 500
      - server-side pagination (limit + offset) with stable ORDER BY
      - response carries an ACCURATE total independent of the page
      - default page-size in /api/defectives/_/ready and
        /api/defectives/_/pending is 500
  * /api/defectives/_/count
      - returns per-status counts (PENDING/READY/COMPLETED) so the
        front-end tab badges reflect the entire database
      - the count is a true ``COUNT(*)`` — never derived from
        materialising a 200k-row page
      - ?status=X scopes the response
  * /api/defectives (general list, bulk-fetch path)
      - default 100, max 200_000 (raised from 10,000 in the 2026-08-14
        scope bump so a single call can pull the entire catalog)
      - the paginated READY/PENDING caps are NOT affected by this
        ceiling — they keep their own whitelist
  * COMPLETED behaviour stays safe
      - ``loadList('COMPLETED')`` keeps the legacy single-shot fetch
        (limit=200000 after the scope bump) and does not paginate

The tests construct a FastAPI TestClient with auth + pool patched so
no real DB is needed. The pagination helpers
(``list_with_parts_paged`` / ``count_by_status``) are patched at the
matcher level so each test can assert the exact args the handler
forwards and craft the page slice the handler should return.
"""
import inspect
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.matcher as matcher_mod
import app.routers.defectives as defectives_mod
from app.auth import current_user


# ---------------------------------------------------------------------------
# Fixtures (mirroring test_list_limit_cap.py / test_returns_permissions.py)
# ---------------------------------------------------------------------------

class _FakeConn:
    def __init__(self):
        self.fetchrow = AsyncMock()
        self.fetch = AsyncMock(return_value=[])
        self.fetchval = AsyncMock(return_value=0)
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


def _build_app(role="returns"):
    app = FastAPI()
    app.include_router(defectives_mod.router)
    fake_user = {
        "id": 100,
        "name": f"test-{role}",
        "role": role,
        "active": True,
        "telegram_id": None,
    }
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
    _auth_mod.require_role = _realistic_require_role
    defectives_mod.require_role = _realistic_require_role
    return app, fake_user


# ---------------------------------------------------------------------------
# Pin constants on the matcher so accidental refactors trip the tests.
# ---------------------------------------------------------------------------

class MatcherConstantsTests(unittest.TestCase):
    """Pin the page-size whitelist + default. Changing these requires
    updating both the router Query default AND the matcher constant —
    this test catches drift between the two layers."""

    def test_allowed_page_sizes(self):
        self.assertEqual(matcher_mod.ALLOWED_PAGE_SIZES, (100, 200, 500, 2000))

    def test_default_page_size_is_500(self):
        self.assertEqual(matcher_mod.DEFAULT_PAGE_SIZE, 500)

    def test_default_in_whitelist(self):
        self.assertIn(matcher_mod.DEFAULT_PAGE_SIZE, matcher_mod.ALLOWED_PAGE_SIZES)


# ---------------------------------------------------------------------------
# Route signature: page_size + offset on /_/ready and /_/pending.
# ---------------------------------------------------------------------------

class RouteSignatureTests(unittest.TestCase):
    """Pin the Query signature on the new READY/PENDING endpoints."""

    def _extract_le_ge(self, query_default):
        from annotated_types import Ge, Le
        le, ge = None, None
        for m in getattr(query_default, "metadata", []) or []:
            if isinstance(m, Le):
                le = m.le
            elif isinstance(m, Ge):
                ge = m.ge
        return ge, le

    def test_ready_endpoint_signature(self):
        from app.routers.defectives import list_ready
        sig = inspect.signature(list_ready)
        self.assertIn("page_size", sig.parameters)
        self.assertIn("offset", sig.parameters)
        ps = sig.parameters["page_size"]
        ge, le = self._extract_le_ge(ps.default)
        self.assertEqual(ps.default.default, 500)  # default page size
        self.assertEqual(ge, 1)
        # Upper bound is the largest allowed size; the whitelist itself
        # is enforced in the handler body (test below).
        self.assertEqual(le, 2000)
        off = sig.parameters["offset"]
        ge2, le2 = self._extract_le_ge(off.default)
        self.assertEqual(off.default.default, 0)
        self.assertEqual(ge2, 0)

    def test_pending_endpoint_signature(self):
        from app.routers.defectives import list_pending
        sig = inspect.signature(list_pending)
        self.assertIn("page_size", sig.parameters)
        self.assertIn("offset", sig.parameters)
        ps = sig.parameters["page_size"]
        ge, le = self._extract_le_ge(ps.default)
        self.assertEqual(ps.default.default, 500)
        self.assertEqual(le, 2000)


# ---------------------------------------------------------------------------
# /_/ready and /_/pending — page-size whitelist + default + offset math.
# ---------------------------------------------------------------------------

class PaginatedListEndpointTests(unittest.TestCase):
    """End-to-end tests against the FastAPI TestClient."""

    def setUp(self):
        self.app, self.user = _build_app()
        self.conn = _FakeConn()
        self._pool_patch = patch.object(defectives_mod, "pool", lambda: make_pool_with(self.conn))
        self._pool_patch.start()
        self.client = TestClient(self.app)

    def tearDown(self):
        self._pool_patch.stop()

    def _patch_lwp_paged(self, total=0, items=None):
        """Patch ``list_with_parts_paged`` on the matcher module so the
        route uses our canned response. The handler imports it via
        ``from app.matcher import ...`` so we have to patch the name as
        bound inside the router."""
        items = items if items is not None else []
        seen = {"calls": []}

        async def fake_paged(status_filter, limit, offset):
            seen["calls"].append({"status_filter": status_filter, "limit": limit, "offset": offset})
            return {"items": items, "total": total, "limit": limit, "offset": offset}

        patcher = patch.object(defectives_mod, "list_with_parts_paged", side_effect=fake_paged)
        return patcher, seen

    # --- default page size --------------------------------------------------

    def test_ready_default_page_size_is_500(self):
        patcher, seen = self._patch_lwp_paged(total=1234)
        patcher.start()
        try:
            r = self.client.get("/api/defectives/_/ready")
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual(body["limit"], 500)
            self.assertEqual(body["offset"], 0)
            self.assertEqual(body["total"], 1234)
            self.assertEqual(seen["calls"][0]["limit"], 500)
        finally:
            patcher.stop()

    def test_pending_default_page_size_is_500(self):
        patcher, seen = self._patch_lwp_paged(total=42)
        patcher.start()
        try:
            r = self.client.get("/api/defectives/_/pending")
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual(body["limit"], 500)
            self.assertEqual(body["offset"], 0)
            self.assertEqual(body["total"], 42)
            self.assertEqual(seen["calls"][0]["status_filter"], "PENDING")
        finally:
            patcher.stop()

    # --- whitelist enforcement ---------------------------------------------

    def test_ready_rejects_page_size_outside_whitelist(self):
        for bad in (50, 150, 250, 499, 501, 1000, 1999, 2001, 5000):
            r = self.client.get(f"/api/defectives/_/ready?page_size={bad}")
            self.assertEqual(r.status_code, 422, f"page_size={bad} should be rejected")

    def test_pending_rejects_page_size_outside_whitelist(self):
        for bad in (50, 150, 250, 499, 501, 1000, 1999, 2001, 5000):
            r = self.client.get(f"/api/defectives/_/pending?page_size={bad}")
            self.assertEqual(r.status_code, 422, f"page_size={bad} should be rejected")

    def test_ready_accepts_all_whitelisted_sizes(self):
        for size in matcher_mod.ALLOWED_PAGE_SIZES:
            patcher, _ = self._patch_lwp_paged(total=size * 3)
            patcher.start()
            try:
                r = self.client.get(f"/api/defectives/_/ready?page_size={size}")
                self.assertEqual(r.status_code, 200, f"page_size={size} must be accepted")
                self.assertEqual(r.json()["limit"], size)
            finally:
                patcher.stop()

    def test_pending_accepts_all_whitelisted_sizes(self):
        for size in matcher_mod.ALLOWED_PAGE_SIZES:
            patcher, _ = self._patch_lwp_paged(total=size * 3)
            patcher.start()
            try:
                r = self.client.get(f"/api/defectives/_/pending?page_size={size}")
                self.assertEqual(r.status_code, 200, f"page_size={size} must be accepted")
                self.assertEqual(r.json()["limit"], size)
            finally:
                patcher.stop()

    # --- offset forwarding --------------------------------------------------

    def test_ready_offset_zero_is_default(self):
        patcher, seen = self._patch_lwp_paged()
        patcher.start()
        try:
            r = self.client.get("/api/defectives/_/ready")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(seen["calls"][0]["offset"], 0)
        finally:
            patcher.stop()

    def test_ready_offset_is_forwarded(self):
        for off in (0, 500, 1000, 1500):
            patcher, seen = self._patch_lwp_paged()
            patcher.start()
            try:
                r = self.client.get(f"/api/defectives/_/ready?page_size=500&offset={off}")
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(seen["calls"][0]["offset"], off)
            finally:
                patcher.stop()

    def test_pending_offset_is_forwarded(self):
        for off in (0, 500, 1000, 1500):
            patcher, seen = self._patch_lwp_paged()
            patcher.start()
            try:
                r = self.client.get(f"/api/defectives/_/pending?page_size=500&offset={off}")
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(seen["calls"][0]["offset"], off)
            finally:
                patcher.stop()

    def test_ready_rejects_negative_offset(self):
        r = self.client.get("/api/defectives/_/ready?offset=-1")
        self.assertEqual(r.status_code, 422, r.text)

    def test_pending_rejects_negative_offset(self):
        r = self.client.get("/api/defectives/_/pending?offset=-1")
        self.assertEqual(r.status_code, 422, r.text)

    # --- accurate total ----------------------------------------------------

    def test_ready_returns_accurate_total_independent_of_page(self):
        """Page 1 of 2000 items, page_size=100 → total must still be 2000,
        not 100 (the size of the returned page)."""
        patcher, _ = self._patch_lwp_paged(total=2000, items=[{"id": 1}] * 100)
        patcher.start()
        try:
            r = self.client.get("/api/defectives/_/ready?page_size=100&offset=0")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["total"], 2000)
            self.assertEqual(len(r.json()["items"]), 100)
        finally:
            patcher.stop()

    def test_pending_returns_accurate_total_on_last_partial_page(self):
        """Total 1234, page_size=500, offset=1000 → 234 items on the
        last page; total must still be 1234."""
        patcher, _ = self._patch_lwp_paged(total=1234, items=[{"id": 1}] * 234)
        patcher.start()
        try:
            r = self.client.get("/api/defectives/_/pending?page_size=500&offset=1000")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["total"], 1234)
            self.assertEqual(len(r.json()["items"]), 234)
        finally:
            patcher.stop()

    # --- response shape -----------------------------------------------------

    def test_ready_response_has_items_total_limit_offset(self):
        patcher, _ = self._patch_lwp_paged(total=7, items=[{"id": 1}, {"id": 2}])
        patcher.start()
        try:
            r = self.client.get("/api/defectives/_/ready?page_size=100&offset=200")
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            for k in ("items", "total", "limit", "offset"):
                self.assertIn(k, body, f"response must include {k!r}")
            self.assertEqual(body["limit"], 100)
            self.assertEqual(body["offset"], 200)
            self.assertEqual(body["total"], 7)
            self.assertEqual(len(body["items"]), 2)
        finally:
            patcher.stop()


# ---------------------------------------------------------------------------
# /_/count — separate endpoint for accurate tab badges.
# ---------------------------------------------------------------------------

class CountEndpointTests(unittest.TestCase):
    """``/_/count`` returns per-status counts independent of any page."""

    def setUp(self):
        self.app, self.user = _build_app()
        self.conn = _FakeConn()
        self._pool_patch = patch.object(defectives_mod, "pool", lambda: make_pool_with(self.conn))
        self._pool_patch.start()
        self.client = TestClient(self.app)

    def tearDown(self):
        self._pool_patch.stop()

    def _patch_count_by_status(self, totals):
        seen = {"calls": []}
        async def fake_count(status):
            seen["calls"].append(status)
            return int(totals.get(status, 0))
        patcher = patch.object(defectives_mod, "count_by_status", side_effect=fake_count)
        return patcher, seen

    def test_count_no_param_returns_all_three(self):
        patcher, seen = self._patch_count_by_status(
            {"PENDING": 11, "READY": 22, "COMPLETED": 33}
        )
        patcher.start()
        try:
            r = self.client.get("/api/defectives/_/count")
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertIn("totals", body)
            self.assertEqual(body["totals"]["PENDING"], 11)
            self.assertEqual(body["totals"]["READY"], 22)
            self.assertEqual(body["totals"]["COMPLETED"], 33)
            # All three statuses queried regardless of order
            self.assertEqual(set(seen["calls"]), {"PENDING", "READY", "COMPLETED"})
        finally:
            patcher.stop()

    def test_count_scoped_to_status(self):
        patcher, seen = self._patch_count_by_status({"READY": 7})
        patcher.start()
        try:
            r = self.client.get("/api/defectives/_/count?status=READY")
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual(body["status"], "READY")
            self.assertEqual(body["total"], 7)
            self.assertEqual(seen["calls"], ["READY"])
        finally:
            patcher.stop()

    def test_count_rejects_bad_status(self):
        r = self.client.get("/api/defectives/_/count?status=BAD")
        self.assertEqual(r.status_code, 422, r.text)


# ---------------------------------------------------------------------------
# Regression guard: the legacy /api/defectives endpoint is unchanged.
# ---------------------------------------------------------------------------

class LegacyEndpointUnchangedTests(unittest.TestCase):
    """The 2026-08-14 cap raise on /api/defectives (default 100, max 200_000)
    must remain intact — paginated READY/PENDING live at their own URLs
    with their own whitelist (default 500, max 2000)."""

    def setUp(self):
        self.app, self.user = _build_app()
        self.conn = _FakeConn()
        self._pool_patch = patch.object(defectives_mod, "pool", lambda: make_pool_with(self.conn))
        self._pool_patch.start()
        self.client = TestClient(self.app)

    def tearDown(self):
        self._pool_patch.stop()

    def test_legacy_default_limit_is_100(self):
        seen = {}

        async def fake_lwp(status_filter=None, limit=200, offset=0):
            seen["limit"] = limit
            seen["offset"] = offset
            return []

        with patch.object(defectives_mod, "list_with_parts", side_effect=fake_lwp):
            r = self.client.get("/api/defectives")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(seen["limit"], 100)
            self.assertEqual(seen["offset"], 0)

    def test_legacy_max_is_200000(self):
        """The general list ceiling is now 200_000 (was 10_000)."""
        r = self.client.get("/api/defectives?limit=200001")
        self.assertEqual(r.status_code, 422, r.text)

    def test_legacy_accepts_previous_cap_value(self):
        """The previous cap (10_000) must still be accepted — a
        regression guard so the bump doesn't accidentally break older
        callers."""
        seen = {}

        async def fake_lwp(status_filter=None, limit=200, offset=0):
            seen["limit"] = limit
            return []

        with patch.object(defectives_mod, "list_with_parts", side_effect=fake_lwp):
            r = self.client.get("/api/defectives?limit=10000")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(seen["limit"], 10000)


# ---------------------------------------------------------------------------
# Front-end independence contract.
#
# The READY and PENDING tabs must keep their own pagination state on the
# client. We assert that the contract is exposed in two places:
#
#   1. The handler functions in app/routers/defectives.py are distinct
#      (list_ready vs list_pending), so the server-side endpoints are
#      truly independent.
#   2. The Alpine state object declares per-tab pagination fields
#      (readyPage / pendingPage / historyPage + their page-size twins),
#      so the client can swap tabs without losing its position.
# ---------------------------------------------------------------------------

class IndependenceContractTests(unittest.TestCase):
    """Pin that READY/PENDING are wired as independent endpoints AND
    that the Alpine state carries per-tab pagination fields."""

    def test_server_routes_are_distinct_functions(self):
        from app.routers import defectives
        self.assertIsNotNone(defectives.list_ready)
        self.assertIsNotNone(defectives.list_pending)
        # They share the same prefix but resolve to different routes.
        ready_path = next(
            (r.path for r in defectives.router.routes if getattr(r, "path", "").endswith("/_/ready")),
            None,
        )
        pending_path = next(
            (r.path for r in defectives.router.routes if getattr(r, "path", "").endswith("/_/pending")),
            None,
        )
        self.assertEqual(ready_path, "/api/defectives/_/ready")
        self.assertEqual(pending_path, "/api/defectives/_/pending")
        self.assertNotEqual(ready_path, pending_path)

    def test_alpine_state_declares_per_tab_pagination(self):
        """Static check on the Alpine template — the source of truth for
        per-tab pagination in the browser. We don't execute Alpine; we
        just require the per-tab fields to be declared as plain Alpine
        state so the front-end can address each tab independently."""
        from pathlib import Path
        html = Path(__file__).resolve().parents[1].joinpath(
            "app", "templates", "index.html"
        ).read_text(encoding="utf-8")
        for field in ("readyPage:", "readyPageSize:", "readyTotal:",
                      "pendingPage:", "pendingPageSize:", "pendingTotal:",
                      "historyPage:", "historyPageSize:", "historyTotal:"):
            self.assertIn(field, html, f"Alpine state must declare {field!r}")
        # Whitelist + default must be present so the front-end and the
        # server can never silently disagree.
        self.assertIn("ALLOWED_PAGE_SIZES: [100, 200, 500, 2000]", html)
        self.assertIn("DEFAULT_PAGE_SIZE: 500", html)


if __name__ == "__main__":
    unittest.main()