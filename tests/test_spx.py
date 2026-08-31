import asyncio
import hashlib
import inspect
import io
import json
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import openpyxl

from app.routers import spx


class SpxParserTests(unittest.TestCase):
    def _workbook(self, rows):
        wb = openpyxl.Workbook()
        ws = wb.active
        for row in rows:
            ws.append(row)
        stream = io.BytesIO()
        wb.save(stream)
        wb.close()
        return stream.getvalue()

    def test_parser_finds_header_and_reads_rows_before_closing_workbook(self):
        raw = self._workbook([
            ["SPX export"],
            ["Tracking No.", "Create Time", "Item in Parcel"],
            ["SPX001", "2026-09-01 05:00:00", "ABC-001*2"],
        ])
        self.assertEqual(
            spx.parse_spx_xlsx(raw),
            [{
                "tracking_no": "SPX001",
                "create_time": "2026-09-01 05:00:00",
                "items": [("ABC-001", 2, "")],
            }],
        )

    def test_parser_rejects_workbook_without_required_columns(self):
        raw = self._workbook([["wrong", "columns"], ["a", "b"]])
        with self.assertRaisesRegex(ValueError, "required SPX columns"):
            spx.parse_spx_xlsx(raw)

    def test_decode_items_json_accepts_asyncpg_string(self):
        self.assertEqual(
            spx.decode_items_json('[{"sku":"ABC","qty":2}]'),
            [{"sku": "ABC", "qty": 2}],
        )

    def test_parse_common_spx_day_first_timestamp_as_malaysia_time(self):
        parsed = spx.parse_create_time("01/09/2026 05:20:30")
        self.assertEqual(parsed, datetime(2026, 9, 1, 5, 20, 30, tzinfo=ZoneInfo("Asia/Kuala_Lumpur")))

    def test_invalid_create_time_returns_none_for_uploaded_at_fallback(self):
        self.assertIsNone(spx.parse_create_time("not-a-date"))


class FakeConnection:
    def __init__(self, rows):
        self.rows = list(rows)
        self.queries = []

    async def fetchrow(self, query, sku):
        self.queries.append(sku)
        return self.rows.pop(0) if self.rows else None


class SpxLocationTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_location_is_awaited_and_returned(self):
        conn = FakeConnection([{"location": "A-01"}])
        self.assertEqual(await spx.resolve_location(conn, "ABC-001"), "A-01")
        self.assertEqual(conn.queries, ["ABC-001"])

    async def test_base_sku_fallback(self):
        conn = FakeConnection([None, {"location": "B-02"}])
        self.assertEqual(await spx.resolve_location(conn, "ABC-001"), "B-02")
        self.assertEqual(conn.queries, ["ABC-001", "ABC"])


class SpxContractTests(unittest.TestCase):
    def test_lookup_route_matches_visible_roles(self):
        source = inspect.getsource(spx.lookup_tracking)
        self.assertNotIn("returns", source)

    def test_upload_route_allows_admin_and_returns(self):
        source = inspect.getsource(spx.upload_spx)
        self.assertIn('require_role("admin", "returns")', source)

    def test_jsonb_payload_is_serialized(self):
        source = Path(spx.__file__).read_text(encoding="utf-8")
        self.assertIn("json.dumps(items_json, ensure_ascii=False)", source)

    def test_all_database_routes_initialize_the_spx_table(self):
        self.assertIn("await ensure_spx_table()", inspect.getsource(spx.upload_spx))
        self.assertIn("await ensure_spx_table()", inspect.getsource(spx.lookup_tracking))
        self.assertIn("await ensure_spx_table()", inspect.getsource(spx.pick_list))

    def test_lookup_normalizes_tracking_and_decodes_jsonb(self):
        source = inspect.getsource(spx.lookup_tracking)
        self.assertIn("UPPER(TRIM(tracking_no))", source)
        self.assertIn("UPPER(TRIM($1))", source)
        self.assertIn("decode_items_json", source)

    def test_pick_list_filters_the_uploaded_batch_and_decodes_jsonb(self):
        source = inspect.getsource(spx.pick_list)
        self.assertIn("WHERE uploaded_at >= $1", source)
        self.assertIn("AND uploaded_at < $2", source)
        self.assertIn("decode_items_json", source)

    def test_ui_contains_buttons_sections_and_http_error_handling(self):
        html = (Path(__file__).parents[1] / "app/templates/index.html").read_text(encoding="utf-8")
        for marker in ("tab==='spx'", "spxView", "发货上传", "运单查询", "拣货单"):
            self.assertIn(marker, html)
        self.assertEqual(html.count("@click=\"goTab('spx')\""), 1)
        self.assertNotIn("goTab('spx-upload')", html)
        self.assertNotIn("goTab('spx-lookup')", html)
        self.assertNotIn("goTab('spx-pick')", html)
        self.assertIn("['admin','repair','returns'].includes(user.role)", html)
        self.assertIn("['admin','returns'].includes(user.role)", html)
        self.assertIn("this.spxUploadResult = r.ok ? data", html)
        self.assertIn("this.spxPickResult = r.ok ? data", html)


if __name__ == "__main__":
    unittest.main()
