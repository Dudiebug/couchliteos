#!/usr/bin/python3
"""One-time "what's new" screen for people who upgrade a system that finished setup before."""

from __future__ import annotations

import curses
import pathlib
import textwrap
from collections.abc import Callable, Iterable

import couchliteos_setup as setup
import couchliteos_stream as stream
import couchliteos_update as update

VERSION_FILES = update.VERSION_FILES
# Holds the version whose notice was last handled, next to the other launcher state.
SEEN = setup.MARKER.parent / "whatsnew-seen"
# /run is always writable, so if SEEN cannot be saved (disk full, read-only) the screen
# still does not come back when the launcher restarts during the same boot.
SESSION_SEEN = pathlib.Path("/run/couchliteos/whatsnew-seen")

RENAME_NOTICE = "MOONLIGHTOS IS NOW CALLED COUCHLITEOS. SAME SYSTEM, NEW NAME."  # rename:keep
# One line per new feature, at most 74 columns. Trim the ones that miss a release here.
FEATURES = (
    "GUIDED SETUP: SETTINGS > SETUP WIZARD",
    "SLEEP: HOLD THE GUIDE BUTTON FOR 5 SECONDS (NEVER DURING A GAME)",
    "USE YOUR TV REMOTE (HDMI-CEC): SETTINGS > TV CONTROL",
    "YOUR GAMING PC WAKES UP WHEN YOU START MOONLIGHT",
    "STREAM YOUR GAMING PC IN ONE PRESS FROM THE HOME SCREEN",
    "FIND GAMING PCS: SETTINGS > STREAMING > PAIR A / ANOTHER GAMING PC",
    "STREAM CHECK: SETTINGS > STREAMING",
    "TV CUTS OFF THE PICTURE? SETTINGS > DISPLAY > SCREEN EDGES",
)
FOOTER = "PRESS A OR B TO CONTINUE"
# gamepad-nav sends Enter for A and Esc for B.
DISMISS_KEYS = (curses.KEY_ENTER, 10, 13, 27)


def should_show(seen_text: str, current: str, setup_complete: bool) -> bool:
    """True for an upgrade: setup was finished before and `current` is newer than the seen marker."""
    now = update.parse_version(current)
    if not setup_complete or now is None:
        return False
    before = update.parse_version(seen_text)  # missing or damaged counts as never seen
    return before is None or before < now


def read_seen(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="ascii", errors="replace")
    except OSError:
        return ""


def mark_seen(path: pathlib.Path, version: str) -> bool:
    try:
        stream._atomic_write(path, f"{version}\n".encode("ascii"))
    except (OSError, ValueError):
        return False
    return True


def setup_was_completed(marker: pathlib.Path) -> bool:
    try:
        return marker.exists()
    except OSError:
        return False


def draw(screen: curses.window, version: str) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    try:
        screen.border(ord("|"), ord("|"), ord("-"), ord("-"), ord("+"), ord("+"), ord("+"), ord("+"))
    except curses.error:
        pass
    footer_row = height - 3 if height >= 14 else height - 2

    def put(row: int, column: int, text: str, attr: int = 0) -> None:
        text = text[: max(0, width - column - 2)]
        if text and 1 <= row <= height - 2:
            try:
                screen.addstr(row, column, text, attr)
            except curses.error:
                pass

    def centered(row: int, text: str, attr: int = 0) -> None:
        put(row, max(1, (width - len(text)) // 2), text, attr)

    row = 2 if height >= 14 else 1
    centered(row, f"WHAT'S NEW IN {version.upper()}", curses.A_BOLD)
    row += 2
    for line in textwrap.wrap(RENAME_NOTICE, width=max(8, width - 8)):
        centered(row, line)
        row += 1
    row += 1
    wrapped = [
        textwrap.wrap(feature, width=max(8, width - 12), initial_indent="- ", subsequent_indent="  ")
        for feature in FEATURES
    ]
    left = max(2, (width - max((len(line) for lines in wrapped for line in lines), default=0)) // 2)
    # A blank line between features when they all still fit above the footer.
    gap = int(row + sum(len(lines) + 1 for lines in wrapped) < footer_row)
    for lines in wrapped:
        for line in lines:
            if row < footer_row:
                put(row, left, line)
            row += 1
        row += gap
    centered(footer_row, FOOTER)
    screen.refresh()


def show_once(
    screen: curses.window,
    read_key: Callable[[], int] | None = None,
    *,
    markers: Iterable[pathlib.Path] = (SEEN, SESSION_SEEN),
    setup_marker: pathlib.Path = setup.MARKER,
    version_files: Iterable[pathlib.Path] = VERSION_FILES,
) -> bool:
    """Show the screen on the first start of a new version after an upgrade; True if it was shown.

    Call it BEFORE the setup wizard: a new user has no setup marker yet, so they are only
    recorded as having seen this version (afterwards they would look like an upgrader)."""
    version = update.installed_version(version_files)
    if not version:
        return False
    markers = tuple(markers)
    stale = [path for path in markers if should_show(read_seen(path), version, True)]
    # Marked before the screen appears, so a crash or power cut while it is up cannot loop it.
    for path in stale:
        mark_seen(path, version)
    if len(stale) < len(markers) or not setup_was_completed(setup_marker):
        return False
    read_key = read_key or screen.getch
    while True:
        draw(screen, version)
        if read_key() in DISMISS_KEYS:
            return True
