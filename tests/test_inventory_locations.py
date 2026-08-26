"""Inventory upload + per-location breakdown tests.

These tests pin the new behaviour:

  * Upload accepts the Chinese/English aliases the daily-refresh runner
    and human users actually send (SKU / 数量 / 仓位 / part_code /
    on_hand_qty / location / 编码 / 配件编码 / 名称 / 配件名称 / ...).
  * Multiple rows with the same part_code are accepted; the aggregate
    ``inventory_snapshot.on_hand_qty`` is the SUM across rows, and each
    (part_code, location) tuple is stored once in
    ``inventory_locations``.
  * Duplicate (part_code, location) rows in a single upload collapse
    to a single SUM rather than producing two rows.
  * The matcher attaches ``inventory_locations`` per part so the UI can
    show "哪几个仓位有多少" beside the part_code without a second round
    trip.  Backward compat: empty breakdown is fine.
  * Legacy data — single inventory_snapshot row, no inventory_locations
    rows — still feeds the matcher via on_hand_qty and yields an empty
    locations list on each part.
"""
import io
import unittest
import csv
from unittest.mock import AsyncMock, MagicMock, patch

import app.routers.inventory as inventory_mod
import app.matcher as matcher_mod
from app.auth import current_user


# ---------------------------------------------------------------------------
# Mock asyncpg pool + connection
# ---------------------------------------------------------------------------

class _FakeConn:
    def __init__(self):
        self.fetchrow = AsyncMock()
        self.fetch = AsyncMock(return_value=[])
        self.fetchval = AsyncMock(return_value=0)
        # NOTE: do NOT set ``self.execute`` here — the class-level
        # ``execute`` method (defined below) tracks TRUNCATE calls.
        self.executemany = AsyncMock(return_value=None)
        self.truncate_calls: list[str] = []

    def transaction(self):
        outer = self
        class _TxCM:
            async def __aenter__(self_inner):
                return outer
            async def __aexit__(self_inner, *exc):
                return None
        return _TxCM()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def execute(self, query, *args, **kwargs):
        # Track TRUNCATE so we can verify full-replace semantics wipe
        # both tables atomically.
        if "TRUNCATE" in (query or "").upper():
            self.truncate_calls.append(query)
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


def _build_app(role="admin"):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(inventory_mod.router)
    fake_user = {"id": 1, "role": role, "active": True, "name": "test"}
    app.dependency_overrides[current_user] = lambda: fake_user
    return app, fake_user


def _csv_upload(name, headers, rows):
    body = io.StringIO()
    w = csv.writer(body)
    w.writerow(headers)
    for r in rows:
        w.writerow(r)
    raw = body.getvalue().encode("utf-8-sig")
    return (name, raw, "text/csv")


# ---------------------------------------------------------------------------
# Upload alias + multi-location tests
# ---------------------------------------------------------------------------

