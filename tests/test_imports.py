import unittest
from datetime import date

from app.routers.imports import _group_tickets


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
