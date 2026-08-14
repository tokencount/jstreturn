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
    """Cc's 2026-08-14 spec: on the READY tab only, the diagnostic
    columns Reserved / Available / Missing / Reason / Status must NOT
    appear in the desktop table, the mobile card list, or the repair
    view. They stay visible on PENDING where they help the user
    understand why a part is short."""

    # -- Header row ----------------------------------------------------

    def test_ready_hides_diagnostic_headers(self):
        for label in ("Reserved", "Available", "Missing", "Reason", "Status"):
            self.assertRegex(
                HTML,
                rf'<th[^>]*x-show="tab !== \'ready\'"[^>]*>[^<]*{label}',
            )

    # -- Filter row ----------------------------------------------------

    def test_ready_hides_diagnostic_filter_inputs(self):
        """The column-filter <input>s that live in the second header row
        must also be gated — leaving them visible on READY would leave
        a non-functional text box whose placeholder is meaningless."""
        for placeholder in ("Resv", "Avail", "Miss", "Reason"):
            self.assertRegex(
                HTML,
                rf'<th[^>]*x-show="tab !== \'ready\'"[^>]*>'
                rf'<input[^>]*placeholder="{placeholder}"',
            )

    # -- Body cells (desktop) -----------------------------------------

    def test_ready_hides_diagnostic_body_cells(self):
        for needle in (
            'x-show="tab !== \'ready\'" class="col-num" :class="row.p.reserved',
            'x-show="tab !== \'ready\'" class="col-num" :class="row.p.available',
            'x-show="tab !== \'ready\'" class="col-num" :class="row.p.missing',
            'x-show="tab !== \'ready\'">\n                      <span class="reason-tag"',
            'x-show="tab !== \'ready\'">\n                      <span x-show="row.pi===0" class="status-chip"',
        ):
            self.assertIn(needle, HTML)

    def test_ready_hides_status_chip_on_desktop_table(self):
        """The desktop table's status chip lives inside a <td
        x-show="tab !== 'ready'"> parent; verify the surrounding <td>
        is gated so the chip is not visible on READY."""
        self.assertRegex(
            HTML,
            r'<td x-show="tab !== \'ready\'">\s*<span x-show="row\.pi===0" class="status-chip"',
        )

    # -- Mobile card list ---------------------------------------------

    def test_ready_hides_status_chip_on_mobile_cards(self):
        self.assertIn(
            '<span x-show="tab !== \'ready\'" class="status-chip chip"',
            HTML,
        )

    def test_ready_hides_diagnostic_text_in_mobile_card_meta(self):
        """The mobile card-row-2 / card-meta blocks must not mention
        Reserved / Available / Missing / Reason as standalone labels on
        READY. They are diagnostic concepts, not useful on the
        read-only READY list."""
        # The mobile-card markup does NOT currently label these
        # diagnostics, but guard against any future drift: if any of
        # these labels appear inside .card-* they must be gated.
        for label in ("Reserved", "Available", "Missing", "Reason"):
            # No unconditional display of the label inside mobile cards.
            card_blocks = re.findall(
                r'<article class="card">(.*?)</article>',
                HTML,
                flags=re.DOTALL,
            )
            for block in card_blocks:
                # The Reason text could conceivably appear inside
                # p.reason or p.reason-text, so we only fail when the
                # label is a stand-alone label span (case-sensitive
                # match, surrounded by markup boundaries).
                self.assertNotRegex(
                    block,
                    rf'>\s*{label}\s*<',
                    f"mobile card must not display {label!r} unconditionally",
                )

    # -- Repair view (mobile + desktop, READY-only) -------------------

    def test_repair_view_has_no_diagnostic_field_remnants(self):
        """The repair view renders inside ``<section class="repair-only">``
        and is shown only when ``user.role === 'repair'``. It must NOT
        surface any of Reserved / Available / Missing / Reason as a
        label or value, and the status chip text 'READY' is acceptable
        (it tells the tech what they're about to complete) but no
        'PENDING' / 'COMPLETED' chips may appear there."""
        m = re.search(
            r'<section[^>]*class="repair-only"[^>]*>(.*?)</section>',
            HTML,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(m, "repair-only section not found")
        repair = m.group(1)
        for label in ("Reserved", "Available", "Missing", "Reason", "Status"):
            self.assertNotIn(label, repair)
        # PENDING / COMPLETED status chips must not appear in the repair
        # view — it is READY-only by definition.
        for forbidden in ("chip-PENDING", "chip-COMPLETED", "PENDING", "COMPLETED"):
            self.assertNotIn(forbidden, repair)

    # -- Column-count parity (header ↔ body) --------------------------

    def test_header_and_body_have_equal_column_counts_on_ready(self):
        """On the READY tab, hiding 5 diagnostic columns in both the
        header and the body must yield a matching column count, so the
        table cells line up under the correct headers."""
        # Pull the FIRST <thead> of the workbench table.
        thead_match = re.search(
            r'<table class="workbench"[^>]*>.*?<thead>(.*?)</thead>',
            HTML,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(thead_match, "workbench <thead> not found")
        thead = thead_match.group(1)

        # The diagnostic column <th>s share the same gating expression;
        # count the cells visible on READY by counting <th>s WITHOUT the
        # gating expression in the FIRST <tr> of <thead>.
        first_tr = re.search(r'<tr>(.*?)</tr>', thead, flags=re.DOTALL)
        self.assertIsNotNone(first_tr, "first <tr> in <thead> missing")
        first_tr_html = first_tr.group(1)

        # The header <th>s always include the diagnostic columns;
        # counting the gated ones tells us how many are removed on READY.
        diag_ths = re.findall(
            r"<th[^>]*x-show=\"tab !== 'ready'\"[^>]*>.*?</th>",
            first_tr_html,
            flags=re.DOTALL,
        )
        self.assertEqual(
            len(diag_ths), 5,
            f"expected 5 diagnostic <th>s gated by tab !== 'ready', got {len(diag_ths)}",
        )

        # Body: the FIRST <tbody> row in the desktop workbench table.
        tbody_match = re.search(
            r'<table class="workbench"[^>]*>.*?<tbody>(.*?)</tbody>',
            HTML,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(tbody_match, "workbench <tbody> not found")
        # The workbench <tbody> contains a <template x-for="row in ...">
        # whose root is the row <tr>. Pull out that first <tr>:
        tr_match = re.search(
            r'<template[^>]*>\s*<tr[^>]*>(.*?)</tr>',
            tbody_match.group(1),
            flags=re.DOTALL,
        )
        self.assertIsNotNone(tr_match, "first <tr> in <tbody> missing")
        first_body_tr = tr_match.group(1)

        diag_tds = re.findall(
            r"<td[^>]*x-show=\"tab !== 'ready'\"[^>]*>.*?</td>",
            first_body_tr,
            flags=re.DOTALL,
        )
        # Body has 5 gated cells (reserved, available, missing, reason,
        # status) — same count as the header.
        self.assertEqual(
            len(diag_tds), 5,
            f"expected 5 diagnostic <td>s gated by tab !== 'ready', "
            f"got {len(diag_tds)}",
        )

    # -- PENDING keeps the diagnostic fields --------------------------

    def test_pending_keeps_diagnostic_fields_visible(self):
        """The READY-only restriction must not leak to PENDING. We
        assert this by checking that the gating expression is
        ``tab !== 'ready'`` (a positive exclude) — not
        ``tab === 'pending'`` (which would hide the columns on every
        other tab, including HISTORY)."""
        # Use the same diagnostic <th> from test_ready_hides_diagnostic_headers
        # and confirm the expression excludes only 'ready'.
        m = re.search(
            r'<th[^>]*x-show="([^"]+)"[^>]*>[^<]*Reserved',
            HTML,
        )
        self.assertIsNotNone(m, "Reserved header <th> not found")
        expr = m.group(1).strip()
        # Must reference 'ready' and exclude it.
        self.assertIn("ready", expr)
        self.assertNotIn(
            "pending", expr.lower().replace("ready", ""),
            "gating expression must not turn into a positive 'pending' filter",
        )
        # The simplest, most stable form is `tab !== 'ready'`. Pin that
        # to lock the contract.
        self.assertEqual(expr, "tab !== 'ready'")


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
