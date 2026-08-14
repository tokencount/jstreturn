"""Pagination UI markup regression tests.

Pure-front-end test for the 2026-08-14 pagination refactor. Like the
P2 mobile UI tests, this module parses ``app/templates/index.html`` as
text and asserts the structural changes are present:

  * Per-tab pagination state declared in the Alpine ``app()`` state
    object (``readyPage``, ``pendingPage``, ``historyPage``, plus
    their ``PageSize`` / ``Total`` twins).
  * Page-size whitelist declared in the client as a single source of
    truth (100 / 200 / 500 / 2000; default 500) — must match the
    server-side whitelist in app/matcher.py.
  * Pagination controls visible on the READY / PENDING tabs:
      - page-size <select> with all four options
      - First / Prev / Next / Last buttons
      - "Page N of M" indicator bound to a getter
  * The workbench summary uses ``tabTotal`` (server-side accurate
    total) instead of the page-local tally so the user sees the full
    database count, not just the page.
  * COMPLETED (history) keeps its legacy behaviour:
      - the pager is hidden on history
      - loadList('COMPLETED') still hits the legacy endpoint
  * ``loadCounts`` is wired to ``/_/count`` so the tab badges reflect
    the whole database, not just the page slice.

No Alpine execution, no live browser. This module also imports the
app package so a Python-side regression in the pagination refactor
would surface here as an ImportError.
"""
import re
import unittest
from pathlib import Path

INDEX = Path(__file__).resolve().parents[1] / "app" / "templates" / "index.html"


def _read():
    return INDEX.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Alpine state — per-tab pagination
# ---------------------------------------------------------------------------

class AlpinePerTabStateTests(unittest.TestCase):
    """The Alpine ``app()`` state object must carry per-tab pagination
    state so READY and PENDING hold independent pages."""

    def _read_state(self):
        """Extract the body of the ``function app() { return { ... } }``
        block — anything inside is the source of truth for Alpine
        reactive state."""
        html = _read()
        # Grab everything from ``function app()`` through the next
        # ``}`` that closes the top-level return object. We slice on
        # the obvious markers so future template edits don't have to
        # keep an exact brace count.
        start = html.index("function app()")
        # First closing brace of the top-level `return {...}`. Two
        # closing braces later we'll close the function — but we only
        # need the inner object, so slice until ``editModal:``.
        end = html.index("editModal: {", start)
        return html[start:end]

    def test_per_tab_state_declared(self):
        state = self._read_state()
        for field in (
            "readyPage:",
            "readyPageSize:",
            "readyTotal:",
            "pendingPage:",
            "pendingPageSize:",
            "pendingTotal:",
            "historyPage:",
            "historyPageSize:",
            "historyTotal:",
        ):
            self.assertIn(field, state, f"Alpine state must declare {field!r}")

    def test_page_size_whitelist_declared(self):
        state = self._read_state()
        self.assertIn("ALLOWED_PAGE_SIZES: [100, 200, 500, 2000]", state)

    def test_default_page_size_is_500(self):
        state = self._read_state()
        self.assertIn("DEFAULT_PAGE_SIZE: 500", state)
        # The per-tab defaults are all 500.
        for line in state.splitlines():
            stripped = line.strip()
            self.assertFalse(
                re.match(r"(ready|pending|history)PageSize:\s*\d+", stripped)
                and "PageSize: 500" not in stripped,
                f"per-tab default page size must be 500; saw: {stripped!r}",
            )

    def test_initial_page_index_is_one(self):
        """Every per-tab page starts at 1 so the first load is page 1,
        not page 0 (which would be an empty page for the user)."""
        state = self._read_state()
        for tab in ("ready", "pending", "history"):
            self.assertRegex(
                state,
                rf"{tab}Page:\s*1",
                f"{tab} page index must default to 1",
            )

    def test_pager_loading_flag_declared(self):
        state = self._read_state()
        self.assertIn("pagerLoading:", state)


# ---------------------------------------------------------------------------
# Pagination getters — must drive the UI from per-tab state
# ---------------------------------------------------------------------------

