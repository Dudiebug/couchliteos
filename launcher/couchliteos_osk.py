#!/usr/bin/python3
"""Buffered controller keyboard, docked along the bottom of the screen, and uinput injector.

Cage places the keyboard's foot window (app-id couchliteos-osk) in the bottom 40% of the
screen, about 13 terminal rows, so the app being typed into stays visible above it.
"""

from __future__ import annotations

import curses
import dataclasses
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable


APP_ID = "couchliteos-osk"  # set by couchliteos-osk-session; Cage docks it
RUN = pathlib.Path(os.environ.get("COUCHLITEOS_RUN_DIR", "/run/couchliteos"))
PAYLOAD = RUN / "osk-payload.json"
# Written by the launcher when the keyboard is opened for a password field.
MASK_REQUEST = RUN / "osk-masked"
MAX_TEXT = 512
# Seconds a new virtual keyboard needs before the compositor sees its keys.
DEVICE_SETTLE = 0.5
# The window the text is for is brought back to the front (Home or the Guide menu may have
# raised the launcher meanwhile): checked every REFOCUS_POLL seconds, REFOCUS_TRIES times.
REFOCUS_POLL = 0.05
REFOCUS_TRIES = 20
# Touched once an app other than the launcher is back in front: a Guide menu opened by Home
# while the keyboard was up must close and hand the controller back to that app.
REFOCUSED = RUN / "osk-refocused"
LAUNCHER_TITLE = "CouchLiteOS Launcher"
LETTERS = (
    tuple("1234567890"),
    tuple("QWERTYUIOP"),
    tuple("ASDFGHJKL"),
    tuple("ZXCVBNM"),
)
SYMBOLS = (
    tuple("!@#$%^&*()"),
    tuple("-_=+[]{}"),
    tuple("\\|;:'\""),
    tuple(",.<>/?`~"),
)
ACTIONS = (
    "SPACE", "BACKSPACE", "CLEAR", "SHIFT", "SYMBOLS", "MASK/SHOW",
    "CANCEL", "TYPE", "TYPE + ENTER",
)
# Two rows of at most 45 columns; one row of nine labels needs 88 and is cut off below 1080p.
ACTION_ROWS = (ACTIONS[:5], ACTIONS[5:])
TITLE = "COUCHLITEOS KEYBOARD"
HINT = "A / CROSS SELECTS  -  Y / SQUARE DELETES  -  B / CIRCLE CANCELS"  # ASCII: no locale is set here
SHORT_HINT = "A SELECT - Y DELETE - B CANCEL"
# The text line, the six key rows and the hint; borders and spacing are added when there is room.
MIN_ROWS = 1 + len(LETTERS) + len(ACTION_ROWS) + 1


class Keyboard:
    def __init__(self) -> None:
        self.text = ""
        self.shift = False
        self.symbols = False
        self.masked = False
        self.row = 0
        self.column = 0

    @property
    def rows(self) -> tuple[tuple[str, ...], ...]:
        rows = SYMBOLS if self.symbols else LETTERS
        if not self.symbols and not self.shift:
            rows = tuple(tuple(key.lower() if key.isalpha() else key for key in row) for row in rows)
        return (*rows, *ACTION_ROWS)

    def move(self, key: int) -> None:
        rows = self.rows
        if key == curses.KEY_UP:
            self.row = (self.row - 1) % len(rows)
        elif key == curses.KEY_DOWN:
            self.row = (self.row + 1) % len(rows)
        elif key == curses.KEY_LEFT:
            self.column = (self.column - 1) % len(rows[self.row])
        elif key == curses.KEY_RIGHT:
            self.column = (self.column + 1) % len(rows[self.row])
        self.column = min(self.column, len(rows[self.row]) - 1)

    def append(self, value: str) -> None:
        if value.isprintable() and value not in "\r\n" and len(self.text) < MAX_TEXT:
            self.text += value

    def select(self) -> str | None:
        key = self.rows[self.row][self.column]
        if len(key) == 1:
            self.append(key)
            if key.isalpha():
                self.shift = False  # SHIFT capitalises one letter, like a phone keyboard
        elif key == "SPACE":
            self.append(" ")
        elif key == "BACKSPACE":
            self.text = self.text[:-1]
        elif key == "CLEAR":
            self.text = ""
        elif key == "SHIFT":
            self.shift = not self.shift
        elif key == "SYMBOLS":
            self.symbols = not self.symbols
            self.row = self.column = 0
        elif key == "MASK/SHOW":
            self.masked = not self.masked
        elif key == "CANCEL":
            return "cancel"
        elif key == "TYPE":
            return "type"
        elif key == "TYPE + ENTER":
            return "enter"
        return None


