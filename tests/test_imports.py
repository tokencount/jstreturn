import unittest
import io
from datetime import date

from openpyxl import Workbook

from app.routers.imports import _group_tickets, _split_xlsx


class ImportGroupingTests(unittest.TestCase):
    def test_one_pallet_can_contain_multiple_skus(self):
        rows = [
            {"_line": 2, "business_date": date(2026, 8, 4), "pallet_no": "A", "sku": "SKU-1"},
            {"_line": 3, "business_date": date(2026, 8, 4), "pallet_no": "A", "sku": "SKU-2"},
        ]
        groups = _group_tickets(rows)
        self.assertEqual(len(groups), 2)

    def test_parts_expanded_from_same_source_row_stay_grouped(self):
        row = {"_line": 2, "business_date": date(2026, 8, 4), "pallet_no": "A", "sku": "SKU-1"}
        groups = _group_tickets([row, dict(row)])
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(next(iter(groups.values()))), 2)

    def test_identical_source_rows_remain_separate_tickets(self):
        base = {"business_date": date(2026, 8, 4), "pallet_no": "A", "sku": "SKU-1", "part_code": "P-1"}
        groups = _group_tickets([
            {**base, "_line": 2},
            {**base, "_line": 3},
        ])
        self.assertEqual(len(groups), 2)


class ImportXlsxTests(unittest.TestCase):
    def test_first_sheet_rows_and_excel_dates_are_normalized(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["DATE", "PALLET", "SKU"])
        sheet.append([date(2026, 8, 13), "P-1", "SKU-1"])
        output = io.BytesIO()
        workbook.save(output)
        self.assertEqual(
            list(_split_xlsx(output.getvalue())),
            [["DATE", "PALLET", "SKU"], ["2026-08-13", "P-1", "SKU-1"]],
        )
