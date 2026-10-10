"""Which picture an XMB item shows, as plain Python (couchliteos_gtk_xmb loads and draws it).

An installed application shows its own, official icon: the one its package (or Flatpak) installs,
found through its .desktop file's Icon= in the hicolor icon theme, largest first, SVG best. No
logo is copied into this project. Everything else (categories, Settings, power, an application
with no icon of its own) shows a bundled icon from ICON_DIR by name, else FALLBACK.

Only files under the icon directories are ever used, only SVG or PNG, and none over MAX_BYTES.
"""

from __future__ import annotations

import configparser
import os
import pathlib
import re

import couchliteos_apps as apps

ICON_DIR = pathlib.Path("/usr/share/couchliteos/icons")
REPO_ICONS = pathlib.Path(__file__).resolve().parents[1] / "overlay/usr/share/couchliteos/icons"
FALLBACK = "app-window"
APPIMAGE_DIR = pathlib.Path("/opt/couchliteos/apps")  # Moonlight and chiaki-ng, unpacked
_DATA_HOME = pathlib.Path(os.environ.get("XDG_DATA_HOME") or pathlib.Path.home() / ".local/share")
SHARES = (
    pathlib.Path("/usr/share"),
    pathlib.Path("/usr/local/share"),
    pathlib.Path("/var/lib/flatpak/exports/share"),
    _DATA_HOME / "flatpak/exports/share",
)
SIZES = ("scalable", "512x512", "256x256", "192x192", "128x128", "96x96", "72x72", "64x64", "48x48")
SUFFIXES = (".svg", ".png")
MAX_BYTES = 2 * 1024 * 1024
MAX_DESKTOP_BYTES = 64 * 1024
ICON_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# .desktop ids of the applications CouchLiteOS ships, when they are not simply "<id>.desktop".
DESKTOP_IDS = {
    "moonlight": ("com.moonlight_stream.Moonlight",),
    "chiaki-ng": ("io.github.streetpea.Chiaki4deck", "chiaki-ng", "chiaki"),
    "firefox": ("firefox-esr", "firefox"),
    "google-chrome": ("google-chrome",),
}


def bundled(name: str, directory: pathlib.Path | None = None) -> pathlib.Path:
    """The bundled icon `name` (FALLBACK when there is no such icon)."""
    if directory is None:
        directory = ICON_DIR if ICON_DIR.is_dir() else REPO_ICONS
    if ICON_NAME_RE.fullmatch(name or ""):
        path = directory / f"{name}.svg"
        if path.is_file():
            return path
    return directory / f"{FALLBACK}.svg"


def _small_file(path: pathlib.Path, limit: int) -> bool:
    try:
        return path.is_file() and path.stat().st_size <= limit
    except OSError:
        return False


def desktop_entry(path: pathlib.Path) -> dict[str, str]:
    """The [Desktop Entry] keys of a .desktop file ({} when unreadable or too big)."""
    if not _small_file(path, MAX_DESKTOP_BYTES):
        return {}
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    try:
        parser.read_string(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, configparser.Error):
        return {}
    return dict(parser["Desktop Entry"]) if parser.has_section("Desktop Entry") else {}


def _program(app: apps.Application) -> str:
    return pathlib.PurePosixPath((apps.binaries(app.binary) or [app.command or ""])[0]).name


def _exec_program(entry: dict[str, str]) -> str:
    words = entry.get("Exec", "").split()
    while words and (words[0] == "env" or "=" in words[0]):  # "env A=b program"
        words.pop(0)
    return pathlib.PurePosixPath(words[0]).name if words else ""


def find_desktop(app: apps.Application, shares: tuple[pathlib.Path, ...] = SHARES) -> dict[str, str]:
    """The .desktop entry that belongs to `app`: by id (DESKTOP_IDS, its Flatpak, its own id),
    else the one whose Exec= runs the same program (not for a web shortcut). {} when none."""
    names = [*DESKTOP_IDS.get(app.id, ()), *([app.flatpak] if app.flatpak else []), app.id]
    folders = [share / "applications" for share in shares]
    for name in names:
        for folder in folders:
            entry = desktop_entry(folder / f"{name}.desktop")
            if entry:
                return entry
    program = _program(app)
    if not program or program in ("bash", "sh", "env") or "://" in app.arguments:
        return {}  # a web shortcut is not its browser: it gets the site's icon (couchliteos_art)
    for folder in folders:
        try:
            candidates = sorted(folder.glob("*.desktop"))[:2000]
        except OSError:
            continue
        for path in candidates:
            entry = desktop_entry(path)
            if entry and _exec_program(entry) == program:
                return entry
    return {}


