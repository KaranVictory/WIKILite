from pathlib import Path
import sqlite3
import unittest

ROOT = Path(__file__).resolve().parent


class V014UiContractTests(unittest.TestCase):
    def test_theme_switch_is_available_without_login_gate(self):
        base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
        css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn('id="theme-toggle"', base)
        self.assertIn("localStorage.setItem('namulite-theme', next)", base)
        self.assertIn('html[data-theme="dark"] body', css)
        self.assertNotIn("prefers-color-scheme", css)

    def test_warning_banner_removed_and_login_modal_is_one_shot(self):
        base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
        app = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("show_login_warning_modal and unread_warnings", base)
        self.assertNotIn("warning-inbox-banner", base)
        self.assertIn('session["show_warning_modal_after_login"] = True', app)
        self.assertIn('session.pop("show_warning_modal_after_login", False)', app)

    def test_warning_management_is_combined_with_user_management(self):
        admin = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
        app = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("compact-warning-action", admin)
        self.assertNotIn("integrated-warning-manager", admin)
        self.assertIn("user['warning_count']", admin)
        self.assertIn("user-warning-history", admin)
        self.assertNotIn("tab='warnings'", admin)
        self.assertIn('if tab == "warnings":', app)

    def test_account_ban_editor_is_collapsed(self):
        admin = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
        self.assertIn('<details class="compact-ban-action">', admin)
        self.assertIn('차단 적용', admin)

    def test_warning_count_query_returns_total_per_user(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        try:
            db.execute("CREATE TABLE user_warnings (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)")
            db.executemany("INSERT INTO user_warnings(user_id) VALUES (?)", [(1,), (1,), (2,)])
            counts = {
                row["user_id"]: row["warning_count"]
                for row in db.execute(
                    "SELECT user_id, COUNT(*) AS warning_count FROM user_warnings GROUP BY user_id"
                ).fetchall()
            }
            self.assertEqual(counts, {1: 2, 2: 1})
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
