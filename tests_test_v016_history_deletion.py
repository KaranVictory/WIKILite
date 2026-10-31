from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent

class V016HistoryDeletionTests(unittest.TestCase):
    def test_random_menu_uses_korean_random_page_label(self):
        base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
        self.assertIn(">랜덤 문서</a>", base)
        self.assertNotIn(">임의 문서</a>", base)

    def test_recent_changes_has_delete_event_display(self):
        recent = (ROOT / "templates" / "recent.html").read_text(encoding="utf-8")
        self.assertIn("row['event_type'] == 'delete'", recent)
        self.assertIn("문서 삭제", (ROOT / "app.py").read_text(encoding="utf-8"))
        self.assertIn("url_for('history', title=row['title'])", recent)

    def test_history_displays_archived_revisions_and_deletion_events(self):
        history = (ROOT / "templates" / "history.html").read_text(encoding="utf-8")
        app = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("deleted_page_revisions", app)
        self.assertIn("page_deletion_events", app)
        self.assertIn("보존 기록", history)
        self.assertIn("삭제", history)

    def test_backup_tables_include_deleted_history(self):
        app = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn('"deleted_page_revisions", "page_deletion_events"', app)

if __name__ == "__main__":
    unittest.main()
