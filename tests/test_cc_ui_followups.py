"""Regression guards for Cc's 2026-08-14 UI/account follow-ups."""
from pathlib import Path
import inspect
import unittest

from app.routers import users


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")


class BottomPagerTests(unittest.TestCase):
    def test_single_pager_is_after_desktop_and_mobile_results(self):
        self.assertEqual(HTML.count('class="pager"'), 1)
        pager = HTML.index('class="pager"')
        self.assertGreater(pager, HTML.index('class="workbench-wrap desktop-only"'))
        self.assertGreater(pager, HTML.index('class="mobile-list"'))

    def test_pager_has_requested_controls(self):
        for label in ("首页", "上一页", "下一页", "末页"):
            self.assertIn(label, HTML)
        self.assertIn('class="pager-size"', HTML)
        self.assertIn("currentPage + ' / ' + totalPageCount", HTML)


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
