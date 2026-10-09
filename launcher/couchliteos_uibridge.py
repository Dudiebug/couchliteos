"""The classic screens, drawn by the TV interface instead of a terminal.

Every Settings screen, the setup wizard and CONNECT were written for curses and ran in a foot
window on top of the TV interface. They now run in the same child process as before
(`couchliteos-launcher --screen <name> --bridge`), but with a Window that stands in for the
curses screen: what a screen draws is sent to the TV interface as a frame (the text at each
row and column, and the lists it drew through couchliteos_listview), and the keys come back
from the TV interface. The TV interface turns each frame into a View (title, lists with a
focused row, text, progress, the hint at the bottom) and draws that with its own widgets, so
these screens look like the rest of the TV interface, while their logic stays the tested
curses code. Nothing here imports GTK.

The two pipes are the only link: the child writes one JSON frame per line on its OUT pipe and
reads one key per line on its IN pipe ("k <curses code>" or "c <character>").
"""

from __future__ import annotations

import curses
import dataclasses
import json
import os
import re
import select
import time
from collections.abc import Callable

ROWS, COLS = 30, 100  # the screen size the curses code lays out for (a 1080p foot window had about this)
ENV = "COUCHLITEOS_BRIDGE"  # "<in fd>,<out fd>", set by the TV interface for the child

# ---------------------------------------------------------------------- the child: a curses stand-in


class KeySource:
    """Keys from the TV interface: blocking, timed, or none waiting."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.buffer = b""
        self.pending: list[int | str] = []
        self.closed = False

    def _parse(self) -> None:
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            token = decode_key(line.decode("utf-8", "replace"))
            if token is not None:
                self.pending.append(token)

    def read(self, timeout: float | None) -> int | str | None:
        """The next key, waiting up to `timeout` seconds (None: until one comes)."""
        deadline = None if timeout is None else time.monotonic() + max(timeout, 0.0)
        while not self.pending:
            if self.closed:
                raise SystemExit(0)  # the TV interface went away: this screen has no one to show it
            wait = None if deadline is None else max(deadline - time.monotonic(), 0.0)
            ready, _w, _x = select.select([self.fd], [], [], wait)
            if not ready:
                return None
            chunk = os.read(self.fd, 4096)
            if not chunk:
                self.closed = True
                continue
            self.buffer += chunk
            self._parse()
        return self.pending.pop(0)

    def drain(self) -> None:
        self.pending.clear()
        while select.select([self.fd], [], [], 0)[0]:
            chunk = os.read(self.fd, 4096)
            if not chunk:
                self.closed = True
                return
        self.buffer = b""


def encode_key(key: int | str) -> str:
    return f"c {key}\n" if isinstance(key, str) else f"k {int(key)}\n"


def decode_key(line: str) -> int | str | None:
    kind, _, value = line.partition(" ")
    if kind == "c" and value:
        return value[0]
    if kind == "k":
        try:
            return int(value)
        except ValueError:
            return None
    return None


class Window:
    """The calls the classic screens make on their curses window, recorded as a frame."""

    def __init__(self, send: Callable[[str], None], keys: KeySource | None, rows: int = ROWS, cols: int = COLS) -> None:
        self.send = send
        self.keys = keys
        self.rows, self.cols = rows, cols
        self.delay: float | None = None  # curses timeout(): None blocks
        self.ops: list[list] = []
        self.lists: list[list] = []
        self.boxed = False
        self.sent = ""

    # drawing
    def getmaxyx(self) -> tuple[int, int]:
        return self.rows, self.cols

    def erase(self) -> None:
        self.ops, self.lists, self.boxed = [], [], False

    clear = erase

    def border(self, *_args) -> None:
        self.boxed = True

    box = border

    def addstr(self, *args) -> None:
        row, column, text, attr = _addstr_args(args)
        if text and 0 <= row < self.rows:
            self.ops.append([row, max(column, 0), text, int(attr)])

    def addnstr(self, *args) -> None:
        if len(args) >= 4 and isinstance(args[0], int):
            row, column, text, count, *rest = args
            self.addstr(row, column, str(text)[: max(int(count), 0)], *rest)
        elif len(args) >= 2:
            text, count, *rest = args
            self.addstr(0, 0, str(text)[: max(int(count), 0)], *rest)

    def report_list(self, rows: list[str], selected: int | None, top: int, left: int) -> None:
        """couchliteos_listview's rows, whole (the TV interface scrolls them itself)."""
        self.lists.append([top, left, [str(row) for row in rows], selected])

    def frame(self) -> dict:
        return {"rows": self.rows, "cols": self.cols, "boxed": self.boxed, "ops": self.ops, "lists": self.lists}

    def refresh(self) -> None:
        text = json.dumps(self.frame(), separators=(",", ":"))
        if text != self.sent:
            self.send(text + "\n")
            self.sent = text

    noutrefresh = refresh

    # keys
    def timeout(self, ms: int) -> None:
        self.delay = None if ms < 0 else ms / 1000

    def nodelay(self, flag: bool) -> None:
        self.delay = 0.0 if flag else None

    def keypad(self, _flag: bool) -> None:
        pass

    def _read(self) -> int | str | None:
        self.refresh()  # as curses: a read shows what was drawn
        return None if self.keys is None else self.keys.read(self.delay)

    def getch(self) -> int:
        key = self._read()
        if key is None:
            return -1
        return ord(key) if isinstance(key, str) else key

    def get_wch(self) -> int | str:
        key = self._read()
        if key is None:
            raise curses.error("no input")
        return key

    def __getattr__(self, name: str):  # any other window call is drawn as nothing
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *_args, **_kwargs: None


