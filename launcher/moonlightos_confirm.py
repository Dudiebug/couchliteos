"""One-question YES/NO screen for actions a controller user must not trigger by accident."""

from __future__ import annotations

import curses
import textwrap

ENTER_KEYS = (curses.KEY_ENTER, 10, 13)
FOOTER = "A/ENTER SELECTS  ·  B/ESC = NO"


def _put(screen: "curses.window", row: int, column: int, text: str) -> None:
    height, width = screen.getmaxyx()
    if 0 <= row < height - 1:  # the last row belongs to the border
        try:
            screen.addnstr(row, column, text, max(1, width - column - 1))
        except curses.error:
            pass


def _draw(screen: "curses.window", question: str, selected: int) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    if height >= 8 and width >= 24:
        try:
            screen.border()
        except curses.error:
            pass
    lines = (textwrap.wrap(question, width=max(8, width - 8)) or [""])[: max(1, height - 6)]
    row = max(1, (height - len(lines) - 4) // 2)
    for offset, text in enumerate(lines):
        _put(screen, row + offset, max(1, (width - len(text)) // 2), text)
    row += len(lines) + 1
    for index, label in enumerate(("NO", "YES")):
        _put(screen, row + index, max(2, width // 2 - 4), f"{'>' if index == selected else ' '}  {label}")
    _put(screen, row + 3, max(1, (width - len(FOOTER)) // 2), FOOTER)
    screen.refresh()


def confirm(stdscr: "curses.window", question: str) -> bool:
    """Ask `question`; True only if the user moves to YES and presses A/Enter.

    NO is preselected, B/Esc answers NO, and presses queued before the question
    was on screen are discarded so a double-tapped A can never answer it.
    """
    try:
        cursor = curses.curs_set(0)  # a text field's cursor must not blink on the answers
    except curses.error:
        cursor = None
    selected = 0
    try:
        _draw(stdscr, question, selected)
        try:
            curses.flushinp()
        except curses.error:
            pass
        while True:
            key = stdscr.getch()
            if key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_LEFT, curses.KEY_RIGHT, ord("j"), ord("k")):
                selected = 1 - selected
            elif key in ENTER_KEYS:
                return selected == 1
            elif key == 27:
                return False
            _draw(stdscr, question, selected)
    finally:
        if cursor is not None:
            try:
                curses.curs_set(cursor)
            except curses.error:
                pass
