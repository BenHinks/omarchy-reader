"""Validate books and manage Omarchy Reader's local SQLite library.

This module owns format detection, lightweight metadata extraction, safe archive
handling, managed-file naming, deduplication, progress, and saved OPDS catalog
records.  It deliberately has no GTK dependencies except for Poppler metadata.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


MAX_UNCOMPRESSED_SIZE = 1_000_000_000
MAX_COMIC_PAGE_SIZE = 100_000_000
COMIC_EXTENSIONS = {".cbz", ".zip"}
COMIC_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
MOBI_EXTENSIONS = {".mobi", ".azw3"}
FB2_EXTENSIONS = {".fb2", ".fbz", ".fb2.zip"}


class InvalidBook(ValueError):
    """Base error for a book that cannot be safely imported."""

    pass


class InvalidEpub(InvalidBook):
    """Raised when an EPUB is malformed, unsafe, or too large."""

    pass


class InvalidPdf(InvalidBook):
    """Raised when Poppler cannot open a PDF as a readable document."""

    pass


class InvalidComic(InvalidBook):
    """Raised when a CBZ/ZIP archive has no safe, readable image pages."""

    pass


class InvalidMobi(InvalidBook):
    """Raised when a file lacks a supported Mobipocket header."""

    pass


class InvalidFb2(InvalidBook):
    """Raised when a plain or compressed FictionBook document is invalid."""

    pass


@dataclass(frozen=True)
class BookMetadata:
    """Metadata discovered in a book before it receives a library record."""

    title: str
    author: str
    identifier: str


@dataclass(frozen=True)
class Book:
    """An immutable view of a book row and its managed on-disk copy."""

    id: int
    title: str
    author: str
    identifier: str
    checksum: str
    path: Path
    progress_cfi: str | None
    progress_section: int | None
    progress_fraction: float

    @property
    def format(self) -> str:
        """Return the lowercase format name used by the UI."""
        if self.path.name.casefold().endswith(".fb2.zip"):
            return "fbz"
        return self.path.suffix.lower().removeprefix(".")


@dataclass(frozen=True)
class Catalog:
    """A saved OPDS endpoint; passwords are intentionally stored elsewhere."""

    id: int
    name: str
    url: str
    username: str


def _safe_archive_name(name: str) -> bool:
    path = PurePosixPath(name)
    return not path.is_absolute() and ".." not in path.parts


def read_epub_metadata(path: Path) -> BookMetadata:
    """Validate an EPUB container and read its package metadata."""
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if not entries or any(not _safe_archive_name(item.filename) for item in entries):
                raise InvalidEpub("The EPUB contains unsafe archive paths")
            if sum(item.file_size for item in entries) > MAX_UNCOMPRESSED_SIZE:
                raise InvalidEpub("The EPUB expands beyond the supported size limit")

            try:
                container = ET.fromstring(archive.read("META-INF/container.xml"))
            except (KeyError, ET.ParseError) as error:
                raise InvalidEpub("The EPUB container metadata is missing or invalid") from error

            rootfile = container.find(".//{*}rootfile")
            package_path = rootfile.get("full-path") if rootfile is not None else None
            if not package_path or not _safe_archive_name(package_path):
                raise InvalidEpub("The EPUB does not identify a valid package document")

            try:
                package = ET.fromstring(archive.read(package_path))
            except (KeyError, ET.ParseError) as error:
                raise InvalidEpub("The EPUB package document is missing or invalid") from error
    except (OSError, zipfile.BadZipFile) as error:
        raise InvalidEpub("The selected file is not a valid EPUB archive") from error

    def text(local_name: str) -> str:
        node = package.find(f".//{{*}}{local_name}")
        return "" if node is None or node.text is None else node.text.strip()

    return BookMetadata(
        title=text("title") or path.stem,
        author=text("creator") or "Unknown author",
        identifier=text("identifier"),
    )


def read_pdf_metadata(path: Path) -> BookMetadata:
    """Ask Poppler to validate a PDF and return its document metadata."""
    try:
        import gi
        gi.require_version("Poppler", "0.18")
        from gi.repository import GLib, Poppler

        document = Poppler.Document.new_from_file(path.resolve().as_uri(), None)
        if document.get_n_pages() < 1:
            raise InvalidPdf("The PDF does not contain any pages")
    except (OSError, GLib.Error) as error:
        raise InvalidPdf("The selected file is not a valid or supported PDF") from error

    return BookMetadata(
        title=(document.get_title() or path.stem).strip(),
        author=(document.get_author() or "Unknown author").strip(),
        identifier="",
    )


def _natural_archive_key(name: str):
    return [
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", name)
    ]


def list_comic_pages(path: Path) -> list[str]:
    """Return image members in human-friendly filename order."""
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if not entries or any(not _safe_archive_name(item.filename) for item in entries):
                raise InvalidComic("The comic archive contains unsafe paths")
            if sum(item.file_size for item in entries) > MAX_UNCOMPRESSED_SIZE:
                raise InvalidComic("The comic archive expands beyond the supported size limit")
            pages = [
                item.filename for item in entries
                if not item.is_dir()
                and Path(item.filename).suffix.lower() in COMIC_IMAGE_EXTENSIONS
            ]
    except (OSError, zipfile.BadZipFile) as error:
        raise InvalidComic("The selected file is not a valid ZIP comic archive") from error
    if not pages:
        raise InvalidComic("The comic archive does not contain supported images")
    return sorted(pages, key=_natural_archive_key)


def read_comic_page(path: Path, page_name: str) -> bytes:
    """Read one validated comic page without extracting the archive."""
    if not _safe_archive_name(page_name):
        raise InvalidComic("The comic page path is unsafe")
    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo(page_name)
            if info.file_size > MAX_COMIC_PAGE_SIZE:
                raise InvalidComic("The comic page exceeds the supported size limit")
            return archive.read(info)
    except (KeyError, OSError, zipfile.BadZipFile) as error:
        raise InvalidComic("The comic page could not be read") from error


def read_comic_metadata(path: Path) -> BookMetadata:
    """Validate a comic archive and optionally read ComicInfo.xml."""
    list_comic_pages(path)
    title = path.stem
    author = "Unknown author"
    try:
        with zipfile.ZipFile(path) as archive:
            metadata_name = next(
                (name for name in archive.namelist() if name.casefold().endswith("comicinfo.xml")),
                None,
            )
            if metadata_name:
                root = ET.fromstring(archive.read(metadata_name))
                title = (root.findtext("Title") or title).strip()
                author = (root.findtext("Writer") or author).strip()
    except (KeyError, ET.ParseError, OSError, zipfile.BadZipFile):
        # ComicInfo.xml is optional; malformed metadata does not invalidate
        # an otherwise readable image archive.
        pass
    return BookMetadata(title=title, author=author, identifier="")


def read_mobi_metadata(path: Path) -> BookMetadata:
    """Perform a lightweight MOBI/AZW3 header check and extract its title."""
    try:
        if path.stat().st_size > MAX_UNCOMPRESSED_SIZE:
            raise InvalidMobi("The Mobipocket book exceeds the supported size limit")
        with path.open("rb") as stream:
            header = stream.read(68)
    except OSError as error:
        raise InvalidMobi("The Mobipocket book could not be read") from error
    if len(header) < 68 or header[60:68] not in (b"BOOKMOBI", b"TEXtREAd"):
        raise InvalidMobi("The selected file is not a valid MOBI or AZW3 book")
    title = header[:32].split(b"\0", 1)[0].decode("cp1252", errors="replace").strip()
    return BookMetadata(title=title or path.stem, author="Unknown author", identifier="")


def _fb2_document(path: Path) -> tuple[ET.Element, str]:
    extension = _book_extension(path)
    try:
        if extension == ".fb2":
            if path.stat().st_size > MAX_UNCOMPRESSED_SIZE:
                raise InvalidFb2("The FB2 book exceeds the supported size limit")
            data = path.read_bytes()
        else:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if not entries or any(not _safe_archive_name(item.filename) for item in entries):
                    raise InvalidFb2("The compressed FB2 contains unsafe archive paths")
                if sum(item.file_size for item in entries) > MAX_UNCOMPRESSED_SIZE:
                    raise InvalidFb2("The compressed FB2 expands beyond the supported size limit")
                document = next((item for item in entries
                    if not item.is_dir() and item.filename.casefold().endswith(".fb2")), None)
                if document is None:
                    raise InvalidFb2("The compressed FB2 does not contain an FB2 document")
                data = archive.read(document)
        return ET.fromstring(data), extension
    except (OSError, zipfile.BadZipFile, ET.ParseError) as error:
        raise InvalidFb2("The selected file is not a valid FB2 book") from error


def read_fb2_metadata(path: Path) -> BookMetadata:
    """Read FictionBook title and author metadata from FB2 or FBZ."""
    root, _extension = _fb2_document(path)
    title_info = root.find(".//{*}title-info")
    if title_info is None:
        raise InvalidFb2("The FB2 book is missing its title information")
    title = (title_info.findtext("{*}book-title") or path.stem).strip()
    authors = []
    for node in title_info.findall("{*}author"):
        parts = [
            (node.findtext(f"{{*}}{name}") or "").strip()
            for name in ("first-name", "middle-name", "last-name")
        ]
        author = " ".join(part for part in parts if part)
        author = author or (node.findtext("{*}nickname") or "").strip()
        if author:
            authors.append(author)
    return BookMetadata(
        title=title or path.stem,
        author="; ".join(authors) or "Unknown author",
        identifier="",
    )


def _book_extension(path: Path) -> str:
    name = path.name.casefold()
    if name.endswith(".fb2.zip"):
        return ".fb2.zip"
    return path.suffix.lower()


def _filename_part(value: str, fallback: str) -> str:
    value = re.sub(r"[\\/\x00-\x1f\x7f]", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value or fallback)[:100].rstrip(" .")


class Library:
    """Own the database and copies of books imported into managed storage."""

    def __init__(self, data_dir: Path, books_dir: Path | None = None):
        self.data_dir = data_dir
        self.books_dir = books_dir or Path.home() / "Books"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.books_dir.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(data_dir / "library.sqlite3")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self._migrate()
        self._migrate_legacy_books()

    def _migrate(self) -> None:
        """Create the current schema idempotently for a new or existing user."""
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS books (
                    id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    author TEXT NOT NULL,
                    publication_identifier TEXT NOT NULL DEFAULT '',
                    checksum TEXT NOT NULL UNIQUE,
                    local_path TEXT NOT NULL UNIQUE,
                    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_opened_at TEXT,
                    progress_cfi TEXT,
                    progress_section INTEGER,
                    progress_fraction REAL NOT NULL DEFAULT 0,
                    progress_updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS catalogs (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    username TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    def _migrate_legacy_books(self) -> None:
        """Move books from the old hidden data directory into ~/Books."""
        legacy_dir = (self.data_dir / "books").resolve()
        if legacy_dir == self.books_dir.resolve() or not legacy_dir.exists():
            return
        rows = self.connection.execute("SELECT * FROM books").fetchall()
        for row in rows:
            source = Path(row["local_path"])
            try:
                if not source.resolve().is_relative_to(legacy_dir) or not source.is_file():
                    continue
                destination = self._destination_for(
                    row["title"], row["author"], row["checksum"], _book_extension(source)
                )
                source.replace(destination)
                with self.connection:
                    self.connection.execute(
                        "UPDATE books SET local_path = ? WHERE id = ?",
                        (str(destination), row["id"]),
                    )
            except OSError:
                # Leave the existing record and file intact; migration can be
                # retried after the underlying filesystem problem is fixed.
                continue

    def _destination_for(
        self, title: str, author: str, checksum: str, extension: str = ".epub"
    ) -> Path:
        title_part = _filename_part(title, "Untitled")
        author_part = _filename_part(author, "Unknown author")
        extension = extension.lower()
        destination = self.books_dir / f"{author_part} - {title_part}{extension}"
        if not destination.exists():
            return destination
        return self.books_dir / f"{author_part} - {title_part} ({checksum[:8]}){extension}"

    def list_books(self) -> list[Book]:
        """List books with the most recently opened or imported first."""
        rows = self.connection.execute(
            "SELECT * FROM books ORDER BY COALESCE(last_opened_at, imported_at) DESC"
        ).fetchall()
        return [self._book(row) for row in rows]

    def list_catalogs(self) -> list[Catalog]:
        """List saved OPDS endpoints alphabetically."""
        rows = self.connection.execute("SELECT * FROM catalogs ORDER BY name COLLATE NOCASE").fetchall()
        return [Catalog(row["id"], row["name"], row["url"], row["username"]) for row in rows]

    def add_catalog(self, name: str, url: str, username: str) -> Catalog:
        """Save non-secret catalog details and return their new record."""
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO catalogs (name, url, username) VALUES (?, ?, ?)",
                (name, url, username),
            )
        return Catalog(cursor.lastrowid, name, url, username)

    def import_epub(self, source: Path) -> tuple[Book, bool]:
        metadata = read_epub_metadata(source)
        return self._import_book(source, metadata, ".epub")

    def import_pdf(self, source: Path) -> tuple[Book, bool]:
        metadata = read_pdf_metadata(source)
        return self._import_book(source, metadata, ".pdf")

    def import_comic(self, source: Path) -> tuple[Book, bool]:
        if source.suffix.lower() not in COMIC_EXTENSIONS:
            raise InvalidComic("Only CBZ and ZIP comic archives are currently supported")
        metadata = read_comic_metadata(source)
        return self._import_book(source, metadata, source.suffix.lower())

    def import_mobi(self, source: Path) -> tuple[Book, bool]:
        extension = _book_extension(source)
        if extension not in MOBI_EXTENSIONS:
            raise InvalidMobi("Only MOBI and AZW3 books are supported by this importer")
        return self._import_book(source, read_mobi_metadata(source), extension)

    def import_fb2(self, source: Path) -> tuple[Book, bool]:
        extension = _book_extension(source)
        if extension not in FB2_EXTENSIONS:
            raise InvalidFb2("Only FB2 and compressed FB2 books are supported by this importer")
        return self._import_book(source, read_fb2_metadata(source), extension)

    def import_book(
        self, source: Path, title: str | None = None, author: str | None = None
    ) -> tuple[Book, bool]:
        """Validate and import any supported format, optionally overriding metadata."""
        extension = _book_extension(source)
        match extension:
            case ".epub":
                metadata = read_epub_metadata(source)
            case ".pdf":
                metadata = read_pdf_metadata(source)
            case ".cbz" | ".zip":
                metadata = read_comic_metadata(source)
            case ".mobi" | ".azw3":
                metadata = read_mobi_metadata(source)
            case ".fb2" | ".fbz" | ".fb2.zip":
                metadata = read_fb2_metadata(source)
            case _:
                raise InvalidBook(
                    "Only EPUB, PDF, CBZ/ZIP, MOBI/AZW3, and FB2 books are currently supported"
                )
        metadata = BookMetadata(
            title=(title or "").strip() or metadata.title,
            author=(author or "").strip() or metadata.author,
            identifier=metadata.identifier,
        )
        return self._import_book(
            source, metadata, extension,
            bool((title or "").strip()), bool((author or "").strip()),
        )

    def _import_book(
        self, source: Path, metadata: BookMetadata, extension: str,
        override_title: bool = False, override_author: bool = False,
    ) -> tuple[Book, bool]:
        """Copy a validated book atomically and deduplicate it by SHA-256."""
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        checksum = digest.hexdigest()

        existing = self.connection.execute(
            "SELECT * FROM books WHERE checksum = ?", (checksum,)
        ).fetchone()
        if existing:
            title = metadata.title if override_title else existing["title"]
            author = metadata.author if override_author else existing["author"]
            if title != existing["title"] or author != existing["author"]:
                with self.connection:
                    self.connection.execute(
                        "UPDATE books SET title = ?, author = ? WHERE id = ?",
                        (title, author, existing["id"]),
                    )
                return self.get_book(existing["id"]), False
            return self._book(existing), False

        destination = self._destination_for(metadata.title, metadata.author, checksum, extension)
        temporary = destination.with_suffix(destination.suffix + ".partial")
        shutil.copyfile(source, temporary)
        temporary.replace(destination)

        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO books
                    (title, author, publication_identifier, checksum, local_path)
                VALUES (?, ?, ?, ?, ?)
                """,
                (metadata.title, metadata.author, metadata.identifier, checksum, str(destination)),
            )
        return self.get_book(cursor.lastrowid), True

    def get_book(self, book_id: int) -> Book:
        """Return one book or raise ``KeyError`` when it no longer exists."""
        row = self.connection.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        if row is None:
            raise KeyError(book_id)
        return self._book(row)

    def mark_opened(self, book_id: int) -> None:
        """Move a book to the front of the recency-sorted library."""
        with self.connection:
            self.connection.execute(
                "UPDATE books SET last_opened_at = CURRENT_TIMESTAMP WHERE id = ?", (book_id,)
            )

    def remove_book(self, book_id: int) -> None:
        """Remove a managed copy and its row, rolling back on database errors."""
        book = self.get_book(book_id)
        path = book.path.resolve()
        if not path.is_relative_to(self.books_dir.resolve()):
            raise ValueError("Refusing to remove a file outside the managed library")

        temporary = path.with_suffix(path.suffix + ".removing")
        if path.exists():
            path.replace(temporary)
        try:
            with self.connection:
                self.connection.execute("DELETE FROM books WHERE id = ?", (book_id,))
        except Exception:
            if temporary.exists():
                temporary.replace(path)
            raise
        temporary.unlink(missing_ok=True)

    def save_progress(self, book_id: int, cfi: str | None, section: int | None, fraction: float) -> None:
        """Persist a normalized reading position for any supported format."""
        fraction = min(1.0, max(0.0, float(fraction)))
        with self.connection:
            self.connection.execute(
                """
                UPDATE books SET progress_cfi = ?, progress_section = ?,
                    progress_fraction = ?, progress_updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (cfi, section, fraction, book_id),
            )

    @staticmethod
    def _book(row: sqlite3.Row) -> Book:
        return Book(
            id=row["id"], title=row["title"], author=row["author"],
            identifier=row["publication_identifier"], checksum=row["checksum"],
            path=Path(row["local_path"]), progress_cfi=row["progress_cfi"],
            progress_section=row["progress_section"],
            progress_fraction=row["progress_fraction"],
        )