def atomic_payload(text: str, enter: bool, path: pathlib.Path = PAYLOAD) -> None:
    if len(text) > MAX_TEXT or "\0" in text or "\n" in text or "\r" in text:
        raise ValueError("invalid keyboard text")
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"text": text, "enter": enter}, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def load_payload(path: pathlib.Path = PAYLOAD, owner_uid: int | None = None) -> tuple[str, bool] | None:
    if not path.exists():
        return None
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            raise ValueError("keyboard payload is not a bounded regular file")
        if info.st_uid != (os.getuid() if owner_uid is None else owner_uid):
            raise ValueError("keyboard payload has the wrong owner")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != {"text", "enter"}:
            raise ValueError("invalid keyboard payload structure")
        text, enter = payload["text"], payload["enter"]
        if not isinstance(text, str) or not isinstance(enter, bool):
            raise ValueError("invalid keyboard payload values")
        if len(text) > MAX_TEXT or "\0" in text or "\n" in text or "\r" in text:
            raise ValueError("invalid keyboard payload text")
        return text, enter
    finally:
        path.unlink(missing_ok=True)


def _mapping(ecodes: Any) -> dict[str, tuple[int, bool]]:
    mapping: dict[str, tuple[int, bool]] = {}
    for letter in "abcdefghijklmnopqrstuvwxyz":
        code = getattr(ecodes, f"KEY_{letter.upper()}")
        mapping[letter] = (code, False)
        mapping[letter.upper()] = (code, True)
    for digit in "0123456789":
        mapping[digit] = (getattr(ecodes, f"KEY_{digit}"), False)
    names = {
        " ": ("KEY_SPACE", False), "\t": ("KEY_TAB", False), "\b": ("KEY_BACKSPACE", False),
        "-": ("KEY_MINUS", False), "_": ("KEY_MINUS", True), "=": ("KEY_EQUAL", False),
        "+": ("KEY_EQUAL", True), "[": ("KEY_LEFTBRACE", False), "{": ("KEY_LEFTBRACE", True),
        "]": ("KEY_RIGHTBRACE", False), "}": ("KEY_RIGHTBRACE", True),
        "\\": ("KEY_BACKSLASH", False), "|": ("KEY_BACKSLASH", True),
        ";": ("KEY_SEMICOLON", False), ":": ("KEY_SEMICOLON", True),
        "'": ("KEY_APOSTROPHE", False), '"': ("KEY_APOSTROPHE", True),
        ",": ("KEY_COMMA", False), "<": ("KEY_COMMA", True), ".": ("KEY_DOT", False),
        ">": ("KEY_DOT", True), "/": ("KEY_SLASH", False), "?": ("KEY_SLASH", True),
        "`": ("KEY_GRAVE", False), "~": ("KEY_GRAVE", True),
    }
    for character, (name, shifted) in names.items():
        mapping[character] = (getattr(ecodes, name), shifted)
    for shifted, digit in zip("!@#$%^&*()", "1234567890"):
        mapping[shifted] = (getattr(ecodes, f"KEY_{digit}"), True)
    return mapping


def character_events(text: str, enter: bool, ecodes: Any) -> list[tuple[int, bool]]:
    mapping = _mapping(ecodes)
    unsupported = next((character for character in text if character not in mapping), None)
    if unsupported is not None:
        raise ValueError(f"unsupported keyboard character: U+{ord(unsupported):04X}")
    events = [mapping[character] for character in text]
    if enter:
        events.append((ecodes.KEY_ENTER, False))
    return events


