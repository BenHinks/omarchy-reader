"""Decode and safely render plain text, Markdown, and standalone HTML files."""

from __future__ import annotations

import codecs
import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit


MAX_DOCUMENT_SIZE = 25_000_000
DOCUMENT_EXTENSIONS = {".txt", ".md", ".markdown", ".html", ".htm"}


class InvalidDocument(ValueError):
    """Raised when a text document cannot be safely decoded or rendered."""


@dataclass(frozen=True)
class RenderedDocument:
    """Normalized metadata and safe HTML ready for the document reader."""

    title: str
    author: str
    encoding: str
    body_html: str


def _declared_html_encoding(data: bytes) -> str | None:
    """Read an HTML5-style charset declaration from the first few kilobytes."""
    head = data[:4096]
    match = re.search(
        br"<meta\s+[^>]*charset\s*=\s*['\"]?\s*([a-zA-Z0-9._-]+)",
        head,
        flags=re.IGNORECASE,
    )
    if match is None:
        match = re.search(
            br"<meta\s+[^>]*content\s*=\s*['\"][^'\"]*charset\s*=\s*([a-zA-Z0-9._-]+)",
            head,
            flags=re.IGNORECASE,
        )
    if match is None:
        return None
    try:
        return codecs.lookup(match.group(1).decode("ascii")).name
    except (LookupError, UnicodeDecodeError):
        return None


