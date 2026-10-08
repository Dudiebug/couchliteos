"""The loading animation on the TV's busy screen, as plain Python: where each dot of the ring is,
and how bright, at a moment.

The busy screen is what the TV shows while something starts or is waited for: STARTING <APP>,
WAKING <PC>, the auto-stream count-down. couchliteos_gtk_loading draws the ring over its title;
it replaces the text spinner ("|/-\\") those titles used to end with. MOTION decides how it moves:

    FULL     the PS3's busy ring: a bright head going round, a fading tail behind it
    REDUCED  nothing goes round: the whole ring brightens and dims, slowly
    OFF      no animation: the head steps on to the next dot twice a second

Nothing here imports GTK.
"""

from __future__ import annotations

import dataclasses
import math

import couchliteos_motion as motion

DOTS = 12
TURN_SECONDS = 1.2  # one turn of the head (FULL)
TAIL = 0.5  # of the ring, behind the head, that still glows
BREATHE_SECONDS = 2.4  # one bright-dim-bright (REDUCED)
STEP_SECONDS = 0.5  # the head's step (OFF)
DIM = 0.2  # a dot away from the head
DOT = 0.15  # a dot's radius against the ring's
HEAD_GROWTH = 0.4  # the head is this much bigger than a dim dot


@dataclasses.dataclass(frozen=True)
class Dot:
    """One dot: its centre from the ring's centre in ring radii (y down; dot 0 at the top, then
    clockwise), its brightness (0 to 1) and its radius in ring radii."""

    x: float
    y: float
    alpha: float
    radius: float


def head(elapsed: float, level: str, count: int = DOTS) -> float:
    """Where the head is, in turns (0 at the top). OFF snaps it to a dot."""
    if level == motion.OFF:
        return (int(elapsed / STEP_SECONDS) % count) / count
    return (elapsed / TURN_SECONDS) % 1.0


def glow(position: float, at: float, count: int = DOTS) -> float:
    """How lit a dot at `position` (turns) is with the head at `at`: 1 under the head, fading
    along the tail, and coming up as the head nears it, so no dot ever flicks on."""
    behind = (at - position) % 1.0  # 0 under the head, growing along the tail
    ahead = (position - at) % 1.0
    tail = max(0.0, 1 - behind / TAIL)
    lead = max(0.0, 1 - ahead * count)  # the next dot, as the head gets there
    return max(tail ** 1.6, lead)


def ring(elapsed: float, level: str, count: int = DOTS) -> tuple[Dot, ...]:
    """The ring `elapsed` seconds after the busy screen came up, at motion `level`."""
    if level == motion.REDUCED:
        lit = 0.5 - 0.5 * math.cos(2 * math.pi * elapsed / BREATHE_SECONDS)
        return tuple(_dot(index, count, DIM + (0.8 - DIM) * lit, 0.0) for index in range(count))
    at = head(elapsed, level, count)
    dots = []
    for index in range(count):
        lit = glow(index / count, at, count)
        dots.append(_dot(index, count, DIM + (1 - DIM) * lit, lit))
    return tuple(dots)


def _dot(index: int, count: int, alpha: float, lit: float) -> Dot:
    angle = 2 * math.pi * index / count
    return Dot(math.sin(angle), -math.cos(angle), round(alpha, 4), DOT * (1 + HEAD_GROWTH * lit))


def frame(elapsed: float, level: str) -> float:
    """Changes when the picture does: the GTK side draws again only then (OFF: twice a second)."""
    return int(elapsed / STEP_SECONDS) if level == motion.OFF else elapsed


def appear(elapsed: float, level: str) -> float:
    """The ring's opacity as the busy screen comes up: a short fade, none with MOTION OFF."""
    seconds = motion.scaled(motion.FADE_MS, level)
    return 1.0 if seconds <= 0 else motion.ease_out_cubic(motion.clamp(elapsed / seconds))
