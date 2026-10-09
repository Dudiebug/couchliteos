"""The loading screen while a game, a stream or an application starts, as plain Python.

As on the PS3: the home screen fades to black, then the item's picture and its name come up in
the middle and a ring of dots turns in the corner until the application takes the screen. When
the TV interface is in front again, the black fades away over the home screen. Everything here is
arithmetic on the time since a phase began; couchliteos_gtk_loading draws what frame() says, and
only while something moves.

    start()   fade to black, then the picture, the name and the turning dots
    black()   plain black and still: the application has the screen, nothing more is drawn
    reveal()  the black fades away; after that the loading screen is gone (HIDDEN)

MOTION REDUCED keeps short fades and a still picture; MOTION OFF jumps, and the dots step
OFF_STEPS times a second instead of turning.
"""

from __future__ import annotations

import dataclasses
import math
import time
from collections.abc import Callable

import couchliteos_motion as motion

HIDDEN, STARTING, BLACK, REVEALING = "hidden", "starting", "black", "revealing"
FADE_MS = 400  # the home screen to black
CONTENT_MS = 300  # then the picture, the name and the dots come up (and go, at black())
REVEAL_MS = 450  # the black away when the TV is back
SPIN_MS = 1200  # one turn of the ring
BREATHE_MS = 2400  # the picture swells a little and settles
SWELL = 0.03
DOTS = 12
TAIL = 6  # lit dots behind the head, each dimmer
DIM_DOT = 0.2  # an unlit dot's brightness
FPS = 30
OFF_STEPS = 4


@dataclasses.dataclass(frozen=True)
class Frame:
    black: float = 0.0  # the scrim's opacity, 0..1
    content: float = 0.0  # the picture's, the name's and the ring's opacity, 0..1
    dots: tuple[float, ...] = (DIM_DOT,) * DOTS  # each dot's brightness, clockwise from the top
    swell: float = 1.0  # the picture's scale
    moving: bool = False  # another frame follows; False: this one holds until the next call

    @property
    def drawn(self) -> bool:
        return self.black > 0 or self.content > 0


def ring(head: float) -> tuple[float, ...]:
    """Each dot's brightness with the head at `head` (in dots, fractional between two): a tail
    fading behind it, and the next dot coming up as the head nears it."""
    lit = []
    for index in range(DOTS):
        behind = (head - index) % DOTS
        if behind < TAIL:
            glow = 1 - behind / TAIL
        elif DOTS - behind < 1:
            glow = 1 - (DOTS - behind)
        else:
            glow = 0.0
        lit.append(round(DIM_DOT + (1 - DIM_DOT) * glow, 3))
    return tuple(lit)


def _part(t: float, span: float, ease: Callable[[float], float] = motion.linear) -> float:
    """How far (0..1, eased) a change of `span` seconds is `t` seconds in; a span of 0 jumps."""
    if span <= 0:
        return 1.0 if t >= 0 else 0.0
    return ease(motion.clamp(t / span))


class Loading:
    """The loading screen's phase and its frames. `clock` is time.monotonic; `level` the MOTION level."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, level: str = motion.FULL) -> None:
        self.clock = clock
        self.level = level
        self.phase = HIDDEN
        self.since = 0.0  # the phase began
        self.spun_from = 0.0  # the ring's and the swell's time
        self.black_from = 0.0  # the scrim and the content when the phase began
        self.content_from = 0.0

    @property
    def shown(self) -> bool:
        return self.phase != HIDDEN

    def start(self) -> None:
        frame = self.frame()
        now = self.clock()
        self.black_from = frame.black  # from wherever a reveal had got to
        self.phase, self.since, self.spun_from = STARTING, now, now

    def black(self) -> None:
        self.content_from = self.frame().content if self.phase == STARTING else 0.0
        self.phase, self.since = BLACK, self.clock()

    def reveal(self) -> None:
        if self.phase == HIDDEN:
            return
        frame = self.frame()
        self.black_from, self.content_from = frame.black, frame.content
        self.phase, self.since = REVEALING, self.clock()

    def frame(self) -> Frame:
        """What to draw now. A reveal that has ended makes it HIDDEN."""
        now = self.clock()
        t = max(0.0, now - self.since)
        level = self.level
        if self.phase == STARTING:
            fade = motion.scaled(FADE_MS, level)
            black = self.black_from + (1 - self.black_from) * _part(t, fade, motion.ease_in_out_cubic)
            content = _part(t - fade, motion.scaled(CONTENT_MS, level), motion.ease_out_cubic)
            return Frame(round(black, 4), round(content, 4), self.dots(now), self.swell(now), True)
        if self.phase == BLACK:
            content = round(self.content_from * (1 - _part(t, motion.scaled(CONTENT_MS, level))), 4)
            if content <= 0:  # plain black: the same Frame every time, so the view stops its tick
                return Frame(1.0, 0.0)
            return Frame(1.0, content, self.dots(now), self.swell(now), True)
        if self.phase == REVEALING:
            span = motion.scaled(REVEAL_MS, level)
            done = _part(t, span, motion.ease_in_out_cubic)
            if done >= 1:
                self.phase = HIDDEN
                return Frame()
            content = self.content_from * (1 - _part(t, span / 2))
            return Frame(round(self.black_from * (1 - done), 4), round(content, 4), self.dots(now), self.swell(now), True)
        return Frame()

    def dots(self, now: float) -> tuple[float, ...]:
        t = now - self.spun_from
        if self.level == motion.OFF:
            return ring(float(int(t * OFF_STEPS) % DOTS))
        return ring(t * 1000 / SPIN_MS % 1 * DOTS)

    def swell(self, now: float) -> float:
        if self.level != motion.FULL:
            return 1.0
        t = (now - self.spun_from) * 1000 / BREATHE_MS
        return round(1 + SWELL * (0.5 - 0.5 * math.cos(2 * math.pi * t)), 4)
