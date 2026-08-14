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

    def test_mobile_pager_stays_on_one_row_no_flex_direction_column(self):
        """Cc's 2026-08-14 follow-up: the pager must stay on ONE
        horizontal row at every viewport width — no vertical stacking,
        no character-wrap, no ``flex-direction: column``. On narrow
        mobile widths the controls block scrolls horizontally rather
        than breaking onto a second line."""
        block = _media_block(".pager")
        # The pager itself must keep flex-direction: row even on mobile.
        # Pin the absence of any column/row-reverse flex-direction for
        # the .pager selector so a future 'optimisation' cannot flip it
        # to a stacked layout.
        self.assertNotRegex(
            block,
            r"\.pager\s*\{[^}]*flex-direction:\s*column",
            "pager must NOT stack vertically on mobile (Cc 2026-08-14)",
        )
        self.assertNotRegex(
            block,
            r"\.pager\s*\{[^}]*flex-direction:\s*column-reverse",
        )
        # And the controls row must remain nowrap (no flex-wrap: wrap),
        # otherwise buttons can wrap onto a second line on narrow widths.
        self.assertNotRegex(
            block,
            r"\.pager\s+\.pager-controls\s*\{[^}]*flex-wrap:\s*wrap",
            "pager controls must not wrap onto multiple lines on mobile",
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
    """Cc's 2026-08-14 account spec:

    * Admin can delete (deactivate) any non-self account.
    * DELETE /api/users/{id} is a SOFT-deactivate (active=FALSE,
      row preserved) so audit/history stays queryable.
    * UI label updated from "禁用" to "删除账户" with a clear
      confirmation dialog that mentions 历史数据保留.
    * Admin cannot delete/deactivate their own account (UI hides
      the button + backend rejects the request).
    * Last-active-admin protection is preserved (backend already
      prevents the last active admin from being deactivated).
    * returns / repair remain forbidden (admin_required gate)."""

    # -- UI markup -----------------------------------------------------

    def test_ui_uses_delete_account_label_and_hides_self_action(self):
        self.assertIn("删除账户", HTML)
        self.assertIn("u.id !== user.id", HTML)

    def test_ui_button_uses_delete_account_label(self):
        """The button text must say "删除账户" — the old "禁用" label is
        confusing because the action still leaves the account in the
        database (just inactive)."""
        self.assertIn(
            '@click="deactivateUser(u.id, u.name)" class="btn btn-danger">删除账户',
            HTML,
        )

    def test_ui_does_not_use_old_disable_label(self):
        """Regression guard: the old "禁用" label must be gone from
        the user-management UI. (We don't ban it everywhere — only on
        the deactivate-user button — so the regex is anchored.)"""
        m = re.search(
            r'<button[^>]*@click="deactivateUser[^>]*>([^<]*)</button>',
            HTML,
        )
        self.assertIsNotNone(m, "deactivate-user <button> not found")
        label = m.group(1).strip()
        self.assertNotIn("禁用", label)
        self.assertEqual(label, "删除账户")

    def test_ui_button_hidden_for_self_and_inactive(self):
        """Admin must not be able to delete their own account from the
        UI. The button's x-show must check ``u.id !== user.id``. The
        button should also be hidden for already-deactivated users
        (u.active=false) to avoid showing a no-op action."""
        m = re.search(
            r'<button[^>]*@click="deactivateUser[^>]*>',
            HTML,
        )
        self.assertIsNotNone(m, "deactivate-user <button> not found")
        button = m.group(0)
        # The x-show must include the self-exclusion.
        self.assertIn("u.id !== user.id", button)
        # And must gate on u.active so already-deactivated rows hide the button.
        self.assertIn("u.active", button)

    def test_ui_confirmation_dialog_is_clear_and_mentions_history(self):
        """The deactivateUser JS handler must confirm with the user.
        The dialog text must mention 历史数据保留 so the admin knows
        the action is reversible by re-activating the row, not a hard
        delete."""
        m = re.search(
            r'async deactivateUser\([^)]*\)\s*\{[^}]*confirm\(([^)]+)\)',
            HTML,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(m, "deactivateUser confirm() not found")
        dialog = m.group(1)
        # Must mention the user name (so the admin confirms the right one).
        self.assertIn("name", dialog)
        # Must mention 历史数据保留 so admins know it's soft-delete.
        self.assertIn("历史数据保留", dialog)
        # Must reference the deactivate/delete action semantically.
        self.assertTrue(
            "禁用" in dialog or "删除" in dialog,
            "dialog must reference the deactivate/delete action",
        )

    def test_ui_users_section_is_admin_only(self):
        """The user-management <section> must gate on the admin role so
        returns / repair never see the delete button."""
        m = re.search(
            r'<section[^>]*x-show="tab===\'users\'"',
            HTML,
        )
        # The exact gate might be different (e.g. user.role==='admin'),
        # so we look for any section that opens the users tab AND
        # references the admin role somewhere in its x-show chain.
        m_admin = re.search(
            r'<section[^>]*x-show="[^"]*users[^"]*"[^>]*>',
            HTML,
        )
        self.assertIsNotNone(m_admin, "users <section> not found")
        x_show = m_admin.group(0)
        # The section must gate on admin role. We accept any of:
        # tab==='users' && user && user.role==='admin'
        # user && user.role==='admin' && tab==='users'
        # etc.
        # Pin "admin" so the gate can never drop the role check.
        self.assertIn("admin", x_show)
        # And it must require user !== null (so returns/repair are
        # blocked even if tab gets set programmatically).
        self.assertIn("user", x_show)

    # -- Backend source-level guards ------------------------------------

    def test_backend_rejects_self_delete(self):
        source = inspect.getsource(users.deactivate_user)
        self.assertIn('user_id == actor["id"]', source)
        self.assertIn("cannot delete your own account", source)
        self.assertIn("cannot deactivate the last admin", source)

    def test_backend_delete_route_uses_soft_deactivate_semantics(self):
        """DELETE /api/users/{id} is implemented as soft-deactivate
        (UPDATE active=FALSE) — NOT a hard DELETE row. This preserves
        the user record so audit_log JOINs still resolve and the user
        can be re-activated later."""
        source = inspect.getsource(users.deactivate_user)
        # Soft-deactivate: set active=FALSE on the existing row.
        self.assertRegex(
            source,
            r'UPDATE\s+users\s+SET\s+active=FALSE',
            "DELETE /api/users/{id} must soft-deactivate (UPDATE active=FALSE), "
            "not hard-delete (DELETE FROM users)",
        )
        # The endpoint is declared as @router.delete(...). Pin that.
        self.assertRegex(
            source,
            r'@router\.delete\("/\{user_id\}"',
        )

    def test_backend_records_audit_log_on_deactivate(self):
        """Every deactivate must write an audit_log row so we can
        prove who deactivated whom and when."""
        source = inspect.getsource(users.deactivate_user)
        self.assertIn("audit_log", source)
        self.assertIn("'deactivate'", source)
        self.assertIn("'user'", source)

    def test_backend_self_delete_guard_fires_before_db_work(self):
        """The admin cannot deactivate their own account. The guard
        must return 400 before any database work (pool acquisition or
        SELECT against the DB)."""
        source = inspect.getsource(users.deactivate_user)
        # The guard must reference both the actor id and the path param.
        self.assertIn('user_id == actor["id"]', source)
        # And raise 400 with a clear message.
        self.assertIn("HTTPException(400", source)
        self.assertIn("cannot delete your own account", source)
        # The guard must come BEFORE the SELECT against the DB so
        # we don't even touch the DB when the admin targets themselves.
        guard_pos = source.index('user_id == actor["id"]')
        # Find the first DB-touching statement: either ``async with
        # pool().acquire()`` or ``SELECT`` — whichever comes first.
        db_signals = ("async with pool().acquire()", "SELECT", "INSERT")
        db_positions = []
        for sig in db_signals:
            try:
                db_positions.append(source.index(sig))
            except ValueError:
                continue
        self.assertGreater(
            len(db_positions), 0,
            "expected at least one DB-touching statement in deactivate_user",
        )
        first_db = min(db_positions)
        self.assertLess(
            guard_pos, first_db,
            f"self-delete guard must run before any DB call "
            f"(guard at {guard_pos}, first DB touch at {first_db})",
        )

    def test_backend_last_admin_protection_preserved(self):
        """The existing last-active-admin guard must still be present
        so we can't accidentally lock ourselves out."""
        source = inspect.getsource(users.deactivate_user)
        # The guard counts active admins and refuses if <= 1.
        self.assertRegex(
            source,
            r"SELECT\s+COUNT\(\*\)\s+FROM\s+users\s+WHERE\s+role='admin'\s+AND\s+active=TRUE",
        )
        self.assertIn("cannot deactivate the last admin", source)
        self.assertIn("admin_count <= 1", source)

    def test_backend_noop_when_already_deactivated(self):
        """Deactivating an already-deactivated user is a noop (200 with
        noop=True). It must NOT raise and must NOT bump audit_log."""
        source = inspect.getsource(users.deactivate_user)
        self.assertIn("not existing[\"active\"]", source)
        self.assertIn("noop", source)
        self.assertIn("True", source)

    def test_backend_admin_only_role_gate(self):
        """returns / repair must remain forbidden on DELETE. The
        admin_required Depends must be wired into the route signature."""
        source = inspect.getsource(users.deactivate_user)
        self.assertIn("admin_required", source)

    def test_backend_role_check_present_at_module_level(self):
        """The coarse-grained admin gate is defined at module level so
        every endpoint in users.py inherits it. Pin the exact line so
        a refactor that drops the gate is caught."""
        module_src = inspect.getsource(users)
        # admin_required is the only role gate for delete/update/list.
        # If someone refactors it to a different role (e.g. returns),
        # this test fails.
        self.assertIn(
            'admin_required = Depends(require_role("admin"))',
            module_src,
        )

    def test_backend_returns_and_repair_cannot_delete(self):
        """Pin the module-level admin gate so a refactor that drops it
        (or replaces it with a less strict role) is caught.

        The HTTP-level behaviour is covered by
        test_returns_permissions.py::UsersPermissionsTests, but this
        static guard catches accidental changes to the gate at the
        import-time wiring level."""
        module_src = inspect.getsource(users)
        self.assertIn(
            'admin_required = Depends(require_role("admin"))',
            module_src,
        )

    def test_backend_delete_returns_deactivated_user_record(self):
        """The response body of a successful deactivate must include
        at least the user's id, name, and active=False so the front-end
        can refresh the user list without an extra GET."""
        source = inspect.getsource(users.deactivate_user)
        # Look for the return statement and confirm it returns the
        # deactivated user record (id + name + active=False).
        m = re.search(
            r'return\s+\{"id":\s*user_id,\s*"name":\s*existing\["name"\],\s*"active":\s*False\}',
            source,
        )
        self.assertIsNotNone(
            m,
            "deactivate_user must return the deactivated user record",
        )



# ---------------------------------------------------------------------------
# 2026-08-14 follow-up: per-part 仓位 column lives beside Part Code, raw value
# (no source prefix like "66:HS168-第3仓:"), multi-location preserved.
# ---------------------------------------------------------------------------


def _slice_html(html: str, start: str, end: str) -> str:
    """Return the substring between two anchor markers (or empty if missing)."""
    s = html.find(start)
    if s < 0:
        return ""
    e = html.find(end, s + len(start))
    if e < 0:
        return ""
    return html[s:e]


# ---------------------------------------------------------------------------
# 2026-08-14 follow-up: pagination controls must stay on ONE horizontal row
# at every viewport width — no flex-direction: column, no character-wrap,
# no vertical stacking. On narrow mobile widths the pager scrolls
# horizontally rather than breaking button labels.
# ---------------------------------------------------------------------------


class PagerOneRowContractTests(unittest.TestCase):
    """Cc's 2026-08-14 spec (follow-up to the bottom-pager work):

      The pagination bar must remain on ONE horizontal row at every
      viewport width. It must NOT use ``flex-direction: column``, must
      NOT wrap onto a second line, and must NOT character-break button
      labels. On narrow mobile widths the pager container may scroll
      horizontally so the controls stay intact and readable.

    These tests pin that contract by scanning the CSS in
    ``app/templates/index.html`` for both the desktop block and the
    ``@media (max-width: 720px)`` mobile block.
    """

    # -- helpers ------------------------------------------------------

    @staticmethod
    def _pager_desktop_block():
        """Return the CSS block for the .pager rule itself (not the
        @media override). Looks for the OUTER ``.pager { ... }`` that
        immediately precedes the @media query.
        """
        m = re.search(r"\.pager\s*\{([^}]+)\}", HTML)
        self_obj = unittest.TestCase()
        self_obj.assertIsNotNone(m, "desktop .pager { ... } rule not found")
        return m.group(1)

    @staticmethod
    def _pager_media_block():
        """Return the body of the @media (max-width: 720px) { ... }
        block that contains the .pager override."""
        return _media_block(".pager")

    # -- desktop -----------------------------------------------------

    def test_desktop_pager_is_flex_row_no_wrap(self):
        """On desktop the .pager is a horizontal flex row that never
        wraps onto a second line. Pin ``flex-wrap: nowrap`` on the
        outer .pager rule so a future revert cannot reintroduce the
        multi-row wrap."""
        css = self._pager_desktop_block()
        self.assertRegex(
            css,
            r"flex-wrap:\s*nowrap",
            "desktop .pager must use flex-wrap: nowrap (Cc 2026-08-14)",
        )

    def test_desktop_pager_uses_white_space_nowrap(self):
        """The .pager must also pin ``white-space: nowrap`` so button
        labels can never character-break (e.g. ‘下一\n页’ rendering as
        two lines inside one button on narrow widths)."""
        css = self._pager_desktop_block()
        self.assertIn("white-space: nowrap", css)

    def test_desktop_pager_allows_horizontal_overflow(self):
        """If the content does not fit horizontally, the .pager must
        scroll horizontally rather than wrap or stack. Pin
        ``overflow-x: auto`` (with the iOS smooth-scroll hint) so the
        contract survives future CSS refactors."""
        css = self._pager_desktop_block()
        self.assertIn("overflow-x: auto", css)

    def test_desktop_pager_controls_never_wrap(self):
        """The controls sub-row must itself stay on one row, regardless
        of how many buttons it contains. Pin ``flex-wrap: nowrap`` on
        .pager .pager-controls."""
        m = re.search(
            r"\.pager\s+\.pager-controls\s*\{([^}]+)\}",
            HTML,
        )
        self.assertIsNotNone(m, ".pager .pager-controls rule not found")
        css = m.group(1)
        self.assertRegex(
            css,
            r"flex-wrap:\s*nowrap",
            ".pager-controls must never wrap buttons onto multiple lines",
        )
        self.assertIn("white-space: nowrap", css)
        self.assertIn("flex-shrink: 0", css)

    def test_desktop_pager_buttons_have_nowrap(self):
        """Every pager button must keep ``white-space: nowrap`` so the
        Chinese label ‘下一页’ cannot split across two visual lines
        inside a single button."""
        for marker in (
            'padding: .32rem .7rem',
        ):
            self.assertIn(marker, HTML)
        # Find each .pager .pager-btn { ... } rule and assert nowrap.
        btn_re = re.compile(
            r"\.pager\s+\.pager-btn\s*\{([^}]+)\}",
        )
        rules = btn_re.findall(HTML)
        self.assertTrue(rules, ".pager .pager-btn rule not found")
        for css in rules:
            self.assertIn(
                "white-space: nowrap", css,
                "each .pager-btn rule must pin white-space: nowrap",
            )

    def test_desktop_pager_no_flex_direction_column(self):
        """The desktop block must never set flex-direction to anything
        other than the default (row). Pin the ABSENCE of any column /
        column-reverse override so a future stacked-pager regression
        cannot slip in."""
        css = self._pager_desktop_block()
        self.assertNotRegex(css, r"flex-direction:\s*column")
        self.assertNotRegex(css, r"flex-direction:\s*column-reverse")

    # -- mobile -------------------------------------------------------

    def test_mobile_pager_no_flex_direction_column(self):
        """Mobile must NOT stack the pager vertically. The previous
        implementation used ``flex-direction: column`` which broke
        Cc's one-row contract."""
        block = self._pager_media_block()
        self.assertNotRegex(
            block,
            r"\.pager\s*\{[^}]*flex-direction:\s*column",
            "mobile .pager must NOT use flex-direction: column",
        )
        self.assertNotRegex(
            block,
            r"\.pager\s*\{[^}]*flex-direction:\s*column-reverse",
        )

    def test_mobile_pager_controls_no_wrap(self):
        """On mobile, the controls block must remain a single row.
        flex-wrap: wrap is forbidden so the 4 nav buttons + page-size
        dropdown can never break onto a second line."""
        block = self._pager_media_block()
        self.assertNotRegex(
            block,
            r"\.pager\s+\.pager-controls\s*\{[^}]*flex-wrap:\s*wrap",
        )

    def test_mobile_pager_keeps_horizontal_overflow(self):
        """Mobile inherits the desktop ``overflow-x: auto`` (the rule
        lives on the outer .pager selector which is NOT inside the
        @media block — and we want one continuous horizontal scroll
        bar to appear when the page is narrower than the bar).

        Confirm no @media override re-sets overflow-x to ``visible``
        (which would break the contract)."""
        block = self._pager_media_block()
        self.assertNotRegex(block, r"\.pager\s*\{[^}]*overflow-x:\s*visible")

    def test_mobile_pager_meta_can_shrink_not_controls(self):
        """Cc's preference on narrow widths: let the meta text shrink
        first (with text-overflow: ellipsis) but keep the nav buttons
        intact. Pin ``flex-shrink: 1`` on .pager-meta and
        ``flex-shrink: 0`` on .pager-controls inside the @media block
        so the layout collapses in the right direction."""
        block = self._pager_media_block()
        self.assertRegex(
            block,
            r"\.pager\s+\.pager-meta\s*\{[^}]*flex-shrink:\s*1",
            "mobile meta text must be allowed to shrink first",
        )
        self.assertRegex(
            block,
            r"\.pager\s+\.pager-controls\s*\{[^}]*flex-shrink:\s*0",
            "mobile controls must never shrink (Cc wants nav buttons intact)",
        )
        # And the meta must clip cleanly with ellipsis.
        self.assertRegex(
            block,
            r"\.pager\s+\.pager-meta\s*\{[^}]*text-overflow:\s*ellipsis",
        )

    def test_mobile_pager_buttons_have_min_height_for_tap(self):
        """The 2.4rem tap-target rule from the previous contract must
        still hold (Apple HIG / Material touch-target minimum)."""
        block = self._pager_media_block()
        self.assertRegex(
            block,
            r"\.pager\s+\.pager-btn\s*,\s*\.pager\s+select\.pager-size\s*\{[^}]*min-height:\s*2\.4rem",
        )

    # -- structure sanity ---------------------------------------------

    def test_pager_markup_contains_no_inline_break_tags(self):
        """Final guard: the rendered pager buttons must not contain
        any <br>, <wbr>, or whitespace-only character that would
        visually break the label into two lines."""
        # We can't easily balance the .pager <div> with regex (it has
        # nested <template>s), so use a simple marker: every button
        # label we care about must appear as a single contiguous
        # string inside a <button>...</button>, never with an embedded
        # newline or <br> between the tag and the label.
        for label in ("« 首页", "‹ 上一页", "下一页 ›", "末页 »"):
            # The label appears at least once in the document.
            self.assertIn(label, HTML)
            # And the exact substring `<button ...>label</button>` must
            # exist with NO newline, no <br>, no <wbr> between the
            # open-tag and the label.
            expected = r"<button[^>]*>" + re.escape(label) + r"</button>"
            self.assertRegex(
                HTML, expected,
                f"button for {label!r} must render as a single contiguous label",
            )
            # And that contiguous label must NOT be preceded by an
            # injected break tag anywhere — confirm with a negative
            # lookbehind.
            self.assertNotRegex(
                HTML,
                r"<button[^>]*>\s*(<br\b|<wbr\b|\n)" + re.escape(label),
                f"button for {label!r} must not embed <br>/<wbr>/newline before the label",
            )


class LocationColumnDesktopTests(unittest.TestCase):
    """The READY/PENDING desktop workbench must have a dedicated 仓位 column
    to the right of Part Code, and the location chips must no longer live
    inside the Part Code cell."""

    def test_part_loc_column_header_present(self):
        # The header just to the right of Part Code must be the new column.
        self.assertRegex(
            HTML,
            r'<th class="col-part-code">Part Code</th>\s*'
            r'<th class="col-part-loc">仓位</th>',
        )

    def test_part_loc_filter_input_present(self):
        # Column-filter input row also gets the new filter.
        self.assertIn('placeholder="仓位"', HTML)
        self.assertIn("columnFilters.partLoc", HTML)

    def test_part_loc_filter_state_initialised(self):
        # The reactive initial state and the reset must include partLoc.
        self.assertIn("partLoc: ''", HTML)

    def test_part_loc_filter_predicate_uses_inventory_locations(self):
        # The filter must consult inventory_locations[].location — not
        # synthesize a string with a source prefix.
        self.assertRegex(
            HTML,
            r"!\s*f\.partLoc\s*\|\|\s*"
            r"parts\.some\(\s*p\s*=>\s*"
            r"\(p\.inventory_locations\s*\|\|\s*\[\]\)\.some\(\s*l\s*=>\s*"
            r"includes\(\s*l\.location",
        )

    def test_part_code_cell_no_longer_holds_inventory_chips(self):
        # Inside the desktop workbench tbody, the Part Code cell must
        # contain ONLY the pc-code span, never the pc-locs chip cluster.
        workbench = _slice_html(HTML, '<table class="workbench"', "</table>")
        self.assertTrue(workbench, "workbench table not found")
        # Locate each row, then assert each Part Code <td> with col-id
        # containing pc-row contains no pc-locs.
        row_re = re.compile(
            r"<td class=\"col-id\">\s*<div class=\"pc-row\">.*?</td>",
            re.DOTALL,
        )
        part_code_cells = row_re.findall(workbench)
        self.assertTrue(part_code_cells, "no Part Code cells found in workbench")
        for cell in part_code_cells:
            self.assertNotIn(
                "pc-locs", cell,
                "pc-locs should no longer be rendered inside Part Code cells",
            )
            self.assertNotIn(
                "pc-loc-chip", cell,
                "pc-loc-chip should no longer be rendered inside Part Code cells",
            )

    def test_part_loc_cell_renders_chips_outside_part_code(self):
        workbench = _slice_html(HTML, '<table class="workbench"', "</table>")
        self.assertTrue(workbench, "workbench table not found")
        # The dedicated col-part-loc cell exists and renders the chips.
        cell_re = re.compile(
            r'<td class="col-part-loc">.*?</td>',
            re.DOTALL,
        )
        cells = cell_re.findall(workbench)
        self.assertTrue(cells, "no col-part-loc cells found")
        joined = "\n".join(cells)
        self.assertIn("pc-loc-chip", joined)
        # And the part-code cells immediately preceding it should NOT.
        for cell in cells:
            self.assertNotIn("col-part-code", cell)
            self.assertNotIn("pc-code", cell)

    def test_part_loc_cell_dash_fallback_present(self):
        workbench = _slice_html(HTML, '<table class="workbench"', "</table>")
        # Empty inventory_locations → render an em-dash placeholder.
        self.assertRegex(
            workbench,
            r"x-show=\"!\(row\.p\.inventory_locations && row\.p\.inventory_locations\.length\)\"",
        )
        self.assertRegex(workbench, r">—</span>")

    def test_no_source_prefix_construction_anywhere(self):
        """Guard rail: nothing in the template should ever compose a
        ``66:HS168-第3仓:``-style prefix. If the back-end data is raw
        (which ``app.routers.inventory`` already guarantees), the front
        end has nothing to add."""
        # Look for any literal that hard-codes an account/warehouse
        # prefix joined to the location text.
        bad_patterns = [
            r"66:HS168",
            r"88:HS168",
            r"99:HS168",
            r"66:第3仓",
            r"88:第3仓",
            r"99:第3仓",
            r"loc\.location\s*\+\s*['\"]\s*:",   # concatenation with separator
            r"location\s*\+\s*['\"]:\s*['\"]",   # any "location + ':' + ..."
        ]
        for pat in bad_patterns:
            self.assertNotRegex(
                HTML, pat,
                f"template should not compose location prefix; found {pat!r}",
            )

    def test_col_part_loc_css_width_present(self):
        self.assertIn(".col-part-loc", HTML)
        # Width declaration exists so the new column has a sensible layout.
        self.assertRegex(HTML, r"\.col-part-loc\s*\{[^}]*width:\s*\d")


class LocationMobileCardTests(unittest.TestCase):
    """The mobile parts cell must keep the per-location breakdown but show
    it under a small 仓位 label so the location is unambiguously the
    location — not a chip floating beneath an unmarked part code."""

    def test_mobile_parts_cell_has_loc_label_and_chips(self):
        mobile = _slice_html(
            HTML, 'class="mobile-list"', "<!-- Mobile sticky bottom filter bar",
        )
        self.assertTrue(mobile, "mobile-list slice not found")
        # The 仓位 label appears inside the parts cell so the chips are
        # clearly labelled rather than visually ambiguous.
        self.assertIn('class="pc-loc-lbl"', mobile)
        self.assertRegex(mobile, r">\s*仓位\s*</span>")
        # The chips still iterate over inventory_locations so multi-location
        # data is preserved.
        self.assertIn("p.inventory_locations", mobile)
        self.assertIn("pc-loc-chip", mobile)


class LocationRepairViewTests(unittest.TestCase):
    """The simplified repair view must keep the per-part location detail
    visually labelled so the chips are not confused for part-code badges."""

    def test_repair_view_has_loc_label_and_chips(self):
        repair = _slice_html(
            HTML, "<!-- REPAIR SIMPLIFIED VIEW (P3) -->", "<!-- INVENTORY TAB -->",
        )
        self.assertTrue(repair, "repair view slice not found")
        self.assertIn('class="pc-loc-lbl"', repair)
        self.assertRegex(repair, r">\s*仓位\s*</span>")
        self.assertIn("p.inventory_locations", repair)
        self.assertIn("pc-loc-chip", repair)


if __name__ == "__main__":
    unittest.main()
