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
RENAMED_IN = "0.2.0"  # upgrades from before this version also see RENAME_NOTICE
# What each release added, newest first: one line per feature, at most 66 columns (the screen wraps longer ones at 80x24).
# An upgrade shows every release newer than the one last seen, newest first.
RELEASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("0.2.4", (
        "BRIGHTNESS AND VOLUME KEYS WORK; BRIGHTNESS IS IN THE GUIDE MENU",
        "HOME FROM ANY PAD: HOLD SELECT+START. KEYBOARD: CTRL+ALT+H",
        "MOUSE AND CONTROLLER MOUSE SPEED: SETTINGS > CONTROLS",
        "THE ON-SCREEN KEYBOARD NO LONGER HIDES THE APP YOU TYPE INTO",
    )),
    ("0.2.3", (
        "TYPE INTO AN APP: GUIDE, PICK IT, PRESS X (XBOX) / TRIANGLE (PS)",
        "CONTROLLER MOUSE IN BROWSERS: LEFT STICK POINTS, A CLICKS",
        "GUIDE OR THE SUPER KEY NOW OPENS THE MENU OVER A RUNNING APP",
        "VOLUME AND CONTROLLER MOUSE ARE IN THE GUIDE MENU",
        "OLDER NVIDIA: VIDEO DECODER FIRMWARE IN SETTINGS > STREAMING",
    )),
    ("0.2.2", (
        "SOUND ON THE TV OVER HDMI ON MORE PCS: SETTINGS > AUDIO OUTPUT",
    )),
    ("0.2.1", (
        "STREAM YOUR GAMING PC IN ONE PRESS FROM THE HOME SCREEN",
        "FIND GAMING PCS: SETTINGS > STREAMING > PAIR A / ANOTHER GAMING PC",
        "STREAM CHECK: SETTINGS > STREAMING",
        "TV CUTS OFF THE PICTURE? SETTINGS > DISPLAY > SCREEN EDGES",
        "SOFTWARE UPDATE: SETTINGS > SOFTWARE UPDATE",
    )),
    ("0.2.0", (
        "GUIDED SETUP: SETTINGS > SETUP WIZARD",
        "SLEEP: HOLD THE GUIDE BUTTON FOR 5 SECONDS (NEVER DURING A GAME)",
        "USE YOUR TV REMOTE (HDMI-CEC): SETTINGS > TV CONTROL",
        "YOUR GAMING PC WAKES UP WHEN YOU START MOONLIGHT",
    )),
)
FEATURES = RELEASES[0][1]  # the newest release
MORE = "AND MORE: SEE THE RELEASE NOTES"
EARLIER = "EARLIER CHANGES: SEE THE RELEASE NOTES"
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


def notes(version: str, seen: str = "") -> tuple[bool, list[tuple[str, tuple[str, ...]]]]:
    """(show the rename notice, [(release, features)]) for an upgrade from `seen` to `version`.

    Only the notes of `version` itself, never an earlier release's: a version without notes
    of its own shows none. A missing or damaged `seen` counts as an upgrade from before the rename."""
    now = update.parse_version(version)
    before = update.parse_version(seen)
    releases = [
        (release, features) for release, features in RELEASES
        # By release number, so a pre-release (0.2.3-rc.1) already shows the 0.2.3 notes.
        if now is not None and update.parse_version(release)[0] == now[0]
        and (before is None or update.parse_version(release) > before)
    ][:1]
    renamed = before is None or before < update.parse_version(RENAMED_IN)
    return renamed, releases


def skipped_releases(version: str, seen: str = "") -> bool:
    """True when releases between `seen` and `version` had notes the screen does not show."""
    now = update.parse_version(version)
    before = update.parse_version(seen)
    return now is not None and any(
        update.parse_version(release)[0] < now[0] and (before is None or update.parse_version(release) > before)
        for release, _features in RELEASES
    )


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


def draw(screen: curses.window, version: str, seen: str = "") -> None:
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
    renamed, releases = notes(version, seen)
    if renamed:
        for line in textwrap.wrap(RENAME_NOTICE, width=max(8, width - 8)):
            centered(row, line)
            row += 1
        row += 1
    wrapped = []
    for release, features in releases:
        if len(releases) > 1:
            wrapped.append([f"NEW IN {release}:"])
        wrapped.extend(
            textwrap.wrap(feature, width=max(8, width - 12), initial_indent="- ", subsequent_indent="  ")
            for feature in features
        )
    if skipped_releases(version, seen):
        wrapped.append([EARLIER])
    if row + sum(map(len, wrapped)) > footer_row:  # too much for the screen: newest first, then a pointer
        room = max(0, footer_row - row - 1)
        kept: list[list[str]] = []
        for lines in wrapped:
            if sum(map(len, kept)) + len(lines) > room:
                break
            kept.append(lines)
        wrapped = kept + [[MORE]]
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
    seen_texts = [read_seen(path) for path in markers]
    stale = [path for path, text in zip(markers, seen_texts) if should_show(text, version, True)]
    # The version last seen (the session marker in /run is empty after every boot):
    # the notes of every release newer than it are shown.
    parsed = [(update.parse_version(text), text.strip()) for text in seen_texts]
    seen = max((item for item in parsed if item[0] is not None), default=(None, ""))[1]
    # Marked before the screen appears, so a crash or power cut while it is up cannot loop it.
    for path in stale:
        mark_seen(path, version)
    if len(stale) < len(markers) or not setup_was_completed(setup_marker):
        return False
    if notes(version, seen) == (False, []):  # nothing new to say for this version
        return False
    read_key = read_key or screen.getch
    while True:
        draw(screen, version, seen)
        if read_key() in DISMISS_KEYS:
            return True
