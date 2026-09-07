"""Fetch and parse DRM-free publications from OPDS 1.x and 2 catalogs.

Network operations are synchronous so callers can decide how to schedule them;
the GTK front end runs them on worker threads.  Basic-auth credentials exist
only for the lifetime of a request and are never persisted by this module.
"""

from __future__ import annotations

import base64
import contextlib
import json
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit


ATOM = "{http://www.w3.org/2005/Atom}"
OPDS = "{http://opds-spec.org/2010/catalog}"
EPUB_MIME = "application/epub+zip"
PDF_MIME = "application/pdf"
CBZ_MIME = "application/vnd.comicbook+zip"
CBZ_MIME_ALIASES = (CBZ_MIME, "application/x-cbz", "application/zip")
MOBI_MIME = "application/x-mobipocket-ebook"
AZW3_MIME = "application/vnd.amazon.mobi8-ebook"
FB2_MIME = "application/x-fictionbook+xml"
FBZ_MIME = "application/x-zip-compressed-fb2"
TXT_MIME = "text/plain"
MARKDOWN_MIME = "text/markdown"
MARKDOWN_MIME_ALIASES = (MARKDOWN_MIME, "text/x-markdown")
HTML_MIME = "text/html"
SUPPORTED_BOOK_TYPES = (
    EPUB_MIME, PDF_MIME, *CBZ_MIME_ALIASES,
    MOBI_MIME, AZW3_MIME, FB2_MIME, FBZ_MIME,
    TXT_MIME, *MARKDOWN_MIME_ALIASES, HTML_MIME,
)
ACQUISITION_REL = "http://opds-spec.org/acquisition"
OPEN_ACCESS_REL = f"{ACQUISITION_REL}/open-access"
FREE_ACQUISITION_RELS = {
    "download", "acquisition", ACQUISITION_REL, OPEN_ACCESS_REL,
}
OPDS_2_MIME = "application/opds+json"
OPDS_1_MIME = "application/atom+xml;profile=opds-catalog"
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


def _parse_atom_feed(data: bytes, base_url: str) -> OpdsFeed:
    """Parse OPDS 1 Atom while retaining direct, unpriced acquisitions."""
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
            if (not (set(link.get("rel", "").split()) & FREE_ACQUISITION_RELS)
                    or link.find(f"{OPDS}price") is not None
                    or link.find(f"{OPDS}indirectAcquisition") is not None
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


def _relations(link: dict) -> set[str]:
    """Normalize an OPDS 2 link's single or multiple relations."""
    relation = link.get("rel", [])
    if isinstance(relation, str):
        return set(relation.split())
    if isinstance(relation, list):
        return {item for item in relation if isinstance(item, str)}
    return set()


def _display_text(value, fallback: str = "") -> str:
    """Select readable text from a plain or localized manifest value."""
    if isinstance(value, str):
        return value.strip() or fallback
    if isinstance(value, dict):
        for key in ("en", "und"):
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key].strip()
        for candidate in value.values():
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return fallback


def _author_text(value) -> str:
    """Flatten the contributor forms allowed by publication manifests."""
    contributors = value if isinstance(value, list) else [value]
    names: list[str] = []
    for contributor in contributors:
        if isinstance(contributor, str):
            name = contributor.strip()
        elif isinstance(contributor, dict):
            name = _display_text(contributor.get("name"))
        else:
            name = ""
        if name and name not in names:
            names.append(name)
    return ", ".join(names)


def _link_size(link: dict) -> int | None:
    size = link.get("length")
    if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
        return size
    return None


def _is_direct_free_acquisition(link: dict) -> bool:
    """Accept free-compatible links with no price, DRM, or intermediary flow."""
    if not (_relations(link) & FREE_ACQUISITION_RELS):
        return False
    properties = link.get("properties")
    if not isinstance(properties, dict):
        return True
    return not any(
        marker in properties for marker in ("price", "encrypted", "indirectAcquisition")
    )


