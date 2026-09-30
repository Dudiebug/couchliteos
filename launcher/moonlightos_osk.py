#!/usr/bin/python3
"""Full-screen buffered controller keyboard and uinput injector."""

from __future__ import annotations

import curses
import json
import os
import pathlib
import stat
import tempfile
import time
from typing import Any


PAYLOAD = pathlib.Path("/run/moonlightos/osk-payload.json")
# Written by the launcher when the keyboard is opened for a password field.
MASK_REQUEST = pathlib.Path("/run/moonlightos/osk-masked")
MAX_TEXT = 512
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


class Keyboard:
    def __init__(self) -> None:
        self.text = ""
        self.shift = True
        self.symbols = False
        self.masked = False
        self.row = 0
        self.column = 0

    @property
    def rows(self) -> tuple[tuple[str, ...], ...]:
        rows = SYMBOLS if self.symbols else LETTERS
        if not self.symbols and not self.shift:
            rows = tuple(tuple(key.lower() if key.isalpha() else key for key in row) for row in rows)
        return (*rows, ACTIONS)

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


def inject(path: pathlib.Path = PAYLOAD) -> int:
    payload = load_payload(path)
    if payload is None:
        return 0
    from evdev import UInput, ecodes

    events = character_events(*payload, ecodes)
    capabilities = {ecodes.EV_KEY: sorted({code for code, _shift in events} | {ecodes.KEY_LEFTSHIFT})}
    with UInput(capabilities, name="MoonlightOS Buffered Keyboard") as device:
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
    return 0


def draw(screen: curses.window, keyboard: Keyboard) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    try:
        screen.border()
    except curses.error:
        pass
    title = "MOONLIGHTOS KEYBOARD"
    screen.addnstr(2, max(1, (width - len(title)) // 2), title, max(1, width - 2))
    shown = "*" * len(keyboard.text) if keyboard.masked else keyboard.text
    preview = (shown[-max(1, width - 10):] or "_")
    screen.addnstr(4, max(1, (width - len(preview)) // 2), preview, max(1, width - 2))
    first = 7
    for row_index, row in enumerate(keyboard.rows):
        cells = [f"[{key}]" if (row_index, column) != (keyboard.row, keyboard.column) else f">{key}<" for column, key in enumerate(row)]
        line = " ".join(cells)
        screen.addnstr(first + row_index * 2, max(1, (width - len(line)) // 2), line, max(1, width - 2))
    footer = "ARROWS MOVE  ENTER/A SELECTS  ESC/B CANCELS"
    screen.addnstr(height - 3, max(1, (width - len(footer)) // 2), footer, max(1, width - 2))
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
        if code in (curses.KEY_BACKSPACE, 8, 127):
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


if __name__ == "__main__":
    import sys

    raise SystemExit(inject() if sys.argv[1:] == ["--inject"] else curses.wrapper(ui) or 0)
