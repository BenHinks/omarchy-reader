import io
import tempfile
import unittest
import zipfile
from pathlib import Path

import cairo

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from library import (
    BUILTIN_CATALOGS, InvalidBook, InvalidComic, InvalidEpub, InvalidFb2, InvalidMobi,
    InvalidPdf, Library,
    list_comic_pages, read_comic_metadata, read_comic_page,
    read_epub_metadata, read_fb2_metadata, read_mobi_metadata, read_pdf_metadata,
)


def make_epub(path: Path, title: str = "Test Book") -> None:
    container = """<?xml version="1.0"?>
    <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
      <rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles>
    </container>"""
    package = f"""<?xml version="1.0"?>
    <package xmlns="http://www.idpf.org/2007/opf" version="3.0">
      <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
        <dc:identifier>book-id</dc:identifier><dc:title>{title}</dc:title><dc:creator>An Author</dc:creator>
      </metadata>
    </package>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("content.opf", package)


def make_pdf(path: Path) -> None:
    surface = cairo.PDFSurface(str(path), 300, 400)
    context = cairo.Context(surface)
    context.move_to(40, 60)
    context.show_text("A test PDF")
    context.show_page()
    surface.finish()


def make_comic(path: Path) -> bytes:
    image = io.BytesIO()
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 8, 12)
    context = cairo.Context(surface)
    context.set_source_rgb(0.2, 0.4, 0.8)
    context.paint()
    surface.write_to_png(image)
    image_data = image.getvalue()
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ComicInfo.xml", "<ComicInfo><Title>Issue One</Title><Writer>A Writer</Writer></ComicInfo>")
        archive.writestr("pages/page10.png", image_data)
        archive.writestr("pages/page2.png", image_data)
        archive.writestr("notes.txt", "ignored")
    return image_data


def make_mobi(path: Path, title: str = "Palm Book") -> None:
    header = bytearray(68)
    encoded_title = title.encode("cp1252")[:31]
    header[:len(encoded_title)] = encoded_title
    header[60:68] = b"BOOKMOBI"
    path.write_bytes(header + b"test payload")


def fb2_data() -> bytes:
    return b'''<?xml version="1.0" encoding="utf-8"?>
    <FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">
      <description><title-info>
        <book-title>Fiction Book</book-title>
        <author><first-name>Test</first-name><last-name>Writer</last-name></author>
      </title-info></description>
      <body><section><p>Hello</p></section></body>
    </FictionBook>'''


class LibraryTests(unittest.TestCase):
    def test_reads_metadata_and_imports_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample.epub"
            make_epub(source)
            metadata = read_epub_metadata(source)
            self.assertEqual(metadata.title, "Test Book")
            self.assertEqual(metadata.author, "An Author")

            library = Library(root / "data", root / "Books")
            book, created = library.import_epub(source)
            duplicate, created_again = library.import_epub(source)
            self.assertTrue(created)
            self.assertFalse(created_again)
            self.assertEqual(book.id, duplicate.id)
            self.assertTrue(book.path.exists())
            self.assertEqual(book.path.parent, root / "Books")
            self.assertEqual(book.path.name, "An Author - Test Book.epub")

            library.save_progress(book.id, "epubcfi(/6/4!/4/2)", 1, 0.42)
            saved = library.get_book(book.id)
            self.assertEqual(saved.progress_cfi, "epubcfi(/6/4!/4/2)")
            self.assertEqual(saved.progress_section, 1)
            self.assertEqual(saved.progress_fraction, 0.42)

            library.remove_book(book.id)
            self.assertEqual(library.list_books(), [])
            self.assertFalse(book.path.exists())
            library.connection.close()

    def test_rejects_non_epub_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.epub"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("hello.txt", "not an epub")
            with self.assertRaises(InvalidEpub):
                read_epub_metadata(path)

    def test_persists_application_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = Library(root / "data", root / "Books")
            self.assertIsNone(library.get_setting("reading-font-percent"))
            library.set_setting("reading-font-percent", "130")
            library.set_setting("reading-font-percent", "140")
            library.connection.close()

            reopened = Library(root / "data", root / "Books")
            self.assertEqual(reopened.get_setting("reading-font-percent"), "140")
            reopened.remove_setting("reading-font-percent")
            self.assertIsNone(reopened.get_setting("reading-font-percent"))
            reopened.connection.close()

    def test_reads_imports_and_deduplicates_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Sample Document.pdf"
            make_pdf(source)

            metadata = read_pdf_metadata(source)
            self.assertEqual(metadata.title, "Sample Document")
            self.assertEqual(metadata.author, "Unknown author")

            library = Library(root / "data", root / "Books")
            book, created = library.import_book(source)
            duplicate, created_again = library.import_pdf(source)
            self.assertTrue(created)
            self.assertFalse(created_again)
            self.assertEqual(book.id, duplicate.id)
            self.assertEqual(book.format, "pdf")
            self.assertEqual(book.path.name, "Unknown author - Sample Document.pdf")
            updated, created_from_catalog = library.import_book(
                source, "Catalog Title", "Catalog Author"
            )
            self.assertFalse(created_from_catalog)
            self.assertEqual(updated.title, "Catalog Title")
            self.assertEqual(updated.author, "Catalog Author")
            library.connection.close()

    def test_rejects_invalid_pdf_and_unknown_format(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = root / "bad.pdf"
            invalid.write_bytes(b"not a pdf")
            with self.assertRaises(InvalidPdf):
                read_pdf_metadata(invalid)

            unknown = root / "book.rtf"
            unknown.write_text("not a book")
            library = Library(root / "data", root / "Books")
            with self.assertRaises(InvalidBook):
                library.import_book(unknown)
            library.connection.close()

    def test_reads_and_imports_cbz_with_natural_page_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "comic.cbz"
            image_data = make_comic(source)

            metadata = read_comic_metadata(source)
            self.assertEqual(metadata.title, "Issue One")
            self.assertEqual(metadata.author, "A Writer")
            self.assertEqual(
                list_comic_pages(source), ["pages/page2.png", "pages/page10.png"]
            )
            self.assertEqual(read_comic_page(source, "pages/page2.png"), image_data)

            library = Library(root / "data", root / "Books")
            book, created = library.import_book(source)
            self.assertTrue(created)
            self.assertEqual(book.format, "cbz")
            self.assertEqual(book.path.name, "A Writer - Issue One.cbz")
            library.connection.close()

    def test_rejects_zip_without_comic_images(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "not-a-comic.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("readme.txt", "No pages here")
            with self.assertRaises(InvalidComic):
                read_comic_metadata(path)

    def test_reads_and_imports_mobi_and_azw3(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = Library(root / "data", root / "Books")
            for extension in (".mobi", ".azw3"):
                source = root / f"book{extension}"
                make_mobi(source, f"Palm Book {extension[1:].upper()}")
                metadata = read_mobi_metadata(source)
                self.assertEqual(metadata.title, f"Palm Book {extension[1:].upper()}")
                book, created = library.import_book(source)
                self.assertTrue(created)
                self.assertEqual(book.format, extension[1:])
            library.connection.close()

    def test_rejects_invalid_mobi(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.mobi"
            path.write_bytes(b"not mobi")
            with self.assertRaises(InvalidMobi):
                read_mobi_metadata(path)

    def test_reads_plain_and_compressed_fb2(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plain = root / "book.fb2"
            plain.write_bytes(fb2_data())
            compressed = root / "book.fb2.zip"
            with zipfile.ZipFile(compressed, "w") as archive:
                archive.writestr("book.fb2", fb2_data())

            library = Library(root / "data", root / "Books")
            for source in (plain, compressed):
                metadata = read_fb2_metadata(source)
                self.assertEqual(metadata.title, "Fiction Book")
                self.assertEqual(metadata.author, "Test Writer")
                book, created = library.import_book(source)
                self.assertTrue(created)
                self.assertIn(book.format, ("fb2", "fbz"))
            library.connection.close()

    def test_rejects_invalid_compressed_fb2(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.fbz"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("readme.txt", "not an FB2")
            with self.assertRaises(InvalidFb2):
                read_fb2_metadata(path)

    def test_imports_text_markdown_and_html_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = Library(root / "data", root / "Books")
            sources = {
                "notes.txt": b"Plain text",
                "guide.md": b"# Markdown Guide\n\nRead me.",
                "page.html": b"<title>HTML Page</title><p>Read me.</p>",
            }
            for filename, content in sources.items():
                source = root / filename
                source.write_bytes(content)
                book, created = library.import_book(source)
                self.assertTrue(created)
                self.assertEqual(book.format, source.suffix[1:])
                self.assertTrue(book.path.exists())
            self.assertEqual(
                {book.title for book in library.list_books()},
                {"notes", "Markdown Guide", "HTML Page"},
            )
            library.connection.close()

    def test_migrates_hidden_managed_book_to_visible_library(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample.epub"
            make_epub(source)
            data_dir = root / "data"
            legacy = Library(data_dir, data_dir / "books")
            book, _created = legacy.import_epub(source)
            legacy.save_progress(book.id, "epubcfi(/6/2!/4)", 0, 0.25)
            legacy.connection.close()

            visible = Library(data_dir, root / "Books")
            migrated = visible.get_book(book.id)
            self.assertEqual(migrated.path.parent, root / "Books")
            self.assertTrue(migrated.path.exists())
            self.assertFalse(book.path.exists())
            self.assertEqual(migrated.progress_cfi, "epubcfi(/6/2!/4)")
            visible.connection.close()

    def test_stores_generic_catalog_connection_without_password(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = Library(root / "data", root / "Books")
            catalog = library.add_catalog(
                "My Catalog", "https://books.example/opds", "reader"
            )
            self.assertEqual(library.list_catalogs(), [catalog, *BUILTIN_CATALOGS])
            self.assertEqual(catalog.username, "reader")
            library.connection.close()

    def test_lists_builtin_catalogs_when_no_user_catalogs_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = Library(root / "data", root / "Books")
            self.assertEqual(library.list_catalogs(), list(BUILTIN_CATALOGS))
            self.assertTrue(all(catalog.is_builtin for catalog in BUILTIN_CATALOGS))
            library.connection.close()

    def test_user_catalog_replaces_builtin_with_the_same_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = Library(root / "data", root / "Books")
            custom = library.add_catalog(
                "My Gutenberg", BUILTIN_CATALOGS[0].url.rstrip("/"), ""
            )
            self.assertEqual(library.list_catalogs(), [custom, *BUILTIN_CATALOGS[1:]])
            library.connection.close()


if __name__ == "__main__":
    unittest.main()
