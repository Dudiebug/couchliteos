"""Settings > SOFTWARE UPDATE: look for a newer release, install it, and watch it happen.

The launcher only asks. Installing is the root service's job: this screen touches
/run/couchliteos/update-install (couchliteos-update.path then starts couchliteos-update.service) and
shows what the service writes to /run/couchliteos/update-status.json. While the file is being
downloaded B asks the service to stop (update-cancel); once the install has started it cannot be
stopped safely, so B only says to wait.

Everything the screen needs from outside is injected, so the tests use fakes.
"""

from __future__ import annotations

import curses
import json
import pathlib
import textwrap
import time
from typing import Any, Callable

import couchliteos_confirm as confirmation
import couchliteos_listview as listview
import couchliteos_update as update

RUN = pathlib.Path("/run/couchliteos")
REQUEST, CANCEL, STATUS = "update-install", "update-cancel", "update-status.json"
TITLE = "SOFTWARE UPDATE"
CHECK, INSTALL, BACK = "CHECK FOR UPDATES", "INSTALL UPDATE", "BACK"
HINT = "A / CROSS SELECTS  ·  B / CIRCLE GOES BACK"
LIVE_TEXT = (
    "UPDATES INSTALL ON A BOX THAT RUNS FROM ITS DISK. ON A USB STICK: WRITE THE NEW ISO TO THE ISO STICK "
    "AND KEEP THE PERSISTENCE STICK."
)
CHECKING = "CHECKING..."
NEWEST = "THIS IS THE NEWEST VERSION"
NO_NETWORK = "COULD NOT REACH GITHUB: CHECK SETTINGS > NETWORK"
NO_FILE = "THIS RELEASE HAS NO FILE FOR THIS BOX"
CLOSE_APPS = "CLOSE RUNNING APPS FIRST"
CANCELLED = "UPDATE CANCELLED"
NOT_STARTED = "THE UPDATE SERVICE DID NOT START"
NOT_RESTARTED = "THE BOX DID NOT RESTART: CHOOSE REBOOT ON THE HOME SCREEN"
FAILED = "UPDATE FAILED"
PRESS_A = "PRESS A"
CHECK_TIMEOUT = 10     # seconds GitHub gets to answer the check
POLL_MS = 500          # how often the progress screen looks at the status file
START_WAIT = 15        # seconds without any status before the service counts as not started
RESTART_WAIT = 90      # seconds in "restarting" before the screen admits the box did not restart
NORMAL_MS = 1000       # the launcher's usual input timeout, restored afterwards
ENTER_KEYS = (curses.KEY_ENTER, 10, 13)
ESC = 27
PHASE_TEXT = {
    "checking": "CHECKING FOR UPDATES...", "downloading": "DOWNLOADING...", "verifying": "CHECKING THE DOWNLOAD...",
    "installing": "INSTALLING...", "restarting": "RESTARTING...",
}
PHASE_HINT = {"downloading": "B / CIRCLE CANCELS THE DOWNLOAD", "installing": "KEEP THE BOX PLUGGED IN"}


def default_fetch(current: str) -> update.Release:
    return update.fetch_release(current, timeout=CHECK_TIMEOUT)


