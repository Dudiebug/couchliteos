"""Scrolling for menu lists that can be taller than the screen (720p shows ~18 rows)."""

from __future__ import annotations

import curses

MORE_ABOVE = "^  MORE"
MORE_BELOW = "v  MORE"


def scroll_window(selected: int, count: int, visible_rows: int) -> int:
    """Return the index of the first row to draw so `selected` stays on screen.

    The cursor sits in the middle of the window while the list scrolls, so moving
    up and moving down both work the same way, and it reaches the edges only at
    the ends of the list.
    """
    visible = max(1, visible_rows)
    if count <= visible:
        return 0
    selected = min(max(selected, 0), count - 1)
    return min(max(selected - visible // 2, 0), count - visible)


def _put(screen: curses.window, row: int, column: int, text: str) -> None:
    _height, width = screen.getmaxyx()
    try:
        screen.addnstr(row, column, text, max(1, width - column - 1))
    except curses.error:
        pass


def draw_rows(
    screen: curses.window, rows: list[str], selected: int | None, top: int, bottom: int, left: int
) -> None:
    """Draw `rows` in screen rows top..bottom-1 with a ">" cursor, scrolling if needed.

    When rows are hidden a "MORE" marker is drawn in the row above `top` and/or in
    the last row of the area, so the caller must keep the row above `top` free.
    """
    height, _width = screen.getmaxyx()
    room = min(bottom, height - 1) - top  # never draw on the border
    if room < 1:
        return
    visible = len(rows) if len(rows) <= room else max(1, room - 1)
    first = 0 if selected is None else scroll_window(selected, len(rows), visible)
    for offset, label in enumerate(rows[first:first + visible]):
        _put(screen, top + offset, left, f"{'>' if first + offset == selected else ' '}  {label}")
    if first > 0 and top > 0:
        _put(screen, top - 1, left + 3, MORE_ABOVE)
    if first + visible < len(rows) and visible < room:
        _put(screen, top + visible, left + 3, MORE_BELOW)
