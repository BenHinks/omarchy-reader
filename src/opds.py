"""Fetch and parse the supported subset of OPDS 1.x catalogs.

Network operations are synchronous so callers can decide how to schedule them;
the GTK front end runs them on worker threads.  Basic-auth credentials exist
only for the lifetime of a request and are never persisted by this module.
"""

from __future__ import annotations

import base64
import contextlib
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit


ATOM = "{http://www.w3.org/2005/Atom}"
EPUB_MIME = "application/epub+zip"
PDF_MIME = "application/pdf"
CBZ_MIME = "application/vnd.comicbook+zip"
CBZ_MIME_ALIASES = (CBZ_MIME, "application/x-cbz", "application/zip")
MOBI_MIME = "application/x-mobipocket-ebook"
AZW3_MIME = "application/vnd.amazon.mobi8-ebook"
FB2_MIME = "application/x-fictionbook+xml"
FBZ_MIME = "application/x-zip-compressed-fb2"
SUPPORTED_BOOK_TYPES = (
    EPUB_MIME, PDF_MIME, *CBZ_MIME_ALIASES,
    MOBI_MIME, AZW3_MIME, FB2_MIME, FBZ_MIME,
)
ACQUISITION_REL = "http://opds-spec.org/acquisition"
MAX_FEED_PAGES = 100
MAX_FEED_ENTRIES = 20_000


class OpdsError(RuntimeError):
    """A user-presentable catalog, protocol, or download failure."""

    pass


@dataclass(frozen=True)
class OpdsAcquisition:
    """One downloadable format advertised for an OPDS publication."""

    href: str
    media_type: str
    size: int | None = None


@dataclass(frozen=True)
class OpdsEntry:
    """A navigation link or downloadable publication in an OPDS feed."""

    title: str
    author: str
    href: str
    kind: str
    media_type: str = ""
    size: int | None = None
    acquisitions: tuple[OpdsAcquisition, ...] = ()

    @property
    def available_acquisitions(self) -> tuple[OpdsAcquisition, ...]:
        """Return all formats, including entries made by older callers."""
        if self.acquisitions:
            return self.acquisitions
        if self.kind == "book" and self.href and self.media_type:
            return (OpdsAcquisition(self.href, self.media_type, self.size),)
        return ()


@dataclass(frozen=True)
class OpdsFeed:
    """A parsed feed, including pagination state and safety-limit status."""

    title: str
    entries: tuple[OpdsEntry, ...]
    next_url: str | None = None
    truncated: bool = False


def _text(parent: ET.Element, path: str) -> str:
    node = parent.find(path)
    return "" if node is None or node.text is None else node.text.strip()


