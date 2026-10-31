from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent

class V015UiTests(unittest.TestCase):
    def test_warning_action_is_under_each_account_ban_action(self):
        admin = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
        self.assertIn('class="compact-ban-action"', admin)
        self.assertIn('class="compact-warning-action"', admin)
        self.assertLess(admin.index('class="compact-ban-action"'), admin.index('class="compact-warning-action"'))
        self.assertNotIn('integrated-warning-manager', admin)

    def test_preview_theme_controls_are_scoped_to_preview(self):
        edit = (ROOT / "templates" / "edit.html").read_text(encoding="utf-8")
        css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn('data-preview-mode="light"', edit)
        self.assertIn('data-preview-mode="dark"', edit)
        self.assertIn('preview.dataset.previewTheme = mode', edit)
        self.assertIn('.preview-theme[data-preview-theme="dark"]', css)

    def test_theme_pair_color_styles_are_defined(self):
        css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn('.wiki-theme-color{color:var(--nm-light-color)}', css)
        self.assertIn('.preview-theme[data-preview-theme="dark"] .wiki-theme-color', css)

if __name__ == "__main__":
    unittest.main()
