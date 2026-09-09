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

    mode: str = "dark"
    background: str = "#101418"
    surface: str = "#1b2229"
    foreground: str = "#e6edf3"
    muted: str = "#8b949e"
    accent: str = "#58a6ff"
    selection: str = "#1f4f7a"
    danger: str = "#f85149"
    font_size: int = 12
    font_family: str = "monospace"

    @staticmethod
    def active_theme_dir() -> Path:
        """Return Omarchy's materialized active-theme directory."""
        state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        return state_home / "omarchy/current/theme"

    @classmethod
    def load(cls) -> "Theme":
        """Load Omarchy's current theme, falling back safely when unavailable."""
        theme_dir = cls.active_theme_dir()
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
            mode = str(colors.get("mode", cls.mode)).lower()
            return cls(
                mode=mode if mode in ("dark", "light") else cls.mode,
                background=colors.get("background", cls.background),
                surface=colors.get("lighter_background", cls.surface),
                foreground=colors.get("foreground", cls.foreground),
                muted=colors.get("dark_foreground", colors.get("muted", cls.muted)),
                accent=colors.get("accent", cls.accent),
                selection=colors.get("selection", cls.selection),
                danger=colors.get("red", cls.danger),
                font_size=int(shell.get("font", {}).get("base-size", cls.font_size)),
                font_family=font_family,
            )
        except (
            OSError,
            subprocess.SubprocessError,
            tomllib.TOMLDecodeError,
            TypeError,
            ValueError,
        ):
            return cls()

    def gtk_css(self) -> str:
        """Render the resolved theme as a GTK application stylesheet."""
        font_family = self.font_family.replace("\\", "\\\\").replace('"', '\\"')
        return f"""
        * {{ font-family: "{font_family}"; font-size: {self.font_size}px; }}
        window, .background, dialog, popover, preferencespage, statuspage {{
            background: {self.background};
            color: {self.foreground};
        }}
        headerbar {{ background: {self.background}; color: {self.foreground};
                     border-bottom: 1px solid alpha({self.accent}, .45); }}
        button {{
            background: alpha({self.foreground}, .04);
            color: {self.foreground};
            border-color: alpha({self.foreground}, .40);
        }}
        button.flat {{ background: transparent; border-color: transparent; }}
        button:hover, button.flat:hover {{
            background: alpha({self.foreground}, .08);
            border-color: alpha({self.foreground}, .25);
        }}
        button:active {{ background: alpha({self.foreground}, .22); }}
        button:focus {{
            background: alpha({self.foreground}, .08);
            color: {self.foreground};
            border-color: {self.accent};
            outline: 2px solid {self.accent};
            outline-offset: -2px;
        }}
        button:disabled {{ color: alpha({self.foreground}, .38); }}
        button.suggested-action {{ background: {self.accent}; color: {self.background}; }}
        button.suggested-action:focus {{
            background: {self.accent};
            color: {self.background};
            border-color: {self.foreground};
            outline-color: {self.foreground};
            outline-offset: 2px;
        }}
        button.destructive-action {{ background: {self.danger}; color: {self.background}; }}
        .source-switcher button:checked {{
            background: {self.selection};
            color: {self.accent};
            border-color: alpha({self.accent}, .65);
            box-shadow: inset 0 -2px {self.accent};
        }}
        .source-switcher button:checked:hover {{
            background: {self.selection};
            color: {self.accent};
        }}
        entry, searchentry, passwordentry {{
            background: {self.surface};
            color: {self.foreground};
            border-color: alpha({self.foreground}, .40);
        }}
        entry:focus, searchentry:focus, passwordentry:focus {{ border-color: {self.accent}; }}
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
        .shortcut-key {{
            background: {self.surface};
            color: {self.foreground};
            border: 1px solid alpha({self.foreground}, .32);
            border-radius: 6px;
            padding: 4px 8px;
        }}
        .guide-bar {{
            min-height: 28px;
            background: {self.background};
            border-top: 1px solid alpha({self.accent}, .45);
        }}
        button.guide-link {{
            min-height: 0;
            padding: 2px 8px;
            color: {self.muted};
            font-size: .82em;
        }}
        button.guide-link:hover, button.guide-link:focus {{
            background: {self.selection};
            color: {self.foreground};
        }}
        button.guide-library-link {{
            color: {self.accent};
            padding: 4px 8px;
        }}
        button.guide-library-link:hover, button.guide-library-link:focus {{
            background: {self.selection};
            color: {self.foreground};
        }}
        .library-open-icon, .opds-open-icon, button.library-delete image {{
            color: {self.muted};
            opacity: .45;
        }}
        row:focus .library-open-icon, row:focus .opds-open-icon,
        button.library-delete:focus image {{
            color: {self.foreground};
            opacity: 1;
        }}
        .dim-label {{ color: {self.muted}; }}
        row {{ color: {self.foreground}; border-bottom: 1px solid alpha({self.foreground}, .10); }}
        row:hover {{ background: alpha({self.foreground}, .04); }}
        row:focus, row:focus-within {{
            background: {self.selection};
            color: {self.foreground};
            border-color: {self.accent};
            outline: 2px solid {self.accent};
            outline-offset: -2px;
        }}
        scale trough {{ background: alpha({self.foreground}, .12); }}
        scale trough highlight {{ background: {self.accent}; }}
        scrollbar slider {{ background: alpha({self.foreground}, .28); }}
        tooltip {{
            background: {self.background};
            color: {self.foreground};
            border: 1px solid {self.accent};
        }}
        selection {{ background: {self.selection}; color: {self.foreground}; }}
        """
