#!/usr/bin/python3
"""Themes: one small INI file of named colours, turned into foot, OSC and GTK CSS colours.

A theme file (`<name>.theme`) has a [theme] section with the colours FIELDS as #rrggbb,
an optional `name` (the label in Settings) and an optional `wallpaper`. Built-ins live in
BUILTIN_DIR, user themes in ~/.config/couchliteos/themes/; a bad file is skipped whole and
reported, never applied halfway. The choice ([appearance] theme and accent) is in config.ini.

foot windows get the colours as `-o colors.*` options when they start (couchliteos_foot.py,
on the boot path, so `current` never raises) and running ones through OSC 4/10/11
sequences: the launcher writes them to its own terminal and to OSK_FILE, which the
on-screen keyboard copies to its terminal. Curses has no colour pairs, so only the palette
changes there.
"""

from __future__ import annotations

import configparser
import dataclasses
import os
import pathlib
import re
import stat
import tempfile

import couchliteos_month as month

BUILTIN_DIR = pathlib.Path("/usr/share/couchliteos/themes")
CONFIG = pathlib.Path(os.environ.get("COUCHLITEOS_STATE_DIR", "/var/lib/couchliteos")) / "config.ini"
SECTION = "appearance"
# The OSC string of the current theme, for the on-screen keyboard's foot window.
OSK_FILE = pathlib.Path(os.environ.get("COUCHLITEOS_RUN_DIR", "/run/couchliteos")) / "theme.osc"
SUFFIX = ".theme"
DEFAULT = "midnight"
FIELDS = ("background", "surface", "text", "muted", "accent", "focus", "warning", "error")
MAX_FILE = 4096
MIN_CONTRAST = 4.5  # WCAG AA for normal text
# WCAG's 3 to 1 for what is not body text: the focused tile's edge, the bold values in Settings.
ACCENT_CONTRAST = 3.0
COLOUR = re.compile(r"#?([0-9a-fA-F]{6})")
THEME_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
# Accent presets (Settings > APPEARANCE > ACCENT); "" keeps the theme's own accent, and
# month.NAME (BY MONTH) is the colour of the month (couchliteos_month).
ACCENTS = (
    ("blue", "3b82f6"), ("amber", "f59e0b"), ("green", "22c55e"), ("red", "ef4444"),
    ("purple", "a855f7"), ("pink", "ec4899"), ("teal", "14b8a6"), ("orange", "f97316"),
)
# foot's 16-colour palette slots, by theme colour; the rest keep foot's defaults.
PALETTE = {0: "background", 1: "error", 3: "warning", 4: "accent", 7: "text", 8: "muted", 15: "text"}
OSC = re.compile(r"(?:\x1b\][0-9;:a-z/]{1,200}\x1b\\)+")
# Used when even the default file is missing or broken: MIDNIGHT's colours.
FALLBACK_COLOURS = {
    "background": "0b1020", "surface": "161d33", "text": "f1f5f9", "muted": "94a3b8",
    "accent": "3b82f6", "focus": "1d4ed8", "warning": "fbbf24", "error": "f87171",
}


class ThemeError(ValueError):
    """A theme file that cannot be used; the message says why, in screen text."""


@dataclasses.dataclass(frozen=True)
class Theme:
    name: str  # the file name without .theme; what config.ini stores
    label: str
    colours: dict[str, str]  # FIELDS -> "rrggbb" (lower case, no #)
    wallpaper: str = ""


FALLBACK = Theme(DEFAULT, "MIDNIGHT", FALLBACK_COLOURS)


def parse_colour(value: str) -> str:
    match = COLOUR.fullmatch(value.strip())
    if match is None:
        raise ThemeError(f"BAD COLOUR {value.strip()[:16]!r}")
    return match.group(1).lower()


def load(path: pathlib.Path) -> Theme:
    """The theme in `path`. Raises ThemeError for a missing, unreadable or malformed file."""
    name = path.name[: -len(SUFFIX)] if path.name.endswith(SUFFIX) else path.name
    if THEME_NAME.fullmatch(name) is None:
        raise ThemeError("BAD FILE NAME")
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
            raise ThemeError("NOT A SMALL REGULAR FILE")
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(path.read_text(encoding="utf-8"))
    except OSError:
        raise ThemeError("CANNOT BE READ") from None
    except (configparser.Error, UnicodeDecodeError):
        raise ThemeError("NOT AN INI FILE") from None
    if not parser.has_section("theme"):
        raise ThemeError("NO [theme] SECTION")
    section = parser["theme"]
    missing = [field for field in FIELDS if not section.get(field, "").strip()]
    if missing:
        raise ThemeError(f"MISSING {missing[0].upper()}")
    colours = {field: parse_colour(section[field]) for field in FIELDS}
    label = " ".join(section.get("name", "").split()).upper() or name.replace("-", " ").upper()
    return Theme(name, label[:32], colours, section.get("wallpaper", "").strip())


