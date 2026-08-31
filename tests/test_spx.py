import asyncio
import hashlib
import inspect
import io
import json
import unittest
from pathlib import Path

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

    def test_jsonb_payload_is_serialized(self):
        source = Path(spx.__file__).read_text(encoding="utf-8")
        self.assertIn("json.dumps(items_json, ensure_ascii=False)", source)

    def test_all_database_routes_initialize_the_spx_table(self):
        self.assertIn("await ensure_spx_table()", inspect.getsource(spx.upload_spx))
        self.assertIn("await ensure_spx_table()", inspect.getsource(spx.lookup_tracking))
        self.assertIn("await ensure_spx_table()", inspect.getsource(spx.pick_list))

    def test_ui_contains_buttons_sections_and_http_error_handling(self):
        html = (Path(__file__).parents[1] / "app/templates/index.html").read_text(encoding="utf-8")
        for marker in ("spx-upload", "spx-lookup", "spx-pick", "发货上传", "运单查询", "拣货单"):
            self.assertIn(marker, html)
        self.assertIn("this.spxUploadResult = r.ok ? data", html)
        self.assertIn("this.spxPickResult = r.ok ? data", html)


if __name__ == "__main__":
    unittest.main()
