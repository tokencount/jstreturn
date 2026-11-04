"""Business-date range and cross-date reservation export regressions."""
import io
import json
import unittest
import zipfile
from datetime import date, datetime, timezone
from unittest.mock import patch
from fastapi.testclient import TestClient
from test_purchase_export import _FakeConn, _build_app, _seed_db, make_pool_with
import app.db as db_mod
import app.routers.exports as exports

def item(rid, day, qty=3, status="PENDING", parts=None):
    return {"id": rid, "business_date": date.fromisoformat(day) if day else None,
            "status": status, "pallet_no": str(rid),
            "created_at": datetime(2020, 1, 1, tzinfo=timezone.utc),
            "parts": parts or [{"part_code": "HE-TEST", "qty": qty}]}

class PurchaseRangeTests(unittest.TestCase):
    def setUp(self):
        self.items = [item(1, "2026-08-13"), item(2, "2026-08-14"),
                      item(3, "2026-08-15"), item(4, "2026-08-16"), item(5, None)]

    def request(self, params=None, items=None, inventory=None, endpoint="/api/exports/purchase"):
        app, _ = _build_app()
        self.conn = _seed_db(_FakeConn(), items if items is not None else self.items, inventory or {})
        with patch.object(db_mod, "pool", lambda: make_pool_with(self.conn)):
            return TestClient(app).get(endpoint, params=params or {})

    def summary(self, params):
        r = self.request({**params, "summary_only": "true"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_inclusive_boundaries_ignore_created_at(self):
        body = self.summary({"start_date": "2026-08-14", "end_date": "2026-08-15"})
        self.assertEqual(body["by_code"], {"HE-TEST": 6})
        self.assertEqual(body["start_date"], "2026-08-14")
        self.assertEqual(body["end_date"], "2026-08-15")
        query = self.conn.fetch.call_args_list[0].args[0]
        self.assertIn("'READY'", query)
        self.assertNotIn("business_date =", query)
        self.assertEqual(len(self.conn.fetch.call_args_list[0].args), 1)
        self.conn.execute.assert_not_called()
        self.conn.executemany.assert_not_called()

    def test_single_day_and_legacy_alias_match(self):
        body = self.summary({"start_date": "2026-08-14", "end_date": "2026-08-14"})
        self.assertEqual(body, self.summary({"business_date": "2026-08-14"}))
        self.assertEqual(body["by_code"], {"HE-TEST": 3})

    def test_start_only(self):
        self.assertEqual(self.summary({"start_date": "2026-08-15"})["by_code"], {"HE-TEST": 6})

    def test_end_only(self):
        self.assertEqual(self.summary({"end_date": "2026-08-14"})["by_code"], {"HE-TEST": 6})

    def test_unbounded_and_blank_dates_include_all(self):
        body = self.summary({})
        self.assertEqual(body["by_code"], {"HE-TEST": 15})
        self.assertEqual(body["date_range"], "全部业务日期")
        self.assertEqual(body, self.summary({"start_date": "", "end_date": ""}))

    def test_bad_ranges_rejected_before_db_for_export_and_preview(self):
        invalid = [{"start_date": "2026-08-15", "end_date": "2026-08-14"},
                   {"start_date": "2026-02-30"}, {"end_date": "20260814"},
                   {"start_date": "oops"}, {"start_date": "2026-W01-1"},
                   {"business_date": "2026-08-14", "end_date": "2026-08-15"}]
        for endpoint in ("/api/exports/purchase", "/api/exports/purchase/preview"):
            for params in invalid:
                with self.subTest(endpoint=endpoint, params=params):
                    r = self.request(params, endpoint=endpoint)
                    self.assertEqual(r.status_code, 400, r.text)
                    self.conn.fetch.assert_not_called()
        self.assertIn("开始日期不能晚于结束日期", self.request(invalid[0]).json()["detail"])

    def test_empty_range_zip_friendly_placeholder(self):
        r = self.request({"start_date": "2030-01-01", "end_date": "2030-01-02"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("2030-01-01_to_2030-01-02", r.headers["content-disposition"])
        with zipfile.ZipFile(io.BytesIO(r.content)) as archive:
            self.assertEqual(archive.namelist(), ["empty-2030-01-01_to_2030-01-02.txt"])
            self.assertIn("无采购需求", archive.read(archive.namelist()[0]).decode())
        self.assertEqual(json.loads(r.headers["x-export-summary"])["row_count"], 0)

    def test_preview_and_download_match(self):
        params = {"start_date": "2026-08-14", "end_date": "2026-08-15"}
        preview = self.request(params, endpoint="/api/exports/purchase/preview").json()
        summary = json.loads(self.request(params).headers["x-export-summary"])
        self.assertEqual(preview["by_code"], summary["by_code"])
        self.assertEqual(preview["date_range"], summary["date_range"])

    def test_outside_range_reservations_remain_for_ready_and_pending(self):
        for status in ("READY", "PENDING"):
            with self.subTest(status=status):
                items = [item(1, "2026-08-01", qty=5, status=status),
                         item(2, "2026-08-14", parts=[{"part_code": "HE-TEST", "qty": 3},
                                                     {"part_code": "HU-MISSING", "qty": 1}])]
                r = self.request({"start_date": "2026-08-14", "end_date": "2026-08-14",
                                  "summary_only": "true"}, items, {"HE-TEST": 5})
                self.assertEqual(r.json()["by_code"], {"HE-TEST": 3, "HU-MISSING": 1})
                self.assertEqual(exports.compute_purchase_rows(items, {"HE-TEST": 5}),
                                 exports.compute_purchase_rows(items, {"HE-TEST": 5},
                                                               date(2026,8,14), date(2026,8,14)))

    def test_authentication_still_required(self):
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(exports.router)
        r = TestClient(app).get("/api/exports/purchase", params={"start_date": "2026-08-14"})
        self.assertIn(r.status_code, (401, 403))