def user_dir() -> pathlib.Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(pathlib.Path.home() / ".config")
    return pathlib.Path(base) / "couchliteos" / "themes"


def available(builtin: pathlib.Path | None = None, user: pathlib.Path | None = None
              ) -> tuple[dict[str, Theme], list[str]]:
    """Every usable theme by name (built-ins first) and one line per skipped file."""
    themes: dict[str, Theme] = {}
    problems: list[str] = []
    for directory in (BUILTIN_DIR if builtin is None else builtin, user_dir() if user is None else user):
        try:
            paths = sorted(directory.glob(f"*{SUFFIX}"))
        except OSError:
            continue
        for path in paths:
            try:
                theme = load(path)
            except ThemeError as error:
                problems.append(f"{path.name}: {error}")
                continue
            if theme.name in themes:
                problems.append(f"{path.name}: NAME ALREADY USED")
                continue
            themes[theme.name] = theme
    return themes, problems


def with_accent(theme: Theme, accent: str) -> Theme:
    """The theme with an ACCENTS preset, or today's colour of the month, in place of its own
    accent (kept readable: readable_accent); unknown or "" keeps it."""
    colour = month.colour() if accent == month.NAME else dict(ACCENTS).get(accent)
    if colour is None:
        return theme
    return dataclasses.replace(theme, colours={**theme.colours, "accent": readable_accent(theme, colour)})


def readable_accent(theme: Theme, colour: str) -> str:
    """`colour`, or as little of the theme's text colour mixed in as keeps ACCENT_CONTRAST against
    the surface and the background. The accent draws the values in Settings and the focused tile's
    edge: AMBER or October's gold would all but vanish on a light theme, so they turn darker there."""
    colours = theme.colours
    for step in range(11):
        mixed = _mix(colour, colours["text"], step / 10)
        if min(contrast(mixed, colours["surface"]), contrast(mixed, colours["background"])) >= ACCENT_CONTRAST:
            return mixed
    return colours["text"]


def _mix(a: str, b: str, amount: float) -> str:
    return "".join(f"{round(int(a[at:at + 2], 16) * (1 - amount) + int(b[at:at + 2], 16) * amount):02x}"
                   for at in (0, 2, 4))


def _luminance(colour: str) -> float:
    def channel(value: int) -> float:
        value /= 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (int(colour[index:index + 2], 16) for index in (0, 2, 4))
    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio of two colours (1.0 to 21.0)."""
    light, dark = sorted((_luminance(parse_colour(a)), _luminance(parse_colour(b))), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def on_focus(theme: Theme) -> str:
    """The text colour on a focus fill: `text`, or `background` where text is not readable on it
    (HIGH CONTRAST draws black on its yellow focus)."""
    colours = theme.colours
    if contrast(colours["text"], colours["focus"]) >= MIN_CONTRAST:
        return colours["text"]
    return colours["background"]


def foot_options(theme: Theme) -> list[str]:
    """foot `-o colors.*` arguments for a new window."""
    colours = theme.colours
    options = [f"colors.background={colours['background']}", f"colors.foreground={colours['text']}"]
    for slot, field in PALETTE.items():
        options.append(f"colors.{'regular' if slot < 8 else 'bright'}{slot % 8}={colours[field]}")
    return [part for option in options for part in ("-o", option)]


def _rgb(colour: str) -> str:
    return f"rgb:{colour[0:2]}/{colour[2:4]}/{colour[4:6]}"


def osc(theme: Theme) -> str:
    """OSC 4 (palette), 10 (foreground) and 11 (background) that recolour a running terminal."""
    colours = theme.colours
    palette = ";".join(f"{slot};{_rgb(colours[field])}" for slot, field in PALETTE.items())
    return (
        f"\x1b]4;{palette}\x1b\\"
        f"\x1b]10;{_rgb(colours['text'])}\x1b\\"
        f"\x1b]11;{_rgb(colours['background'])}\x1b\\"
    )


def css(theme: Theme) -> str:
    """GTK CSS variables (--couchliteos-<field>) for the GTK screens."""
    values = {**theme.colours, "on-focus": on_focus(theme)}
    lines = [f"  --couchliteos-{field}: #{colour};" for field, colour in values.items()]
    return ":root {\n" + "\n".join(lines) + "\n}\n"


def load_choice(path: pathlib.Path | None = None) -> tuple[str, str]:
    """The saved (theme, accent); anything missing or unreadable gives (DEFAULT, "")."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string((CONFIG if path is None else path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, configparser.Error):
        return DEFAULT, ""
    name = parser.get(SECTION, "theme", fallback="").strip().lower()
    accent = parser.get(SECTION, "accent", fallback="").strip().lower()
    known = accent in dict(ACCENTS) or accent == month.NAME
    return (name if THEME_NAME.fullmatch(name) else DEFAULT), (accent if known else "")


