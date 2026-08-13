"""P2: pure-front-end test for the mobile UI refactor.

This test does NOT exercise Alpine or a real browser — it parses
``app/templates/index.html`` as text and asserts that the structural
changes from the P2 ticket are present (and that the desktop workbench
table is still there untouched). The goal is regression-safety: if a
later rewrite accidentally drops the mobile-card markup or the sticky
filter bar, this test fails.

The test also loads the app module to make sure that we did NOT
introduce a Python-side import failure by accident.
"""
import re
import unittest
from pathlib import Path

INDEX = Path(__file__).resolve().parents[1] / "app" / "templates" / "index.html"


def _read():
    return INDEX.read_text(encoding="utf-8")


class DesktopRegressionTests(unittest.TestCase):
    """Desktop workbench table + sort + filter must still be present."""

    def test_workbench_table_markup_present(self):
        html = _read()
        # The desktop table container
        self.assertIn('class="workbench-wrap desktop-only"', html)
        # The workbench <table> still exists with the same primary header
        self.assertIn('<table class="workbench"', html)
        for label in ("Date", "Pallet", "Location", "Product", "SKU",
                      "Part Code", "Part Name", "Need", "Stock", "Reserved",
                      "Available", "Missing", "Reason", "Status"):
            self.assertIn(label, html,
                          f"header label '{label}' should still be in the page")

    def test_desktop_sort_handlers_present(self):
        html = _read()
        # Each sortable column header still wires a setSort(...) handler
        for col in ("business_date", "pallet_no", "sku"):
            self.assertRegex(
                html,
                r"@click=\"setSort\('" + col + r"'\)\"",
                f"setSort('{col}') handler should still exist for desktop",
            )

    def test_desktop_column_filter_inputs_present(self):
        html = _read()
        # Per-column filter inputs (table row marked .column-filters) still exists
        self.assertIn('class="column-filters"', html)
        for placeholder in ("Date", "Pallet", "Location", "SKU", "Part Code"):
            self.assertIn(f'placeholder="{placeholder}"', html)

    def test_desktop_table_is_hidden_via_workbench_wrap(self):
        """Mobile CSS rule should hide .workbench-wrap on mobile.

        We do a substring-anchored check that the mobile media query
        contains a `.workbench-wrap { display: none !important; }` rule
        (the order of rules inside @media does not matter, so we look
        inside the block).
        """
        html = _read()
        m = re.search(r"@media\s*\(max-width:\s*720px\)\s*\{", html)
        self.assertIsNotNone(m, "mobile @media block missing")
        # Find the matching closing brace by brace-counting.
        start = m.end()
        depth = 1
        i = start
        while i < len(html) and depth > 0:
            c = html[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
            i += 1
        block = html[start:i - 1]
        self.assertIn('.workbench-wrap', block)
        self.assertRegex(block, r"\.workbench-wrap\s*\{\s*display:\s*none\s*!important")


class MobileCardLayoutTests(unittest.TestCase):
    """Mobile card markup + sticky bottom bar."""

    def test_mobile_card_container_present(self):
        html = _read()
        self.assertIn('class="mobile-list"', html)

    def test_card_row_1_has_pallet_sku_status(self):
        html = _read()
        # Strip the markup around mobile-list to make the assertions explicit
        # We just confirm the 3 primary fragments are rendered in the card.
        self.assertIn('class="pallet"', html)
        self.assertIn('class="sku"', html)
        self.assertIn("it.pallet_no", html)
        self.assertIn("it.sku", html)
        # status chip with chip-PENDING/READY/COMPLETED classes
        self.assertIn("chip-PENDING", html)
        self.assertIn("chip-READY", html)
        self.assertIn("chip-COMPLETED", html)

    def test_card_row_2_has_location_and_product(self):
        html = _read()
        self.assertIn('class="card-row-2"', html)
        # The two .ellipsis children render Location + Product
        # We rely on multiple occurrences of "Location" / "Product" inside the
        # mobile list; both labels appear in the column-filter inputs too, so
        # use a more specific anchor.
        mob = html.split('class="mobile-list"', 1)[1]
        self.assertIn("Location", mob)
        self.assertIn("Product", mob)

    def test_parts_toggle_collapsible(self):
        html = _read()
        # The mobile card must use <details class="parts-toggle">
        self.assertIn('<details class="parts-toggle"', html)
        # <summary> contains "配件清单"
        self.assertIn("配件清单", html)
        # Each part row shows part_code / part_name / qty / stock + ✓/✗
        self.assertIn('class="pcode"', html)
        self.assertIn('class="pname"', html)
        self.assertIn('class="pstock"', html)
        self.assertIn('class="pqty"', html)
        self.assertIn('class="pmark', html)

    def test_sticky_bottom_filter_bar_present(self):
        html = _read()
        self.assertIn('class="mobile-filter-bar"', html)
        # Bottom-bar is fixed-position on mobile
        self.assertIn("position: fixed", html)
        self.assertIn("bottom: 0", html)
        # Search input + filter button
        self.assertIn('type="search"', html)
        self.assertIn('placeholder="搜索 Pallet / SKU / Part', html)

    def test_mobile_filter_sheet(self):
        html = _read()
        self.assertIn("mobile-filter-sheet", html)
        # The sheet has at least the standard 6 filter inputs
        for label in ("Pallet", "SKU", "Location", "Product", "Part Code", "Part Name"):
            self.assertIn(f'<label>{label}</label>', html)


class DesignTokensTests(unittest.TestCase):
    """A handful of design-token CSS variables exist (P2 requirement)."""

    def test_radius_scale_tokens(self):
        css = _read()
        for token in ("--radius-xs", "--radius-sm", "--radius-md",
                      "--radius-lg", "--radius-xl", "--radius-pill"):
            self.assertIn(token + ":", css, f"missing radius token {token}")

    def test_font_scale_tokens(self):
        css = _read()
        for token in ("--fs-xs", "--fs-sm", "--fs-base", "--fs-md",
                      "--fs-lg", "--fs-xl"):
            self.assertIn(token + ":", css, f"missing font token {token}")

    def test_existing_status_colors_preserved(self):
        css = _read()
        # Status color CSS variables must remain so we keep brand
        self.assertIn("--brand:", css)
        self.assertIn("--red:", css)
        self.assertIn("--green:", css)
        self.assertIn("--gray:", css)


class AppImportTests(unittest.TestCase):
    """A safety net so we know we didn't break app startup."""

    def test_app_module_imports(self):
        # Imports only; do not start the server or touch the DB.
        from app.main import app  # noqa: F401


if __name__ == "__main__":
    unittest.main()
