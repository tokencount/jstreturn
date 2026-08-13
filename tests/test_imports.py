import unittest
from datetime import date

from app.routers.imports import _group_tickets, _parse_business_date


class ImportGroupingTests(unittest.TestCase):
    def test_one_pallet_can_contain_multiple_skus(self):
        rows = [
            {"business_date": date(2026, 8, 4), "pallet_no": "A", "sku": "SKU-1"},
            {"business_date": date(2026, 8, 4), "pallet_no": "A", "sku": "SKU-2"},
        ]
        groups = _group_tickets(rows)
        self.assertEqual(len(groups), 2)

    def test_part_rows_for_same_ticket_stay_grouped(self):
        row = {"business_date": date(2026, 8, 4), "pallet_no": "A", "sku": "SKU-1"}
        groups = _group_tickets([row, dict(row)])
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(next(iter(groups.values()))), 2)


class ImportDateTests(unittest.TestCase):
    def test_common_excel_display_formats(self):
        expected = date(2026, 8, 13)
        for value in ("2026-08-13", "13/08/2026", "13-08-2026", "13.08.2026", "2026/08/13"):
            with self.subTest(value=value):
                self.assertEqual(_parse_business_date(value), expected)

    def test_ambiguous_dates_are_day_first(self):
        self.assertEqual(_parse_business_date("01/02/2026"), date(2026, 2, 1))

    def test_excel_serial_date(self):
        self.assertEqual(_parse_business_date("46247"), date(2026, 8, 13))

    def test_iso_datetime(self):
        self.assertEqual(_parse_business_date("2026-08-13 09:30:00"), date(2026, 8, 13))

    def test_invalid_date_is_rejected(self):
        with self.assertRaises(ValueError):
            _parse_business_date("not-a-date")