class PaginationGettersTests(unittest.TestCase):
    """The Alpine getters (``currentPage``, ``currentPageSize``,
    ``tabTotal``, ``totalPageCount``, ``canPrevPage``, ``canNextPage``)
    are what the pager controls bind against. They MUST be present
    and MUST read per-tab state, not a single shared variable."""

    def test_required_getters_present(self):
        html = _read()
        for getter in (
            "get currentPage()",
            "get currentPageSize()",
            "get tabTotal()",
            "get totalPageCount()",
            "get canPrevPage()",
            "get canNextPage()",
        ):
            self.assertIn(getter, html, f"Alpine must expose {getter!r}")

    def test_current_page_reads_per_tab_state(self):
        """``currentPage`` must branch on tab so READY/pending/history
        each keep their own page index."""
        html = _read()
        # Extract the getter body (between ``get currentPage()`` and
        # the next ``get `` or end-of-method marker).
        m = re.search(r"get currentPage\(\)\s*\{(.+?)\n\s*\}", html, flags=re.DOTALL)
        self.assertIsNotNone(m, "could not locate currentPage getter")
        body = m.group(1)
        self.assertIn("readyPage", body)
        self.assertIn("pendingPage", body)
        self.assertIn("historyPage", body)

    def test_current_page_size_reads_per_tab_state(self):
        html = _read()
        m = re.search(r"get currentPageSize\(\)\s*\{(.+?)\n\s*\}", html, flags=re.DOTALL)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertIn("readyPageSize", body)
        self.assertIn("pendingPageSize", body)
        self.assertIn("historyPageSize", body)

    def test_tab_total_reads_per_tab_state(self):
        html = _read()
        m = re.search(r"get tabTotal\(\)\s*\{(.+?)\n\s*\}", html, flags=re.DOTALL)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertIn("readyTotal", body)
        self.assertIn("pendingTotal", body)
        self.assertIn("historyTotal", body)

    def test_total_page_count_uses_tab_total(self):
        html = _read()
        m = re.search(r"get totalPageCount\(\)\s*\{(.+?)\n\s*\}", html, flags=re.DOTALL)
        self.assertIsNotNone(m)
        body = m.group(1)
        # The getter must use tabTotal AND currentPageSize; otherwise
        # the pager would render "page 1 of 1" forever even when the
        # database has thousands of items.
        self.assertIn("tabTotal", body)
        self.assertIn("currentPageSize", body)


# ---------------------------------------------------------------------------
# Pager controls — markup in the LIST tab section
# ---------------------------------------------------------------------------

class PagerMarkupTests(unittest.TestCase):
    """The pager UI must live in the LIST tab section, show only for
    READY/PENDING (NOT history), and bind to the per-tab getters."""

    def test_pager_section_present(self):
        html = _read()
        self.assertIn('class="pager"', html)

    def test_pager_only_shows_for_ready_and_pending(self):
        html = _read()
        # The pager must NOT be visible on history (legacy behaviour).
        # We look for an x-show on the .pager element that lists both
        # tabs but not history.
        m = re.search(r'<div class="pager"[^>]*x-show="([^"]+)"', html)
        self.assertIsNotNone(m, "pager must have an x-show binding")
        expr = m.group(1)
        self.assertIn("ready", expr)
        self.assertIn("pending", expr)
        self.assertNotIn("history", expr)

    def test_page_size_select_has_all_whitelisted_options(self):
        html = _read()
        # Find the page-size <select> on the pager
        m = re.search(
            r'<select class="pager-size"[^>]*>(.+?)</select>',
            html,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(m, "page-size <select> not found")
        body = m.group(1)
        # The page-size <select> is rendered from the Alpine
        # ``allowedPageSizes`` getter via a <template x-for>. Pin
        # both: (a) the dynamic-template path is in place, and
        # (b) the whitelist source-of-truth is ``ALLOWED_PAGE_SIZES``
        # (100 / 200 / 500 / 2000) so the front-end can never drift
        # from the backend whitelist.
        self.assertIn("x-for=\"opt in allowedPageSizes\"", body)
        self.assertIn("ALLOWED_PAGE_SIZES: [100, 200, 500, 2000]", html)

    def test_page_size_select_bound_to_current_page_size(self):
        html = _read()
        # The :value binding on the select must read from currentPageSize.
        m = re.search(r'<select class="pager-size"[^>]*:value="([^"]+)"', html)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "currentPageSize")

    def test_page_size_change_handler_declared(self):
        html = _read()
        # The select must wire its @change to a method that clamps
        # the value and reloads the right tab.
        m = re.search(r'<select class="pager-size"[^>]*@change="([^"]+)"', html)
        self.assertIsNotNone(m)
        self.assertIn("onPageSizeChange", m.group(1))

    def test_pager_navigation_buttons_present(self):
        html = _read()
        # Each button must be wired to its own handler so first/prev
        # /next/last all do something different. Use a more permissive
        # regex: the button's body can contain any text (incl. CJK) so
        # we don't constrain it — we only require the handler+label
        # to be present in the same <button ...> ... </button>.
        for handler, label in (
            ("gotoFirstPage", "首页"),
            ("gotoPrevPage", "上一页"),
            ("gotoNextPage", "下一页"),
            ("gotoLastPage", "末页"),
        ):
            m = re.search(
                rf'<button[^>]*@click="{handler}\(\)"[^>]*>(.+?)</button>',
                html,
                flags=re.DOTALL,
            )
            self.assertIsNotNone(m, f"button wired to {handler} not found")
            self.assertIn(label, m.group(1), f"{handler} button label should include {label!r}")

    def test_pager_disabled_when_cant_navigate(self):
        html = _read()
        # First/Prev must be disabled when canPrevPage is false; the
        # Next/Last disabled when canNextPage is false. The :disabled
        # attribute can appear before or after @click in the markup.
        for handler in ("gotoFirstPage", "gotoPrevPage"):
            m = re.search(
                rf'<button[^>]+@click="{handler}\(\)"[^>]*:disabled="([^"]+)"',
                html,
            )
            if m is None:
                # Order may be reversed.
                m = re.search(
                    rf'<button[^>]+:disabled="([^"]+)"[^>]*@click="{handler}\(\)"',
                    html,
                )
            self.assertIsNotNone(m, f"{handler} must have :disabled binding")
            self.assertIn("canPrevPage", m.group(1))
        for handler in ("gotoNextPage", "gotoLastPage"):
            m = re.search(
                rf'<button[^>]+@click="{handler}\(\)"[^>]*:disabled="([^"]+)"',
                html,
            )
            if m is None:
                m = re.search(
                    rf'<button[^>]+:disabled="([^"]+)"[^>]*@click="{handler}\(\)"',
                    html,
                )
            self.assertIsNotNone(m, f"{handler} must have :disabled binding")
            self.assertIn("canNextPage", m.group(1))

    def test_page_indicator_shows_page_and_max(self):
        html = _read()
        # The .pager-page element must show "N / M" derived from the
        # current page + total page count getters.
        m = re.search(
            r'<span class="pager-page"[^>]*x-text="([^"]+)"',
            html,
        )
        self.assertIsNotNone(m, "page indicator missing")
        expr = m.group(1)
        self.assertIn("currentPage", expr)
        self.assertIn("totalPageCount", expr)