Run = Callable[..., subprocess.CompletedProcess]


def _wlrctl(run: Run, *arguments: str) -> subprocess.CompletedProcess | None:
    try:
        return run(["wlrctl", "toplevel", *arguments], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _toplevels(run: Run, *matches: str) -> list[tuple[str, str]]:
    """(app_id, title) of each window matching; wlrctl prints "app_id: title" lines."""
    result = _wlrctl(run, "list", *matches)
    if result is None or result.returncode != 0:
        return []
    windows = []
    for line in result.stdout.splitlines():
        app_id, separator, title = line.partition(": ")
        if not separator and line.endswith(":"):
            app_id, title = line[:-1], ""
        windows.append((app_id, title))
    return windows


def active_toplevel(run: Run = subprocess.run) -> dict[str, str] | None:
    """The focused window, the one the keyboard is opened for; None when it is not known."""
    windows = _toplevels(run, "state:active")
    if len(windows) != 1 or windows[0] == ("", "") or windows[0][0] == APP_ID:
        return None
    app_id, title = windows[0]
    return {"app_id": app_id, "title": title}


def parse_target(argument: str) -> dict[str, str] | None:
    try:
        target = json.loads(argument)
    except ValueError:
        return None
    if not isinstance(target, dict) or set(target) != {"app_id", "title"}:
        return None
    if not all(isinstance(value, str) for value in target.values()) or not any(target.values()):
        return None
    return target


def target_matches(target: dict[str, str], run: Run = subprocess.run) -> list[tuple[str, ...]]:
    """wlrctl matches for the window, most exact first. The title may have changed since
    (a browser loading a page), so the app_id alone too, but only while no other window
    shares it: the launcher and terminal apps are all foot windows."""
    app_id, title = target["app_id"], target["title"]
    matches: list[tuple[str, ...]] = []
    if app_id and title:
        matches.append((f"app_id:{app_id}", f"title:{title}"))
    elif title:
        matches.append((f"title:{title}",))
    if app_id and len(_toplevels(run, f"app_id:{app_id}")) == 1:
        matches.append((f"app_id:{app_id}",))
    return matches


def refocus(target: dict[str, str], run: Run = subprocess.run) -> bool:
    """Bring the window the keyboard was opened for back to the front; False when it is gone."""
    for match in target_matches(target, run):
        focused = _wlrctl(run, "focus", *match)
        if focused is None or focused.returncode != 0:
            continue
        for _ in range(REFOCUS_TRIES):
            active = _wlrctl(run, "find", *match, "state:active")
            if active is not None and active.returncode == 0:
                return True
            time.sleep(REFOCUS_POLL)
    return False


def inject(path: pathlib.Path = PAYLOAD, target: dict[str, str] | None = None, run: Run = subprocess.run,
           refocused: pathlib.Path = REFOCUSED) -> int:
    """Types the payload. With a target (the window focused when the keyboard opened) the
    text goes to that window, or nowhere once it has closed; without one, to the focused window."""
    payload = load_payload(path)
    if payload is None:
        return 0
    if target is not None:
        if not refocus(target, run):
            print("couchliteos-osk: the window the text was for has closed; nothing typed", file=sys.stderr)
            return 0
        if target.get("title") != LAUNCHER_TITLE:
            try:
                refocused.touch()
            except OSError:
                pass
    from evdev import UInput, ecodes

    events = character_events(*payload, ecodes)
    capabilities = {ecodes.EV_KEY: sorted({code for code, _shift in events} | {ecodes.KEY_LEFTSHIFT})}
    with UInput(capabilities, name="CouchLiteOS Buffered Keyboard") as device:
        # The compositor only reads a new input device once udev has announced it and
        # libinput has opened it; keys written before then are lost (the first letters).
        time.sleep(DEVICE_SETTLE)
        for code, shifted in events:
            if shifted:
                device.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 1)
            device.write(ecodes.EV_KEY, code, 1)
            device.syn()
            device.write(ecodes.EV_KEY, code, 0)
            if shifted:
                device.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 0)
            device.syn()
            time.sleep(0.004)
        time.sleep(0.1)  # let the last release be read before the device goes away
    return 0