def parse_feed(data: bytes, base_url: str) -> OpdsFeed:
    """Parse an Atom feed and retain supported acquisitions and navigation."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise OpdsError("The server returned invalid OPDS XML") from error
    if root.tag != f"{ATOM}feed":
        raise OpdsError("The server response is not an OPDS Atom feed")

    entries: list[OpdsEntry] = []
    for node in root.findall(f"{ATOM}entry"):
        title = _text(node, f"{ATOM}title") or "Untitled"
        author = _text(node, f"{ATOM}author/{ATOM}name")
        links = node.findall(f"{ATOM}link")
        acquisitions: list[OpdsAcquisition] = []
        seen_acquisitions: set[tuple[str, str]] = set()
        for link in links:
            media_type = link.get("type", "").split(";", 1)[0].strip()
            href = link.get("href")
            if (not link.get("rel", "").startswith(ACQUISITION_REL)
                    or media_type not in SUPPORTED_BOOK_TYPES or not href):
                continue
            absolute_href = urljoin(base_url, href)
            identity = absolute_href, media_type
            if identity in seen_acquisitions:
                continue
            seen_acquisitions.add(identity)
            try:
                size = int(link.get("length", ""))
                if size < 0:
                    size = None
            except ValueError:
                size = None
            acquisitions.append(OpdsAcquisition(absolute_href, media_type, size))
        if acquisitions:
            first = acquisitions[0]
            entries.append(OpdsEntry(
                title, author, first.href, "book", first.media_type, first.size,
                tuple(acquisitions),
            ))
            continue
        navigation = next((link for link in links
            if link.get("href") and "atom+xml" in link.get("type", "")), None)
        if navigation is not None:
            entries.append(OpdsEntry(title, author, urljoin(base_url, navigation.get("href")), "navigation"))
    next_link = next((link for link in root.findall(f"{ATOM}link")
        if "next" in link.get("rel", "").split() and link.get("href")), None)
    next_url = urljoin(base_url, next_link.get("href")) if next_link is not None else None
    return OpdsFeed(
        _text(root, f"{ATOM}title") or "OPDS Catalog", tuple(entries), next_url
    )


def _request(url: str, username: str, password: str, accept: str) -> urllib.request.Request:
    scheme = urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise OpdsError("OPDS catalog URLs must use HTTP or HTTPS")
    headers = {"Accept": accept, "User-Agent": "Omarchy Reader/0.1"}
    if username:
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    return urllib.request.Request(url, headers=headers)


def fetch_feed(url: str, username: str = "", password: str = "") -> OpdsFeed:
    """Fetch and parse one OPDS page with bounded response size and timeout."""
    try:
        with urllib.request.urlopen(
            _request(url, username, password, "application/atom+xml;profile=opds-catalog"),
            timeout=20,
        ) as response:
            return parse_feed(response.read(10_000_000), response.geturl())
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise OpdsError("The catalog rejected the supplied credentials") from error
        raise OpdsError(f"The catalog returned HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise OpdsError(f"Could not connect to the catalog: {error}") from error


def fetch_complete_feed(url: str, username: str = "", password: str = "") -> OpdsFeed:
    """Follow pagination until complete or a configured safety limit is met."""
    entries: list[OpdsEntry] = []
    seen_urls: set[str] = set()
    first_title = "OPDS Catalog"
    next_url: str | None = url

    for page_number in range(MAX_FEED_PAGES):
        if not next_url:
            return OpdsFeed(first_title, tuple(entries))
        if next_url in seen_urls:
            return OpdsFeed(first_title, tuple(entries), next_url, truncated=True)
        seen_urls.add(next_url)
        page = fetch_feed(next_url, username, password)
        if page_number == 0:
            first_title = page.title
        remaining = MAX_FEED_ENTRIES - len(entries)
        entries.extend(page.entries[:remaining])
        if len(entries) >= MAX_FEED_ENTRIES:
            truncated = bool(page.next_url or len(page.entries) > remaining)
            return OpdsFeed(first_title, tuple(entries), page.next_url, truncated=truncated)
        next_url = page.next_url

    return OpdsFeed(first_title, tuple(entries), next_url, truncated=bool(next_url))


def download_book(
    url: str, destination, media_type: str, username: str = "", password: str = "",
    expected_size: int | None = None, progress=None,
) -> None:
    """Stream one supported acquisition to disk while reporting byte progress."""
    if media_type not in SUPPORTED_BOOK_TYPES:
        raise OpdsError("The catalog book format is not supported")
    try:
        with urllib.request.urlopen(
            _request(url, username, password, media_type), timeout=60
        ) as response, destination.open("wb") as output:
            with contextlib.suppress(TypeError, ValueError):
                expected_size = int(response.headers.get("Content-Length")) or expected_size
            total = 0
            if progress:
                progress(total, expected_size)
            while block := response.read(1024 * 1024):
                total += len(block)
                if total > 1_000_000_000:
                    raise OpdsError("The book exceeds the supported download size")
                output.write(block)
                if progress:
                    progress(total, expected_size)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise OpdsError("The download was rejected by the catalog") from error
        raise OpdsError(f"The download returned HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise OpdsError(f"Could not download the book: {error}") from error