# ---------------------------------------------------------------------------
# Workbench summary — uses tabTotal (not just tally)
# ---------------------------------------------------------------------------

class WorkbenchSummaryTests(unittest.TestCase):
    """The header on the LIST tabs must show the database-wide total,
    not the page-local tally. The page-local tally still drives the
    "X / Y tickets" footer inside the table."""

    def test_summary_uses_tab_total(self):
        html = _read()
        # Find the workbench-summary block.
        m = re.search(
            r'<div class="workbench-summary">(.+?)</div>\s*<!-- Pagination controls',
            html,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(m, "workbench-summary block not found")
        block = m.group(1)
        # The summary must reference tabTotal — that's the whole point.
        self.assertIn("tabTotal", block)
        # The summary must NOT depend solely on the in-page tally.
        self.assertNotRegex(
            block,
            r'>\s*<b[^>]*x-text="tally\.tickets"\s*/>\s*票\s*·',
            "summary must not lead with the page tally",
        )

    def test_tab_total_handles_null_state(self):
        """When the tab total hasn't loaded yet (``tabTotal === null``)
        we fall back to the page tally so the UI is never blank."""
        html = _read()
        # Pin that the fallback exists explicitly.
        self.assertRegex(
            html,
            r'x-show="tabTotal === null"',
            "summary must have an explicit fallback for unloaded tabTotal",
        )


# ---------------------------------------------------------------------------
# COMPLETED (history) behaviour stays safe
# ---------------------------------------------------------------------------

class HistoryTabRegressionTests(unittest.TestCase):
    """The history tab keeps the legacy single-shot fetch (no
    pagination). The pager must be hidden there so the user is not
    given navigation controls that do nothing."""

    def test_pager_hidden_on_history(self):
        html = _read()
        m = re.search(r'<div class="pager"[^>]*x-show="([^"]+)"', html)
        self.assertIsNotNone(m)
        expr = m.group(1)
        self.assertNotIn("history", expr)

    def test_load_list_completed_uses_legacy_endpoint(self):
        """``loadList('COMPLETED')`` must keep hitting the legacy
        single-shot endpoint. The 2026-08-14 scope bump raised the
        general-list ceiling to 200,000 so the history tab can pull
        the full archive in one shot."""
        html = _read()
        # The legacy endpoint string must still appear in the page's
        # JavaScript (not just in test fixtures). The hard-coded limit
        # was bumped from 10000 to 200000 in lock-step with the
        # backend cap raise.
        self.assertIn(
            "/api/defectives?status=COMPLETED&limit=200000",
            html,
            "history tab must still call the legacy single-shot endpoint",
        )
        # And the OLD value must no longer be present — the front-end
        # was updated in lock-step with the backend cap.
        self.assertNotIn(
            "/api/defectives?status=COMPLETED&limit=10000",
            html,
            "history tab hard-coded limit must be 200000, not the old 10000",
        )

    def test_history_alias_normalised(self):
        """``loadList('HISTORY')`` must be normalised to COMPLETED so
        callers that do ``tab.toUpperCase()`` don't end up requesting
        ``status=HISTORY`` and getting a 422."""
        html = _read()
        self.assertRegex(
            html,
            r"const\s+normalized\s*=\s*status\s*===\s*['\"]HISTORY['\"]\s*\?\s*['\"]COMPLETED['\"]",
            "loadList must normalise HISTORY -> COMPLETED",
        )


# ---------------------------------------------------------------------------
# loadCounts / tab badges — wired to /_/count
# ---------------------------------------------------------------------------

class LoadCountsWiringTests(unittest.TestCase):
    """The tab badges use ``/_/count`` so the counts reflect the entire
    database, not just the items on the current page."""

    def test_load_counts_uses_count_endpoint(self):
        html = _read()
        self.assertIn("/api/defectives/_/count", html)

    def test_load_counts_falls_back_to_local_tally(self):
        """If ``/_/count`` ever fails (network, deploy), the front-end
        must still show reasonable counts rather than go blank."""
        html = _read()
        # Find the loadCounts method body and assert it has BOTH a
        # try/catch around the /count fetch and a catch-branch that
        # populates ``this.counts`` from a fallback tally. The body
        # contains nested braces (the try/catch blocks) so we can't
        # rely on a naive ``\\n\\s*\\}`` terminator — instead we slice
        # from ``async loadCounts()`` to the next method (which starts
        # with the same indentation + a different name).
        m = re.search(
            r"async\s+loadCounts\(\)\s*\{(.+?)\n    async\s+\w+\(",
            html,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(m, "loadCounts method not found")
        body = m.group(1)
        self.assertIn("try", body, "loadCounts must wrap /count in try")
        self.assertIn("catch", body, "loadCounts must have a catch branch")
        self.assertIn("/api/defectives/_/count", body, "fallback must still try /count first")
        # Catch branch must populate counts from a local tally, not just swallow.
        catch_m = re.search(r"catch\s*\(\s*\w+\s*\)\s*\{(.+?)\n\s*\}", body, flags=re.DOTALL)
        self.assertIsNotNone(catch_m, "loadCounts catch must have a body")
        catch_body = catch_m.group(1)
        self.assertIn("this.counts", catch_body, "catch must populate this.counts")
        self.assertIn("it.status", catch_body, "catch must tally from items")


# ---------------------------------------------------------------------------
# loadList + per-tab loaders — must hit the new paginated endpoints
# ---------------------------------------------------------------------------

class LoadListRoutingTests(unittest.TestCase):
    """``loadList('READY')`` must hit ``/_/ready`` and
    ``loadList('PENDING')`` must hit ``/_/pending``. The legacy
    endpoint stays for COMPLETED."""

    def test_load_ready_hits_new_endpoint(self):
        html = _read()
        self.assertIn("/api/defectives/_/ready?", html)
        # And the offset/page_size params are forwarded.
        self.assertIn("page_size=", html)
        self.assertIn("offset=", html)

    def test_load_pending_hits_new_endpoint(self):
        html = _read()
        self.assertIn("/api/defectives/_/pending?", html)

    def test_per_tab_loaders_exist(self):
        html = _read()
        self.assertIn("_loadReadyPage", html)
        self.assertIn("_loadPendingPage", html)

    def test_per_tab_loaders_send_offset(self):
        """The offset forwarded to the server must be derived from
        ``(page - 1) * pageSize`` — not from a single shared offset —
        so each tab keeps its own scroll position."""
        html = _read()
        # Find _loadReadyPage and assert it computes the offset from
        # readyPage / readyPageSize, not from a shared field.
        m = re.search(r"async\s+_loadReadyPage\(\)[^{]*\{(.+?)\n\s*\}", html, flags=re.DOTALL)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertIn("readyPage", body)
        self.assertIn("readyPageSize", body)
        self.assertRegex(body, r"\(this\.readyPage\s*-\s*1\)\s*\*\s*size")

        m = re.search(r"async\s+_loadPendingPage\(\)[^{]*\{(.+?)\n\s*\}", html, flags=re.DOTALL)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertIn("pendingPage", body)
        self.assertIn("pendingPageSize", body)
        self.assertRegex(body, r"\(this\.pendingPage\s*-\s*1\)\s*\*\s*size")


# ---------------------------------------------------------------------------
# Module-import smoke test
# ---------------------------------------------------------------------------

class AppImportTests(unittest.TestCase):
    """Loading the app package must NOT raise — a Python-side regression
    in the pagination refactor would surface here as an ImportError."""

    def test_app_imports_clean(self):
        # Importing the FastAPI app triggers the same code paths used
        # by the production server (router registration, lifespan, etc.).
        from app.main import app  # noqa: F401
        self.assertIsNotNone(app)


if __name__ == "__main__":
    unittest.main()