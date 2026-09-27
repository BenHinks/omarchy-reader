# Omarchy Reader

Omarchy Reader is an offline-first ebook and comic reader for Linux. It follows
the active Omarchy theme, keeps imported books in a visible local library, and
remembers reading progress.

Supported formats are EPUB, PDF, CBZ/ZIP comics, MOBI/AZW3, FB2/FBZ, plain
text, Markdown, and standalone HTML.
OPDS 1.x catalogs and experimental OPDS 2 catalogs can provide any supported
format; when a free, DRM-free publication offers several formats, each is shown
as a separate download choice. Purchase, lending, subscription, preview,
encrypted, and indirect acquisition links are intentionally excluded.

## Requirements

- Python 3
- PyGObject with GTK 4 and libadwaita
- Poppler GLib
- WebKitGTK 6.0
- libsecret
- Cairo Python bindings (for the test suite)

The web reader is supplied by the pinned `foliate-js` Git submodule.

## Install and launch

Clone the project and its submodule, then run it directly:

```bash
git clone --recurse-submodules <repository-url>
cd omarchy-reader
./run
```

If the repository was cloned without submodules, initialize them with:

```bash
git submodule update --init --recursive
```

To add **Omarchy Reader** to the desktop Apps list:

```bash
./install-desktop
```

The launcher runs this checkout, so rerun the installer if the project directory
moves. Rerun it after upgrading to register any newly supported file types. No
system files or root privileges are required.

## User guide

Select **Add Book** and choose one of these sources:

- **From File** copies a supported file into the managed library.
- **From OPDS** lets you save a catalog URL and optional Basic-auth credentials,
  browse or filter its listing, and download a chosen free, DRM-free format.

Project Gutenberg and Unglue.it are included as built-in OPDS catalogs. Catalogs
you add are listed above the built-ins. If you add a built-in URL yourself, your
saved name and credentials take its place in the list. OPDS 2 support is
experimental; compatible catalogs may vary in how they represent navigation and
downloads.

Select a library row to resume reading. Use the on-screen previous/next controls,
the progress slider, or the Left/Right and Page Up/Page Down keys. Space advances
a page and Shift+Space goes back. EPUB-family books also show chapter marks on
the progress control. Open the F1 guide to adjust text size from 70% to 200% of
the active Omarchy theme size; the setting is remembered across books and app
restarts. TXT,
Markdown, and HTML documents use a themed scrolling view with page-sized jumps.

The library, catalogs, downloads, dialogs, and readers can all be operated from
the keyboard. On the library screen, Up/Down moves through Add Book, the books,
and Guide. A focused book starts on its trailing `>` Open action: Left moves
back through its available actions, while Right moves toward Open and opens the
book when `>` already has focus. Enter also opens the focused book. In the
initial Add Book screen, Left/Right switches between From File and From OPDS.
Within a catalog, Up/Down moves through entries and Left/Right chooses among a
book's download formats. Hierarchy rows focus their trailing `>` action; Right
opens the next level while `>` is focused, just as Enter does. Enter activates
the focused item and Escape moves back one level. Open the in-app keyboard guide
with F1 or Ctrl+G, or select **Guide (F1 or Ctrl-G)** in the library screen's
bottom bar.
The Guide also links directly to the managed library folder in Files.

Reader colors, typography, focus indicators, controls, and interaction states
follow the active Omarchy theme. If the theme changes while Reader is open, the
interface refreshes automatically. In reflowable ebooks, Reader normalizes the
font and ordinary body-text size so publisher styling cannot make chapters or
books in a collection unexpectedly smaller or switch them to another typeface.
Fixed-layout ebooks retain their designed typography.

Text documents recognize UTF-8, byte-order-marked UTF-16/UTF-32, UTF-16 without
a byte-order mark when its byte pattern is clear, Windows-1252, and Latin-1.
HTML charset declarations are honored. Imported HTML is sanitized: scripts,
forms, frames, active attributes, and remote images are removed before display.
Markdown supports common headings, paragraphs, emphasis, links, lists, quotes,
horizontal rules, inline code, and fenced code blocks; raw HTML is shown as text.

The trash button beside a book removes Omarchy Reader's managed copy and its
saved progress. It does not remove the original file from which the book was
imported or any remote catalog copy.

Passwords for OPDS catalogs are stored in the desktop keyring through libsecret.
The local database stores the catalog name, URL, and username, but not its
password. Prefer HTTPS catalogs; the app warns before sending credentials over
unencrypted HTTP.

## Uninstall

Remove the user-local desktop launcher and icon with:

```bash
./uninstall-desktop
```

Uninstalling deliberately keeps books and reading data. They can be reviewed or
removed separately at:

- Imported book copies: `~/Books/`
- Database and application state: `$XDG_DATA_HOME/omarchy-reader/` (normally
  `~/.local/share/omarchy-reader/`)

## Development

The first-party code is split by responsibility:

- `src/main.py` builds the GTK interface and coordinates reader views, downloads,
  and keyring access.
- `src/library.py` validates formats, imports and deduplicates files, and owns the
  SQLite data model.
- `src/opds.py` parses OPDS feeds and performs bounded network transfers.
- `src/document.py` decodes text encodings and safely renders TXT, Markdown, and
  standalone HTML.
- `src/theme.py` loads the current Omarchy palette and generates GTK CSS.
- `src/web/` contains the WebKit reader bridge and UI; `src/web/foliate-js/` is a
  pinned third-party submodule.

Run the unit tests from the repository root:

```bash
python3 -m unittest discover -s tests
node --test tests/document-reader.test.mjs
```

Generate a local library of long TXT, Markdown, and HTML fixtures in several
encodings for manual reader testing:

```bash
python3 tools/generate-test-library.py
```

## License

Omarchy Reader is available under the [MIT License](LICENSE). The foliate-js
submodule and its bundled components retain their own license notices.
