"""What the TV interface does while a game, a stream or an application has the screen, as plain Python.

The application covers the TV interface in cage, but nothing underneath stops by itself: GLib
timers fire, threads poll, the music player plays to nobody. This decides when the TV interface
rests (its music player process ends, the GL wave and the picture caches go, the pollers and the
background workers wait, the tick slows down) and when it wakes again, and which sound marks
each change:

    start()       a start request went out: the launch sound now, the rest once the loading
                  screen is black (BLACK_AFTER)
    start_done()  the start's own loop ended (it started, or it quit by itself at once)
    failed()      the start failed: the error sound, awake again, the loading screen away
    update(seen)  every tick: rest at once for the blank screen and sleep, after REST_AFTER for an
                  application in front (a quick look Home costs nothing); wake when the TV is in
                  front again, with the return sound when something had the screen
    returning()   the TV is about to take the front (Home, a resume from sleep): wake first

What it looks at (look()) are the marks in RUN that gamepad-nav reads too: an application is in
front while its ready mark is there and launcher-focus is not. Each call returns a Plan, which
couchliteos-tv.py carries out; `awake` is the Event the pollers and the workers wait on.
"""

from __future__ import annotations

import dataclasses
import math
import pathlib
import threading
import time
from collections.abc import Callable

LAUNCH, RETURN, ERROR = "launch", "return", "error"  # the sounds (couchliteos_quick.SOUND_NAMES)
REST_AFTER = 2.0  # s an application has been in front before the TV rests
BLACK_AFTER = 0.75  # s after a start request: the loading screen is black by then
QUIET_AFTER_LAUNCH = 2.0  # s: no return sound this soon after the launch sound (it quit at once)
RETURN_MUSIC = 0.9  # s the music waits after the return sound before it fades in
AWAY_TICK = 5  # s between ticks behind an application (while RUN is watched for what brings it back)

READY = "-ready"
LAUNCHER_READY = "launcher-ready"
LAUNCHER_FOCUS = "launcher-focus"
WATCHED = (LAUNCHER_FOCUS, "home.request", "resumed")  # and every application's ready mark


def watched(name: str) -> bool:
    """A file in RUN whose change can bring the TV back, for the directory monitor."""
    return name in WATCHED or (name.endswith(READY) and name != LAUNCHER_READY)


@dataclasses.dataclass(frozen=True)
class Seen:
    apps: frozenset[str] = frozenset()  # the ready marks: the applications running
    launcher_focus: bool = False  # the TV was brought in front of them
    blank: bool = False
    asleep: bool = False

    @property
    def app_in_front(self) -> bool:
        return bool(self.apps) and not self.launcher_focus

    @property
    def hidden(self) -> bool:
        return self.app_in_front or self.blank or self.asleep


def look(run_dir: pathlib.Path, *, blank: bool = False, asleep: bool = False) -> Seen:
    """What is in front now, from RUN (the same marks as gamepad-nav's launcher_in_front)."""
    try:
        apps = frozenset(path.name[:-len(READY)] for path in run_dir.glob(f"*{READY}") if path.name != LAUNCHER_READY)
    except OSError:
        apps = frozenset()
    return Seen(apps, (run_dir / LAUNCHER_FOCUS).exists(), blank, asleep)


@dataclasses.dataclass(frozen=True)
class Plan:
    rest: bool = False  # stop the music player, drop the wave and the caches, pause the workers
    wake: bool = False  # undo the rest
    sound: str = ""  # LAUNCH, RETURN or ERROR
    music_after: float = 0.0  # s the music stays held after the sound
    black: bool = False  # the loading screen goes plain black and still: an application has the screen
    reveal: bool = False  # the loading screen fades away

    def __bool__(self) -> bool:
        return self != Plan()


class Front:
    """Who has the screen, and whether the TV interface rests. `clock` is time.monotonic."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.awake = threading.Event()  # cleared while it rests: pollers and workers wait on it
        self.awake.set()
        self.starting = False
        self.started_at = -math.inf
        self.launched_at = -math.inf  # the last launch sound
        self.apps: frozenset[str] = frozenset()
        self.app_in_front = False
        self.front_since: float | None = None  # an application in front, and nothing starting, since
        self.app_shown = False  # an application had the screen since the TV last had it
        self.resting = False
        self.dark = False  # the loading screen is up (starting, or plain black)
        self.black = False  # ... and plain black

    @property
    def slow(self) -> bool:
        """Resting behind an application with the loading screen black: the slow tick."""
        return self.resting and self.black and self.app_in_front

    def start(self) -> Plan:
        now = self.clock()
        self.starting, self.started_at, self.launched_at = True, now, now
        self.dark, self.black = True, False
        return Plan(sound=LAUNCH)

    def start_done(self) -> None:
        self.starting = False

    def failed(self) -> Plan:
        """The start failed (its failure screen comes next): the error sound, never the return one."""
        self.starting = False
        plan = self._back(self.clock())
        return dataclasses.replace(plan, sound=ERROR, music_after=0.0)

    def returning(self) -> Plan:
        """The TV takes the front now: wake before it is seen."""
        if self.starting:
            return Plan()
        return self._back(self.clock())

    def update(self, seen: Seen) -> Plan:
        now = self.clock()
        ended = bool(self.apps - seen.apps)
        self.apps = seen.apps
        self.app_in_front = seen.app_in_front
        if seen.app_in_front and not self.starting:
            if self.front_since is None:
                self.front_since = now
            self.app_shown = True
        else:
            self.front_since = None
        if not (seen.hidden or self.starting):
            return self._back(now, ended)
        settled = self.front_since is not None and now - self.front_since >= REST_AFTER
        rest = not self.resting and (seen.blank or seen.asleep or settled
                                     or (self.starting and now - self.started_at >= BLACK_AFTER))
        black = settled and not self.black
        if rest:
            self.resting = True
            self.awake.clear()
        if black:
            self.dark = self.black = True
        return Plan(rest=rest, black=black)

    def _back(self, now: float, ended: bool = False) -> Plan:
        """The TV is in front and nothing starts: awake, the loading screen away, and the return
        sound when something had the screen (not right after the launch sound: it quit at once)."""
        back = self.app_shown or self.dark or ended
        sound = RETURN if back and now - self.launched_at >= QUIET_AFTER_LAUNCH else ""
        plan = Plan(wake=self.resting, reveal=self.dark, sound=sound, music_after=RETURN_MUSIC if sound else 0.0)
        self.resting = self.dark = self.black = self.app_shown = self.app_in_front = False
        self.front_since = None
        self.awake.set()
        return plan
