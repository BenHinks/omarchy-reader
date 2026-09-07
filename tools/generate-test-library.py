#!/usr/bin/env python3
"""Generate long, harmless documents for manually testing Omarchy Reader."""

from __future__ import annotations

import html
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_DIR / "test-library"
LOREM = (
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do eiusmod "
    "tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, "
    "quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo "
    "consequat. Duis aute irure dolor in reprehenderit in voluptate velit esse "
    "cillum dolore eu fugiat nulla pariatur. Excepteur sint occaecat cupidatat non "
    "proident, sunt in culpa qui officia deserunt mollit anim id est laborum."
)


def paragraphs(count: int = 5) -> list[str]:
    """Return numbered Lorem ipsum paragraphs that make progress visible."""
    return [f"Paragraph {number}. {LOREM}" for number in range(1, count + 1)]


def plain_text() -> str:
    """Build a long text document with readable plain-text structure."""
    sections = [
        "OMARCHY READER — PLAIN TEXT TEST\n"
        "================================\n\n"
        "Contents\n- Opening Notes\n- Reading Practice\n- Final Chapter\n"
    ]
    for chapter in range(1, 9):
        sections.append(
            f"\nCHAPTER {chapter}: SAMPLE TEXT\n"
            f"{'-' * 30}\n\n"
            + "\n\n".join(paragraphs(6))
            + "\n\nChecklist:\n  1. Turn a page\n  2. Drag the progress slider\n"
              "  3. Close and reopen the document\n"
        )
    return "\n".join(sections)


def markdown() -> str:
    """Build Markdown exercising the reader's supported formatting subset."""
    sections = [
        "# Omarchy Reader Markdown Test\n\n"
        "A long test document with **bold text**, *italic text*, `inline code`, "
        "and a [safe external link](https://example.com).\n\n"
        "## Contents\n\n- Opening Notes\n- Lists and Quotations\n- Code Samples\n- Final Chapter\n"
    ]
    for chapter in range(1, 9):
        sections.append(
            f"\n## Chapter {chapter}: Formatted Lorem Ipsum\n\n"
            + "\n\n".join(paragraphs(5))
            + "\n\n### Reading checklist\n\n"
              "1. Check the heading style\n2. Check the list spacing\n"
              "3. Move the progress slider\n\n"
              "> This quotation marks the middle of a chapter.\n\n"
              "```text\nprogress = saved_fraction\nencoding = UTF-8\n```\n\n---\n"
        )
    return "\n".join(sections)


def html_document(encoding: str) -> str:
    """Build standalone HTML with semantic, non-executable formatting."""
    sections = []
    for chapter in range(1, 9):
        body = "".join(f"<p>{html.escape(text)}</p>" for text in paragraphs(5))
        sections.append(
            f"<section><h2>Chapter {chapter}: Formatted Lorem Ipsum</h2>{body}"
            "<h3>Reading checklist</h3><ol><li>Check the heading style</li>"
            "<li>Check the list spacing</li><li>Move the progress slider</li></ol>"
            "<blockquote>This quotation marks the middle of a chapter.</blockquote>"
            "<table><thead><tr><th>Feature</th><th>Expected result</th></tr></thead>"
            "<tbody><tr><td>Strong text</td><td><strong>Visible</strong></td></tr>"
            "<tr><td>Emphasis</td><td><em>Visible</em></td></tr></tbody></table>"
            "<pre><code>progress = saved_fraction\nformat = HTML</code></pre></section>"
        )
    return (
        "<!doctype html><html><head>"
        f'<meta charset="{encoding}"><meta name="author" content="Omarchy Reader">'
        "<title>Omarchy Reader HTML Test</title></head><body>"
        "<h1>Omarchy Reader HTML Test</h1>"
        "<p>A long standalone HTML document — the reader’s encoding sample.</p>"
        "<ul><li>Headings</li><li>Lists</li><li>Tables</li><li>Code</li></ul>"
        + "".join(sections)
        + "</body></html>"
    )


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    text = plain_text()
    files = {
        "plain-text-utf8.txt": text.encode("utf-8"),
        "plain-text-windows-1252.txt": text.encode("cp1252"),
        "plain-text-utf16.txt": text.encode("utf-16"),
        "markdown-utf8.md": markdown().encode("utf-8"),
        "html-utf8.html": html_document("utf-8").encode("utf-8"),
        "html-windows-1252.html": html_document("windows-1252").encode("cp1252"),
    }
    for name, content in files.items():
        (OUTPUT_DIR / name).write_bytes(content)
    print(f"Generated {len(files)} documents in {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
