"""Regression guards for Cc's 2026-08-14 UI/account follow-ups."""
import re
from pathlib import Path
import inspect
import unittest

from app.routers import users


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")


def _media_block(selector: str) -> str:
    """Return the body of the @media (max-width: 720px) block that
    contains ``selector`` (e.g. ``'.pager'``). Multiple @media blocks
    may exist for the same breakpoint (one for the global layout, one
    for the pager), so we pick the one that touches the requested
    selector.

    Raises AssertionError if no matching block is found.
    """
    pattern = re.compile(
        r'@media\s*\(max-width:\s*720px\)\s*\{((?:[^{}]|\{[^{}]*\})*)\}',
        flags=re.DOTALL,
    )
    matches = list(pattern.finditer(HTML))
    self_obj = unittest.TestCase()
    for m in matches:
        body = m.group(1)
        if selector in body:
            return body
    self_obj.fail(
        f"no @media (max-width: 720px) block contains {selector!r}; "
        f"available blocks: {len(matches)}"
    )


class BottomPagerTests(unittest.TestCase):
    """The READY/PENDING pager must live at the bottom of the workbench
    section (after desktop table + mobile list) and not be left only at
    the top — per Cc's 2026-08-14 screenshot spec."""

    def test_single_pager_is_after_desktop_and_mobile_results(self):
        # Exactly one pager (not zero, not duplicated).
        self.assertEqual(HTML.count('class="pager"'), 1)
        pager_idx = HTML.index('class="pager"')
        self.assertGreater(pager_idx, HTML.index('class="workbench-wrap desktop-only"'))
        self.assertGreater(pager_idx, HTML.index('class="mobile-list"'))
        # And before the </section> that closes the workbench tab — so
        # the pager lives inside the READY/PENDING section, not in a
        # sibling tab.
        workbench_open = HTML.index("<section x-show=\"['ready','pending','history'].includes(tab)")
        workbench_close = HTML.index("</section>", workbench_open)
        self.assertLess(pager_idx, workbench_close)

    def test_pager_sits_above_sticky_mobile_filter_bar(self):
        # On mobile the filter bar is fixed at the bottom of the viewport
        # (z-index: 30). The pager must render BEFORE that bar in the
        # DOM so the natural reading order is correct even though
        # position: fixed lifts the bar visually.
        pager_idx = HTML.index('class="pager"')
        bar_idx = HTML.index('class="mobile-filter-bar"')
        self.assertLess(pager_idx, bar_idx)

    def test_pager_has_requested_controls(self):
        for label in ("首页", "上一页", "下一页", "末页"):
            self.assertIn(label, HTML)
        self.assertIn('class="pager-size"', HTML)
        self.assertIn("currentPage + ' / ' + totalPageCount", HTML)

    def test_pager_layout_matches_screenshot(self):
        """Screenshot contract:
           left  text  → '第 X / Y 页 · 每页 N'
           right block → page-size dropdown, then 首页, 上一页,
                         current X/Y indicator, 下一页, 末页.

        Pin both the LEFT text and the RIGHT control order inside the
        same .pager div."""
        # Extract the whole pager div. We can't use a non-greedy regex
        # for ``.*?`` reliably here because the pager body has nested
        # <span>s and <template x-for>s. Match by counting <div>
        # depth instead: walk forward from the opening <div class="pager">
        # until depth returns to zero.
        open_idx = HTML.index('<div class="pager"')
        depth = 0
        i = open_idx
        while i < len(HTML):
            if HTML.startswith('<div', i):
                depth += 1
                i += 4
                continue
            if HTML.startswith('</div>', i):
                depth -= 1
                if depth == 0:
                    pager = HTML[open_idx:i + len('</div>')]
                    break
                i += len('</div>')
                continue
            i += 1
        else:
            self.fail("could not find matching </div> for <div class=\"pager\">")
        self.assertIn('class="pager"', pager)
        # LEFT text: 第 X / Y 页 · 每页 N
        self.assertIn('第', pager)
        self.assertIn('<b x-text="currentPage">', pager)
        self.assertIn('<b x-text="totalPageCount">', pager)
        self.assertIn('<b x-text="currentPageSize">', pager)
        self.assertIn('每页', pager)

        # RIGHT controls in the exact order from the screenshot.
        # All markers must appear in source order inside .pager.
        ordered = [
            ('select', 'class="pager-size"'),
            ('button', 'gotoFirstPage'),
            ('button', 'gotoPrevPage'),
            ('span', 'class="pager-page"'),
            ('button', 'gotoNextPage'),
            ('button', 'gotoLastPage'),
        ]
        last_idx = -1
        for tag, marker in ordered:
            idx = pager.index(marker)
            self.assertGreater(
                idx, last_idx,
                f"{tag} with marker {marker!r} must come AFTER the previous "
                f"control (left/right ordering per screenshot)",
            )
            last_idx = idx

    def test_pager_meta_uses_margin_left_auto_to_right_align_controls(self):
        # The .pager-controls block pushes itself to the right of the
        # meta text via margin-left: auto (the standard flex trick).
        m = re.search(
            r'\.pager\s+\.pager-controls\s*\{([^}]+)\}',
            HTML,
        )
        self.assertIsNotNone(m, ".pager .pager-controls CSS not found")
        css = m.group(1)
        self.assertIn("margin-left: auto", css)