def _addstr_args(args: tuple) -> tuple[int, int, str, int]:
    if len(args) >= 3 and isinstance(args[0], int) and isinstance(args[1], int):
        return args[0], args[1], str(args[2]), (args[3] if len(args) > 3 else 0)
    if args:
        return 0, 0, str(args[0]), (args[1] if len(args) > 1 else 0)
    return 0, 0, "", 0


def install(window: Window) -> None:
    """Make the curses module's own functions safe without a terminal (this process has none)."""
    noop = lambda *_args, **_kwargs: None  # noqa: E731
    for name in ("curs_set", "set_escdelay", "use_default_colors", "start_color", "init_pair", "endwin",
                 "doupdate", "beep", "flash", "def_prog_mode", "reset_prog_mode", "update_lines_cols",
                 "resizeterm", "noecho", "echo", "cbreak", "nocbreak", "raw", "noraw"):
        setattr(curses, name, noop)
    curses.flushinp = (lambda: window.keys.drain()) if window.keys is not None else noop
    curses.napms = lambda ms: time.sleep(ms / 1000)
    curses.has_colors = lambda: False
    curses.isendwin = lambda: False
    curses.color_pair = lambda number: int(number) << 8
    curses.wrapper = lambda function, *args, **kwargs: function(window, *args, **kwargs)
    curses.LINES, curses.COLS = window.rows, window.cols


def run(function: Callable[..., object], *args: object) -> int:
    """Run `function(window, *args)` with the pipes the TV interface gave in COUCHLITEOS_BRIDGE."""
    keys_fd, frames_fd = (int(part) for part in os.environ[ENV].split(","))
    for fd in (keys_fd, frames_fd):
        os.set_inheritable(fd, False)  # programs the screen starts must not hold the TV's pipes
    os.environ.pop(ENV, None)

    def send(text: str) -> None:
        data = text.encode()
        while data:
            data = data[os.write(frames_fd, data):]

    window = Window(send, KeySource(keys_fd))
    install(window)
    try:
        function(window, *args)
    except BrokenPipeError:
        return 0
    return 0


# ---------------------------------------------------------------------- the TV side: a frame as a View

A_REVERSE = getattr(curses, "A_REVERSE", 1 << 18)
A_BOLD = getattr(curses, "A_BOLD", 1 << 21)
MORE = re.compile(r"^[\^v]\s+MORE$")
RULE = re.compile(r"^[-=_|+~. ]+$")
BAR = re.compile(r"\[([#=|*]*)([ .\-]*)\]")
PERCENT = re.compile(r"(\d{1,3})\s*%")
GAP = re.compile(r"\s{2,}")