def read_status(path: pathlib.Path) -> dict:
    """The service's status; {} while it is missing or only half written."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def bar(percent: int | None, width: int, frame: int = 0) -> str:
    """An ASCII bar exactly `width` wide; a segment bounces along it when the percentage is unknown."""
    width = max(8, width)
    inner = width - 2
    if percent is None:
        segment = max(2, min(8, inner // 4))
        travel = inner - segment
        cycle = frame % (travel * 2)
        position = cycle if cycle <= travel else travel * 2 - cycle
        return "[" + " " * position + "=" * segment + " " * (inner - position - segment) + "]"
    filled = inner * max(0, min(100, percent)) // 100
    return "[" + "=" * filled + " " * (inner - filled) + "]"


def _percent(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, min(100, int(value)))


def _text(value: Any, fallback: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _put(screen: Any, row: int, column: int, text: str, attr: int = 0) -> None:
    height, width = screen.getmaxyx()
    if 0 <= row < height - 1 and 0 <= column < width - 1:  # the last row and column belong to the border
        try:
            screen.addnstr(row, column, text, width - column - 1, attr)
        except curses.error:
            pass


def _centered(screen: Any, row: int, text: str, attr: int = 0) -> None:
    _height, width = screen.getmaxyx()
    _put(screen, row, max(1, (width - len(text)) // 2), text, attr)


class SoftwareUpdate:
    def __init__(
        self, screen: Any, *, read_key: Callable[[Any], int], apps_running: Callable[[], bool],
        confirm: Callable[[Any, str], bool] = confirmation.confirm, keep_awake: Callable[[], None] = lambda: None,
        current: str | None = None, profile: dict[str, str] | None = None, live: bool | None = None,
        fetch: Callable[[str], update.Release] = default_fetch, run_dir: pathlib.Path = RUN,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.screen = screen
        self.read_key = read_key
        self.apps_running = apps_running
        self.confirm = confirm
        self.keep_awake = keep_awake
        self.current = update.installed_version() if current is None else current
        self.profile = update.read_profile() if profile is None else profile
        self.live = update.is_live() if live is None else live
        self.fetch = fetch
        self.run_dir = pathlib.Path(run_dir)
        self.clock = clock
        self.release: update.Release | None = None
        self.asset: update.Asset | None = None
        self.result = ""
        self.selected = 0

    # -- the main screen -----------------------------------------------------------------------

    def rows(self) -> list[str]:
        if self.live:
            return [BACK]
        return ([INSTALL] if self.release else []) + [CHECK, BACK]

    def box_line(self) -> str:
        name = self.profile.get("PROFILE_NAME", "").upper()
        return f"THIS BOX: COUCHLITEOS {self.current or 'UNKNOWN'}" + (f" ({name})" if name else "")

    def frame(self) -> tuple[int, int, int]:
        """Erase, draw the border and the title; returns (height, width, first row below the title)."""
        height, width = self.screen.getmaxyx()
        self.screen.erase()
        if height >= 8 and width >= 24:
            try:
                self.screen.border()
            except curses.error:
                pass
        title_row = 2 if height >= 16 else 1
        _centered(self.screen, title_row, TITLE)
        return height, width, title_row + 2

    def draw_main(self) -> None:
        height, width, row = self.frame()
        wrap = max(8, width - 8)
        lines = [(self.box_line(), 0), ("", 0)]
        if self.live:
            lines += [(text, 0) for text in textwrap.wrap(LIVE_TEXT, wrap)] + [("", 0), (update.RELEASES_TEXT, 0)]
        elif self.result:
            lines += [(text, curses.A_BOLD) for text in textwrap.wrap(self.result, wrap)]
        left = max(2, (width - max(len(text) for text, _attr in lines)) // 2)
        for text, attr in lines:
            _put(self.screen, row, left, text, attr)
            row += 1
        rows = self.rows()
        self.selected = min(self.selected, len(rows) - 1)
        listview.draw_rows(self.screen, rows, self.selected, row + 1, height - 4, left)
        _centered(self.screen, height - 3, HINT)
        self.screen.refresh()

    def run(self) -> None:
        while True:
            self.draw_main()
            key = self.read_key(self.screen)
            rows = self.rows()
            if key in (curses.KEY_UP, ord("k")):
                self.selected = (self.selected - 1) % len(rows)
            elif key in (curses.KEY_DOWN, ord("j")):
                self.selected = (self.selected + 1) % len(rows)
            elif key == ESC:
                return
            elif key in ENTER_KEYS:
                choice = rows[self.selected]
                if choice == BACK:
                    return
                if choice == CHECK:
                    self.check()
                    self.selected = 0  # INSTALL UPDATE when there is one, else CHECK again
                else:
                    self.install()

    # -- checking and installing -----------------------------------------------------------------

    def check(self) -> None:
        self.release = self.asset = None
        self.result = CHECKING
        self.draw_main()
        try:
            release = self.fetch(self.current)
        except Exception:  # noqa: BLE001 - offline, DNS, TLS, a changed API: all one message for the owner
            self.result = NO_NETWORK
            return
        if not update.is_newer(release.version, self.current):
            self.result = NEWEST
            return
        try:
            asset = update.pick_iso(release, self.profile.get("ISO_SUFFIX", ""))
            update.sums_asset(release)  # the service verifies the download against it
        except update.UpdateError:
            self.result = NO_FILE
            return
        self.release, self.asset = release, asset
        self.result = f"COUCHLITEOS {release.version} IS AVAILABLE"

    def install(self) -> None:
        if self.apps_running():
            self.result = CLOSE_APPS
            return
        if self.release is None or self.asset is None:
            return
        gigabytes = max(1, int(self.asset.size / 1e9 + 0.5))
        question = (
            f"INSTALL COUCHLITEOS {self.release.version}? DOWNLOADS ABOUT {gigabytes} GB, INSTALLS IT, THEN "
            "RESTARTS. PAIRINGS, WI-FI, BLUETOOTH AND SETTINGS ARE KEPT. KEEP THE BOX PLUGGED IN UNTIL IT RESTARTS."
        )
        if not self.confirm(self.screen, question):
            return
        try:
            (self.run_dir / STATUS).unlink(missing_ok=True)  # the last run's result is not this run's
            (self.run_dir / REQUEST).touch()
        except OSError as error:
            self.result = f"COULD NOT START THE UPDATE: {error.strerror or 'ERROR'}".upper()
            return
        self.result = self.progress(self.release.version)
        if self.result == NEWEST:  # the service found nothing newer after all
            self.release = self.asset = None

    # -- watching the service --------------------------------------------------------------------

    def draw_progress(
        self, version: str, text: str, percent: int | None, frame: int, hint: str, with_bar: bool = True
    ) -> None:
        height, width, row = self.frame()
        _centered(self.screen, row, f"UPDATING TO COUCHLITEOS {version}", curses.A_BOLD)
        for offset, line in enumerate(textwrap.wrap(text, max(8, width - 8))[: max(1, height - 12)]):
            _centered(self.screen, row + 2 + offset, line)
        if with_bar:
            line = bar(percent, max(8, min(48, width - 16)), frame)
            _centered(self.screen, row + 6, line + (f"  {percent}%" if percent is not None else ""))
        _centered(self.screen, height - 3, hint)
        self.screen.refresh()

    def progress(self, version: str) -> str:
        """Follow the service until it finishes; returns the text the main screen keeps showing."""
        path = self.run_dir / STATUS
        started = self.clock()
        restarting_since: float | None = None
        cancelling = False
        note = last_phase = ""
        frame = 0
        state: dict = {}
        self.screen.timeout(POLL_MS)
        try:
            while True:
                phase = state.get("phase") if isinstance(state.get("phase"), str) else ""
                shown = _text(state.get("version"), version)
                text = PHASE_TEXT.get(phase, "WORKING...") if phase else "STARTING THE UPDATE SERVICE..."
                if phase in ("downloading", "installing", "checking", "verifying"):
                    text = _text(state.get("message"), text)
                hint = "CANCELLING..." if cancelling and phase == "downloading" else note or PHASE_HINT.get(phase, "")
                self.draw_progress(shown, text, _percent(state.get("percent")) if phase else None, frame, hint)
                frame += 1
                key = self.read_key(self.screen)
                self.keep_awake()  # a long download must not blank or sleep the box
                state = read_status(path)
                phase = state.get("phase") if isinstance(state.get("phase"), str) else ""
                if phase != last_phase:
                    last_phase, note = phase, ""
                now = self.clock()
                if phase == "failed":
                    return self.failure(shown, _text(state.get("message"), FAILED))
                if phase == "cancelled":
                    return CANCELLED
                if phase == "uptodate":
                    return NEWEST
                if not phase and now - started >= START_WAIT:
                    return self.failure(shown, NOT_STARTED)
                if phase == "restarting":
                    restarting_since = now if restarting_since is None else restarting_since
                    if now - restarting_since >= RESTART_WAIT:
                        return self.failure(shown, NOT_RESTARTED)
                if key == ESC:
                    if phase == "downloading" and not cancelling:
                        cancelling = True
                        try:
                            (self.run_dir / CANCEL).touch()
                        except OSError:
                            cancelling = False
                            note = "COULD NOT CANCEL"
                    elif phase == "installing":
                        note = "INSTALLING: PLEASE WAIT"
                    elif phase not in ("downloading", "restarting"):
                        note = "PLEASE WAIT"
        finally:
            self.screen.timeout(NORMAL_MS)

    def failure(self, version: str, message: str) -> str:
        """Show why it stopped until A or B is pressed."""
        while True:
            self.draw_progress(version, message, None, 0, PRESS_A, with_bar=False)
            key = self.read_key(self.screen)
            self.keep_awake()
            if key in ENTER_KEYS or key == ESC:
                return message


def show(screen: Any, **injected: Any) -> None:
    """Run the screen on `screen`; `read_key` and `apps_running` must be given (see SoftwareUpdate)."""
    SoftwareUpdate(screen, **injected).run()