class InventoryUploadAliasesTests(unittest.TestCase):
    """Verify the new alias list accepts SKU/数量/仓位 and stores multi-location rows."""

    def setUp(self):
        self.app, self.user = _build_app("admin")
        self.conn = _FakeConn()
        # Capture every executemany call so we can inspect rows the test
        # wrote to inventory_snapshot and inventory_locations.
        self.snapshot_rows: list[tuple] = []
        self.location_rows: list[tuple] = []

        orig_executemany = self.conn.executemany

        async def capturing_executemany(query, params):
            if "INSERT INTO inventory_snapshot" in query:
                self.snapshot_rows.extend(list(params))
            elif "INSERT INTO inventory_locations" in query:
                self.location_rows.extend(list(params))
            return await orig_executemany(query, params)

        self.conn.executemany = capturing_executemany
        self._pool_patch = patch.object(inventory_mod, "pool", lambda: make_pool_with(self.conn))
        self._pool_patch.start()
        # Avoid the real matcher re-evaluation: it would need a real DB.
        self._matcher_patch = patch.object(
            inventory_mod, "reevaluate_all_pending_ready",
            AsyncMock(return_value={"to_pending": 0, "to_ready": 0, "no_change": 0}),
        )
        self._matcher_patch.start()
        from fastapi.testclient import TestClient
        self.client = TestClient(self.app)

    def tearDown(self):
        self._matcher_patch.stop()
        self._pool_patch.stop()

    def _post(self, name, headers, rows):
        fname, raw, ctype = _csv_upload(name, headers, rows)
        return self.client.post(
            "/api/inventory/upload",
            files={"file": (fname, raw, ctype)},
        )

    def test_accepts_chinese_aliases_sku_quantity_location(self):
        # Daily-refresh runner emits SKU/数量/仓位 (the user-required aliases).
        r = self._post(
            "inv.csv",
            ["SKU", "数量", "仓位", "名称"],
            [
                ["HS-A", 3, "A-01-01", "套筒"],
                ["HS-B", 1, "B-02-03", "扣子"],
            ],
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["inserted"], 2)
        # Each row landed in inventory_snapshot and inventory_locations.
        snapshot_codes = sorted(row[0] for row in self.snapshot_rows)
        self.assertEqual(snapshot_codes, ["HS-A", "HS-B"])
        location_pairs = sorted((row[0], row[1], row[2]) for row in self.location_rows)
        self.assertEqual(
            location_pairs,
            [("HS-A", "A-01-01", 3), ("HS-B", "B-02-03", 1)],
        )

    def test_accepts_english_aliases(self):
        # Mixed exporters send part_code/on_hand_qty/location.
        r = self._post(
            "inv.csv",
            ["part_code", "on_hand_qty", "location"],
            [["HS-X", 5, "R-1-1"]],
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self.snapshot_rows), 1)
        # Compare element-by-element (snapshot_rows[0] is a tuple, the
        # expected value is a list).
        self.assertEqual(self.snapshot_rows[0][:3], ("HS-X", None, 5))
        self.assertEqual(self.location_rows, [("HS-X", "R-1-1", 5)])

    def test_accepts_image_url_and_chinese_image_alias(self):
        image_url = "https://example.com/part.jpg"
        r = self._post(
            "inv.csv",
            ["SKU", "数量", "仓位", "图片"],
            [["HS-IMG", 2, "A-01", image_url]],
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.snapshot_rows[0], ("HS-IMG", None, 2, "A-01", image_url))

    def test_multiple_locations_same_part_code_aggregate(self):
        # Same part_code in multiple rows, each contributing its own
        # (location, qty). The aggregate snapshot row must SUM, while
        # the breakdown must keep every location separate.
        r = self._post(
            "inv.csv",
            ["part_code", "on_hand_qty", "location"],
            [
                ["HS-A", 2, "A-01-01"],
                ["HS-A", 5, "A-01-02"],
                ["HS-A", 1, "B-99-99"],
            ],
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # Single aggregate row (one per part_code).
        self.assertEqual(body["inserted"], 1)
        self.assertEqual(len(self.snapshot_rows), 1)
        self.assertEqual(self.snapshot_rows[0][0], "HS-A")
        self.assertEqual(self.snapshot_rows[0][2], 8)  # SUM
        # Three breakdown rows — one per (part_code, location).
        self.assertEqual(body["locations"], 3)
        pairs = sorted((r[0], r[1], r[2]) for r in self.location_rows)
        self.assertEqual(
            pairs,
            [("HS-A", "A-01-01", 2), ("HS-A", "A-01-02", 5), ("HS-A", "B-99-99", 1)],
        )

    def test_duplicate_location_rows_collapse_to_sum(self):
        # The same (part_code, location) appearing twice in a CSV should
        # collapse to one SUM (so re-uploading a stale export on top of
        # a fresh one doesn't poison the breakdown).
        r = self._post(
            "inv.csv",
            ["part_code", "on_hand_qty", "location"],
            [
                ["HS-A", 2, "A-01-01"],
                ["HS-A", 3, "A-01-01"],
            ],
        )
        self.assertEqual(r.status_code, 200, r.text)
        pairs = sorted((row[0], row[1], row[2]) for row in self.location_rows)
        self.assertEqual(pairs, [("HS-A", "A-01-01", 5)])

    def test_full_replace_truncates_both_tables(self):
        r = self._post(
            "inv.csv",
            ["part_code", "on_hand_qty", "location"],
            [["HS-A", 1, "A-01-01"]],
        )
        self.assertEqual(r.status_code, 200, r.text)
        # TRUNCATE must hit both tables so stale rows don't accumulate.
        self.assertEqual(len(self.conn.truncate_calls), 1)
        joined = self.conn.truncate_calls[0].lower()
        self.assertIn("inventory_snapshot", joined)
        self.assertIn("inventory_locations", joined)

    def test_missing_required_columns_rejected(self):
        # ``quantity`` column missing entirely → 400 with a helpful error.
        r = self._post(
            "inv.csv",
            ["part_code", "location"],
            [["HS-A", "A-01-01"]],
        )
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("qty", r.json()["detail"])

    def test_empty_location_treated_as_default_bucket(self):
        # An empty / whitespace-only location cell still lands in the
        # breakdown (one row with location="") so the snapshot is not
        # silently dropped.
        r = self._post(
            "inv.csv",
            ["part_code", "on_hand_qty", "location"],
            [["HS-A", 7, ""]],
        )
        self.assertEqual(r.status_code, 200, r.text)
        pairs = [(r[0], r[1], r[2]) for r in self.location_rows]
        self.assertEqual(pairs, [("HS-A", "", 7)])

    def test_negative_quantity_skipped(self):
        # Negative quantities are nonsensical for a stock snapshot; they
        # are skipped (not subtracted from the aggregate).
        r = self._post(
            "inv.csv",
            ["part_code", "on_hand_qty", "location"],
            [
                ["HS-A", 5, "A-01-01"],
                ["HS-A", -1, "A-01-01"],
            ],
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self.snapshot_rows), 1)
        self.assertEqual(self.snapshot_rows[0][2], 5)
        pairs = [(r[0], r[1], r[2]) for r in self.location_rows]
        self.assertEqual(pairs, [("HS-A", "A-01-01", 5)])


