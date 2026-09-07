"""Translate the active Omarchy theme into application-facing GTK CSS."""

from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Theme:
    """Resolved colors and typography, with usable non-Omarchy defaults."""

    background: str = "#101418"
    surface: str = "#1b2229"
    foreground: str = "#e6edf3"
    muted: str = "#8b949e"
    accent: str = "#58a6ff"
    selection: str = "#1f4f7a"
    font_size: int = 12
    font_family: str = "monospace"

    @classmethod
    def load(cls) -> "Theme":
        """Load Omarchy's current theme, falling back safely when unavailable."""
        state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        theme_dir = state_home / "omarchy/current/theme"
        try:
            with (theme_dir / "colors.toml").open("rb") as stream:
                colors = tomllib.load(stream)
            shell = {}
            if (theme_dir / "shell.toml").exists():
                with (theme_dir / "shell.toml").open("rb") as stream:
                    shell = tomllib.load(stream)
            font_family = subprocess.run(
                ["fc-match", "monospace", "--format=%{family}"],
                check=False, capture_output=True, text=True, timeout=2,
            ).stdout.split(",", 1)[0].strip() or cls.font_family
            return cls(
                background=colors.get("background", cls.background),
                surface=colors.get("lighter_background", cls.surface),
                foreground=colors.get("foreground", cls.foreground),
                muted=colors.get("dark_foreground", colors.get("muted", cls.muted)),
                accent=colors.get("accent", cls.accent),
                selection=colors.get("selection", cls.selection),
                font_size=int(shell.get("font", {}).get("base-size", cls.font_size)),
                font_family=font_family,
            )
        except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError):
            return cls()

    def gtk_css(self) -> str:
        """Render the resolved theme as a GTK application stylesheet."""
        font_family = self.font_family.replace("\\", "\\\\").replace('"', '\\"')
        return f"""
        * {{ font-family: "{font_family}"; }}
        window, .background {{ background: {self.background}; color: {self.foreground}; }}
        headerbar {{ background: {self.background}; color: {self.foreground};
                     border-bottom: 1px solid alpha({self.accent}, .45); }}
        button {{ color: {self.foreground}; }}
        button.suggested-action {{ background: {self.accent}; color: {self.background}; }}
        button.opds-download {{
            background: {self.surface};
            border: 1px solid alpha({self.foreground}, .22);
            border-radius: 6px;
            min-width: 0;
            min-height: 0;
            padding: 2px 4px;
        }}
        button.opds-download:hover {{
            background: alpha({self.foreground}, .14);
            border-color: alpha({self.foreground}, .38);
        }}
        button.opds-download:focus {{
            background: {self.selection};
            border-color: {self.accent};
            outline: 1px solid {self.accent};
            outline-offset: 1px;
        }}
        .opds-book-title {{ color: {self.foreground}; }}
        .opds-download-label {{ font-size: .72em; }}
        .dim-label {{ color: {self.muted}; }}
        row {{ border-bottom: 1px solid alpha({self.foreground}, .10); }}
        selection {{ background: {self.selection}; color: {self.foreground}; }}
        """
