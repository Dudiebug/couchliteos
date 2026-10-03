"""The TV interface's sizes, colours, keys and idle blanking, as plain Python.

couchliteos-tv.py (GTK) draws the home screen; everything here can be tested without
GTK: which key does what, how big things are on a given screen, which tiles of a long
row are on screen, the stylesheet, and when the screen goes blank.

Sizes follow the 10-foot rules: everything scales with the window height (1080 px is
the reference), no text is smaller than MIN_FONT px at 1080p, a SAFE_MARGIN of the
screen is left free on every side (overscan), and a row shows at most MAX_TILES tiles.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable

import couchliteos_power as power
import couchliteos_theme as theme

REFERENCE_HEIGHT = 1080
MIN_FONT = 28  # px at 1080p
SAFE_MARGIN = 0.05
MAX_TILES = 6
ANCHOR = 1  # a long row scrolls so the focused tile stays in this column, as console carousels do
# The same keys gamepad-nav sends (D-pad, A = Enter, B = Esc, Y = Delete, X = F12, LB/RB/VIEW/MENU =
# F5-F8), and a keyboard's. Gdk key names; anything else is ignored.
KEY_ACTIONS = {
    "Up": "up", "Down": "down", "Left": "left", "Right": "right",
    "KP_Up": "up", "KP_Down": "down", "KP_Left": "left", "KP_Right": "right",
    "Return": "activate", "KP_Enter": "activate", "ISO_Enter": "activate",
    "Escape": "back", "BackSpace": "back",
    "Delete": "close", "KP_Delete": "close",
    "Home": "home",
    "F12": "keyboard",
    "F9": "hold-y",  # gamepad-nav sends F9 while Y / Square is held (CHANGE ARTWORK)
    "F5": "shortcut:lb", "F6": "shortcut:rb", "F7": "shortcut:view", "F8": "shortcut:menu",
}
HOME_HINT = "A / CROSS OR ENTER OPENS  ·  B / CIRCLE OR ESC GOES BACK"
ACTIVE_HINT = "A / CROSS OR ENTER RESUMES  ·  Y / SQUARE OR DELETE CLOSES  ·  B / CIRCLE OR ESC GOES BACK"
FAILURE_HINT = "A / CROSS OR ENTER TRIES AGAIN  ·  B / CIRCLE OR ESC GOES BACK"
QUESTION_HINT = "A / CROSS OR ENTER: YES  ·  B / CIRCLE OR ESC: NO"
WAIT_HINT = "PRESS ANY BUTTON TO CANCEL"


def action(key_name: str | None) -> str | None:
    return KEY_ACTIONS.get(key_name or "")


@dataclasses.dataclass(frozen=True)
class Layout:
    """Pixel sizes for a `width` x `height` window."""

    width: int
    height: int

    @property
    def scale(self) -> float:
        return max(self.height, 1) / REFERENCE_HEIGHT

    def px(self, reference: float) -> int:
        """`reference` px at 1080p, scaled to this window (never below 1)."""
        return max(1, round(reference * self.scale))

    @property
    def margin_x(self) -> int:
        return round(self.width * SAFE_MARGIN)

    @property
    def margin_y(self) -> int:
        return round(self.height * SAFE_MARGIN)

    @property
    def gap(self) -> int:
        return self.px(24)

    @property
    def fonts(self) -> dict[str, int]:
        """Text sizes in px; at 1080p none is under MIN_FONT."""
        return {name: self.px(size) for name, size in (
            ("bar", MIN_FONT), ("prompt", MIN_FONT), ("row", 30), ("tile", 32), ("detail", MIN_FONT),
            ("initial", 96), ("title", 44), ("body", 32),
        )}

    @property
    def tile_width(self) -> int:
        usable = self.width - 2 * self.margin_x
        return max(1, (usable - (MAX_TILES - 1) * self.gap) // MAX_TILES)

    def tile_height(self, row: str) -> int:
        """GAMES tiles are the big ones; SYSTEM is the smaller row."""
        usable = self.height - 2 * self.margin_y
        return max(1, round(usable * {"games": 0.30, "apps": 0.17, "system": 0.11}.get(row, 0.17)))


def visible(count: int, focus: int, slots: int = MAX_TILES, anchor: int = ANCHOR) -> range:
    """Indexes of the tiles on screen in a row of `count` with tile `focus` focused."""
    if count <= slots:
        return range(count)
    start = max(0, min(focus - anchor, count - slots))
    return range(start, start + slots)


def initial(text: str) -> str:
    """The big letter on a generated title card (the PC's initial on a game)."""
    return next((char for char in text.upper() if char.isalnum()), "?")


def stylesheet(colours: theme.Theme, layout: Layout) -> str:
    """The TV interface's CSS, with the theme's colours written in (GTK before 4.16 has no var())."""
    c = {field: f"#{value}" for field, value in colours.colours.items()}
    on_focus = f"#{theme.on_focus(colours)}"
    f = layout.fonts
    border = layout.px(6)
    radius = layout.px(14)
    return f"""
window, .tv-root {{ background-color: {c['background']}; color: {c['text']}; }}
.tv-bar {{ font-size: {f['bar']}px; color: {c['muted']}; }}
.tv-bar .tv-warning {{ color: {c['warning']}; }}
.tv-row-title {{ font-size: {f['row']}px; font-weight: bold; color: {c['muted']}; }}
.tv-tile {{ background-color: {c['surface']}; border-radius: {radius}px;
  border: {border}px solid transparent; padding: {layout.px(10)}px; }}
.tv-tile label {{ color: {c['text']}; }}
.tv-tile .tv-name {{ font-size: {f['tile']}px; font-weight: bold; }}
.tv-tile .tv-detail {{ font-size: {f['detail']}px; color: {c['muted']}; }}
.tv-tile .tv-initial {{ font-size: {f['initial']}px; font-weight: bold; color: {c['accent']}; }}
.tv-tile.tv-focused {{ background-color: {c['focus']}; border-color: {c['accent']}; }}
.tv-tile.tv-focused label {{ color: {on_focus}; }}
.tv-prompt {{ font-size: {f['prompt']}px; color: {c['muted']}; }}
.tv-status {{ font-size: {f['prompt']}px; color: {c['text']}; }}
.tv-title {{ font-size: {f['title']}px; font-weight: bold; color: {c['text']}; }}
.tv-body {{ font-size: {f['body']}px; color: {c['text']}; }}
.tv-item {{ font-size: {f['body']}px; padding: {layout.px(8)}px {layout.px(20)}px; border-radius: {radius}px; }}
.tv-item.tv-focused {{ background-color: {c['focus']}; color: {on_focus}; }}
.tv-error {{ color: {c['error']}; }}
.tv-blank {{ background-color: #000000; }}
"""


class IdleWatch:
    """power.IdleGuard for a GTK window: blank after the saved timeout, sleep after the other;
    the first key on a blank screen only wakes it, and so does the first key after a resume."""

    def __init__(
        self, settings: power.Settings, *, apps_running: Callable[[], bool],
        clock: Callable[[], float] = time.monotonic, enabled: Callable[[], bool] = lambda: True,
    ) -> None:
        self.apps_running = apps_running
        self.clock = clock
        self.enabled = enabled
        self.swallow_until = float("-inf")
        self.timer = power.IdleTimer(settings, clock())

    @property
    def blanked(self) -> bool:
        return self.timer.blanked

    def apply(self, settings: power.Settings) -> None:
        self.timer.settings = settings
        self.timer.reset(self.clock())

    def keep_awake(self) -> None:
        self.timer.reset(self.clock())

    def resumed(self) -> None:
        """The box woke from sleep: wake the screen and drop the next key (the pad reconnecting, then A)."""
        now = self.clock()
        self.timer.activity(now)
        self.swallow_until = now + power.RESUME_SWALLOW_SECONDS

    def key(self) -> bool:
        """A key arrived; True when it only wakes the screen and must not act."""
        now = self.clock()
        if now < self.swallow_until:
            self.swallow_until = float("-inf")
            self.timer.activity(now)
            return True
        return self.timer.activity(now) and self.enabled()

    def tick(self) -> str | None:
        """Call about once a second: power.BLANK (show the black screen), power.SLEEP (ask for sleep) or None."""
        if not self.enabled():
            self.timer.reset(self.clock())
            return None
        return self.timer.poll(self.clock(), self.apps_running)
