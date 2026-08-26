"""Repair simplified view — HTML structure tests.

These tests parse ``app/templates/index.html`` as text and assert that
the structural changes required by the P3 repair-only test plan are in
place:

  * ``<section class="repair-only">`` exists and contains a card list
  * each card carries Pallet / Location / SKU / multiple Part Code rows /
    完成 button
  * the body container switches to ``class="repair-view"`` when the
    logged-in user has role ``repair``
  * the CSS rule chain hides admin-only panes (workbench, batch,
    inventory, users, purchase export) when the container has
    ``repair-view`` class

These tests are read-only (no Alpine, no DB). They exist as a
regression guard so the repair view cannot silently regress.
"""
import re
import unittest
from pathlib import Path

INDEX = Path(__file__).resolve().parents[1] / "app" / "templates" / "index.html"


def _read():
    return INDEX.read_text(encoding="utf-8")


class RepairViewMarkupTests(unittest.TestCase):
    """The repair-only section + card structure must exist."""

    def test_repair_only_section_present(self):
        html = _read()
        self.assertIn("class=\"repair-only\"", html)
        self.assertIn("x-show=\"user && user.role === 'repair' && tab === 'ready'\"", html)

    def test_container_class_includes_repair_view_for_repair_role(self):
        html = _read()
        # The body container uses :class binding so role === 'repair' adds class
        self.assertRegex(
            html,
            r'<div[^>]*x-data[^>]*container[^>]*:class="user\s*&&\s*user\.role\s*===\s*\'repair\'\s*\?\s*\'repair-view\'\s*:\s*\'\'"',
        )

    def test_repair_card_targets_pallet_location_sku(self):
        html = _read()
        section = html.split("class=\"repair-only\"", 1)[1]
        section = section.split("</section>", 1)[0]
        # Pallet appears with rc-pallet class
        self.assertIn("rc-pallet", section)
        # Location label
        self.assertIn("Location", section)
        # SKU label
        self.assertIn("SKU", section)
        # it.sku / it.pallet_no / it.location references
        self.assertIn("it.pallet_no", section)
        self.assertIn("it.sku", section)
        self.assertIn("it.location", section)

    def test_repair_card_part_code_repeats_one_row_per_part(self):
        html = _read()
        section = html.split("class=\"repair-only\"", 1)[1].split("</section>", 1)[0]
        # There's a template x-for over parts (with optional filter on
        # empty part_codes). Either form is acceptable.
        self.assertTrue(
            "x-for=\"p in (it.parts" in section
            or "x-for=\"p in ((it.parts" in section,
            "x-for over it.parts not found in repair view",
        )
        # Each part row shows part_code + qty
        self.assertIn("p.part_code", section)
        self.assertIn("p.need", section)
        # qty is shown explicitly
        self.assertIn("x-text=\"p.need", section)

    def test_repair_card_complete_button(self):
        html = _read()
        section = html.split("class=\"repair-only\"", 1)[1].split("</section>", 1)[0]
        # 完成 button (Chinese) wirings to the complete(id) handler
        self.assertIn("完成", section)
        self.assertIn("complete(it.id)", section)
        # btn-primary modal class
        self.assertIn("btn-primary", section)

    def test_repair_card_does_not_show_date_or_product(self):
        html = _read()
        section = html.split("class=\"repair-only\"", 1)[1].split("</section>", 1)[0]
        # The view intentionally hides Date / Product / stock / reserved /
        # available / missing / reason.
        self.assertNotIn("it.business_date", section)
        self.assertNotIn("it.product_name", section)
        self.assertNotIn("Stock", section)
        self.assertNotIn("Reserved", section)
        self.assertNotIn("Available", section)
        self.assertNotIn("p.stock", section)
        self.assertNotIn("p.reserved", section)
        self.assertNotIn("p.available", section)
        self.assertNotIn("p.missing", section)
        self.assertNotIn("p.reason", section)

    def test_repair_card_no_checkbox_no_bulk_no_edit_no_delete(self):
        html = _read()
        section = html.split("class=\"repair-only\"", 1)[1].split("</section>", 1)[0]
        self.assertNotIn("type=\"checkbox\"", section)
        self.assertNotIn("bulkAction", section)
        self.assertNotIn("openEdit", section)
        self.assertNotIn("confirmDelete", section)
        self.assertNotIn("改", section)  # edit button label
        self.assertNotIn("删", section)  # delete button label

    def test_empty_state_when_no_ready_items(self):
        html = _read()
        section = html.split("class=\"repair-only\"", 1)[1].split("</section>", 1)[0]
        self.assertIn("repair-empty", section)
        self.assertIn("暂无 READY", section)