def _inside(path: pathlib.Path, roots: list[pathlib.Path]) -> bool:
    try:
        real = path.resolve()
    except OSError:
        return False
    return any(real.is_relative_to(root.resolve()) for root in roots)


def resolve(icon: str, shares: tuple[pathlib.Path, ...] = SHARES) -> pathlib.Path | None:
    """An Icon= value as a file: the largest hicolor size there is (SVG first), else pixmaps."""
    roots = [share / "icons" for share in shares] + [share / "pixmaps" for share in shares]
    icon = (icon or "").strip()
    if icon.startswith("/"):
        path = pathlib.Path(icon)
        ok = path.suffix.lower() in SUFFIXES and _small_file(path, MAX_BYTES) and _inside(path, roots)
        return path if ok else None
    if not ICON_NAME_RE.fullmatch(icon):
        return None
    for size in SIZES:
        for share in shares:
            for suffix in SUFFIXES:
                path = share / "icons/hicolor" / size / "apps" / f"{icon}{suffix}"
                if _small_file(path, MAX_BYTES) and _inside(path, roots):
                    return path
    for share in shares:
        for suffix in SUFFIXES:
            path = share / "pixmaps" / f"{icon}{suffix}"
            if _small_file(path, MAX_BYTES) and _inside(path, roots):
                return path
    return None


def _appimage(app: apps.Application, folder: pathlib.Path) -> pathlib.Path | None:
    """The icon of an AppImage the image unpacks to APPIMAGE_DIR/<id>: its desktop entry's Icon=,
    in its own usr/share, else the file next to that entry (where every AppImage keeps it)."""
    if not ICON_NAME_RE.fullmatch(app.id) or not folder.is_dir():
        return None
    share = folder / "usr/share"
    entry = find_desktop(app, (share,))
    if not entry:
        try:
            entry = next((e for p in sorted(folder.glob("*.desktop"))[:20] if (e := desktop_entry(p))), {})
        except OSError:
            entry = {}
    icon = entry.get("Icon", "").strip()
    found = resolve(icon, (share,))
    if found is None and ICON_NAME_RE.fullmatch(icon):
        for suffix in SUFFIXES:
            path = folder / f"{icon}{suffix}"
            if _small_file(path, MAX_BYTES) and _inside(path, [folder]):
                return path
    return found


def official(app: apps.Application, shares: tuple[pathlib.Path, ...] = SHARES,
             appimages: pathlib.Path = APPIMAGE_DIR) -> pathlib.Path | None:
    """The application's own icon file, or None (then the item's bundled icon is used)."""
    found = _appimage(app, appimages / app.id)
    if found is not None:
        return found
    entry = find_desktop(app, shares)
    return resolve(entry.get("Icon", ""), shares) if entry else None


class Icons:
    """Icon files for XMB items, looked up once per application."""

    def __init__(self, shares: tuple[pathlib.Path, ...] = SHARES, directory: pathlib.Path | None = None,
                 appimages: pathlib.Path = APPIMAGE_DIR) -> None:
        self.shares = shares
        self.directory = directory
        self.appimages = appimages
        self.official: dict[str, pathlib.Path | None] = {}

    def for_item(self, icon: str, app: apps.Application | None = None) -> tuple[pathlib.Path, bool]:
        """(file, official): the application's own icon when it has one, else bundled `icon`."""
        if app is not None and not app.icon:  # a manifest's icon= is a choice: it wins
            if app.id not in self.official:
                try:
                    self.official[app.id] = official(app, self.shares, self.appimages)
                except OSError:
                    self.official[app.id] = None
            found = self.official[app.id]
            if found is not None:
                return found, True
        return bundled(icon, self.directory), False

    def forget(self) -> None:
        """Applications were installed or removed: look again."""
        self.official.clear()
