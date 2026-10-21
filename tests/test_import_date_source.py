"""New file imports default to Malaysia's upload date, with file DATE opt-in."""
import asyncio
import io
from contextlib import asynccontextmanager
from datetime import date, datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from fastapi import UploadFile

from app.routers import imports


class Conn:
    def __init__(self):
        self.insert_dates = []

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchval(self, sql, *args):
        self.insert_dates.append(args[0])
        return len(self.insert_dates)

    async def execute(self, sql, *args):
        pass


class Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def run_import(source="import", file_date="2026-01-02"):
    body = ("DATE,PALLET,次品仓位,商品名称,SKU,part_code,part_name,part_quantity\n"
            f"{file_date},A-1,H-1,产品,SKU-1,PART-1,螺丝,1\n")
    file = UploadFile(filename="items.csv", file=io.BytesIO(body.encode()))
    conn = Conn()
    with patch.object(imports, "pool", return_value=Pool(conn)), patch.object(
        imports, "evaluate_status", new_callable=AsyncMock, return_value="PENDING"
    ):
        result = asyncio.run(imports._upload_defectives_impl(file, {"id": 1}, source))
    return result, conn


def test_default_upload_date_ignores_file_date():
    result, conn = run_import()
    assert result["summary"]["succeeded"] == 1
    assert result["summary"]["date_source"] == "import"
    assert conn.insert_dates == [datetime.now(ZoneInfo("Asia/Kuala_Lumpur")).date()]


def test_explicit_file_date_is_preserved():
    result, conn = run_import("file")
    assert result["summary"]["date_source"] == "file"
    assert conn.insert_dates == [date(2026, 1, 2)]


def test_default_upload_does_not_reject_bad_file_date():
    result, conn = run_import("import", "not-a-date")
    assert result["summary"]["succeeded"] == 1
    assert len(conn.insert_dates) == 1
