#!/usr/bin/env python3
"""GTK/libadwaita front end and application entry point for Omarchy Reader.

The window coordinates the local library, native PDF/comic renderers, the
WebKit/foliate-js ebook view, background OPDS work, and system-keyring access.
"""
from __future__ import annotations

import json
import html
import mimetypes
import os
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
gi.require_version("Poppler", "0.18")
gi.require_version("Secret", "1")
gi.require_version("WebKit", "6.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Poppler, Secret, WebKit  # noqa: E402

from document import DOCUMENT_EXTENSIONS, InvalidDocument, read_document
from library import Book, Catalog, InvalidBook, InvalidComic, Library, list_comic_pages, read_comic_page
from opds import (
    AZW3_MIME, CBZ_MIME_ALIASES, FB2_MIME, FBZ_MIME, HTML_MIME,
    MARKDOWN_MIME_ALIASES, MOBI_MIME, PDF_MIME, TXT_MIME,
    OpdsAcquisition, OpdsEntry, OpdsError, download_book, fetch_complete_feed,
)
from theme import Theme


APP_ID = "org.omarchy.Reader"
PROJECT_DIR = Path(__file__).resolve().parent.parent
SECRET_SCHEMA = Secret.Schema.new(
    "org.omarchy.Reader.Catalog",
    Secret.SchemaFlags.NONE,
    {"catalog-id": Secret.SchemaAttributeType.STRING},
)


def format_file_size(size: int | None) -> str:
    """Format an optional byte count for compact catalog controls."""
    if size is None:
        return "Size unknown"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1000 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1000
    return f"{size} B"


def format_name(media_type: str) -> str:
    """Map a supported acquisition MIME type to its reader-facing name."""
    if media_type == PDF_MIME:
        return "PDF"
    if media_type in CBZ_MIME_ALIASES:
        return "CBZ"
    if media_type == MOBI_MIME:
        return "MOBI"
    if media_type == AZW3_MIME:
        return "AZW3"
    if media_type == FB2_MIME:
        return "FB2"
    if media_type == FBZ_MIME:
        return "FBZ"
    if media_type == TXT_MIME:
        return "TXT"
    if media_type in MARKDOWN_MIME_ALIASES:
        return "Markdown"
    if media_type == HTML_MIME:
        return "HTML"
    return "EPUB"


def local_format_name(book: Book) -> str:
    """Return the display name for an already imported book's format."""
    if book.format in ("md", "markdown"):
        return "Markdown"
    if book.format in ("html", "htm"):
        return "HTML"
    return book.format.upper()


class ReaderScheme:
    """Serve trusted app assets and managed books from one WebKit origin."""

    def __init__(self, web_root: Path, books_root: Path):
        self.web_root = web_root.resolve()
        self.books_root = books_root.resolve()

    def register(self, context: WebKit.WebContext) -> None:
        context.register_uri_scheme("reader", self._handle)
        security = context.get_security_manager()
        security.register_uri_scheme_as_local("reader")
        security.register_uri_scheme_as_secure("reader")
        security.register_uri_scheme_as_cors_enabled("reader")

    def _handle(self, request: WebKit.URISchemeRequest) -> None:
        relative = unquote(urlsplit(request.get_uri()).path).lstrip("/")
        if relative.startswith("books/"):
            candidate = (self.books_root / relative.removeprefix("books/")).resolve()
            allowed_root = self.books_root
        else:
            candidate = (self.web_root / relative).resolve()
            allowed_root = self.web_root

        if not candidate.is_relative_to(allowed_root) or not candidate.is_file():
            request.finish(Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(b"Not found")), 9, "text/plain")
            return

        stream = Gio.File.new_for_path(str(candidate)).read(None)
        size = candidate.stat().st_size
        content_type = {
            ".js": "text/javascript",
            ".mjs": "text/javascript",
            ".epub": "application/epub+zip",
            ".mobi": MOBI_MIME,
            ".azw3": AZW3_MIME,
            ".fb2": FB2_MIME,
            ".fbz": FBZ_MIME,
        }.get(candidate.suffix.lower(), mimetypes.guess_type(candidate.name)[0] or "application/octet-stream")
        request.finish(stream, size, content_type)