@dataclasses.dataclass(frozen=True)
class Layout:
    """Screen rows of each part of the keyboard; None for a part left out."""

    border: bool
    title: int | None
    text: int
    keys: tuple[int, ...]
    hint: int | None


def layout(height: int) -> Layout:
    """Where everything goes in a panel `height` lines high.

    The text line, the six key rows and the hint come first (MIN_ROWS lines). Spare lines
    then go, in order, to a border (two lines, with the title in the top one), a blank line
    under the text, one above the hint, one between the letters and the actions, and a
    blank first line; whatever is left over centres the block. Below MIN_ROWS the hint goes,
    then the bottom key rows are cut.
    """
    border = height >= MIN_ROWS + 2
    top = 1 if border else 0
    spare = height - 2 * top - MIN_ROWS
    gaps = [spare > index for index in range(4)]  # after text, before hint, mid keys, leading
    spare -= sum(gaps)
    row = top + gaps[3] + max(0, spare) // 2
    text = row
    row += 1 + gaps[0]
    keys = []
    for index in range(len(LETTERS) + len(ACTION_ROWS)):
        if index == len(LETTERS):
            row += gaps[2]
        keys.append(row)
        row += 1
    hint = row + gaps[1] if height >= MIN_ROWS else None
    return Layout(border=border, title=0 if border else None, text=text, keys=tuple(keys), hint=hint)


def draw(screen: curses.window, keyboard: Keyboard) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    places = layout(height)
    if places.border:
        try:
            screen.border()
        except curses.error:
            pass

    def centered(row: int, text: str) -> None:
        try:
            screen.addnstr(row, max(1, (width - len(text)) // 2), text, max(1, width - 2))
        except curses.error:
            pass

    if places.title is not None:
        centered(places.title, f" {TITLE} ")
    shown = "*" * len(keyboard.text) if keyboard.masked else keyboard.text
    centered(places.text, shown[-max(1, width - 10):] or "_")
    for row_index, row in enumerate(keyboard.rows):
        cells = [f"[{key}]" if (row_index, column) != (keyboard.row, keyboard.column) else f">{key}<" for column, key in enumerate(row)]
        centered(places.keys[row_index], " ".join(cells))
    if places.hint is not None:
        centered(places.hint, HINT if len(HINT) <= width - 2 else SHORT_HINT)
    screen.refresh()


def consume_mask_request(path: pathlib.Path = MASK_REQUEST) -> bool:
    try:
        requested = path.is_file() and not path.is_symlink()
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return requested


def ui(screen: curses.window) -> None:
    keyboard = Keyboard()
    keyboard.masked = consume_mask_request()
    curses.curs_set(0)
    curses.set_escdelay(25)  # B sends a bare Esc; don't wait 1 s for an escape sequence
    screen.keypad(True)
    while True:
        draw(screen, keyboard)
        key = screen.get_wch()
        if isinstance(key, str) and key not in {"\n", "\r", "\x1b", "\x7f", "\b"}:
            keyboard.append(key)
            continue
        code = ord(key) if isinstance(key, str) else key
        if code in (27,):
            return
        if code in (curses.KEY_BACKSPACE, 8, 127, curses.KEY_DC):  # Y/Square arrives as KEY_DC
            keyboard.text = keyboard.text[:-1]
            continue
        keyboard.move(code)
        if code in (curses.KEY_ENTER, 10, 13):
            action = keyboard.select()
            if action == "cancel":
                return
            if action in {"type", "enter"}:
                atomic_payload(keyboard.text, action == "enter")
                return


def main(argv: list[str]) -> int:
    """No argument: the keyboard. --target: print the focused window (JSON) for --inject.
    --inject [TARGET]: type the keyboard's text, into TARGET when it is given and known."""
    if argv == ["--target"]:
        target = active_toplevel()
        if target is not None:
            print(json.dumps(target, separators=(",", ":")))
        return 0
    if argv[:1] == ["--inject"] and len(argv) <= 2:
        return inject(target=parse_target(argv[1]) if len(argv) == 2 and argv[1] else None)
    return curses.wrapper(ui) or 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