@dataclasses.dataclass(frozen=True)
class Row:
    label: str
    value: str = ""


@dataclasses.dataclass(frozen=True)
class ListBlock:
    rows: tuple[Row, ...]
    selected: int | None


@dataclasses.dataclass(frozen=True)
class TextBlock:
    lines: tuple[str, ...]
    centered: bool
    strong: bool = False


@dataclasses.dataclass(frozen=True)
class ProgressBlock:
    fraction: float
    text: str


@dataclasses.dataclass(frozen=True)
class View:
    title: str
    blocks: tuple
    hint: tuple[str, ...]

    @property
    def focus(self) -> ListBlock | None:
        return next((block for block in self.blocks if isinstance(block, ListBlock) and block.selected is not None), None)


def split_row(text: str) -> Row:
    """"CHECK FOR UPDATES  ON" is a label and its value; one space is part of the label."""
    text = text.strip()
    parts = GAP.split(text, maxsplit=1)
    if len(parts) == 2:
        value = parts[1].strip()
        value = value[2:].strip() if value.startswith("- ") else value
        return Row(parts[0], GAP.sub("  ", value))
    return Row(text)


def _rows(frame: dict) -> dict[int, list[tuple[int, str, int]]]:
    rows: dict[int, list[tuple[int, str, int]]] = {}
    for row, column, text, attr in frame.get("ops", []):
        rows.setdefault(int(row), []).append((int(column), str(text), int(attr)))
    for parts in rows.values():
        parts.sort()
    return rows


def _joined(parts: list[tuple[int, str, int]]) -> tuple[int, str, int]:
    """One screen row as text from its first column, the columns kept apart by spaces."""
    first = parts[0][0]
    text, attr = "", 0
    for column, piece, piece_attr in parts:
        at = column - first
        text = text.ljust(at) + piece if at >= len(text) else text[:at] + piece + text[at + len(piece):]
        attr |= piece_attr
    return first, text.rstrip(), attr