# ---------------------------------------------------------------------------
# Matcher-side: inventory_locations attached per part, backward compat
# ---------------------------------------------------------------------------

class InventoryLocationsHelperTests(unittest.TestCase):
    """Cover _fetch_locations_for_codes + matcher list_with_parts annotation."""

    def setUp(self):
        self.conn = _FakeConn()
        self._pool_patch = patch.object(matcher_mod, "pool", lambda: make_pool_with(self.conn))
        self._pool_patch.start()

    def tearDown(self):
        self._pool_patch.stop()

    def test_fetch_locations_returns_qty_sorted_desc(self):
        # The SQL query orders by qty DESC, then location ASC. The mocked
        # fetch does not run SQL, so we pre-sort the rows in the expected
        # order to mirror the production ORDER BY.
        rows = [
            {"part_code": "HS-A", "location": "A-2", "qty": 5},
            {"part_code": "HS-A", "location": "A-1", "qty": 2},
            {"part_code": "HS-B", "location": "B-1", "qty": 1},
        ]
        self.conn.fetch = AsyncMock(return_value=rows)
        import asyncio
        out = asyncio.run(matcher_mod._fetch_locations_for_codes({"HS-A", "HS-B", "HS-NONE"}))
        # The two known codes are returned; HS-NONE is omitted.
        self.assertEqual(set(out.keys()), {"HS-A", "HS-B"})
        # HS-A is ordered by qty DESC: 5 then 2.
        self.assertEqual(out["HS-A"], [
            {"location": "A-2", "qty": 5},
            {"location": "A-1", "qty": 2},
        ])
        self.assertEqual(out["HS-B"], [{"location": "B-1", "qty": 1}])

    def test_fetch_locations_empty_codes_returns_empty_dict(self):
        import asyncio
        out = asyncio.run(matcher_mod._fetch_locations_for_codes(set()))
        self.assertEqual(out, {})

    def test_matchers_inventory_locations_attach(self):
        # list_with_parts annotates each part with ``inventory_locations``
        # so the UI can render the chips without a second round trip.
        from datetime import datetime, timezone

        item_row = {
            "id": 1,
            "business_date": datetime(2026, 8, 14).date(),
            "pallet_no": "P-1",
            "product_name": "测试商品",
            "sku": "SKU-1",
            "qty": 1,
            "status": "READY",
            "location": None,
            "created_at": datetime(2026, 8, 14, tzinfo=timezone.utc),
            "completed_at": None,
            "created_by_name": "tester",
            "completed_by_name": None,
            "parts": '[{"part_code": "HS-A", "part_name": null, "need": 1, "have": 5, "short": 0}]',
        }
        location_rows = [
            {"part_code": "HS-A", "location": "A-01-01", "qty": 5},
            {"part_code": "HS-A", "location": "A-02-03", "qty": 2},
        ]
        # list_with_parts calls fetch at least twice — once for items,
        # once for inventory_locations. Distinguish by query text so
        # extra fetches (e.g. per-status re-evaluation) don't get fed
        # the wrong mock data.
        from unittest.mock import MagicMock as _M
        calls = []

        async def fake_fetch(query, *args, **kwargs):
            calls.append(query)
            if "parts_agg" in query:
                return [item_row]
            if "inventory_locations" in query:
                return location_rows
            return []

        self.conn.fetch = fake_fetch

        import asyncio
        items = asyncio.run(matcher_mod.list_with_parts(status_filter=None, limit=10, offset=0))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["parts"][0]["inventory_locations"], [
            {"location": "A-01-01", "qty": 5},
            {"location": "A-02-03", "qty": 2},
        ])

    def test_backward_compat_empty_breakdown_still_works(self):
        # Old data: only inventory_snapshot rows; no inventory_locations.
        # Matcher must still work and produce an empty ``inventory_locations``
        # list per part (UI hides the chips when empty).
        from datetime import datetime, timezone

        item_row = {
            "id": 1,
            "business_date": datetime(2026, 8, 14).date(),
            "pallet_no": "P-1",
            "product_name": "测试商品",
            "sku": "SKU-1",
            "qty": 1,
            "status": "PENDING",
            "location": None,
            "created_at": datetime(2026, 8, 14, tzinfo=timezone.utc),
            "completed_at": None,
            "created_by_name": "tester",
            "completed_by_name": None,
            "parts": '[{"part_code": "HS-A", "part_name": null, "need": 1, "have": 1, "short": 0}]',
        }

        async def fake_fetch(query, *args, **kwargs):
            if "parts_agg" in query:
                return [item_row]
            if "inventory_locations" in query:
                return []  # legacy data — empty breakdown
            return []

        self.conn.fetch = fake_fetch

        import asyncio
        items = asyncio.run(matcher_mod.list_with_parts(status_filter=None, limit=10, offset=0))
        self.assertEqual(items[0]["parts"][0]["inventory_locations"], [])