class MobilePagerResponsiveTests(unittest.TestCase):
    """Mobile (≤720px) responsive behaviour: pager must stack the meta
    text above the controls and use full-height tap targets so the
    page-size dropdown + buttons stay usable on touchscreens."""

    def test_mobile_breakpoint_exists(self):
        # Find the @media block that touches the pager.
        block = _media_block(".pager")
        self.assertIn(".pager-controls", block)

    def test_mobile_pager_stacks_meta_above_controls(self):
        block = _media_block(".pager")
        # flex-direction: column on the pager so the meta line ends up
        # above the controls on narrow screens.
        self.assertRegex(
            block,
            r'\.pager\s*\{[^}]*flex-direction:\s*column',
        )
        # margin-left: auto is reset to 0 so the controls no longer
        # push themselves to the right; they sit centred below the
        # meta line on mobile.
        self.assertRegex(
            block,
            r'\.pager\s+\.pager-controls\s*\{[^}]*margin-left:\s*0',
        )
        # Controls themselves use flex-wrap: wrap so the 4 navigation
        # buttons + page-size dropdown don't overflow horizontally.
        self.assertRegex(
            block,
            r'\.pager\s+\.pager-controls\s*\{[^}]*flex-wrap:\s*wrap',
        )

    def test_mobile_tap_targets_meet_minimum_height(self):
        block = _media_block(".pager")
        # 2.4rem (~38px) is the minimum Apple HIG / Material touch
        # target. The pager buttons and the page-size <select> must
        # clear that on mobile.
        self.assertRegex(
            block,
            r'\.pager\s+\.pager-btn\s*,\s*\.pager\s+select\.pager-size\s*\{[^}]*min-height:\s*2\.4rem',
        )

    def test_mobile_pager_lifts_above_fixed_filter_bar(self):
        block = _media_block(".pager")
        # The pager lives in normal flow above the fixed bottom filter
        # bar; add bottom margin equal to the bar height + safe area so
        # the last pager button isn't hidden under the fixed bar.
        self.assertRegex(
            block,
            r'\.pager\s*\{[^}]*margin-bottom:.*mobile-bar-height',
        )


class ReadyColumnTests(unittest.TestCase):
    def test_ready_hides_diagnostic_headers(self):
        for label in ("Reserved", "Available", "Missing", "Reason", "Status"):
            self.assertRegex(
                HTML,
                rf'<th[^>]*x-show="tab !== \'ready\'"[^>]*>[^<]*{label}',
            )

    def test_ready_hides_diagnostic_cells(self):
        self.assertIn('x-show="tab !== \'ready\'" class="col-num" :class="row.p.reserved', HTML)
        self.assertIn('x-show="tab !== \'ready\'" class="status-chip chip"', HTML)


class AdminDeleteAccountTests(unittest.TestCase):
    def test_ui_uses_delete_account_label_and_hides_self_action(self):
        self.assertIn("删除账户", HTML)
        self.assertIn("u.id !== user.id", HTML)

    def test_backend_rejects_self_delete(self):
        source = inspect.getsource(users.deactivate_user)
        self.assertIn('user_id == actor["id"]', source)
        self.assertIn("cannot delete your own account", source)
        self.assertIn("cannot deactivate the last admin", source)


if __name__ == "__main__":
    unittest.main()
