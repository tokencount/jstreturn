"""Tests for compute_reservation_plan and the _allocation_reason classifier.

These cover the P0 requirement: every PENDING part must have accurate
Need/Stock/Reserved/Available/Missing numbers that come from the same
global reservation plan that decides READY/PENDING status.
"""
import unittest

from app.matcher import (
    _allocation_reason,
    compute_reservation_plan,
    allocate_by_pallet,
)


def item(item_id, pallet, parts, created_at=None):
    return {
        "id": item_id,
        "pallet_no": pallet,
        "created_at": created_at or "2026-08-04T00:00:00Z",
        "parts": parts,
    }


def part(code, qty):
    return {"part_code": code, "qty": qty}


class ReservationPlanTests(unittest.TestCase):
    def test_ready_ids_match_allocate_by_pallet(self):
        items = [
            item(1, "A", [part("P1", 1)]),
            item(2, "B", [part("P1", 1)]),
        ]
        plan = compute_reservation_plan(items, {"P1": 1})
        self.assertEqual(plan["ready_ids"], allocate_by_pallet(items, {"P1": 1}))

    def test_reserved_counted_only_for_ready_items(self):
        # Both items compete for P1=1; only one becomes READY. The READY one
        # reserves 1, the PENDING one reserves 0.
        items = [
            item(1, "A", [part("P1", 1)]),
            item(2, "B", [part("P1", 1)]),
        ]
        plan = compute_reservation_plan(items, {"P1": 1})
        self.assertEqual(plan["reserved_by_part"], {"P1": 1})

    def test_available_reflects_remaining_stock(self):
        items = [
            item(1, "A", [part("P1", 2)]),  # A: 2x P1 — needs 2
            item(2, "B", [part("P1", 1)]),  # B: 1x P1
        ]
        # With stock=2 only A (needs 2) can be READY. B becomes PENDING.
        # Reserved = 2 (A), available = max(2 - 2, 0) = 0.
        plan = compute_reservation_plan(items, {"P1": 2})
        self.assertEqual(plan["ready_ids"], {1})
        self.assertEqual(plan["reserved_by_part"].get("P1"), 2)
        self.assertEqual(plan["available_by_part"].get("P1"), 0)

    def test_partial_stock_leaves_some_available(self):
        # Stock=3 lets both A (need 2) and B (need 1) become READY. Reserved=3,
        # available = max(3 - 3, 0) = 0; if a 3rd PENDING item showed up it
        # would see available = 0 even though some stock existed.
        items = [
            item(1, "A", [part("P1", 2)]),
            item(2, "B", [part("P1", 1)]),
            item(3, "C", [part("P1", 1)]),  # PENDING, would see available=0
        ]
        plan = compute_reservation_plan(items, {"P1": 3})
        self.assertEqual(plan["ready_ids"], {1, 2})
        self.assertEqual(plan["available_by_part"].get("P1"), 0)

    def test_unstocked_records_pending_shortage(self):
        # PENDING items still need part codes; track how much more stock
        # would be required to cover them.
        items = [
            item(1, "A", [part("P1", 1)]),  # will be READY
            item(2, "B", [part("MISSING", 2)]),  # PENDING, no inventory
        ]
        plan = compute_reservation_plan(items, {"P1": 1})
        # PENDING demand for MISSING is 2; available for MISSING is 0.
        self.assertEqual(plan["unstocked_by_part"].get("MISSING"), 2)

    def test_unstocked_ignores_already_covered_pending(self):
        items = [
            item(1, "A", [part("P1", 1)]),  # READY
            item(2, "B", [part("P1", 1)]),  # PENDING, but stock = 1 reserved = 1
        ]
        plan = compute_reservation_plan(items, {"P1": 1})
        # B's need is 1; available after A's reservation is 0; shortage is 1.
        self.assertEqual(plan["unstocked_by_part"].get("P1"), 1)


class AllocationReasonTests(unittest.TestCase):
    def test_ready_part_has_reserved_reason(self):
        self.assertEqual(_allocation_reason("READY", 1, 5, 1, 4, 0), "已预留")

    def test_completed_part_is_finished(self):
        self.assertEqual(_allocation_reason("COMPLETED", 1, 0, 0, 0, 0), "已维修")

    def test_no_stock_says_no_inventory(self):
        self.assertEqual(_allocation_reason("PENDING", 1, 0, 0, 0, 1), "无库存")

    def test_all_stock_reserved(self):
        # stock=2, reserved=2 → 已被占用
        self.assertEqual(
            _allocation_reason("PENDING", 3, 2, 2, 0, 3),
            "已被其它 READY 单占用",
        )

    def test_partial_reservation(self):
        # stock=2, reserved=1, available=1, need=3
        self.assertEqual(
            _allocation_reason("PENDING", 3, 2, 1, 1, 2),
            "部分被占用，库存不足",
        )

    def test_simple_shortage(self):
        # stock=1, reserved=0, available=1, need=2
        self.assertEqual(
            _allocation_reason("PENDING", 2, 1, 0, 1, 1),
            "库存不足",
        )

    def test_pending_reason_uses_global_ready_reservation(self):
        plan = compute_reservation_plan(
            [
                item(1, "A", [part("P1", 1)]),
                item(2, "B", [part("P1", 1)]),
            ],
            {"P1": 1},
        )
        reserved = plan["reserved_by_part"]["P1"]
        available = plan["available_by_part"]["P1"]
        self.assertEqual(reserved, 1)
        self.assertEqual(available, 0)
        self.assertEqual(
            _allocation_reason("PENDING", 1, 1, reserved, available, 1),
            "已被其它 READY 单占用",
        )


if __name__ == "__main__":
    unittest.main()