# ---------------------------------------------------------------------------
# Preview endpoint test
# ---------------------------------------------------------------------------

class InventoryPreviewTests(unittest.TestCase):
    """The /api/inventory/preview/{part_code} endpoint returns the
    location breakdown so repair users can see 仓位 without an extra
    round trip."""

    def setUp(self):
        self.app, self.user = _build_app("admin")
        self.conn = _FakeConn()
        self._pool_patch = patch.object(inventory_mod, "pool", lambda: make_pool_with(self.conn))
        self._pool_patch.start()
        from fastapi.testclient import TestClient
        self.client = TestClient(self.app)

    def tearDown(self):
        self._pool_patch.stop()

    def test_preview_returns_locations_breakdown(self):
        async def fake_fetchrow(query, *args, **kwargs):
            return {
                "part_code": "HS-A",
                "part_name": "套筒",
                "image_url": "https://example.com/hs-a.jpg",
                "on_hand_qty": 7,
                "location": "A-01-01",
                "updated_at": None,
            }
        self.conn.fetchrow = AsyncMock(side_effect=fake_fetchrow)
        self.conn.fetch = AsyncMock(return_value=[
            {"location": "A-01-01", "qty": 5},
            {"location": "A-02-03", "qty": 2},
        ])
        r = self.client.get("/api/inventory/preview/HS-A")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["part_code"], "HS-A")
        self.assertEqual(body["on_hand_qty"], 7)
        self.assertEqual(body["image_url"], "https://example.com/hs-a.jpg")
        self.assertEqual(body["inventory_locations"], [
            {"location": "A-01-01", "qty": 5},
            {"location": "A-02-03", "qty": 2},
        ])

    def test_preview_unknown_part_returns_404(self):
        self.conn.fetchrow = AsyncMock(return_value=None)
        r = self.client.get("/api/inventory/preview/UNKNOWN")
        self.assertEqual(r.status_code, 404)

    def test_image_proxy_serves_only_jst_image_content(self):
        image_url = (
            "https://jst-yikan-picspace.oss-ap-southeast-1.aliyuncs.com/"
            "yikan/test.png"
        )
        self.conn.fetchval = AsyncMock(return_value=image_url)

        class FakeResponse:
            status_code = 200
            headers = {"content-type": "image/png"}
            content = b"png-bytes"

        client = AsyncMock()
        client.get.return_value = FakeResponse()
        client.__aenter__.return_value = client
        client.__aexit__.return_value = None
        with patch.object(inventory_mod.httpx, "AsyncClient", return_value=client):
            r = self.client.get("/api/inventory/image/HS-A")

        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers["content-type"], "image/png")
        self.assertEqual(r.content, b"png-bytes")
        self.assertEqual(r.headers["cache-control"], "public, max-age=86400")

    def test_image_proxy_rejects_untrusted_host(self):
        self.conn.fetchval = AsyncMock(return_value="https://example.com/part.png")
        r = self.client.get("/api/inventory/image/HS-A")
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
