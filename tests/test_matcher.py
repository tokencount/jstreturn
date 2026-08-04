import unittest
from datetime import datetime, timezone

from app.matcher import allocate_by_pallet


NOW = datetime(2026, 8, 4, tzinfo=timezone.utc)


def item(item_id, pallet, part_code, qty=1):
    return {
        "id": item_id,
        "pallet_no": pallet,
        "created_at": NOW,
        "parts": [{"part_code": part_code, "qty": qty}],
    }


class PalletAllocationTests(unittest.TestCase):
    def test_stock_is_not_counted_twice(self):
        ready = allocate_by_pallet(
            [item(1, "A", "P1"), item(2, "B", "P1")], {"P1": 1}
        )
        self.assertEqual(len(ready), 1)

    def test_higher_repairable_ratio_gets_stock_first(self):
        # A can repair 2/2 (100%); B can repair 1/2 (50%). Both compete for P1.
        items = [
            item(1, "A", "P1"),
            item(2, "A", "P2"),
            item(3, "B", "P1"),
            item(4, "B", "MISSING"),
        ]
        ready = allocate_by_pallet(items, {"P1": 1, "P2": 1})
        self.assertEqual(ready, {1, 2})

    def test_seventy_percent_pallet_beats_fifty_percent_pallet(self):
        items = []
        # A: seven repairable rows and three unavailable rows.
        for item_id in range(1, 8):
            items.append(item(item_id, "A", "SHARED"))
        for item_id in range(8, 11):
            items.append(item(item_id, "A", f"A-MISSING-{item_id}"))
        # B: five repairable rows and five unavailable rows.
        for item_id in range(11, 16):
            items.append(item(item_id, "B", "SHARED"))
        for item_id in range(16, 21):
            items.append(item(item_id, "B", f"B-MISSING-{item_id}"))

        ready = allocate_by_pallet(items, {"SHARED": 7})
        self.assertEqual(ready, set(range(1, 8)))

    def test_no_partial_reservation(self):
        items = [{
            "id": 1,
            "pallet_no": "A",
            "created_at": NOW,
            "parts": [
                {"part_code": "P1", "qty": 1},
                {"part_code": "P2", "qty": 1},
            ],
        }]
        self.assertEqual(allocate_by_pallet(items, {"P1": 1, "P2": 0}), set())

    def test_duplicate_part_rows_are_summed(self):
        items = [{
            "id": 1,
            "pallet_no": "A",
            "created_at": NOW,
            "parts": [
                {"part_code": "P1", "qty": 1},
                {"part_code": "P1", "qty": 1},
            ],
        }]
        self.assertEqual(allocate_by_pallet(items, {"P1": 1}), set())


if __name__ == "__main__":
    unittest.main()