def accent_label(accent: str) -> str:
    """The saved accent as Settings shows it."""
    return month.LABEL if accent == month.NAME else accent.upper() or "THEME DEFAULT"


def _write_atomically(path: pathlib.Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def load_interface(path: pathlib.Path | None = None) -> str:
    """`classic` (the curses launcher) or `tv`, as scripts/couchliteos-session reads it."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string((CONFIG if path is None else path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, configparser.Error):
        return "tv"
    return "classic" if parser.get(SECTION, "interface", fallback="").strip().lower() == "classic" else "tv"


def load_value(key: str, fallback: str = "", path: pathlib.Path | None = None) -> str:
    """Any [appearance] key, lower case and stripped ([appearance] home, motion); `fallback` when
    the file or the key is missing or unreadable."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string((CONFIG if path is None else path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, configparser.Error):
        return fallback
    return parser.get(SECTION, key, fallback=fallback).strip().lower()


def save_interface(interface: str, path: pathlib.Path | None = None) -> None:
    """Raises OSError. The session reads it when the launcher service starts again."""
    save_values({"interface": "classic" if interface == "classic" else "tv"}, path)


def save_choice(name: str, accent: str, path: pathlib.Path | None = None) -> None:
    """Rewrite theme and accent in the [appearance] section of config.ini, atomically. Raises OSError."""
    save_values({"theme": name, "accent": accent}, path)


def save_values(values: dict[str, str], path: pathlib.Path | None = None) -> None:
    """Set these keys of the [appearance] section of config.ini, atomically, keeping its other keys
    (the TV interface's `sounds`) and the other sections. Raises OSError."""
    path = CONFIG if path is None else path
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        lines = []
    start = next((i for i, line in enumerate(lines) if line.strip().lower() == f"[{SECTION}]"), None)
    kept: list[str] = []
    if start is not None:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        kept = [line if line.endswith("\n") else line + "\n" for line in lines[start + 1:end]
                if line.strip() and line.partition("=")[0].strip().lower() not in values]
    block = f"[{SECTION}]\n" + "".join(f"{key} = {value}\n" for key, value in values.items()) + "".join(kept)
    if start is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.append(block)
    else:
        lines[start:end] = [block + ("\n" if end < len(lines) else "")]
    _write_atomically(path, "".join(lines), 0o640)


def chosen(config: pathlib.Path | None = None, builtin: pathlib.Path | None = None,
           user: pathlib.Path | None = None) -> Theme | None:
    """The chosen theme (DEFAULT when it is gone) with its accent; None when no theme file can be
    had at all. Never raises."""
    try:
        name, accent = load_choice(config)
        themes, _problems = available(builtin, user)
        found = themes.get(name) or themes.get(DEFAULT)
        return None if found is None else with_accent(found, accent)
    except Exception:  # boot path: a broken theme must never stop foot or the launcher
        return None


def current(config: pathlib.Path | None = None, builtin: pathlib.Path | None = None,
            user: pathlib.Path | None = None) -> Theme:
    """The chosen theme with its accent, or FALLBACK. Never raises."""
    return chosen(config, builtin, user) or FALLBACK


def apply(theme: Theme, terminal: int | None = 1, osk_file: pathlib.Path | None = None) -> None:
    """Recolour running foot windows: write the OSC string to `terminal` (the launcher's own,
    None to skip) and to OSK_FILE for the on-screen keyboard. Raises OSError."""
    text = osc(theme)
    if terminal is not None:
        os.write(terminal, text.encode("ascii"))
    _write_atomically(OSK_FILE if osk_file is None else osk_file, text, 0o600)


def read_osc(path: pathlib.Path | None = None, owner_uid: int | None = None) -> str | None:
    """The OSC string the launcher left for the keyboard, or None when it is missing or not one."""
    path = OSK_FILE if path is None else path
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
            return None
        if info.st_uid != (os.getuid() if owner_uid is None else owner_uid):
            return None
        text = path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None
    return text if OSC.fullmatch(text) else None
