import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opds import (
    AZW3_MIME, EPUB_MIME, FB2_MIME, FBZ_MIME, MOBI_MIME,
    OpdsEntry, OpdsError, OpdsFeed, _request,
    download_book, fetch_complete_feed, parse_feed,
)


FEED = b'''<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Test Catalog</title>
  <link rel="next" href="?page=2"/>
  <entry><title>Browse Authors</title>
    <link rel="subsection" type="application/atom+xml;profile=opds-catalog" href="authors"/>
  </entry>
  <entry><title>A Book</title><author><name>An Author</name></author>
    <link rel="http://opds-spec.org/acquisition" type="application/epub+zip" href="files/book.epub"/>
    <link rel="http://opds-spec.org/acquisition" type="application/x-mobipocket-ebook" length="7654321" href="files/book.mobi"/>
  </entry>
  <entry><title>A PDF</title><author><name>Another Author</name></author>
    <link rel="http://opds-spec.org/acquisition" type="application/pdf" length="12345678" href="files/book.pdf"/>
  </entry>
  <entry><title>A Comic</title>
    <link rel="http://opds-spec.org/acquisition" type="application/vnd.comicbook+zip" length="5000000" href="files/comic.cbz"/>
  </entry>
  <entry><title>A MOBI</title>
    <link rel="http://opds-spec.org/acquisition" type="application/x-mobipocket-ebook" href="files/book.mobi"/>
  </entry>
  <entry><title>An AZW3</title>
    <link rel="http://opds-spec.org/acquisition" type="application/vnd.amazon.mobi8-ebook" href="files/book.azw3"/>
  </entry>
  <entry><title>An FB2</title>
    <link rel="http://opds-spec.org/acquisition" type="application/x-fictionbook+xml" href="files/book.fb2"/>
  </entry>
  <entry><title>A Compressed FB2</title>
    <link rel="http://opds-spec.org/acquisition" type="application/x-zip-compressed-fb2" href="files/book.fb2.zip"/>
  </entry>
</feed>'''


class OpdsTests(unittest.TestCase):
    def test_parses_navigation_and_epub_acquisition(self):
        feed = parse_feed(FEED, "https://books.example/opds/")
        self.assertEqual(feed.title, "Test Catalog")
        self.assertEqual(feed.entries[0].kind, "navigation")
        self.assertEqual(feed.entries[0].href, "https://books.example/opds/authors")
        self.assertEqual(feed.entries[1].kind, "book")
        self.assertEqual(feed.entries[1].author, "An Author")
        self.assertEqual(feed.entries[1].media_type, "application/epub+zip")
        self.assertEqual(
            [item.media_type for item in feed.entries[1].available_acquisitions],
            [EPUB_MIME, MOBI_MIME],
        )
        self.assertEqual(feed.entries[1].available_acquisitions[1].size, 7654321)
        self.assertEqual(feed.entries[2].href, "https://books.example/opds/files/book.pdf")
        self.assertEqual(feed.entries[2].media_type, "application/pdf")
        self.assertEqual(feed.entries[2].size, 12345678)
        self.assertEqual(feed.entries[3].media_type, "application/vnd.comicbook+zip")
        self.assertEqual(feed.entries[3].size, 5000000)
        self.assertEqual(
            [entry.media_type for entry in feed.entries[4:]],
            [MOBI_MIME, AZW3_MIME, FB2_MIME, FBZ_MIME],
        )
        self.assertEqual(feed.next_url, "https://books.example/opds/?page=2")

    def test_follows_paginated_feed_for_complete_searchable_listing(self):
        first = OpdsFeed(
            "Series", (OpdsEntry("Book One", "Author", "one", "book", EPUB_MIME),),
            "https://books.example/opds?page=2",
        )
        second = OpdsFeed(
            "Series, page 2", (OpdsEntry("Book Two", "Author", "two", "book", EPUB_MIME),)
        )
        with patch("opds.fetch_feed", side_effect=(first, second)) as fetch:
            feed = fetch_complete_feed("https://books.example/opds", "reader", "secret")
        self.assertEqual([entry.title for entry in feed.entries], ["Book One", "Book Two"])
        self.assertEqual(feed.title, "Series")
        self.assertFalse(feed.truncated)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args_list[1].args[0], "https://books.example/opds?page=2")

    def test_download_reports_byte_progress(self):
        class Response(io.BytesIO):
            headers = {"Content-Length": "6"}

        reports = []
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "book.epub"
            with patch("urllib.request.urlopen", return_value=Response(b"abcdef")):
                download_book(
                    "https://books.example/book.epub", destination, EPUB_MIME,
                    progress=lambda downloaded, total: reports.append((downloaded, total)),
                )
            self.assertEqual(destination.read_bytes(), b"abcdef")
        self.assertEqual(reports, [(0, 6), (6, 6)])

    def test_rejects_non_atom_document(self):
        with self.assertRaises(OpdsError):
            parse_feed(b"<html/>", "https://books.example/opds")

    def test_allows_http_but_rejects_other_url_schemes(self):
        request = _request("http://books.lan/opds", "", "", "application/atom+xml")
        self.assertEqual(request.full_url, "http://books.lan/opds")
        with self.assertRaises(OpdsError):
            _request("ftp://books.lan/opds", "", "", "application/atom+xml")


if __name__ == "__main__":
    unittest.main()
