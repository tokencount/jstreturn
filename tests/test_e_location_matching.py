"""Only E-location stock may make a defective item READY."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import patch

from app import matcher


class Conn:
    def __init__(self, eligible_qty):
        self.eligible_qty = eligible_qty
        self.queries = []
        self.updates = []

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def execute(self, sql, *args):
        self.queries.append(sql)

    async def fetch(self, sql, *args):
        self.queries.append(sql)
        if "FROM defective_items di" in sql:
            return [{
                "id": 1, "pallet_no": "A", "status": "READY",
                "created_at": datetime(2026, 10, 20, tzinfo=timezone.utc),
                "parts": [{"part_code": "PART-1", "qty": 1}],
            }]
        if sql == matcher.ELIGIBLE_INVENTORY_SQL:
            return [{"part_code": "PART-1", "on_hand_qty": self.eligible_qty}]
        raise AssertionError(sql)

    async def executemany(self, sql, rows):
        self.updates.extend(rows)


class Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def test_non_e_only_stock_cannot_keep_item_ready():
    conn = Conn(eligible_qty=0)
    with patch.object(matcher, "pool", return_value=Pool(conn)):
        flips = asyncio.run(matcher.reevaluate_all_pending_ready())
    assert flips["to_pending"] == 1
    assert conn.updates == [("PENDING", 1)]


def test_e_stock_can_make_item_ready():
    conn = Conn(eligible_qty=1)
    with patch.object(matcher, "pool", return_value=Pool(conn)):
        flips = asyncio.run(matcher.reevaluate_all_pending_ready())
    assert flips["no_change"] == 1
    assert conn.updates == []


def test_eligible_stock_query_uses_location_rows_before_snapshot_fallback():
    sql = matcher.ELIGIBLE_INVENTORY_SQL
    assert "FROM inventory_locations" in sql
    assert "LEFT(BTRIM(COALESCE(location, '')), 1)) = 'E'" in sql
    assert "WHEN loc.part_code IS NOT NULL THEN loc.e_qty" in sql
    assert "LEFT(BTRIM(COALESCE(s.location, '')), 1)) = 'E'" in sql
