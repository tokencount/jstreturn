"""200k ceiling raise + COUNT-based totals regression tests.

Pinned behaviour after the 2026-08-14 scope bump:

  - ``GET /api/defectives`` accepts up to ``limit=200_000`` so a
    single bulk-fetch-style call can pull the entire catalog.
  - The paginated READY/PENDING endpoints (``/_/ready``,
    ``/_/pending``) keep their own whitelist (100/200/500/2000,
    default 500) — see ``test_ready_pending_pagination.py`` for
    those caps. They are NOT affected by the 200k raise.
  - The general list response NEVER carries a derived-from-page ``total``
    field; the only way to get an accurate total is through
    ``/_/count`` (true ``COUNT(*)``) or the paginated ``/_/ready``
    /``/_/pending`` endpoints (which also use ``COUNT(*)``).
    Materialising a 200k-row page and counting the result is
    explicitly forbidden.

Tests construct a FastAPI TestClient with auth + pool patched so no
real DB is needed. The pagination helpers are patched at the matcher
level so each test can assert the exact args the handler forwards and
craft the page slice the handler should return.
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
# General list ceiling — 200k cap.
# ---------------------------------------------------------------------------

class GeneralListCeilingTests(unittest.TestCase):
    """``GET /api/defectives`` must accept up to 200_000 so a single
    bulk-fetch call can pull the whole catalog. The cap is enforced by
    FastAPI's ``Query(le=200_000)`` — anything over the cap is a 422
    before the handler is invoked."""

    def setUp(self):
        self.app, self.user = _build_app()
        self.conn = _FakeConn()
        self._pool_patch = patch.object(defectives_mod, "pool", lambda: make_pool_with(self.conn))
        self._pool_patch.start()
        self.client = TestClient(self.app)

    def tearDown(self):
        self._pool_patch.stop()

    def _patch_lwp(self, fake_factory=None):
        seen = {"calls": []}

        async def fake_lwp(status_filter=None, limit=200, offset=0):
            seen["calls"].append({"status_filter": status_filter, "limit": limit, "offset": offset})
            if fake_factory is not None:
                return fake_factory(limit, offset)
            return []

        patcher = patch.object(defectives_mod, "list_with_parts", side_effect=fake_lwp)
        return patcher, seen

    def test_limit_200000_accepted(self):
        """The new ceiling — 200_000 — must be accepted."""
        patcher, seen = self._patch_lwp()
        patcher.start()
        try:
            r = self.client.get("/api/defectives?limit=200000")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(seen["calls"][0]["limit"], 200_000)
        finally:
            patcher.stop()

    def test_limit_200001_rejected(self):
        """One over the cap must be a 422 (FastAPI Query gate)."""
        r = self.client.get("/api/defectives?limit=200001")
        self.assertEqual(r.status_code, 422, r.text)

    def test_limit_far_above_cap_rejected(self):
        """Out-of-band values like 999_999 must be rejected with 422."""
        for huge in (300_000, 500_000, 999_999, 1_000_000):
            r = self.client.get(f"/api/defectives?limit={huge}")
            self.assertEqual(r.status_code, 422, f"limit={huge} must be rejected")

    def test_limit_previous_cap_still_accepted(self):
        """The previous cap (10_000) must still work — the bump is
        upward-compatible, not a breaking change."""
        patcher, seen = self._patch_lwp()
        patcher.start()
        try:
            r = self.client.get("/api/defectives?limit=10000")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(seen["calls"][0]["limit"], 10000)
        finally:
            patcher.stop()

    def test_status_filter_with_max_limit(self):
        """Common bulk-export-style call: ``?status=COMPLETED&limit=200000``."""
        patcher, seen = self._patch_lwp()
        patcher.start()
        try:
            r = self.client.get("/api/defectives?status=COMPLETED&limit=200000")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(seen["calls"][0]["status_filter"], "COMPLETED")
            self.assertEqual(seen["calls"][0]["limit"], 200_000)
        finally:
            patcher.stop()

    def test_offset_with_max_limit(self):
        """Offset up to 10_000_000 with limit=200_000 must work."""
        patcher, seen = self._patch_lwp()
        patcher.start()
        try:
            r = self.client.get("/api/defectives?limit=200000&offset=10000000")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(seen["calls"][0]["offset"], 10_000_000)
        finally:
            patcher.stop()

    def test_route_signature_pins_200k_cap(self):
        """Route signature introspection — pinning the cap at 200_000 so
        any future change shows up as a failing test."""
        sig = inspect.signature(defectives_mod.list_defectives)
        limit = sig.parameters["limit"]
        from annotated_types import Ge, Le
        le, ge = None, None
        for m in getattr(limit.default, "metadata", []) or []:
            if isinstance(m, Le):
                le = m.le
            elif isinstance(m, Ge):
                ge = m.ge
        self.assertEqual(ge, 1)
        self.assertEqual(le, 200_000)
        # Default must be 100 — unchanged.
        self.assertEqual(limit.default.default, 100)


# ---------------------------------------------------------------------------
# READY/PENDING caps are NOT affected by the 200k raise.
# ---------------------------------------------------------------------------

class PaginatedEndpointCapsUnchangedTests(unittest.TestCase):
    """The 200k general-list ceiling does NOT apply to the
    paginated READY/PENDING endpoints. Those keep their own whitelist
    (default 500, max 2000). This is a regression guard so a future
    bulk-fetch refactor doesn't accidentally widen the user-facing
    page-size selector."""

    def setUp(self):
        self.app, self.user = _build_app()
        self.conn = _FakeConn()
        self._pool_patch = patch.object(defectives_mod, "pool", lambda: make_pool_with(self.conn))
        self._pool_patch.start()
        self.client = TestClient(self.app)

    def tearDown(self):
        self._pool_patch.stop()

    def _patch_lwp_paged(self):
        async def fake_paged(status_filter, limit, offset):
            return {"items": [], "total": 0, "limit": limit, "offset": offset}
        return patch.object(defectives_mod, "list_with_parts_paged", side_effect=fake_paged)

    def test_ready_rejects_200k(self):
        """The 200k cap is NOT applicable to ``/_/ready`` — even if a
        caller asks for ``page_size=200000``, the whitelist rejects it."""
        with self._patch_lwp_paged():
            r = self.client.get("/api/defectives/_/ready?page_size=200000")
        self.assertEqual(r.status_code, 422, r.text)

    def test_pending_rejects_200k(self):
        with self._patch_lwp_paged():
            r = self.client.get("/api/defectives/_/pending?page_size=200000")
        self.assertEqual(r.status_code, 422, r.text)

    def test_ready_max_cap_is_2000(self):
        """``/_/ready`` whitelist upper bound is 2000, not 200000."""
        with self._patch_lwp_paged():
            r = self.client.get("/api/defectives/_/ready?page_size=2000")
        self.assertEqual(r.status_code, 200, r.text)

    def test_pending_max_cap_is_2000(self):
        with self._patch_lwp_paged():
            r = self.client.get("/api/defectives/_/pending?page_size=2000")
        self.assertEqual(r.status_code, 200, r.text)

    def test_ready_default_is_500(self):
        """Default page size on ``/_/ready`` is 500, not 200000."""
        with self._patch_lwp_paged():
            r = self.client.get("/api/defectives/_/ready")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["limit"], 500)

    def test_pending_default_is_500(self):
        with self._patch_lwp_paged():
            r = self.client.get("/api/defectives/_/pending")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["limit"], 500)


# ---------------------------------------------------------------------------
# Totals are true COUNT — never derived from a 200k-row page.
# ---------------------------------------------------------------------------

class CountBasedTotalsTests(unittest.TestCase):
    """The total returned by the paginated endpoints AND by ``/_/count``
    MUST come from a true ``COUNT(*)`` query. Materialising 200k rows
    and counting them in Python is explicitly forbidden — the cap
    raise amplifies the cost of any accidental derivation by a factor
    of 20 vs. the previous cap."""

    def setUp(self):
        self.app, self.user = _build_app()
        self.conn = _FakeConn()
        self._pool_patch = patch.object(defectives_mod, "pool", lambda: make_pool_with(self.conn))
        self._pool_patch.start()
        self.client = TestClient(self.app)

    def tearDown(self):
        self._pool_patch.stop()

    def test_count_endpoint_uses_count_query(self):
        """``/_/count`` must use ``count_by_status`` (which runs
        ``SELECT COUNT(*)``) — not iterate over paged items."""
        # Patch the helper the route imports.
        async def fake_count(status):
            return 42

        with patch.object(defectives_mod, "count_by_status", side_effect=fake_count):
            r = self.client.get("/api/defectives/_/count?status=READY")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["total"], 42)

    def test_list_with_parts_paged_total_is_count(self):
        """``list_with_parts_paged`` must call ``count_by_status`` —
        not derive the total from the page slice."""
        seen = {"count_calls": [], "lwp_calls": []}

        async def fake_lwp(status_filter=None, limit=200, offset=0):
            seen["lwp_calls"].append({"status_filter": status_filter, "limit": limit, "offset": offset})
            # Return a small page slice — the total must NOT be derived
            # from this slice's length.
            return [{"id": i, "status": status_filter or "READY"} for i in range(limit)]

        async def fake_count(status):
            seen["count_calls"].append(status)
            return 100_000  # a number that could never come from the page

        # list_with_parts_paged calls list_with_parts and count_by_status
        # INTERNALLY (in the same module), so we must patch the matcher
        # module's names, not the router's import bindings.
        with patch.object(matcher_mod, "list_with_parts", side_effect=fake_lwp), \
             patch.object(matcher_mod, "count_by_status", side_effect=fake_count):
            r = self.client.get("/api/defectives/_/ready?page_size=500&offset=0")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # The page returned 500 items, but the total is 100,000 — the
        # total was NOT derived from the page slice.
        self.assertEqual(len(body["items"]), 500)
        self.assertEqual(body["total"], 100_000)
        # The count helper was called exactly once with the right status.
        self.assertEqual(seen["count_calls"], ["READY"])
        # The page fetch ran with the right offset math.
        self.assertEqual(seen["lwp_calls"][0]["limit"], 500)
        self.assertEqual(seen["lwp_calls"][0]["offset"], 0)

    def test_total_is_independent_of_page_size(self):
        """Total must be the same regardless of the page size requested
        — proving it is computed from a COUNT, not from the page."""
        async def fake_lwp(status_filter=None, limit=200, offset=0):
            return [{"id": i} for i in range(limit)]

        async def fake_count(status):
            return 5000

        # list_with_parts_paged calls list_with_parts and count_by_status
        # internally; patch the matcher module's names.
        with patch.object(matcher_mod, "list_with_parts", side_effect=fake_lwp), \
             patch.object(matcher_mod, "count_by_status", side_effect=fake_count):
            for size in (100, 500, 2000):
                r = self.client.get(f"/api/defectives/_/ready?page_size={size}")
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(r.json()["total"], 5000)

    def test_count_by_status_function_signature(self):
        """Sanity-check the helper itself: it returns an int and runs
        a COUNT(*) on the connection — guarding against a future
        refactor that accidentally materialises rows."""
        import inspect
        from app.matcher import count_by_status
        src = inspect.getsource(count_by_status)
        # Must contain a SELECT COUNT.
        self.assertIn("SELECT COUNT", src)
        # Must NOT fetch the rows of defective_items.
        self.assertNotIn("SELECT * FROM defective_items", src)
        self.assertNotIn("FROM defective_items WHERE", src.replace("SELECT COUNT(*)::int FROM defective_items WHERE", ""))
        # And signature: takes a single status string.
        sig = inspect.signature(count_by_status)
        self.assertEqual(list(sig.parameters.keys()), ["status"])


# ---------------------------------------------------------------------------
# /api/defectives/_/count scope & behaviour — still correct after the bump.
# ---------------------------------------------------------------------------

class CountEndpointStillAccurateTests(unittest.TestCase):
    """The /_/count endpoint is the source of truth for accurate totals
    and must remain so after the 200k raise. The endpoint ENABLES the
    pagination UIs to show accurate ``X / Y`` counts without ever
    fetching a 200k-row page."""

    def setUp(self):
        self.app, self.user = _build_app()
        self.conn = _FakeConn()
        self._pool_patch = patch.object(defectives_mod, "pool", lambda: make_pool_with(self.conn))
        self._pool_patch.start()
        self.client = TestClient(self.app)

    def tearDown(self):
        self._pool_patch.stop()

    def test_count_endpoint_handles_large_return_value(self):
        """Just because the ceiling is 200k doesn't mean the count is
        capped — ``/_/count`` returns the true DB count regardless."""
        async def fake_count(status):
            return 200_000 if status == "READY" else 0

        with patch.object(defectives_mod, "count_by_status", side_effect=fake_count):
            r = self.client.get("/api/defectives/_/count?status=READY")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["total"], 200_000)

    def test_count_endpoint_returns_all_three(self):
        """No-param call returns dict-of-totals — used by the tab
        badges so they reflect the entire database, not the page slice."""
        seen = {"calls": []}

        async def fake_count(status):
            seen["calls"].append(status)
            return {"PENDING": 10, "READY": 20, "COMPLETED": 30}[status]

        with patch.object(defectives_mod, "count_by_status", side_effect=fake_count):
            r = self.client.get("/api/defectives/_/count")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["totals"], {"PENDING": 10, "READY": 20, "COMPLETED": 30})
        # All three statuses must be queried regardless of size.
        self.assertEqual(set(seen["calls"]), {"PENDING", "READY", "COMPLETED"})


# ---------------------------------------------------------------------------
# Front-end independence: general list is the bulk-fetch path,
# READY/PENDING tabs never use the 200k cap.
# ---------------------------------------------------------------------------

class FrontendCapPathTests(unittest.TestCase):
    """The front-end treats the general list endpoint as the
    bulk-fetch path (history tab uses limit=200000). The READY/PENDING
    tabs use the paginated endpoints with the whitelist. This module
    pins that the front-end never accidentally sends the 200k cap to
    the paginated endpoints."""

    def _read_html(self):
        from pathlib import Path
        return Path(__file__).resolve().parents[1].joinpath(
            "app", "templates", "index.html"
        ).read_text(encoding="utf-8")

    def test_history_tab_uses_200000_limit(self):
        """The history tab (legacy single-shot fetch) was bumped from
        10000 to 200000 in lock-step with the backend cap raise."""
        html = self._read_html()
        self.assertIn("/api/defectives?status=COMPLETED&limit=200000", html)

    def test_paginated_endpoints_never_receive_200000_limit(self):
        """The READY/PENDING tabs use the per-tab page size from the
        whitelist (100/200/500/2000). They must NEVER send a 200000
        limit — that would defeat the entire pagination refactor."""
        html = self._read_html()
        # Search for any URL containing 200000 alongside /_/ready or /_/pending.
        import re
        for match in re.finditer(r"/api/defectives/_/(?:ready|pending)\?[^'\"]*", html):
            self.assertNotIn(
                "limit=200000", match.group(0),
                f"paginated endpoint must not be called with limit=200000; saw: {match.group(0)}",
            )
            self.assertNotIn(
                "page_size=200000", match.group(0),
                f"paginated endpoint must not be called with page_size=200000; saw: {match.group(0)}",
            )

    def test_pager_page_size_uses_whitelist(self):
        """The pager page-size <select> is rendered from the whitelist
        — never from the 200k cap. Pin the four whitelisted values."""
        html = self._read_html()
        # The allowedPageSizes getter is the source of truth.
        self.assertIn("ALLOWED_PAGE_SIZES: [100, 200, 500, 2000]", html)
        # 200000 must not appear in the page-size selector.
        # (It IS used in the history tab fetch, but that's a separate URL.)
        for line in html.splitlines():
            if "pager-size" in line or "allowedPageSizes" in line or "ALLOWED_PAGE_SIZES" in line:
                self.assertNotIn("200000", line, f"pager must not show 200000: {line!r}")


if __name__ == "__main__":
    unittest.main()