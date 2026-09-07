import codecs
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from document import (
    InvalidDocument, decode_document, read_document, render_markdown, sanitize_html,
)


class DocumentTests(unittest.TestCase):
    def test_decodes_utf8_utf16_and_windows_1252(self):
        self.assertEqual(decode_document("Résumé".encode())[0], "Résumé")
        self.assertEqual(decode_document(codecs.BOM_UTF16_LE + "Hello".encode("utf-16-le"))[0], "Hello")
        text, encoding = decode_document(b"It\x92s ANSI")
        self.assertEqual(text, "It’s ANSI")
        self.assertEqual(encoding, "cp1252")

    def test_reads_declared_html_encoding_and_metadata(self):
        source = (
            b'<html><head><meta charset="windows-1252"><meta name="author" content="A Writer">'
            b"<title>A Reader\x92s Notes</title></head><body><h1>Hello</h1></body></html>"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notes.html"
            path.write_bytes(source)
            document = read_document(path)
        self.assertEqual(document.title, "A Reader’s Notes")
        self.assertEqual(document.author, "A Writer")
        self.assertEqual(document.encoding, "cp1252")
        self.assertIn("<h1>Hello</h1>", document.body_html)
        self.assertNotIn("<head", document.body_html)

    def test_markdown_renders_common_blocks_without_raw_html(self):
        rendered = render_markdown(
            "# My Notes\n\nA **bold** paragraph with `code`.\n\n- One\n- Two\n\n<script>bad()</script>"
        )
        self.assertIn("<h1>My Notes</h1>", rendered)
        self.assertIn("<strong>bold</strong>", rendered)
        self.assertIn("<code>code</code>", rendered)
        self.assertIn("<ul>", rendered)
        self.assertIn("&lt;script&gt;bad()&lt;/script&gt;", rendered)

    def test_html_sanitizer_removes_active_content_and_remote_images(self):
        rendered = sanitize_html(
            '<p onclick="bad()">Safe <strong>text</strong></p>'
            '<script>alert(1)</script><iframe src="https://example.com"></iframe>'
            '<img src="https://example.com/tracker.png" alt="blocked">'
            '<a href="javascript:bad()">bad link</a>'
        )
        self.assertEqual(
            rendered,
            '<p>Safe <strong>text</strong></p><img alt="blocked"><a rel="noreferrer noopener">bad link</a>',
        )

    def test_rejects_binary_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "binary.txt"
            path.write_bytes(b"\x00\x01\x02\x03")
            with self.assertRaises(InvalidDocument):
                read_document(path)


if __name__ == "__main__":
    unittest.main()
