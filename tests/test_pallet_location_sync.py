"""Pallet location sync updates active tickets atomically, not history."""
from contextlib import asynccontextmanager
from datetime import date
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import current_user
from app.routers import defectives


class Conn:
    def __init__(self, status="READY"):
        self.fetchrow = AsyncMock(side_effect=[
            {"status": status, "pallet_no": "8813"},
            {"id": 10, "business_date": date(2026, 10, 21), "pallet_no": "8813",
             "product_name": "产品", "location": "H6-1", "sku": "SKU-1", "qty": 1, "status": status},
        ])
        self.fetch = AsyncMock(return_value=[{"id": 11}, {"id": 12}])
        self.execute = AsyncMock()

    @asynccontextmanager
    async def transaction(self):
        yield self


class Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def client(role):
    app = FastAPI()
    app.include_router(defectives.router)
    app.dependency_overrides[current_user] = lambda: {"id": 1, "role": role}
    return TestClient(app)


def test_sync_location_updates_only_same_pallet_active_rows_and_audits():
    conn = Conn()
    with patch.object(defectives, "pool", return_value=Pool(conn)), patch(
        "app.matcher.evaluate_status", new_callable=AsyncMock, return_value="READY"
    ):
        response = client("returns").patch(
            "/api/defectives/10", json={"location": "H6-1", "sync_location": True}
        )
    assert response.status_code == 200, response.text
    assert response.json()["synced_count"] == 3
    sql, args, _ = conn.fetch.mock_calls[0]
    assert "pallet_no=$2" in args[0]
    assert "status IN ('READY', 'PENDING')" in args[0]
    assert args[1:] == ("H6-1", "8813", 10)
    assert '"sync_pallet_location": true' in conn.execute.call_args.args[3]


def test_repair_cannot_sync_location():
    response = client("repair").patch(
        "/api/defectives/10", json={"location": "H6-1", "sync_location": True}
    )
    assert response.status_code == 403


def test_completed_cannot_sync_pallet_location():
    conn = Conn(status="COMPLETED")
    with patch.object(defectives, "pool", return_value=Pool(conn)):
        response = client("admin").patch(
            "/api/defectives/10", json={"location": "H6-1", "sync_location": True}
        )
    assert response.status_code == 409
    conn.fetch.assert_not_awaited()