class ReaderWindow(Adw.ApplicationWindow):
    """Main library window and controller for every reading surface."""

    def __init__(self, app: Adw.Application, library: Library):
        super().__init__(application=app, title="Reader", default_width=1050, default_height=760)
        self.library = library
        self.current_book: Book | None = None
        self.current_catalog: Catalog | None = None
        self.catalog_history: list[str] = []
        self.theme = Theme.load()
        self.theme_provider: Gtk.CssProvider | None = None
        self.theme_reload_source: int | None = None
        self.theme_monitors: list[Gio.FileMonitor] = []
        self.reader_scheme = ReaderScheme(PROJECT_DIR / "src/web", library.books_dir)
        self.reader_scheme.register(WebKit.WebContext.get_default())
        self._apply_theme()

        # Persistent shell shared by the library and all three reader views.
        self.header = Adw.HeaderBar()
        self.back_button = Gtk.Button(icon_name="go-previous-symbolic", visible=False)
        self.back_button.set_tooltip_text("Back")
        self.back_button.connect("clicked", self._go_back)
        self.header.pack_start(self.back_button)

        self.add_button = Gtk.Button(label="Add Book")
        self.add_button.add_css_class("suggested-action")
        self.add_button.connect("clicked", self._show_add_book_dialog)
        self.header.pack_end(self.add_button)

        self.stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True,
        )
        self.empty_page = self._build_empty_page()
        self.books_page, self.books_list = self._build_books_page()
        self.epub_reader_page = self._build_epub_reader_page()
        self.pdf_reader_page = self._build_pdf_reader_page()
        self.comic_reader_page = self._build_comic_reader_page()
        self.stack.add_named(self.empty_page, "empty")
        self.stack.add_named(self.books_page, "books")
        self.stack.add_named(self.epub_reader_page, "epub-reader")
        self.stack.add_named(self.pdf_reader_page, "pdf-reader")
        self.stack.add_named(self.comic_reader_page, "comic-reader")

        self.guide_button = Gtk.Button(label="Guide (F1 or Ctrl-G)")
        self.guide_button.add_css_class("flat")
        self.guide_button.add_css_class("guide-link")
        self.guide_button.set_tooltip_text("Open the keyboard shortcuts and user guide")
        self.guide_button.connect("clicked", self._show_keyboard_help)
        self.guide_bar = Gtk.CenterBox()
        self.guide_bar.add_css_class("guide-bar")
        self.guide_bar.set_center_widget(self.guide_button)

        window_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        window_content.append(self.stack)
        window_content.append(self.guide_bar)

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(self.header)
        toolbar_view.set_content(window_content)
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._window_key_pressed)
        toolbar_view.add_controller(keys)
        self.set_content(toolbar_view)
        self._watch_active_theme()
        self._refresh_library()
        GLib.idle_add(self._focus_library_book, None)

    def _apply_theme(self) -> None:
        if self.theme_provider is None:
            self.theme_provider = Gtk.CssProvider()
            Gtk.StyleContext.add_provider_for_display(
                self.get_display(), self.theme_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        self.theme_provider.load_from_string(self.theme.gtk_css())
        style_manager = Adw.StyleManager.get_default()
        style_manager.set_color_scheme(
            Adw.ColorScheme.FORCE_LIGHT if self.theme.mode == "light"
            else Adw.ColorScheme.FORCE_DARK
        )

    def _watch_active_theme(self) -> None:
        """Reload application styling when Omarchy materializes a new theme."""
        paths = (Theme.active_theme_dir(), Theme.active_theme_dir().parent)
        for path in paths:
            try:
                monitor = Gio.File.new_for_path(str(path)).monitor_directory(
                    Gio.FileMonitorFlags.WATCH_MOVES, None
                )
            except GLib.Error:
                continue
            monitor.connect("changed", self._active_theme_changed)
            self.theme_monitors.append(monitor)

    def _active_theme_changed(self, *_args) -> None:
        if self.theme_reload_source is not None:
            GLib.source_remove(self.theme_reload_source)
        self.theme_reload_source = GLib.timeout_add(250, self._reload_active_theme)

    def _reload_active_theme(self) -> bool:
        self.theme_reload_source = None
        theme = Theme.load()
        if theme == self.theme:
            return GLib.SOURCE_REMOVE
        self.theme = theme
        self._apply_theme()
        self.web_view.set_background_color(self._rgba(self.theme.background))
        self.pdf_canvas.queue_draw()

        if self.current_book and self.stack.get_visible_child_name() == "epub-reader":
            current = next(
                (book for book in self.library.list_books() if book.id == self.current_book.id),
                self.current_book,
            )
            self.current_book = current
            if f".{current.format}" in DOCUMENT_EXTENSIONS:
                self._open_document(current)
            else:
                self._open_epub(current)
        return GLib.SOURCE_REMOVE

    def _build_empty_page(self) -> Gtk.Widget:
        page = Adw.StatusPage(
            icon_name="accessories-text-editor-symbolic",
            title="Your library is empty",
            description="Add a book from a file or an OPDS catalog.",
        )
        add_button = Gtk.Button(label="Add Book", halign=Gtk.Align.CENTER)
        add_button.add_css_class("suggested-action")
        add_button.connect("clicked", self._show_add_book_dialog)
        page.set_child(add_button)
        return page

    def _build_books_page(self) -> tuple[Gtk.Widget, Gtk.ListBox]:
        books = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        books.add_css_class("boxed-list")
        books.connect("row-activated", lambda _list, row: self._open_book(row.book))
        clamp = Adw.Clamp(maximum_size=760, tightening_threshold=560, margin_top=24,
                          margin_bottom=24, margin_start=18, margin_end=18)
        clamp.set_child(books)
        scroll = Gtk.ScrolledWindow(child=clamp)
        return scroll, books

    def _build_epub_reader_page(self) -> Gtk.Widget:
        """Create the WebKit bridge used for EPUB, MOBI/AZW3, and FB2."""
        manager = WebKit.UserContentManager()
        manager.register_script_message_handler("reader")
        manager.connect("script-message-received::reader", self._reader_message)
        view = WebKit.WebView(user_content_manager=manager)
        view.set_background_color(self._rgba(self.theme.background))
        view.get_settings().set_enable_javascript(True)
        view.get_settings().set_allow_file_access_from_file_urls(True)
        view.connect("load-failed", self._reader_load_failed)
        self.web_view = view
        return view

    def _build_pdf_reader_page(self) -> Gtk.Widget:
        """Build a native Poppler canvas with page and keyboard navigation."""
        self.pdf_document: Poppler.Document | None = None
        self.pdf_page_index = 0
        self.pdf_page_count = 0
        self.pdf_updating_controls = False

        self.pdf_canvas = Gtk.DrawingArea(hexpand=True, vexpand=True, focusable=True)
        self.pdf_canvas.set_draw_func(self._draw_pdf_page)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._pdf_key_pressed)
        self.pdf_canvas.add_controller(keys)

        self.pdf_previous = Gtk.Button(icon_name="go-previous-symbolic")
        self.pdf_previous.set_tooltip_text("Previous page")
        self.pdf_previous.connect("clicked", lambda *_args: self._show_pdf_page(self.pdf_page_index - 1))
        self.pdf_next = Gtk.Button(icon_name="go-next-symbolic")
        self.pdf_next.set_tooltip_text("Next page")
        self.pdf_next.connect("clicked", lambda *_args: self._show_pdf_page(self.pdf_page_index + 1))
        self.pdf_position = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 2, 1)
        self.pdf_position.set_draw_value(False)
        self.pdf_position.set_hexpand(True)
        self.pdf_position.connect("value-changed", self._pdf_position_changed)
        self.pdf_page_label = Gtk.Label(label="Page 1 of 1")

        navigation = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
            margin_top=8, margin_bottom=8, margin_start=12, margin_end=12,
        )
        navigation.append(self.pdf_previous)
        navigation.append(self.pdf_position)
        navigation.append(self.pdf_page_label)
        navigation.append(self.pdf_next)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page.append(self.pdf_canvas)
        page.append(Gtk.Separator())
        page.append(navigation)
        return page

    def _build_comic_reader_page(self) -> Gtk.Widget:
        """Build the native image surface used for CBZ and ZIP comics."""
        self.comic_pages: list[str] = []
        self.comic_page_index = 0
        self.comic_updating_controls = False

        self.comic_picture = Gtk.Picture(
            hexpand=True, vexpand=True, can_shrink=True,
            content_fit=Gtk.ContentFit.CONTAIN, focusable=True,
        )
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._comic_key_pressed)
        self.comic_picture.add_controller(keys)

        self.comic_previous = Gtk.Button(icon_name="go-previous-symbolic")
        self.comic_previous.set_tooltip_text("Previous page")
        self.comic_previous.connect(
            "clicked", lambda *_args: self._show_comic_page(self.comic_page_index - 1)
        )
        self.comic_next = Gtk.Button(icon_name="go-next-symbolic")
        self.comic_next.set_tooltip_text("Next page")
        self.comic_next.connect(
            "clicked", lambda *_args: self._show_comic_page(self.comic_page_index + 1)
        )
        self.comic_position = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 2, 1)
        self.comic_position.set_draw_value(False)
        self.comic_position.set_hexpand(True)
        self.comic_position.connect("value-changed", self._comic_position_changed)
        self.comic_page_label = Gtk.Label(label="Page 1 of 1")

        navigation = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
            margin_top=8, margin_bottom=8, margin_start=12, margin_end=12,
        )
        navigation.append(self.comic_previous)
        navigation.append(self.comic_position)
        navigation.append(self.comic_page_label)
        navigation.append(self.comic_next)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page.append(self.comic_picture)
        page.append(Gtk.Separator())
        page.append(navigation)
        return page

    @staticmethod
    def _rgba(value: str):
        from gi.repository import Gdk
        color = Gdk.RGBA()
        color.parse(value)
        return color

    @staticmethod
    def _has_modifier(state, modifier) -> bool:
        return bool(state & modifier)

    @staticmethod
    def _ancestor_of_type(widget: Gtk.Widget | None, widget_type):
        while widget is not None:
            if isinstance(widget, widget_type):
                return widget
            widget = widget.get_parent()
        return None

    @staticmethod
    def _descendant_buttons(widget: Gtk.Widget) -> list[Gtk.Button]:
        buttons = []
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Button) and child.get_visible() and child.get_sensitive():
                buttons.append(child)
            buttons.extend(ReaderWindow._descendant_buttons(child))
            child = child.get_next_sibling()
        return buttons

    def _row_focus_target(self, row: Gtk.ListBoxRow, option: int = 0) -> Gtk.Widget:
        if getattr(row, "opds_is_book", False):
            buttons = self._descendant_buttons(row)
            if buttons:
                return buttons[min(option, len(buttons) - 1)]
        return row

    @staticmethod
    def _visible_rows(list_box: Gtk.ListBox) -> list[Gtk.ListBoxRow]:
        rows = []
        child = list_box.get_first_child()
        while child is not None:
            if (
                isinstance(child, Gtk.ListBoxRow)
                and child.get_visible()
                and child.get_child_visible()
                and child.get_sensitive()
            ):
                rows.append(child)
            child = child.get_next_sibling()
        return rows

    def _focus_first_row(self, list_box: Gtk.ListBox) -> bool:
        rows = self._visible_rows(list_box)
        if rows:
            self._row_focus_target(rows[0]).grab_focus()
        return GLib.SOURCE_REMOVE

    def _focus_library_book(self, book_id: int | None) -> bool:
        rows = self._visible_rows(self.books_list)
        target = next(
            (row for row in rows if getattr(getattr(row, "book", None), "id", None) == book_id),
            rows[0] if rows else None,
        )
        if target is not None:
            self._row_focus_target(target).grab_focus()
        elif self.stack.get_visible_child_name() == "empty":
            self.add_button.grab_focus()
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _focus_widget(widget: Gtk.Widget) -> bool:
        widget.grab_focus()
        return GLib.SOURCE_REMOVE

    def _move_list_focus(self, direction: int) -> bool:
        focus = self.get_focus()
        row = self._ancestor_of_type(focus, Gtk.ListBoxRow)
        list_box = self._ancestor_of_type(focus, Gtk.ListBox)
        if row is None or list_box is None:
            return False
        rows = self._visible_rows(list_box)
        if row not in rows:
            return False
        option = 0
        if getattr(row, "opds_is_book", False) and isinstance(focus, Gtk.Button):
            buttons = self._descendant_buttons(row)
            if focus in buttons:
                option = buttons.index(focus)
        target_index = min(len(rows) - 1, max(0, rows.index(row) + direction))
        self._row_focus_target(rows[target_index], option).grab_focus()
        return True

    def _move_library_focus(self, direction: int) -> bool:
        """Move through Add Book, library rows, and the bottom Guide link."""
        focus = self.get_focus()
        rows = self._visible_rows(self.books_list)
        if focus is self.add_button:
            if direction > 0:
                target = self._row_focus_target(rows[0]) if rows else self.guide_button
                target.grab_focus()
            return True
        if focus is self.guide_button:
            if direction < 0:
                target = self._row_focus_target(rows[-1]) if rows else self.add_button
                target.grab_focus()
            return True

        row = self._ancestor_of_type(focus, Gtk.ListBoxRow)
        list_box = self._ancestor_of_type(focus, Gtk.ListBox)
        if row is None or list_box is not self.books_list or row not in rows:
            return False
        index = rows.index(row) + direction
        if index < 0:
            self.add_button.grab_focus()
        elif index >= len(rows):
            self.guide_button.grab_focus()
        else:
            self._row_focus_target(rows[index]).grab_focus()
        return True

    def _move_library_action_focus(self, direction: int) -> bool:
        """Move toward Open with Right, opening it at the end of the sequence."""
        focus = self.get_focus()
        row = self._ancestor_of_type(focus, Gtk.ListBoxRow)
        if row is None or not hasattr(row, "book"):
            return False
        actions: list[Gtk.Widget] = [*self._descendant_buttons(row), row]
        if focus not in actions:
            row.grab_focus()
            return True
        if direction > 0 and focus is row:
            self._open_book(row.book)
            return True
        current = actions.index(focus)
        target = min(len(actions) - 1, max(0, current + direction))
        actions[target].grab_focus()
        return True

    def _move_format_focus(self, direction: int) -> bool:
        focus = self.get_focus()
        if not isinstance(focus, Gtk.Button):
            return False
        row = self._ancestor_of_type(focus, Gtk.ListBoxRow)
        if row is None or not getattr(row, "opds_is_book", False):
            return False
        buttons = self._descendant_buttons(row)
        if focus not in buttons:
            return False
        target = min(len(buttons) - 1, max(0, buttons.index(focus) + direction))
        buttons[target].grab_focus()
        return True

    def _handle_opds_horizontal(self, direction: int) -> bool:
        """Move within an OPDS row, opening a focused hierarchy arrow with Right."""
        focus = self.get_focus()
        row = self._ancestor_of_type(focus, Gtk.ListBoxRow)
        list_box = self._ancestor_of_type(focus, Gtk.ListBox)
        if row is None or list_box is not self.opds_list:
            return False
        if getattr(row, "opds_is_book", False):
            self._move_format_focus(direction)
            return True
        if not getattr(row, "opds_opens_next", False):
            return True

        actions: list[Gtk.Widget] = [*self._descendant_buttons(row), row]
        if focus not in actions:
            row.grab_focus()
            return True
        if direction > 0 and focus is row:
            row.activate()
            return True
        current = actions.index(focus)
        target = min(len(actions) - 1, max(0, current + direction))
        actions[target].grab_focus()
        return True

    def _window_key_pressed(self, _controller, keyval, _keycode, state) -> bool:
        control = self._has_modifier(state, Gdk.ModifierType.CONTROL_MASK)
        if keyval == Gdk.KEY_F1 or (control and keyval in (Gdk.KEY_g, Gdk.KEY_G)):
            self._show_keyboard_help()
            return True
        if keyval == Gdk.KEY_Escape and self.stack.get_visible_child_name() in (
            "epub-reader", "pdf-reader", "comic-reader",
        ):
            self._show_library()
            return True

        if state & (
            Gdk.ModifierType.CONTROL_MASK
            | Gdk.ModifierType.ALT_MASK
            | Gdk.ModifierType.SUPER_MASK
        ):
            return False
        if self.stack.get_visible_child_name() not in ("empty", "books"):
            return False
        if keyval == Gdk.KEY_Up:
            return self._move_library_focus(-1)
        if keyval == Gdk.KEY_Down:
            return self._move_library_focus(1)
        if keyval == Gdk.KEY_Left:
            return self._move_library_action_focus(-1)
        if keyval == Gdk.KEY_Right:
            return self._move_library_action_focus(1)
        return False

    def _show_keyboard_help(self, *_args) -> None:
        """Show the fixed keyboard map and a concise keyboard-first guide."""
        dialog = Adw.Dialog(title="Keyboard & Help", content_width=620, content_height=620)
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=18,
            margin_top=24, margin_bottom=24, margin_start=24, margin_end=24,
        )
        intro = Gtk.Label(
            label=(
                "Omarchy Reader can be operated without a pointer. Focus is shown with "
                "the active Omarchy theme, and Escape always moves back one level."
            ),
            wrap=True, xalign=0,
        )
        intro.add_css_class("dim-label")
        content.append(intro)
        library_link = Gtk.LinkButton(
            uri=self.library.books_dir.resolve().as_uri(),
            label="Open Library in Files",
            halign=Gtk.Align.START,
        )
        library_link.add_css_class("flat")
        library_link.add_css_class("guide-library-link")
        content.append(library_link)

        sections = (
            ("Everywhere", (
                ("Open this guide", "F1  /  Ctrl+G"),
                ("Move back or close", "Escape"),
                ("Move between controls", "Tab  /  Shift+Tab"),
                ("Activate the focused item", "Enter"),
            )),
            ("Library and catalogs", (
                ("Move through Add Book, books, Guide, or feeds", "Up  /  Down"),
                ("Move back through a book's actions", "Left"),
                ("Move toward >; open it when already focused", "Right"),
                ("Switch From File and From OPDS", "Left  /  Right"),
                ("Move between download formats", "Left  /  Right"),
                ("Type to filter an open catalog", "Type in listing"),
            )),
            ("While reading", (
                ("Previous page", "Left  /  Page Up  /  Shift+Space"),
                ("Next page", "Right  /  Page Down  /  Space"),
                ("Adjust a focused progress control", "Left  /  Right"),
            )),
        )
        for title, shortcuts in sections:
            group = Adw.PreferencesGroup(title=title)
            for description, shortcut in shortcuts:
                row = Adw.ActionRow(title=description, activatable=False)
                key = Gtk.Label(label=shortcut, valign=Gtk.Align.CENTER)
                key.add_css_class("shortcut-key")
                row.add_suffix(key)
                group.add(row)
            content.append(group)

        clamp = Adw.Clamp(maximum_size=720, child=content)
        scroll = Gtk.ScrolledWindow(child=clamp, vexpand=True)
        header = Adw.HeaderBar(title_widget=Adw.WindowTitle(title="Keyboard & Help"))
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(scroll)
        dialog.set_child(toolbar)
        dialog.present(self)

    def _refresh_library(self) -> None:
        """Rebuild the library rows from persistent state."""
        while child := self.books_list.get_first_child():
            self.books_list.remove(child)
        books = self.library.list_books()
        for book in books:
            book_format = local_format_name(book)
            subtitle = f"{book.author} · {book_format}" if book.author else book_format
            row = Adw.ActionRow(title=book.title, subtitle=subtitle, activatable=True)
            row.book = book
            progress = Gtk.Label(label=f"{round(book.progress_fraction * 100)}%")
            progress.add_css_class("dim-label")
            row.add_suffix(progress)
            remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
            remove.add_css_class("flat")
            remove.add_css_class("library-delete")
            remove.set_tooltip_text(f"Remove {book.title} from the library")
            remove.connect("clicked", self._confirm_remove_book, book)
            row.add_suffix(remove)
            open_indicator = Gtk.Image(icon_name="go-next-symbolic")
            open_indicator.add_css_class("library-open-icon")
            row.add_suffix(open_indicator)
            self.books_list.append(row)
        self.stack.set_visible_child_name("books" if books else "empty")
        self.header.set_title_widget(Adw.WindowTitle(title="Library", subtitle=f"{len(books)} books"))

    @staticmethod
    def _clear_list(list_box: Gtk.ListBox) -> None:
        while child := list_box.get_first_child():
            list_box.remove(child)

    def _show_add_book_dialog(self, *_args) -> None:
        """Present local-file and OPDS import paths in one modal workflow."""
        self.add_dialog = Adw.Dialog(title="Add Book", content_width=600, content_height=520)

        self.add_pages = Adw.ViewStack(vexpand=True)
        self.add_pages.add_titled_with_icon(
            self._build_local_add_page(), "local", "From File", "folder-symbolic"
        )
        self.add_pages.add_titled_with_icon(
            self._build_opds_add_page(), "opds", "From OPDS", "web-browser-symbolic"
        )
        self.add_pages.connect("notify::visible-child-name", self._add_page_changed)

        switcher = Adw.ViewSwitcher(stack=self.add_pages, policy=Adw.ViewSwitcherPolicy.WIDE)
        switcher.add_css_class("source-switcher")
        self.opds_back_button = Gtk.Button(icon_name="go-previous-symbolic", visible=False)
        self.opds_back_button.set_tooltip_text("Back to catalogs")
        self.opds_back_button.connect("clicked", self._opds_go_back)

        dialog_header = Adw.HeaderBar(title_widget=switcher)
        dialog_header.pack_start(self.opds_back_button)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(dialog_header)
        toolbar.set_content(self.add_pages)
        escape = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        escape.connect("key-pressed", self._add_dialog_key_pressed)
        toolbar.add_controller(escape)
        self.add_dialog.set_child(toolbar)
        self._show_catalogs()
        self.add_dialog.present(self)
        GLib.idle_add(self._focus_widget, self.local_choose_button)

    def _add_dialog_key_pressed(self, _controller, keyval, _keycode, state) -> bool:
        control = self._has_modifier(state, Gdk.ModifierType.CONTROL_MASK)
        if keyval == Gdk.KEY_F1 or (control and keyval in (Gdk.KEY_g, Gdk.KEY_G)):
            self._show_keyboard_help()
            return True
        if keyval == Gdk.KEY_Escape:
            if (
                self.add_pages.get_visible_child_name() == "opds"
                and self.current_catalog is not None
            ):
                self._opds_go_back()
                return True
            self.add_dialog.close()
            return True
        if state & (
            Gdk.ModifierType.CONTROL_MASK
            | Gdk.ModifierType.ALT_MASK
            | Gdk.ModifierType.SUPER_MASK
        ):
            return False
        if (
            keyval == Gdk.KEY_Down
            and self._ancestor_of_type(self.get_focus(), Gtk.SearchEntry) is not None
        ):
            rows = self._visible_rows(self.opds_list)
            if rows:
                self._row_focus_target(rows[0]).grab_focus()
                return True
        if self._ancestor_of_type(self.get_focus(), Gtk.Editable) is not None:
            return False
        if keyval == Gdk.KEY_Up:
            return self._move_list_focus(-1)
        if keyval == Gdk.KEY_Down:
            return self._move_list_focus(1)
        if keyval == Gdk.KEY_Left:
            if self._handle_opds_horizontal(-1):
                return True
            if self.add_pages.get_visible_child_name() == "local":
                return self._switch_add_source(-1)
            return False
        if keyval == Gdk.KEY_Right:
            if self._handle_opds_horizontal(1):
                return True
            if self.add_pages.get_visible_child_name() == "local":
                return self._switch_add_source(1)
            return False
        return False

    def _switch_add_source(self, direction: int) -> bool:
        """Switch From File/From OPDS while still at the source-selection level."""
        pages = ("local", "opds")
        current = self.add_pages.get_visible_child_name()
        index = pages.index(current) if current in pages else 0
        target = min(len(pages) - 1, max(0, index + direction))
        self.add_pages.set_visible_child_name(pages[target])
        return True

    def _build_local_add_page(self) -> Gtk.Widget:
        page = Adw.StatusPage(
            icon_name="accessories-dictionary-symbolic",
            title="Choose a book",
            description="Select a supported ebook, PDF, comic, or text document.",
        )
        self.local_choose_button = Gtk.Button(label="Choose Book", halign=Gtk.Align.CENTER)
        self.local_choose_button.add_css_class("suggested-action")
        self.local_choose_button.connect("clicked", self._choose_local_book)
        page.set_child(self.local_choose_button)
        return page

    def _build_opds_add_page(self) -> Gtk.Widget:
        self.opds_title = Gtk.Label(label="Your catalogs", xalign=0)
        self.opds_title.add_css_class("title-2")
        self.opds_context = Gtk.Label(xalign=0, visible=False)
        self.opds_context.add_css_class("dim-label")
        self.opds_feed_title = ""
        self.opds_feed_truncated = False
        self.opds_book_count = 0
        self.opds_search = Gtk.SearchEntry(
            placeholder_text="Search this listing", hexpand=True, visible=False,
        )
        self.opds_search.set_tooltip_text("Filter by title, author, or format")
        self.opds_search.connect("search-changed", self._opds_search_changed)
        self.opds_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.opds_list.add_css_class("boxed-list")
        self.opds_list.set_filter_func(self._opds_filter_row)
        no_results = Gtk.Label(
            label="No matching results", margin_top=32, margin_bottom=32,
            margin_start=16, margin_end=16,
        )
        no_results.add_css_class("dim-label")
        self.opds_list.set_placeholder(no_results)
        header_clamp = Adw.Clamp(
            maximum_size=760, tightening_threshold=560,
            margin_top=24, margin_bottom=16, margin_start=18, margin_end=18,
        )
        header_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        heading.append(self.opds_title)
        heading.append(self.opds_context)
        header_content.append(heading)
        header_content.append(self.opds_search)
        header_clamp.set_child(header_content)

        list_clamp = Adw.Clamp(
            maximum_size=760, tightening_threshold=560,
            margin_bottom=24, margin_start=18, margin_end=18,
        )
        list_clamp.set_child(self.opds_list)
        scroll = Gtk.ScrolledWindow(child=list_clamp, vexpand=True)
        self.opds_search.set_key_capture_widget(scroll)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page.append(header_clamp)
        page.append(Gtk.Separator())
        page.append(scroll)
        return page

    def _opds_search_changed(self, *_args) -> None:
        self.opds_list.invalidate_filter()
        self._update_opds_context()

    def _opds_filter_row(self, row: Gtk.ListBoxRow) -> bool:
        terms = self.opds_search.get_text().casefold().split()
        searchable = getattr(row, "opds_search_text", "")
        return all(term in searchable for term in terms)

    def _update_opds_context(self) -> None:
        if not self.current_catalog:
            return
        parts = []
        if self.opds_feed_title.casefold() != self.current_catalog.name.casefold():
            parts.append(self.opds_feed_title)
        if self.opds_book_count:
            terms = self.opds_search.get_text().casefold().split()
            matching = 0
            child = self.opds_list.get_first_child()
            while child:
                if getattr(child, "opds_is_book", False) and self._opds_filter_row(child):
                    matching += 1
                child = child.get_next_sibling()
            noun = "book" if self.opds_book_count == 1 else "books"
            if terms:
                parts.append(f"{matching:,} of {self.opds_book_count:,} {noun}")
            else:
                parts.append(f"{self.opds_book_count:,} {noun}")
        if self.opds_feed_truncated:
            parts.append("Result limit reached")
        context = " · ".join(part for part in parts if part)
        self.opds_context.set_label(context)
        self.opds_context.set_visible(bool(context))

    def _choose_local_book(self, *_args) -> None:
        filters = Gio.ListStore.new(Gtk.FileFilter)
        book_filter = Gtk.FileFilter(name="Supported books")
        book_filter.add_mime_type("application/epub+zip")
        book_filter.add_mime_type("application/pdf")
        book_filter.add_mime_type("application/vnd.comicbook+zip")
        book_filter.add_mime_type("application/x-cbz")
        book_filter.add_mime_type("application/zip")
        book_filter.add_mime_type(MOBI_MIME)
        book_filter.add_mime_type(AZW3_MIME)
        book_filter.add_mime_type(FB2_MIME)
        book_filter.add_mime_type(FBZ_MIME)
        book_filter.add_pattern("*.epub")
        book_filter.add_pattern("*.pdf")
        book_filter.add_pattern("*.PDF")
        book_filter.add_pattern("*.cbz")
        book_filter.add_pattern("*.CBZ")
        book_filter.add_pattern("*.zip")
        book_filter.add_pattern("*.ZIP")
        for pattern in (
            "*.mobi", "*.MOBI", "*.azw3", "*.AZW3", "*.fb2", "*.FB2",
            "*.fbz", "*.FBZ", "*.fb2.zip", "*.FB2.ZIP",
        ):
            book_filter.add_pattern(pattern)
        filters.append(book_filter)
        epub_filter = Gtk.FileFilter(name="EPUB books")
        epub_filter.add_mime_type("application/epub+zip")
        epub_filter.add_pattern("*.epub")
        filters.append(epub_filter)
        pdf_filter = Gtk.FileFilter(name="PDF documents")
        pdf_filter.add_mime_type("application/pdf")
        pdf_filter.add_pattern("*.pdf")
        pdf_filter.add_pattern("*.PDF")
        filters.append(pdf_filter)
        comic_filter = Gtk.FileFilter(name="Comic archives")
        comic_filter.add_mime_type("application/vnd.comicbook+zip")
        comic_filter.add_mime_type("application/x-cbz")
        comic_filter.add_mime_type("application/zip")
        for pattern in ("*.cbz", "*.CBZ", "*.zip", "*.ZIP"):
            comic_filter.add_pattern(pattern)
        filters.append(comic_filter)
        kindle_filter = Gtk.FileFilter(name="MOBI and AZW3 books")
        kindle_filter.add_mime_type(MOBI_MIME)
        kindle_filter.add_mime_type(AZW3_MIME)
        for pattern in ("*.mobi", "*.MOBI", "*.azw3", "*.AZW3"):
            kindle_filter.add_pattern(pattern)
        filters.append(kindle_filter)
        fb2_filter = Gtk.FileFilter(name="FictionBook books")
        fb2_filter.add_mime_type(FB2_MIME)
        fb2_filter.add_mime_type(FBZ_MIME)
        for pattern in ("*.fb2", "*.FB2", "*.fbz", "*.FBZ", "*.fb2.zip", "*.FB2.ZIP"):
            fb2_filter.add_pattern(pattern)
        filters.append(fb2_filter)
        document_filter = Gtk.FileFilter(name="Text, Markdown, and HTML documents")
        document_filter.add_mime_type("text/plain")
        document_filter.add_mime_type("text/markdown")
        document_filter.add_mime_type("text/html")
        for pattern in (
            "*.txt", "*.TXT", "*.md", "*.MD", "*.markdown", "*.MARKDOWN",
            "*.html", "*.HTML", "*.htm", "*.HTM",
        ):
            book_filter.add_pattern(pattern)
            document_filter.add_pattern(pattern)
        filters.append(document_filter)
        self.local_file_dialog = Gtk.FileDialog(
            title="Choose a book",
            accept_label="Add Book",
            modal=True,
            filters=filters,
            default_filter=book_filter,
        )
        # A native file dialog cannot be presented above libadwaita's modal
        # in-window dialog. Temporarily dismiss the overlay during selection.
        self.add_dialog.close()
        self.local_file_dialog.open(self, None, self._local_book_chosen)

    def _local_book_chosen(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            file = dialog.open_finish(result)
            path = file.get_path()
            if not path:
                raise InvalidBook("Only local book files can be imported")
            book, _created = self.library.import_book(Path(path))
            self.add_dialog.close()
            self._refresh_library()
            self._open_book(book)
        except GLib.Error as error:
            if error.matches(Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED):
                self.add_dialog.present(self)
            else:
                self._show_error("Could not select the book", error.message)
        except (InvalidBook, InvalidDocument, OSError) as error:
            self._show_error("Could not import the book", str(error))
        finally:
            self.local_file_dialog = None

    def _add_page_changed(self, *_args) -> None:
        self._update_opds_back_button()
        if self.add_pages.get_visible_child_name() == "opds":
            GLib.idle_add(self._focus_first_row, self.opds_list)
        else:
            GLib.idle_add(self._focus_widget, self.local_choose_button)

    def _update_opds_back_button(self) -> None:
        on_opds = self.add_pages.get_visible_child_name() == "opds"
        self.opds_back_button.set_visible(on_opds and bool(self.catalog_history))

    def _show_catalogs(self, *_args) -> None:
        self.current_catalog = None
        self.catalog_history = []
        self.opds_feed_title = ""
        self.opds_feed_truncated = False
        self.opds_book_count = 0
        self.opds_search.set_text("")
        self.opds_search.set_visible(False)
        self.opds_context.set_visible(False)
        self._clear_list(self.opds_list)
        for catalog in self.library.list_catalogs():
            row = Adw.ActionRow(title=catalog.name, subtitle=catalog.url, activatable=True)
            row.opds_search_text = f"{catalog.name} {catalog.url}".casefold()
            row.opds_opens_next = True
            if catalog.is_builtin:
                built_in = Gtk.Label(label="Built-in")
                built_in.add_css_class("dim-label")
                row.add_suffix(built_in)
            open_indicator = Gtk.Image(icon_name="go-next-symbolic")
            open_indicator.add_css_class("opds-open-icon")
            row.add_suffix(open_indicator)
            row.connect("activated", lambda _row, item=catalog: self._open_catalog(item))
            self.opds_list.append(row)
        add = Adw.ActionRow(title="Add OPDS catalog", activatable=True)
        add.opds_search_text = "add opds catalog"
        add.add_prefix(Gtk.Image(icon_name="list-add-symbolic"))
        add.connect("activated", self._add_catalog_dialog)
        self.opds_list.append(add)
        self.opds_title.set_label("OPDS catalogs")
        self._update_opds_back_button()
        if self.add_pages.get_visible_child_name() == "opds":
            GLib.idle_add(self._focus_first_row, self.opds_list)

    def _add_catalog_dialog(self, *_args) -> None:
        group = Adw.PreferencesGroup()
        name = Adw.EntryRow(title="Name")
        url = Adw.EntryRow(title="OPDS URL")
        username = Adw.EntryRow(title="Username (optional)")
        password = Adw.PasswordEntryRow(title="Password (optional)")
        for row in (name, url, username, password):
            group.add(row)
        dialog = Adw.AlertDialog(
            heading="Add OPDS catalog",
            body="Connect to an OPDS 1.x catalog or an experimental OPDS 2 catalog.",
        )
        dialog.set_extra_child(group)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("add", "Connect")
        dialog.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("add")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._catalog_dialog_response, name, url, username, password)
        dialog.present(self.add_dialog)

    def _catalog_dialog_response(self, _dialog, response, name, url, username, password) -> None:
        if response != "add":
            return
        catalog_url = url.get_text().strip()
        catalog_name = name.get_text().strip() or urlsplit(catalog_url).hostname or "OPDS Catalog"
        user = username.get_text().strip()
        secret = password.get_text()
        if urlsplit(catalog_url).scheme.lower() == "http":
            warning = Adw.AlertDialog(
                heading="Use an insecure HTTP connection?",
                body="HTTP does not encrypt the catalog, downloaded books, or Basic authentication credentials. "
                     "Only continue on a network or VPN you trust.",
            )
            warning.add_response("cancel", "Cancel")
            warning.add_response("connect", "Connect insecurely")
            warning.set_response_appearance("connect", Adw.ResponseAppearance.DESTRUCTIVE)
            warning.set_default_response("cancel")
            warning.set_close_response("cancel")
            warning.connect(
                "response", self._insecure_catalog_response,
                catalog_name, catalog_url, user, secret,
            )
            warning.present(self.add_dialog)
            return
        self._connect_catalog(catalog_name, catalog_url, user, secret)

    def _insecure_catalog_response(self, _dialog, response, name, url, username, password) -> None:
        if response == "connect":
            self._connect_catalog(name, url, username, password)

    def _connect_catalog(self, catalog_name: str, catalog_url: str, user: str, secret: str) -> None:
        """Connect off the GTK thread, then save details and keyring secret."""
        self._set_busy_title("Connecting…", catalog_name)

        def worker():
            try:
                feed = fetch_complete_feed(catalog_url, user, secret)
                GLib.idle_add(self._catalog_connected, catalog_name, catalog_url, user, secret, feed)
            except OpdsError as error:
                GLib.idle_add(self._catalog_failed, str(error))
        threading.Thread(target=worker, daemon=True).start()

    def _catalog_connected(self, name, url, username, password, feed) -> bool:
        try:
            catalog = self.library.add_catalog(name, url, username)
            if password:
                Secret.password_store_sync(
                    SECRET_SCHEMA, {"catalog-id": str(catalog.id)}, Secret.COLLECTION_DEFAULT,
                    f"{name} OPDS password", password, None,
                )
        except Exception as error:
            self._show_error("Could not save the catalog", str(error))
            self._show_catalogs()
            return GLib.SOURCE_REMOVE
        self.current_catalog = catalog
        self.catalog_history = [url]
        self._display_feed(feed)
        return GLib.SOURCE_REMOVE

    def _catalog_failed(self, message: str) -> bool:
        self._show_error("Could not open the OPDS catalog", message)
        self._show_catalogs()
        return GLib.SOURCE_REMOVE

    def _catalog_password(self, catalog: Catalog) -> str:
        if catalog.is_builtin:
            return ""
        return Secret.password_lookup_sync(
            SECRET_SCHEMA, {"catalog-id": str(catalog.id)}, None
        ) or ""

    def _open_catalog(self, catalog: Catalog) -> None:
        self.current_catalog = catalog
        self.catalog_history = [catalog.url]
        self._load_feed(catalog.url)

    def _load_feed(self, url: str, add_history: bool = False) -> None:
        catalog = self.current_catalog
        if catalog is None:
            return
        self._set_busy_title("Loading catalog…", catalog.name)
        password = self._catalog_password(catalog)

        def worker():
            try:
                feed = fetch_complete_feed(url, catalog.username, password)
                GLib.idle_add(self._feed_loaded, feed, url, add_history)
            except OpdsError as error:
                GLib.idle_add(self._catalog_failed, str(error))
        threading.Thread(target=worker, daemon=True).start()

    def _feed_loaded(self, feed, url: str, add_history: bool) -> bool:
        if add_history:
            self.catalog_history.append(url)
        self._display_feed(feed)
        return GLib.SOURCE_REMOVE

    def _display_feed(self, feed) -> None:
        """Render a complete OPDS listing with navigation and format choices."""
        self.opds_search.set_text("")
        self.opds_search.set_sensitive(True)
        self.opds_search.set_visible(True)
        self._clear_list(self.opds_list)
        for entry in feed.entries:
            if entry.kind == "navigation":
                row = Adw.ActionRow(title=entry.title, subtitle=entry.author, activatable=True)
                row.opds_opens_next = True
                open_indicator = Gtk.Image(icon_name="go-next-symbolic")
                open_indicator.add_css_class("opds-open-icon")
                row.add_suffix(open_indicator)
                row.connect("activated", lambda _row, item=entry: self._load_feed(item.href, True))
                details = ""
            else:
                details = " · ".join(
                    f"{format_name(item.media_type)} ({format_file_size(item.size)})"
                    for item in entry.available_acquisitions
                )
                subtitle = f"{entry.author} · {details}" if entry.author else details
                row = self._build_opds_book_row(entry, subtitle)
            row.opds_search_text = f"{entry.title} {entry.author} {details}".casefold()
            row.opds_is_book = entry.kind == "book"
            self.opds_list.append(row)
        connection_title = self.current_catalog.name if self.current_catalog else feed.title
        self.opds_title.set_label(connection_title)
        self.opds_feed_title = feed.title
        self.opds_feed_truncated = feed.truncated
        self.opds_book_count = sum(entry.kind == "book" for entry in feed.entries)
        self._update_opds_context()
        self._update_opds_back_button()
        GLib.idle_add(self._focus_first_row, self.opds_list)

    def _build_opds_book_row(self, entry: OpdsEntry, subtitle: str) -> Gtk.ListBoxRow:
        title = Gtk.Label(label=entry.title, xalign=0, wrap=True)
        title.add_css_class("opds-book-title")
        details = Gtk.Label(label=subtitle, xalign=0, wrap=True)
        details.add_css_class("dim-label")
        text = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=3,
            hexpand=True, valign=Gtk.Align.CENTER,
        )
        text.append(title)
        text.append(details)

        downloads = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=4,
            halign=Gtk.Align.END,
            valign=Gtk.Align.CENTER,
        )
        downloads.add_css_class("opds-downloads")
        downloads.set_tooltip_text("Choose a format to download")
        for acquisition in entry.available_acquisitions:
            downloads.append(self._build_download_control(entry, acquisition))

        content = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
            hexpand=True,
            margin_top=10, margin_bottom=10, margin_start=24, margin_end=24,
        )
        content.append(text)
        content.append(downloads)
        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        row.set_child(content)
        return row

    def _build_download_control(
        self, entry: OpdsEntry, acquisition: OpdsAcquisition
    ) -> Gtk.Widget:
        name = format_name(acquisition.media_type)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.append(Gtk.Image(icon_name="folder-download-symbolic", pixel_size=14))
        label = Gtk.Label(label=name)
        label.add_css_class("opds-download-label")
        content.append(label)
        button = Gtk.Button(child=content, valign=Gtk.Align.CENTER)
        button.add_css_class("opds-download")
        button.set_tooltip_text(f"Download {name} — {format_file_size(acquisition.size)}")
        progress = Gtk.ProgressBar(
            visible=False, show_text=True, valign=Gtk.Align.CENTER,
        )
        button.connect(
            "clicked", self._download_catalog_book, entry, acquisition, progress
        )
        controls = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=2,
            width_request=46, halign=Gtk.Align.END, valign=Gtk.Align.CENTER,
        )
        controls.append(button)
        controls.append(progress)
        return controls

    def _download_catalog_book(
        self, button: Gtk.Button, entry: OpdsEntry, acquisition: OpdsAcquisition,
        progress: Gtk.ProgressBar,
    ) -> None:
        """Download to a temporary file before validating and importing it."""
        catalog = self.current_catalog
        if catalog is None:
            return
        button.set_visible(False)
        progress.set_visible(True)
        progress.set_fraction(0)
        progress.set_text("Starting…")
        password = self._catalog_password(catalog)

        def worker():
            if acquisition.media_type == PDF_MIME:
                extension = ".pdf"
            elif acquisition.media_type in CBZ_MIME_ALIASES:
                extension = ".cbz"
            elif acquisition.media_type == MOBI_MIME:
                extension = ".mobi"
            elif acquisition.media_type == AZW3_MIME:
                extension = ".azw3"
            elif acquisition.media_type == FB2_MIME:
                extension = ".fb2"
            elif acquisition.media_type == FBZ_MIME:
                extension = ".fbz"
            elif acquisition.media_type == TXT_MIME:
                extension = ".txt"
            elif acquisition.media_type in MARKDOWN_MIME_ALIASES:
                extension = ".md"
            elif acquisition.media_type == HTML_MIME:
                extension = ".html"
            else:
                extension = ".epub"
            descriptor, filename = tempfile.mkstemp(suffix=extension)
            os.close(descriptor)
            path = Path(filename)
            try:
                def report(downloaded, total):
                    GLib.idle_add(self._download_progress, progress, downloaded, total)

                download_book(
                    acquisition.href, path, acquisition.media_type,
                    catalog.username, password, acquisition.size, report,
                )
                GLib.idle_add(self._download_complete, path, button, progress, entry)
            except OpdsError as error:
                path.unlink(missing_ok=True)
                GLib.idle_add(self._download_failed, str(error), button, progress)
        threading.Thread(target=worker, daemon=True).start()

    def _download_progress(
        self, progress: Gtk.ProgressBar, downloaded: int, total: int | None
    ) -> bool:
        if total and total > 0:
            fraction = min(1.0, downloaded / total)
            progress.set_fraction(fraction)
            progress.set_text(f"{round(fraction * 100)}%")
        else:
            progress.pulse()
            progress.set_text(format_file_size(downloaded))
        return GLib.SOURCE_REMOVE

    def _download_complete(
        self, path: Path, button: Gtk.Button, progress: Gtk.ProgressBar, entry: OpdsEntry
    ) -> bool:
        try:
            book, _created = self.library.import_book(path, entry.title, entry.author)
            progress.set_fraction(1)
            progress.set_text("100%")
            self.add_dialog.close()
            self._refresh_library()
            self._open_book(book)
        except (InvalidBook, InvalidDocument, OSError) as error:
            self._download_failed(str(error), button, progress)
        finally:
            path.unlink(missing_ok=True)
        return GLib.SOURCE_REMOVE

    def _download_failed(
        self, message: str, button: Gtk.Button, progress: Gtk.ProgressBar
    ) -> bool:
        progress.set_visible(False)
        button.set_visible(True)
        self._show_error("Could not download the book", message)
        return GLib.SOURCE_REMOVE

    def _set_busy_title(self, title: str, subtitle: str) -> None:
        self.opds_title.set_label(subtitle)
        self.opds_context.set_label(title)
        self.opds_context.set_visible(True)
        self.opds_search.set_sensitive(False)

    def _opds_go_back(self, *_args) -> None:
        if len(self.catalog_history) > 1:
            self.catalog_history.pop()
            self._load_feed(self.catalog_history[-1])
        else:
            self._show_catalogs()

    def _open_book(self, book: Book) -> None:
        """Select the correct reader implementation for an imported book."""
        self.current_book = book
        self.library.mark_opened(book.id)
        if book.format == "pdf":
            self._open_pdf(book)
        elif book.format in ("cbz", "zip"):
            self._open_comic(book)
        elif f".{book.format}" in DOCUMENT_EXTENSIONS:
            self._open_document(book)
        else:
            self._open_epub(book)

    def _open_document(self, book: Book) -> None:
        """Render a safe text-based document in the shared WebKit view."""
        try:
            document = read_document(book.path)
            template = (PROJECT_DIR / "src/web/document.html").read_text(encoding="utf-8")
        except (InvalidDocument, OSError) as error:
            self._show_error("Could not open this document", str(error))
            return

        font_family = self.theme.font_family.replace("\\", "\\\\").replace('"', '\\"')
        theme_style = "<style>:root {" + ";".join((
            f"color-scheme:{self.theme.mode}",
            f"--background:{self.theme.background}",
            f"--surface:{self.theme.surface}",
            f"--foreground:{self.theme.foreground}",
            f"--muted:{self.theme.muted}",
            f"--accent:{self.theme.accent}",
            f"--selection:{self.theme.selection}",
            f'--reader-font:"{font_family}"',
            f"--reader-font-size:{self.theme.font_size}px",
        )) + "}</style>"
        page = template.replace("<!-- THEME_STYLE -->", theme_style)
        page = page.replace("<!-- DOCUMENT_TITLE -->", html.escape(document.title))
        page = page.replace("<!-- DOCUMENT_BODY -->", document.body_html)
        page = page.replace(
            '<body data-progress="0">',
            f'<body data-progress="{book.progress_fraction:.6f}">',
        )
        self.web_view.load_html(page, "reader://app/")
        self.stack.set_visible_child_name("epub-reader")
        self._show_reader_header(book)

    def _open_epub(self, book: Book) -> None:
        reader_uri = "reader://app/reader.html"
        params = {
            "book": f"reader://app/books/{quote(book.path.name)}",
            "cfi": book.progress_cfi or "",
            "background": self.theme.background,
            "foreground": self.theme.foreground,
            "accent": self.theme.accent,
            "selection": self.theme.selection,
            "fontFamily": self.theme.font_family,
            "fontSize": self.theme.font_size,
            "mode": self.theme.mode,
        }
        query = "&".join(f"{key}={quote(str(value), safe='')}" for key, value in params.items())
        self.web_view.load_uri(f"{reader_uri}?{query}")
        self.stack.set_visible_child_name("epub-reader")
        self._show_reader_header(book)

    def _open_pdf(self, book: Book) -> None:
        try:
            self.pdf_document = Poppler.Document.new_from_file(book.path.resolve().as_uri(), None)
            self.pdf_page_count = self.pdf_document.get_n_pages()
            if self.pdf_page_count < 1:
                raise ValueError("The PDF does not contain any pages")
        except (GLib.Error, ValueError) as error:
            self.pdf_document = None
            self._show_error("Could not open this PDF", str(error))
            return

        restored_page = book.progress_section if book.progress_section is not None else 0
        self.pdf_updating_controls = True
        self.pdf_position.set_range(1, self.pdf_page_count)
        self.pdf_updating_controls = False
        self._show_pdf_page(restored_page, save_progress=False)
        self.stack.set_visible_child_name("pdf-reader")
        self._show_reader_header(book)
        self.pdf_canvas.grab_focus()

    def _show_reader_header(self, book: Book) -> None:
        self.back_button.set_visible(True)
        self.add_button.set_visible(False)
        self.guide_bar.set_visible(False)
        self.header.set_title_widget(Adw.WindowTitle(title=book.title, subtitle=book.author))

    def _show_pdf_page(self, index: int, save_progress: bool = True) -> None:
        if self.pdf_document is None or self.pdf_page_count < 1:
            return
        self.pdf_page_index = min(self.pdf_page_count - 1, max(0, int(index)))
        self.pdf_updating_controls = True
        self.pdf_position.set_value(self.pdf_page_index + 1)
        self.pdf_updating_controls = False
        self.pdf_page_label.set_label(
            f"Page {self.pdf_page_index + 1} of {self.pdf_page_count}"
        )
        self.pdf_previous.set_sensitive(self.pdf_page_index > 0)
        self.pdf_next.set_sensitive(self.pdf_page_index < self.pdf_page_count - 1)
        self.pdf_canvas.queue_draw()
        if save_progress and self.current_book is not None:
            fraction = (self.pdf_page_index + 1) / self.pdf_page_count
            self.library.save_progress(
                self.current_book.id, None, self.pdf_page_index, fraction
            )

    def _draw_pdf_page(self, _area, context, width: int, height: int) -> None:
        if self.pdf_document is None:
            return
        page = self.pdf_document.get_page(self.pdf_page_index)
        page_width, page_height = page.get_size()
        padding = 16
        scale = min(
            max(1, width - padding * 2) / page_width,
            max(1, height - padding * 2) / page_height,
        )
        rendered_width = page_width * scale
        rendered_height = page_height * scale
        x = (width - rendered_width) / 2
        y = (height - rendered_height) / 2

        context.save()
        context.set_source_rgb(1, 1, 1)
        context.rectangle(x, y, rendered_width, rendered_height)
        context.fill()
        context.translate(x, y)
        context.scale(scale, scale)
        page.render(context)
        context.restore()

    def _pdf_position_changed(self, scale: Gtk.Scale) -> None:
        if not self.pdf_updating_controls:
            self._show_pdf_page(round(scale.get_value()) - 1)

    def _pdf_key_pressed(self, _controller, keyval, _keycode, _state) -> bool:
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Page_Up) or (
            keyval == Gdk.KEY_space and _state & Gdk.ModifierType.SHIFT_MASK
        ):
            self._show_pdf_page(self.pdf_page_index - 1)
            return True
        if keyval in (Gdk.KEY_Right, Gdk.KEY_Page_Down, Gdk.KEY_space):
            self._show_pdf_page(self.pdf_page_index + 1)
            return True
        return False

    def _open_comic(self, book: Book) -> None:
        try:
            self.comic_pages = list_comic_pages(book.path)
        except InvalidComic as error:
            self.comic_pages = []
            self._show_error("Could not open this comic", str(error))
            return

        restored_page = book.progress_section if book.progress_section is not None else 0
        self.comic_updating_controls = True
        self.comic_position.set_range(1, len(self.comic_pages))
        self.comic_updating_controls = False
        if not self._show_comic_page(restored_page, save_progress=False):
            return
        self.stack.set_visible_child_name("comic-reader")
        self._show_reader_header(book)
        self.comic_picture.grab_focus()

    def _show_comic_page(self, index: int, save_progress: bool = True) -> bool:
        if not self.comic_pages or self.current_book is None:
            return False
        self.comic_page_index = min(len(self.comic_pages) - 1, max(0, int(index)))
        try:
            data = read_comic_page(
                self.current_book.path, self.comic_pages[self.comic_page_index]
            )
            texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
        except (InvalidComic, GLib.Error) as error:
            self._show_error("Could not display this comic page", str(error))
            return False

        self.comic_picture.set_paintable(texture)
        self.comic_updating_controls = True
        self.comic_position.set_value(self.comic_page_index + 1)
        self.comic_updating_controls = False
        page_count = len(self.comic_pages)
        self.comic_page_label.set_label(f"Page {self.comic_page_index + 1} of {page_count}")
        self.comic_previous.set_sensitive(self.comic_page_index > 0)
        self.comic_next.set_sensitive(self.comic_page_index < page_count - 1)
        if save_progress:
            self.library.save_progress(
                self.current_book.id, None, self.comic_page_index,
                (self.comic_page_index + 1) / page_count,
            )
        return True

    def _comic_position_changed(self, scale: Gtk.Scale) -> None:
        if not self.comic_updating_controls:
            self._show_comic_page(round(scale.get_value()) - 1)

    def _comic_key_pressed(self, _controller, keyval, _keycode, _state) -> bool:
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Page_Up) or (
            keyval == Gdk.KEY_space and _state & Gdk.ModifierType.SHIFT_MASK
        ):
            self._show_comic_page(self.comic_page_index - 1)
            return True
        if keyval in (Gdk.KEY_Right, Gdk.KEY_Page_Down, Gdk.KEY_space):
            self._show_comic_page(self.comic_page_index + 1)
            return True
        return False

    def _confirm_remove_book(self, _button, book: Book) -> None:
        dialog = Adw.AlertDialog(
            heading=f"Remove “{book.title}”?",
            body="This removes the reader’s local copy and saved progress. "
                 "It does not affect the original file or a remote catalog.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._remove_book_response, book)
        dialog.present(self)

    def _remove_book_response(self, _dialog, response: str, book: Book) -> None:
        if response != "remove":
            return
        try:
            self.library.remove_book(book.id)
            self._refresh_library()
            GLib.idle_add(self._focus_library_book, None)
        except (OSError, ValueError) as error:
            self._show_error("Could not remove the book", str(error))

    def _show_library(self, *_args) -> None:
        previous_book_id = self.current_book.id if self.current_book else None
        self.current_book = None
        self.pdf_document = None
        self.comic_pages = []
        self.comic_picture.set_paintable(None)
        self.web_view.load_html("", None)
        self.back_button.set_visible(False)
        self.add_button.set_visible(True)
        self.guide_bar.set_visible(True)
        self._refresh_library()
        GLib.idle_add(self._focus_library_book, previous_book_id)

    def _go_back(self, *_args) -> None:
        self._show_library()

    def _reader_message(self, _manager, message) -> None:
        """Handle progress, readiness, and errors sent by the web reader."""
        try:
            payload = json.loads(message.to_json(0))
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        if payload.get("type") == "progress" and self.current_book:
            self.library.save_progress(
                self.current_book.id,
                payload.get("cfi"),
                payload.get("section"),
                payload.get("fraction", 0),
            )
        elif payload.get("type") == "error":
            self._show_error("Could not open this book", payload.get("message", "Unknown error"))
        elif payload.get("type") == "ready":
            self.web_view.grab_focus()
        elif payload.get("type") == "back":
            self._show_library()
        elif payload.get("type") == "show-help":
            self._show_keyboard_help()
        elif payload.get("type") == "external-link":
            uri = payload.get("href", "")
            if urlsplit(uri).scheme.lower() in ("http", "https", "mailto"):
                Gio.AppInfo.launch_default_for_uri(uri, None)

    def _reader_load_failed(self, _view, _event, uri: str, error: GLib.Error) -> bool:
        self._show_error("The reader page failed to load", f"{error.message}\n\n{uri}")
        return False

    def _show_error(self, heading: str, body: str) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("close", "Close")
        dialog.present(self)


class ReaderApplication(Adw.Application):
    """Single-instance application that also accepts books from file handlers."""
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)

    def do_activate(self) -> None:
        window = self.get_active_window()
        if window is None:
            data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
            window = ReaderWindow(
                self,
                Library(data_home / "omarchy-reader", Path.home() / "Books"),
            )
        window.present()

    def do_open(self, files, _count, _hint) -> None:
        self.do_activate()
        window = self.get_active_window()
        for file in files:
            if path := file.get_path():
                try:
                    book, _created = window.library.import_book(Path(path))
                    window._refresh_library()
                    window._open_book(book)
                except (InvalidBook, InvalidDocument, OSError) as error:
                    window._show_error("Could not import the book", str(error))
                break


if __name__ == "__main__":
    raise SystemExit(ReaderApplication().run(sys.argv))
