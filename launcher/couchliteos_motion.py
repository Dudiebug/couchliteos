"""Motion for the TV interface, as plain Python: easing, tweens and the animator that drives them.

couchliteos-tv.py (GTK) calls Animator.step() from the window's frame clock while anything is
moving and stops the tick callback when Animator.idle, so a still screen costs no CPU. Tweens are
time-based: a dropped frame jumps ahead, it never slows the motion down.

gamepad-nav repeats a held D-pad every 120 ms. A tween that is retargeted while it runs starts
from where it is now and takes at most HELD_MS, so held keys never queue up animations.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

FULL, REDUCED, OFF = "full", "reduced", "off"
LEVELS = (FULL, REDUCED, OFF)

# Durations in ms (MOTION FULL).
FOCUS_MS = 140
ITEM_MS = 160
CATEGORY_MS = 220
PAGE_MS = 220
FADE_MS = 180
TOAST_MS = 250
WAVE_IN_MS = 600
HELD_MS = 100  # a retarget mid-tween finishes within this, under gamepad-nav's 120 ms repeat
HELD_WINDOW = 0.25  # s: moves this close together are a held key (gamepad-nav repeats every 0.12 s)
SLOW_FRAME_MS = 40  # median frame interval above this drops FULL to REDUCED


def linear(t: float) -> float:
    return t


def ease_out_cubic(t: float) -> float:
    return 1 - (1 - t) ** 3


def ease_in_out_cubic(t: float) -> float:
    return 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def ease_out_back(t: float, overshoot: float = 1.2) -> float:
    """A little past the end and back: the XMB's focused icon 'pop'."""
    c3 = overshoot + 1
    return 1 + c3 * (t - 1) ** 3 + overshoot * (t - 1) ** 2


def clamp(t: float) -> float:
    return 0.0 if t <= 0 else 1.0 if t >= 1 else t


@dataclasses.dataclass
class Tween:
    """A value going from `start` to `end` over `duration` seconds from `t0`."""

    start: float
    end: float
    t0: float
    duration: float
    ease: Callable[[float], float] = ease_out_cubic

    def progress(self, now: float) -> float:
        return 1.0 if self.duration <= 0 else clamp((now - self.t0) / self.duration)

    def value(self, now: float) -> float:
        p = self.progress(now)
        return self.end if p >= 1 else self.start + (self.end - self.start) * self.ease(p)

    def done(self, now: float) -> bool:
        return self.progress(now) >= 1


def scaled(ms: float, level: str) -> float:
    """`ms` as seconds at motion `level`: REDUCED keeps short fades, OFF jumps."""
    if level == OFF:
        return 0.0
    if level == REDUCED:
        return min(ms, FADE_MS) / 1000 * 0.6
    return ms / 1000


class Animator:
    """Named values that move towards their targets; read them with get()."""

    def __init__(self, clock: Callable[[], float], level: str = FULL) -> None:
        self.clock = clock
        self.level = level if level in LEVELS else FULL
        self.tweens: dict[str, Tween] = {}
        self.values: dict[str, float] = {}
        self.moved: dict[str, float] = {}  # when each value was last sent somewhere new

    def set(self, name: str, value: float) -> None:
        """Jump straight to `value` (first layout, theme change, MOTION OFF)."""
        self.tweens.pop(name, None)
        self.values[name] = value

    def to(self, name: str, target: float, ms: float, ease: Callable[[float], float] = ease_out_cubic,
           *, movement: bool = True) -> None:
        """Move `name` to `target` over `ms` (at FULL). Under REDUCED a `movement` (slide, scale)
        jumps and only fades animate. A move soon after the last one (a held key) starts from the
        current value and takes at most HELD_MS, so the motion keeps up with the repeats."""
        now = self.clock()
        current = self.get(name, target)
        running = self.tweens.get(name)
        if running is not None and running.end == target:
            return
        seconds = 0.0 if (movement and self.level == REDUCED) else scaled(ms, self.level)
        if now - self.moved.get(name, float("-inf")) < HELD_WINDOW:
            seconds = min(seconds, HELD_MS / 1000)
        self.moved[name] = now
        if seconds <= 0 or current == target:
            self.set(name, target)
            return
        self.tweens[name] = Tween(current, target, now, seconds, ease)

    def get(self, name: str, default: float = 0.0) -> float:
        tween = self.tweens.get(name)
        if tween is not None:
            return tween.value(self.clock())
        return self.values.get(name, default)

    def target(self, name: str, default: float = 0.0) -> float:
        tween = self.tweens.get(name)
        return tween.end if tween is not None else self.values.get(name, default)

    def step(self) -> bool:
        """Settle finished tweens; True while anything is still moving (keep ticking)."""
        now = self.clock()
        for name, tween in list(self.tweens.items()):
            if tween.done(now):
                self.values[name] = tween.end
                del self.tweens[name]
        return bool(self.tweens)

    @property
    def idle(self) -> bool:
        return not self.tweens


class FrameMeter:
    """Watches the first animated frames; slow() when their median interval is over SLOW_FRAME_MS."""

    SAMPLES = 30

    def __init__(self) -> None:
        self.last: float | None = None
        self.intervals: list[float] = []

    def frame(self, now: float) -> None:
        if self.last is not None and len(self.intervals) < self.SAMPLES:
            gap = now - self.last
            if gap < 0.5:  # a pause between animations is not a slow frame
                self.intervals.append(gap)
        self.last = now

    def pause(self) -> None:
        self.last = None

    def slow(self) -> bool:
        if len(self.intervals) < self.SAMPLES:
            return False
        ordered = sorted(self.intervals)
        return ordered[len(ordered) // 2] * 1000 > SLOW_FRAME_MS

    def fps(self) -> float:
        if not self.intervals:
            return 0.0
        mean = sum(self.intervals) / len(self.intervals)
        return 0.0 if mean <= 0 else round(1 / mean, 1)


def level_for(saved: str | None, renderer: str | None) -> str:
    """The motion level to use: the saved one, but at most REDUCED on the software (cairo) renderer."""
    level = saved if saved in LEVELS else FULL
    if (renderer or "").strip().lower() == "cairo" and level == FULL:
        return REDUCED
    return level

