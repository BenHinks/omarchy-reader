import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from theme import Theme


class ThemeTests(unittest.TestCase):
    def test_loads_active_omarchy_colors_and_typography(self):
        with tempfile.TemporaryDirectory() as directory:
            state_home = Path(directory)
            theme_dir = state_home / "omarchy/current/theme"
            theme_dir.mkdir(parents=True)
            (theme_dir / "colors.toml").write_text(
                '\n'.join((
                    'mode = "light"',
                    'background = "#111111"',
                    'lighter_background = "#222222"',
                    'foreground = "#eeeeee"',
                    'dark_foreground = "#999999"',
                    'accent = "#abcdef"',
                    'selection = "#334455"',
                    'red = "#cc3344"',
                )),
                encoding="utf-8",
            )
            (theme_dir / "shell.toml").write_text(
                '[font]\nbase-size = 15\n', encoding="utf-8"
            )
            font_match = Mock(stdout="Omarchy Mono,Regular")
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(state_home)}), patch(
                "theme.subprocess.run", return_value=font_match
            ):
                theme = Theme.load()

        self.assertEqual(theme.mode, "light")
        self.assertEqual(theme.surface, "#222222")
        self.assertEqual(theme.danger, "#cc3344")
        self.assertEqual(theme.font_size, 15)
        self.assertEqual(theme.font_family, "Omarchy Mono")
        self.assertIn("outline: 2px solid #abcdef", theme.gtk_css())
        self.assertIn("button.suggested-action:focus", theme.gtk_css())
        self.assertIn("outline-color: #eeeeee", theme.gtk_css())
        self.assertIn(".source-switcher button:checked", theme.gtk_css())
        self.assertIn("box-shadow: inset 0 -2px #abcdef", theme.gtk_css())
        self.assertIn("row:hover { background: alpha(#eeeeee, .04)", theme.gtk_css())
        self.assertIn("row:focus, row:focus-within", theme.gtk_css())
        self.assertIn("background: #334455", theme.gtk_css())
        self.assertIn(".library-open-icon, .opds-open-icon", theme.gtk_css())
        self.assertIn("button.guide-library-link", theme.gtk_css())
        self.assertIn("font-size: 15px", theme.gtk_css())

    def test_font_lookup_failure_uses_safe_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            state_home = Path(directory)
            theme_dir = state_home / "omarchy/current/theme"
            theme_dir.mkdir(parents=True)
            (theme_dir / "colors.toml").write_text(
                'background = "#111111"\n', encoding="utf-8"
            )
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(state_home)}), patch(
                "theme.subprocess.run", side_effect=subprocess.TimeoutExpired("fc-match", 2)
            ):
                theme = Theme.load()

        self.assertEqual(theme, Theme())


if __name__ == "__main__":
    unittest.main()
