"""Whole-pallet pick sheet keeps READY-only quantities and all ticket rows."""
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import current_user
from app.routers import pallet_pick


class FakeConn:
    def __init__(self, rows, locations):
        self.rows = rows
        self.locations = locations
        self.queries = []

    @asynccontextmanager
    async def transaction(self, **kwargs):
        assert kwargs == {"isolation": "repeatable_read", "readonly": True}
        yield self

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        return self.locations if "FROM inventory_locations" in sql else self.rows


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def client_for(conn):
    app = FastAPI()
    app.include_router(pallet_pick.router)
    app.dependency_overrides[current_user] = lambda: {"id": 1, "role": "repair"}
    return TestClient(app)


def row(id, status, code, qty, part_id):
    return {
        "id": id, "business_date": date(2026, 10, 20), "pallet_no": "6601",
        "location": "R-1", "sku": "PRODUCT-A", "product_name": "柜子",
        "qty": 1, "status": status, "part_id": part_id,
        "part_code": code, "part_name": "螺丝", "part_qty": qty,
        "stock_part_name": "螺丝", "stock_location": "66-A-1",
        "stock_qty": 20,
        "stock_updated_at": datetime(2026, 10, 20, tzinfo=timezone.utc),
    }


def test_whole_pallet_ready_pick_aggregates_and_excludes_pending():
    conn = FakeConn(
        [row(10, "READY", "P-001", 2, 1), row(11, "READY", "P-001", 3, 2),
         row(12, "PENDING", "P-001", 100, 3), row(13, "COMPLETED", "P-001", 1, 4)],
        [{"part_code": "P-001", "location": "66-A-1", "qty": 20}],
    )
    with patch.object(pallet_pick, "pool", return_value=FakePool(conn)), patch.object(
        pallet_pick, "_rebuild_plan_with_breakdown", new_callable=AsyncMock
    ) as rebuild:
        response = client_for(conn).get("/api/pallet-pick/6601")
    assert response.status_code == 200
    data = response.json()
    assert data["counts"] == {"READY": 2, "PENDING": 1, "COMPLETED": 1}
    assert [it["id"] for it in data["ready_items"]] == [10, 11]
    assert [it["id"] for it in data["active_items"]] == [10, 11, 12]
    assert data["active_items"][2]["status"] == "PENDING"
    assert data["pallet_locations"] == ["R-1"]
    assert data["pick_parts"][0]["qty"] == 5
    assert data["pick_parts"][0]["item_ids"] == [10, 11]
    assert data["pick_parts"][0]["locations"] == [{"location": "66-A-1", "qty": 20}]
    assert conn.queries[0][1] == ("6601",)
    rebuild.assert_awaited_once()


def test_missing_pallet_is_404_and_signature_changes_with_qty():
    conn = FakeConn([], [])
    with patch.object(pallet_pick, "pool", return_value=FakePool(conn)), patch.object(
        pallet_pick, "_rebuild_plan_with_breakdown", new_callable=AsyncMock
    ):
        client = client_for(conn)
        assert client.get("/api/pallet-pick/unknown").status_code == 404
        conn.rows = [row(10, "READY", "P-001", 2, 1)]
        first = client.get("/api/pallet-pick/6601").json()["signature"]
        conn.rows = [row(10, "READY", "P-001", 3, 1)]
        second = client.get("/api/pallet-pick/6601").json()["signature"]
    assert first != second


def test_ranked_pallets_excludes_completed_from_rate():
    conn = FakeConn([{
        "pallet_no": "8813", "ready_count": 6,
        "pending_count": 8, "completed_count": 10,
    }], [])
    with patch.object(pallet_pick, "pool", return_value=FakePool(conn)), patch.object(
        pallet_pick, "_rebuild_plan_with_breakdown", new_callable=AsyncMock
    ) as rebuild:
        response = client_for(conn).get("/api/pallet-pick/_/ranked")
    assert response.status_code == 200
    assert response.json() == {
        "denominator": "READY+PENDING",
        "pallets": [{
            "pallet_no": "8813", "ready_count": 6,
            "pending_count": 8, "completed_count": 10,
            "ready_percent": 42.9,
        }],
    }
    sql = conn.queries[0][0]
    assert "WHERE ready_count > 0" in sql
    assert "ready_count::numeric / (ready_count + pending_count) DESC" in sql
    assert "oldest_date ASC, pallet_no ASC" in sql
    rebuild.assert_awaited_once()