def decode_document(data: bytes, declared_encoding: str | None = None) -> tuple[str, str]:
    """Decode bytes using BOMs, declarations, UTF heuristics, then ANSI fallbacks.

    "ANSI" is not one encoding. Windows-1252 is the most useful Western fallback;
    ISO-8859-1 is retained as a final byte-preserving fallback for unusual files.
    """
    for marker, encoding in (
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if data.startswith(marker):
            return data.decode(encoding), codecs.lookup(encoding).name

    if declared_encoding:
        try:
            normalized = codecs.lookup(declared_encoding).name
            return data.decode(normalized, errors="replace"), normalized
        except LookupError:
            pass

    # UTF-16 files occasionally omit their BOM. A strong alternating-NUL pattern
    # is more dependable here than treating them as a legacy single-byte file.
    sample = data[:4096]
    if len(sample) >= 4:
        even_nuls = sample[0::2].count(0)
        odd_nuls = sample[1::2].count(0)
        pairs = max(1, len(sample) // 2)
        if odd_nuls / pairs > 0.3 and even_nuls / pairs < 0.05:
            return data.decode("utf-16-le"), "utf-16-le"
        if even_nuls / pairs > 0.3 and odd_nuls / pairs < 0.05:
            return data.decode("utf-16-be"), "utf-16-be"

    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        try:
            return data.decode("cp1252"), "cp1252"
        except UnicodeDecodeError:
            return data.decode("latin-1"), "iso8859-1"


def _safe_url(value: str, *, image: bool = False) -> str | None:
    value = value.strip()
    if image:
        if re.match(r"^data:image/(?:png|jpe?g|gif|webp);base64,", value, re.IGNORECASE):
            return value
        return None
    if value.startswith("#"):
        return value
    return value if urlsplit(value).scheme.lower() in {"http", "https", "mailto"} else None


class _MetadataParser(HTMLParser):
    """Extract title and author without trusting or rendering the source HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_title = False
        self.title_parts: list[str] = []
        self.author = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = {name.casefold(): value or "" for name, value in attrs}
        if tag.casefold() == "title":
            self.in_title = True
        if tag.casefold() == "meta" and attributes.get("name", "").casefold() == "author":
            self.author = attributes.get("content", "").strip()

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)


class _SafeHTMLParser(HTMLParser):
    """Retain reading-oriented markup while removing active web content."""

    ALLOWED_TAGS = {
        "a", "abbr", "b", "blockquote", "br", "caption", "cite", "code",
        "dd", "del", "details", "div", "dl", "dt", "em", "figcaption",
        "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img",
        "kbd", "li", "mark", "ol", "p", "pre", "q", "s", "samp", "small",
        "span", "strong", "sub", "summary", "sup", "table", "tbody", "td",
        "tfoot", "th", "thead", "tr", "u", "ul", "var",
    }
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
    DROP_CONTENT = {
        "applet", "audio", "button", "canvas", "embed", "form", "head",
        "iframe", "input", "noscript", "object", "script", "select", "style",
        "svg", "template", "textarea", "video",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.output: list[str] = []
        self.blocked_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.casefold()
        if self.blocked_depth:
            if tag not in self.VOID_TAGS:
                self.blocked_depth += 1
            return
        if tag in self.DROP_CONTENT:
            self.blocked_depth = 1
            return
        if tag not in self.ALLOWED_TAGS:
            return

        safe_attrs: list[tuple[str, str]] = []
        for name, value in attrs:
            name = name.casefold()
            value = value or ""
            if name in {"title", "alt", "lang", "dir"}:
                safe_attrs.append((name, value))
            elif tag == "a" and name == "href" and (safe := _safe_url(value)):
                safe_attrs.append((name, safe))
            elif tag == "img" and name == "src" and (safe := _safe_url(value, image=True)):
                safe_attrs.append((name, safe))
            elif tag in {"td", "th"} and name in {"colspan", "rowspan"} and value.isdigit():
                safe_attrs.append((name, str(min(100, max(1, int(value))))))
        if tag == "a":
            safe_attrs.append(("rel", "noreferrer noopener"))
        rendered_attrs = "".join(
            f' {name}="{html.escape(value, quote=True)}"' for name, value in safe_attrs
        )
        self.output.append(f"<{tag}{rendered_attrs}>")

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self.blocked_depth:
            self.blocked_depth -= 1
            return
        if tag in self.ALLOWED_TAGS and tag not in self.VOID_TAGS:
            self.output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.blocked_depth:
            self.output.append(html.escape(data))


def sanitize_html(source: str) -> str:
    """Reduce arbitrary HTML to inert, reading-oriented markup."""
    parser = _SafeHTMLParser()
    parser.feed(source)
    parser.close()
    return "".join(parser.output)


def _markdown_inline(value: str) -> str:
    """Render a deliberately small, safe subset of inline Markdown."""
    escaped = html.escape(value)
    placeholders: list[str] = []

    def protect(rendered: str) -> str:
        placeholders.append(rendered)
        return f"\x00{len(placeholders) - 1}\x00"

    escaped = re.sub(
        r"`([^`]+)`",
        lambda match: protect(f"<code>{match.group(1)}</code>"),
        escaped,
    )

    def link(match: re.Match[str]) -> str:
        href = html.unescape(match.group(2))
        safe = _safe_url(href)
        if safe is None:
            return match.group(1)
        return protect(
            f'<a href="{html.escape(safe, quote=True)}" rel="noreferrer noopener">'
            f"{match.group(1)}</a>"
        )

    escaped = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, escaped)
    escaped = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)|(?<!_)_([^_\n]+)_(?!_)", lambda m: f"<em>{m.group(1) or m.group(2)}</em>", escaped)
    return re.sub(r"\x00(\d+)\x00", lambda m: placeholders[int(m.group(1))], escaped)


def render_markdown(source: str) -> str:
    """Convert common Markdown blocks to safe HTML without external packages."""
    output: list[str] = []
    paragraph: list[str] = []
    list_kind: str | None = None
    code_lines: list[str] | None = None

    def flush_paragraph() -> None:
        if paragraph:
            output.append(f"<p>{_markdown_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal list_kind
        if list_kind:
            output.append(f"</{list_kind}>")
            list_kind = None

    for line in source.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if code_lines is not None:
            if line.startswith("```"):
                output.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
                code_lines = None
            else:
                code_lines.append(line)
            continue
        if line.startswith("```"):
            flush_paragraph()
            close_list()
            code_lines = []
            continue
        if not line.strip():
            flush_paragraph()
            close_list()
            continue
        if match := re.match(r"^(#{1,6})\s+(.+)$", line):
            flush_paragraph()
            close_list()
            level = len(match.group(1))
            output.append(f"<h{level}>{_markdown_inline(match.group(2).strip())}</h{level}>")
            continue
        if re.match(r"^\s*(?:[-*_]\s*){3,}$", line):
            flush_paragraph()
            close_list()
            output.append("<hr>")
            continue
        if match := re.match(r"^\s*[-+*]\s+(.+)$", line):
            flush_paragraph()
            if list_kind != "ul":
                close_list()
                output.append("<ul>")
                list_kind = "ul"
            output.append(f"<li>{_markdown_inline(match.group(1))}</li>")
            continue
        if match := re.match(r"^\s*\d+[.)]\s+(.+)$", line):
            flush_paragraph()
            if list_kind != "ol":
                close_list()
                output.append("<ol>")
                list_kind = "ol"
            output.append(f"<li>{_markdown_inline(match.group(1))}</li>")
            continue
        if match := re.match(r"^\s*>\s?(.*)$", line):
            flush_paragraph()
            close_list()
            output.append(f"<blockquote>{_markdown_inline(match.group(1))}</blockquote>")
            continue
        close_list()
        paragraph.append(line.strip())

    if code_lines is not None:
        output.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
    flush_paragraph()
    close_list()
    return "\n".join(output)


def read_document(path: Path) -> RenderedDocument:
    """Validate, decode, and render a supported text-based document."""
    extension = path.suffix.casefold()
    if extension not in DOCUMENT_EXTENSIONS:
        raise InvalidDocument("Only TXT, Markdown, and HTML documents are supported")
    try:
        if path.stat().st_size > MAX_DOCUMENT_SIZE:
            raise InvalidDocument("The document exceeds the supported 25 MB size limit")
        data = path.read_bytes()
    except OSError as error:
        raise InvalidDocument("The document could not be read") from error
    sample = data[:4096]
    if b"\0" in sample and not data.startswith(
        (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE, codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)
    ):
        pairs = max(1, len(sample) // 2)
        even_ratio = sample[0::2].count(0) / pairs
        odd_ratio = sample[1::2].count(0) / pairs
        if not (
            (odd_ratio > 0.3 and even_ratio < 0.05)
            or (even_ratio > 0.3 and odd_ratio < 0.05)
        ):
            raise InvalidDocument("The selected document appears to be a binary file")

    declared = _declared_html_encoding(data) if extension in {".html", ".htm"} else None
    try:
        source, encoding = decode_document(data, declared)
    except UnicodeDecodeError as error:
        raise InvalidDocument("The document's text encoding could not be decoded") from error
    controls = sum(
        ord(character) < 32 and character not in "\t\n\r\f"
        for character in source
    )
    if controls and (len(source) < 100 or controls / len(source) > 0.02):
        raise InvalidDocument("The selected document appears to be a binary file")
    title = path.stem
    author = "Unknown author"

    if extension == ".txt":
        body = f'<pre class="plain-text">{html.escape(source)}</pre>'
    elif extension in {".md", ".markdown"}:
        heading = re.search(r"^#\s+(.+?)\s*$", source, flags=re.MULTILINE)
        if heading:
            title = re.sub(r"[*_`]", "", heading.group(1)).strip() or title
        body = render_markdown(source)
    else:
        metadata = _MetadataParser()
        metadata.feed(source)
        extracted_title = " ".join("".join(metadata.title_parts).split())
        title = extracted_title or title
        author = metadata.author or author
        body = sanitize_html(source)

    return RenderedDocument(title, author, encoding, body)