def _publication_entry(publication: dict, base_url: str) -> OpdsEntry | None:
    metadata = publication.get("metadata")
    links = publication.get("links")
    if not isinstance(metadata, dict) or not isinstance(links, list):
        return None
    acquisitions: list[OpdsAcquisition] = []
    seen: set[tuple[str, str]] = set()
    for link in links:
        if not isinstance(link, dict) or not _is_direct_free_acquisition(link):
            continue
        href = link.get("href")
        media_type = link.get("type", "")
        if not isinstance(href, str) or not href or not isinstance(media_type, str):
            continue
        media_type = media_type.split(";", 1)[0].strip()
        if media_type not in SUPPORTED_BOOK_TYPES:
            continue
        absolute_href = urljoin(base_url, href)
        identity = absolute_href, media_type
        if identity in seen:
            continue
        seen.add(identity)
        acquisitions.append(OpdsAcquisition(absolute_href, media_type, _link_size(link)))
    if not acquisitions:
        return None
    first = acquisitions[0]
    return OpdsEntry(
        _display_text(metadata.get("title"), "Untitled"),
        _author_text(metadata.get("author")),
        first.href,
        "book",
        first.media_type,
        first.size,
        tuple(acquisitions),
    )


def _navigation_entries(links, base_url: str) -> list[OpdsEntry]:
    entries: list[OpdsEntry] = []
    if not isinstance(links, list):
        return entries
    for link in links:
        if not isinstance(link, dict):
            continue
        href = link.get("href")
        media_type = link.get("type", "")
        if (not isinstance(href, str) or not href
                or (media_type and media_type.split(";", 1)[0].strip() != OPDS_2_MIME)):
            continue
        entries.append(OpdsEntry(
            _display_text(link.get("title"), "Untitled section"),
            "",
            urljoin(base_url, href),
            "navigation",
        ))
    return entries


def _parse_opds2_feed(data: bytes, base_url: str) -> OpdsFeed:
    """Parse an OPDS 2 JSON feed into the reader's shared catalog model."""
    try:
        root = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OpdsError("The server returned invalid OPDS JSON") from error
    if not isinstance(root, dict) or not any(
        key in root for key in ("navigation", "publications", "groups")
    ):
        raise OpdsError("The server response is not an OPDS 2 feed")

    metadata = root.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    entries = _navigation_entries(root.get("navigation"), base_url)
    publications = root.get("publications")
    if isinstance(publications, list):
        entries.extend(
            entry for publication in publications
            if isinstance(publication, dict)
            and (entry := _publication_entry(publication, base_url)) is not None
        )

    groups = root.get("groups")
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict):
                continue
            entries.extend(_navigation_entries(group.get("navigation"), base_url))
            group_publications = group.get("publications")
            if isinstance(group_publications, list):
                entries.extend(
                    entry for publication in group_publications
                    if isinstance(publication, dict)
                    and (entry := _publication_entry(publication, base_url)) is not None
                )

    facets = root.get("facets")
    if isinstance(facets, list):
        for facet in facets:
            if isinstance(facet, dict):
                entries.extend(_navigation_entries(facet.get("links"), base_url))

    next_url = None
    links = root.get("links")
    if isinstance(links, list):
        next_link = next((
            link for link in links
            if isinstance(link, dict) and "next" in _relations(link)
            and isinstance(link.get("href"), str) and link["href"]
        ), None)
        if next_link is not None:
            next_url = urljoin(base_url, next_link["href"])
    return OpdsFeed(
        _display_text(metadata.get("title"), "OPDS Catalog"),
        tuple(entries),
        next_url,
    )


def parse_feed(data: bytes, base_url: str) -> OpdsFeed:
    """Detect and parse an OPDS 1 Atom or OPDS 2 JSON feed."""
    if data.lstrip().startswith((b"{", b"[", b"\xef\xbb\xbf{", b"\xef\xbb\xbf[")):
        return _parse_opds2_feed(data, base_url)
    return _parse_atom_feed(data, base_url)


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
            _request(
                url, username, password,
                f"{OPDS_2_MIME}, {OPDS_1_MIME};q=0.9, application/atom+xml;q=0.8",
            ),
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
