"""Exercise real resolver SQL, including both route fallback chains.

Warehouse scope comes from the verified Main source import, never an E prefix.
These tests cover quantity eligibility, not hypothetical warehouse metadata.
"""
import sqlite3
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from app.routers import spx


class InventoryConnection:
    def __init__(self, stock, sku="ABC-001"):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE inventory_snapshot(part_code TEXT, on_hand_qty INT, location TEXT, image_url TEXT)")
        self.db.executemany("INSERT INTO inventory_snapshot VALUES(?,?,?,?)", stock)
        self.sku = sku

    async def fetchrow(self, query, *args):
        if "FROM spx_shipments" in query:
            return self.shipment()
        row = self.db.execute(query, {str(i + 1): value for i, value in enumerate(args)}).fetchone()
        return dict(row) if row else None

    async def fetchval(self, query, *args):
        return self.db.execute(query, {str(i + 1): value for i, value in enumerate(args)}).fetchone()[0]

    async def fetch(self, query, *args):
        if "FROM spx_upload_batches" in query:
            return [{"id": "test-batch", "name": "synthetic test"}]
        if "FROM spx_shipments" in query:
            return [self.shipment()]
        raise AssertionError("Unexpected query")

    def shipment(self):
        return {"tracking_no": "SYNTHETIC", "effective_time": None,
                "items_json": [{"sku": self.sku, "qty": 1, "employee_location": "UNTRUSTED-OLD"}]}

    @asynccontextmanager
    async def acquire(self):
        yield self


class PositiveStockTests(unittest.IsolatedAsyncioTestCase):
    async def test_parts_exact_and_base_zero_or_negative_have_no_location(self):
        for qty in (0, -3):
            with self.subTest(qty=qty):
                conn = InventoryConnection([("ABC-001", qty, "OLD-E", "old"), ("ABC", qty, "OLD-A", "old")])
                self.assertIsNone(await spx.resolve_parts_sku_details(conn, "ABC-001"))
                self.assertIsNone(await spx.resolve_all_sku_details(conn, "ABC-001"))
                self.assertIsNone(await spx.resolve_location(conn, "ABC-001"))

    async def test_positive_non_e_exact_keeps_priority(self):
        conn = InventoryConnection([("ABC-001", 2, "A-12", "exact"), ("ABC", 100, "E-99", "base")])
        self.assertEqual(await spx.inventory_match_sku(conn, "ABC-001"), "ABC-001")
        self.assertEqual(await spx.resolve_parts_sku_details(conn, "ABC-001"), {"location": "A-12", "image_url": "exact"})

    async def test_zero_exact_falls_back_to_positive_non_e_base(self):
        conn = InventoryConnection([("ABC-001", 0, "STALE", "old"), ("ABC", 3, "B-02", "base")])
        self.assertEqual(await spx.inventory_match_sku(conn, "ABC-001"), "ABC")
        self.assertEqual((await spx.resolve_parts_sku_details(conn, "ABC-001"))["location"], "B-02")

    async def _routes(self, conn):
        with patch.object(spx, "pool", return_value=conn), patch.object(spx, "ensure_spx_table", new=AsyncMock()), patch.object(spx, "ensure_all_sku_table", new=AsyncMock()), patch.object(spx, "resolve_original_sku_image", new=AsyncMock(return_value="")):
            scan = await spx.lookup_tracking("SYNTHETIC", user={"role": "admin"})
            picking = await spx.pick_list(batch_ids=["test-batch"], user={"role": "admin"})
        return scan.items[0], picking.items[0]

    async def test_scan_and_pick_list_zero_exact_base_never_reuse_old_location(self):
        conn = InventoryConnection([("ABC-001", 0, "OLD-E", "old"), ("ABC", 0, "OLD-A", "old")])
        scan, picking = await self._routes(conn)
        for item in (scan, picking):
            self.assertEqual(item.our_location, "无库存")
            self.assertEqual(item.matched_sku, "ABC-001")
            self.assertEqual(item.employee_location, "UNTRUSTED-OLD")

    async def test_scan_and_pick_list_prefix_replacement_uses_positive_non_e(self):
        conn = InventoryConnection([("HE-THING-001", 0, "STALE", "old"), ("HE-THING", 0, "STALE", "old"), ("THING", 2, "C-03", "new")], sku="HE-THING-001")
        scan, picking = await self._routes(conn)
        for item in (scan, picking):
            self.assertEqual(item.sku, "HE-THING-001")
            self.assertEqual(item.matched_sku, "THING")
            self.assertEqual(item.our_location, "C-03")

    async def test_empty_snapshot_scan_and_pick_list_are_unstocked(self):
        scan, picking = await self._routes(InventoryConnection([]))
        self.assertEqual(scan.our_location, "无库存")
        self.assertEqual(picking.our_location, "无库存")