def _centered(column: int, text: str, cols: int) -> bool:
    return abs(column - (cols - len(text)) // 2) <= 2 and column > 2


def parse(frame: dict) -> View:
    """The View the TV interface draws for one frame of a classic screen."""
    rows_count, cols = int(frame.get("rows", ROWS)), int(frame.get("cols", COLS))
    lines: list[tuple[int, int, str, int]] = []
    for row, parts in sorted(_rows(frame).items()):
        column, text, attr = _joined(parts)
        stripped = text.strip()
        if not stripped or MORE.match(stripped) or RULE.match(stripped):
            continue
        lines.append((row, column, text, attr))

    items: list[tuple[int, object]] = []
    for top, _left, rows, selected in frame.get("lists", []):
        if selected is None:  # a list without a cursor is text (a message, wrapped to the width)
            items.append((int(top), TextBlock(tuple(row.strip() for row in rows if row.strip()), True)))
        else:
            items.append((int(top), ListBlock(tuple(split_row(row) for row in rows), int(selected))))

    # Lists drawn row by row (">  LABEL" at one column, or the focused row in reverse video).
    index = 0
    loose: list[tuple[int, int, str, int]] = []
    while index < len(lines):
        run = _marked_run(lines, index)
        if run:
            chosen = None
            rows_out = []
            for position, (_row, column, text, attr) in enumerate(lines[index:index + run]):
                body = text[3:] if text[:1] in (">", " ") and text[1:3] == "  " else text.lstrip("> ")
                if text.startswith(">") or attr & A_REVERSE:
                    chosen = position
                rows_out.append(split_row(body))
            items.append((lines[index][0], ListBlock(tuple(rows_out), chosen)))
            index += run
            continue
        loose.append(lines[index])
        index += 1

    title = ""
    hint: list[str] = []
    columns = {(row, column) for row, column, _text, _attr in loose}
    for row, column, text, attr in loose:
        stripped = text.strip()
        # Lines one under another at one column are a left-aligned block, even when one of them
        # happens to sit in the middle; a line alone is centred if it is in the middle.
        aligned = any((row + step, column) in columns for step in (-2, -1, 1, 2))
        if not title and row < rows_count // 4 and _centered(column, stripped, cols):
            title = stripped
            continue
        if row >= rows_count - 4 and _centered(column, stripped, cols):
            hint.append(stripped)
            continue
        bar = BAR.search(stripped)
        if bar and len(bar.group(1)) + len(bar.group(2)) >= 8:
            percent = PERCENT.search(stripped)
            filled = len(bar.group(1)) / (len(bar.group(1)) + len(bar.group(2)))
            fraction = int(percent.group(1)) / 100 if percent else filled
            items.append((row, ProgressBlock(min(max(fraction, 0.0), 1.0), BAR.sub("", stripped).strip())))
            continue
        centered = _centered(column, stripped, cols) and not aligned
        items.append((row, TextBlock((stripped,), centered, bool(attr & (A_BOLD | A_REVERSE)))))
    if not title and items and isinstance(items[0][1], TextBlock) and items[0][0] < rows_count // 4:
        title = items.pop(0)[1].lines[0]

    items.sort(key=lambda item: item[0])
    return View(title, tuple(_merge_text(block for _row, block in items)), tuple(hint))


def _marked_run(lines: list[tuple[int, int, str, int]], start: int) -> int:
    """How many lines from `start` form a list: one after another, at one column, each either
    ">  ..." or "   ..." (the classic cursor) with one ">", or one in reverse video."""
    row0, column0, _text, _attr = lines[start]
    count, marked = 0, False
    for offset, (row, column, text, attr) in enumerate(lines[start:]):
        if row != row0 + offset or column != column0:
            break
        cursor = text.startswith(">  ")
        if not (cursor or text.startswith("   ") or attr & A_REVERSE):
            break
        marked = marked or cursor or bool(attr & A_REVERSE)
        count += 1
    return count if marked and count >= 1 else 0


def _merge_text(blocks) -> list:
    """Text lines one under another, with the same alignment, are one paragraph block."""
    merged: list = []
    for block in blocks:
        last = merged[-1] if merged else None
        if (isinstance(block, TextBlock) and isinstance(last, TextBlock) and last.centered == block.centered
                and last.strong == block.strong):
            merged[-1] = TextBlock(last.lines + block.lines, last.centered, last.strong)
        else:
            merged.append(block)
    return merged


def visible(count: int, selected: int | None, slots: int) -> range:
    """Which rows of a list fit in `slots`, keeping the focused one in the middle."""
    slots = max(1, slots)
    if count <= slots or selected is None:
        return range(min(count, slots))
    first = min(max(selected - slots // 2, 0), count - slots)
    return range(first, first + slots)


# Keys: the TV interface's GTK key names, as the classic screens read them from foot.
KEYS = {
    "Up": curses.KEY_UP, "Down": curses.KEY_DOWN, "Left": curses.KEY_LEFT, "Right": curses.KEY_RIGHT,
    "Return": 10, "KP_Enter": 10, "ISO_Enter": 10, "Escape": 27, "BackSpace": curses.KEY_BACKSPACE,
    "Tab": 9, "ISO_Left_Tab": curses.KEY_BTAB, "Page_Up": curses.KEY_PPAGE, "Page_Down": curses.KEY_NPAGE,
    "Home": curses.KEY_HOME, "End": curses.KEY_END, "Delete": curses.KEY_DC, "Insert": curses.KEY_IC,
    **{f"F{number}": curses.KEY_F0 + number for number in range(1, 13)},
}


def key_for(name: str | None, character: str = "") -> int | str | None:
    """What the classic screen reads for a key: a curses code, a typed character, or None."""
    if name in KEYS:
        return KEYS[name]
    if character and (character == " " or character.isprintable()) and len(character) == 1:
        return character
    return None
