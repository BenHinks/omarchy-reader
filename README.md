# Omarchy Reader

Omarchy Reader is an offline-first ebook and comic reader for Linux. It follows
the active Omarchy theme, keeps imported books in a visible local library, and
remembers reading progress.

Supported formats are EPUB, PDF, CBZ/ZIP comics, MOBI/AZW3, and FB2/FBZ.
Generic OPDS 1.x catalogs can provide any supported format; when a publication
offers several formats, each one is shown as a separate download choice.

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
moves. No system files or root privileges are required.

## User guide

Select **Add Book** and choose one of these sources:

- **From This Device** copies a supported local file into the managed library.
- **From OPDS** lets you save a catalog URL and optional Basic-auth credentials,
  browse or search its listing, and download a chosen format.

Select a library row to resume reading. Use the on-screen previous/next controls,
the progress slider, or the Left/Right and Page Up/Page Down keys. Space advances
a page. EPUB-family books also show chapter marks on the progress control.

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
- `src/theme.py` loads the current Omarchy palette and generates GTK CSS.
- `src/web/` contains the WebKit reader bridge and UI; `src/web/foliate-js/` is a
  pinned third-party submodule.

Run the unit tests from the repository root:

```bash
python3 -m unittest discover -s tests
```

## License

Omarchy Reader is available under the [MIT License](LICENSE). The foliate-js
submodule and its bundled components retain their own license notices.