class RepairViewCssTests(unittest.TestCase):
    """CSS rule chain must hide admin panels under .repair-view."""

    def test_repair_view_hides_workbench_and_mobile_list(self):
        html = _read()
        # The CSS rules use multi-selector chains. We scan the source for
        # every `.repair-view .<class>` selector and check whether any rule
        # body containing that selector also sets `display: none !important`.
        # The selectors list extracts child classes from the multi-selector
        # rule ``.repair-view .A, .repair-view .B, ... { ... } ``.
        # Strategy: find an opening selector ".repair-view", then collect
        # every child class selector up to the opening brace, then check
        # the block body.
        found_hides = set()
        i = 0
        while True:
            m = re.search(r"\.repair-view\b", html[i:])
            if not m:
                break
            start = i + m.end()
            j = start
            # Walk through selectors until the opening brace
            while j < len(html) and html[j] != "{":
                j += 1
            if j >= len(html):
                break
            selector_chunk = html[start:j]
            # Extract every `.childclass` occurrence
            classes = re.findall(r"\.([\w-]+)", selector_chunk)
            # Find the matching closing brace
            depth = 1
            j += 1
            while j < len(html) and depth > 0:
                if html[j] == "{":
                    depth += 1
                elif html[j] == "}":
                    depth -= 1
                j += 1
            block = html[start + 1:j - 1]
            if "display: none" in block and "!important" in block:
                for cls in classes:
                    found_hides.add(cls)
            i = j
        # Each of these admin panes must be hidden under .repair-view
        for sel in (
            "workbench-summary",
            "workbench-wrap",
            "mobile-list",
            "mobile-filter-bar",
            "modal",
            "inv-stats",
            "drop-zone",
            "users-panel",
            "purchase-panel",
        ):
            self.assertIn(sel, found_hides, f"missing hide rule for .{sel}")

    def test_repair_view_hides_sections_except_repair_only(self):
        html = _read()
        # The catch-all rule must preserve the READY repair section and the
        # read-only inventory-query section.
        self.assertIn("section:not(.repair-only):not(.repair-allow)", html)

    def test_repair_view_hides_nav_buttons(self):
        html = _read()
        # The nav-btn :not(.repair-keep) selector is mentioned
        self.assertIn("nav-btn:not(.repair-keep)", html)

    def test_repair_view_keeps_ready_and_inventory_navigation(self):
        html = _read()
        self.assertRegex(html, r"goTab\('ready'\)[^>]+repair-keep")
        self.assertRegex(html, r"goTab\('inventory'\)[^>]+repair-keep")
        self.assertIn("class=\"repair-allow\"", html)

    def test_repair_defaults_to_ready_and_loads_ready_items(self):
        html = _read()
        self.assertIn("this.user.role === 'repair'", html)
        self.assertIn("this.tab = 'ready'", html)
        self.assertIn("await this.loadList('READY')", html)

    def test_480px_media_block_compacts_repair_view(self):
        html = _read()
        m = re.search(r"@media\s*\(max-width:\s*480px\)\s*\{", html)
        self.assertIsNotNone(m, "missing @media (max-width: 480px) block")
        start = m.end()
        depth = 1
        i = start
        while i < len(html) and depth > 0:
            c = html[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            i += 1
        block = html[start:i - 1]
        # The repair-view compact rules must be present
        self.assertIn(".repair-view .container", block)
        self.assertIn("padding", block)
        self.assertIn(".repair-card", block)


class RepairViewNoHorizontalScrollTests(unittest.TestCase):
    """CSS sanity: repair-view CSS must constrain container width."""

    def test_repair_view_container_max_width_defined(self):
        html = _read()
        # The non-media rule for .repair-view .container must set max-width
        m = re.search(r"\.repair-view\s+\.container\s*\{[^}]*max-width", html)
        self.assertIsNotNone(m, "missing .repair-view .container max-width rule")


class RepairViewAuthFixtureTests(unittest.TestCase):
    """Fixture support for the repair role in dev (?fixture=1&role=repair)."""

    def test_fixture_role_query_param_supported(self):
        html = _read()
        # The fixture JS block reads role= from query params
        self.assertIn("_role", html)
        self.assertIn("'repair'", html)


if __name__ == "__main__":
    unittest.main()
