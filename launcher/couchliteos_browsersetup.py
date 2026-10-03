"""ADD A WEB BROWSER: pick Firefox or Google Chrome and watch it install from the internet.

Opened by the setup wizard and by Settings > APPLICATIONS. Neither browser ships in the image.
The launcher only asks: this screen writes the browser's name to /run/couchliteos/browser-install
(couchliteos-browser.path then starts couchliteos-browser.service) and shows what the service
writes to /run/couchliteos/browser-status.json. An install cannot be stopped safely once apt runs,
so B only says to wait.

Everything the screen needs from outside is injected, so the tests use fakes.
"""

from __future__ import annotations

import curses
import os
import pathlib
import textwrap
import time
from typing import Any, Callable

import couchliteos_browser as browser
import couchliteos_listview as listview
import couchliteos_softwareupdate as softwareupdate

RUN = pathlib.Path("/run/couchliteos")
TITLE = "ADD A WEB BROWSER"
NOT_NOW = "NOT NOW"
INTRO = "A WEB BROWSER INSTALLS FROM THE INTERNET. PICK ONE:"
# One line each on what it is for, in the order of the rows.
ABOUT = (
    "FIREFOX: OPEN-SOURCE BROWSER FOR EVERYDAY BROWSING",
    "GOOGLE CHROME: BEST FOR STREAMING SITES AND WEB APPS",
    "NOT NOW: ADD ONE LATER IN SETTINGS > APPLICATIONS",
)
CHOICES = ("firefox", "chrome")
INSTALLED = "  (INSTALLED)"
HINT = "A / CROSS OR ENTER INSTALLS  ·  B / CIRCLE OR ESC GOES BACK"
NOT_STARTED = "THE INSTALL SERVICE DID NOT START"
FAILED = "INSTALL FAILED"
WAIT = "INSTALLING: PLEASE WAIT"
PRESS_A = "PRESS A"
POLL_MS = 500          # how often the progress screen looks at the status file
START_WAIT = 15        # seconds without any status before the service counts as not started
NORMAL_MS = 1000       # the launcher's usual input timeout, restored afterwards
ENTER_KEYS = (curses.KEY_ENTER, 10, 13)
ESC = 27
PHASE_TEXT = {"checking": "CHECKING...", "downloading": "DOWNLOADING...", "installing": "INSTALLING..."}


class BrowserSetup:
    def __init__(
        self, screen: Any, *, read_key: Callable[[Any], int], keep_awake: Callable[[], None] = lambda: None,
        installed: Callable[[], list[str]] | None = None, run_dir: pathlib.Path = RUN,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.screen = screen
        self.read_key = read_key
        self.keep_awake = keep_awake
        self.installed = installed or (lambda: [item.name for item in browser.installed_browsers()])
        self.run_dir = pathlib.Path(run_dir)
        self.clock = clock
        self.result = ""
        self.selected = 0
        self.added = False  # a browser was installed on this screen

    def rows(self) -> list[str]:
        have = self.installed()
        return [browser.BROWSERS[name].label + (INSTALLED if name in have else "") for name in CHOICES] + [NOT_NOW]

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
        softwareupdate._centered(self.screen, title_row, TITLE)
        return height, width, title_row + 2

    def draw_main(self) -> None:
        height, width, row = self.frame()
        wrap = max(8, width - 8)
        lines = [(text, 0) for text in textwrap.wrap(INTRO, wrap)] + [("", 0)] + [(text, 0) for text in ABOUT]
        if self.result:
            lines += [("", 0)] + [(text, curses.A_BOLD) for text in textwrap.wrap(self.result, wrap)]
        left = max(2, (width - max(len(text) for text, _attr in lines)) // 2)
        for text, attr in lines:
            softwareupdate._put(self.screen, row, left, text, attr)
            row += 1
        rows = self.rows()
        self.selected = min(self.selected, len(rows) - 1)
        listview.draw_rows(self.screen, rows, self.selected, row + 1, height - 4, left)
        softwareupdate._centered(self.screen, height - 3, HINT)
        self.screen.refresh()

    def run(self) -> bool | None:
        """True once a browser is installed here, False when the last try failed, None for NOT NOW or B."""
        failed = False
        while True:
            self.draw_main()
            key = self.read_key(self.screen)
            count = len(CHOICES) + 1
            if key in (curses.KEY_UP, ord("k")):
                self.selected = (self.selected - 1) % count
            elif key in (curses.KEY_DOWN, ord("j")):
                self.selected = (self.selected + 1) % count
            elif key == ESC:
                return True if self.added else (False if failed else None)
            elif key in ENTER_KEYS:
                if self.selected >= len(CHOICES):
                    return True if self.added else (False if failed else None)
                name = CHOICES[self.selected]
                if name in self.installed():
                    self.result = f"{browser.BROWSERS[name].label} IS ALREADY INSTALLED"
                    continue
                self.result = self.install(name)
                failed = name not in self.installed()
                self.added = self.added or not failed

    def install(self, name: str) -> str:
        label = browser.BROWSERS[name].label
        request = self.run_dir / browser.REQUEST_NAME
        temporary = self.run_dir / f".{browser.REQUEST_NAME}.tmp"
        try:
            (self.run_dir / browser.STATUS_NAME).unlink(missing_ok=True)  # the last run's result is not this run's
            temporary.write_text(name + "\n", encoding="ascii")
            os.replace(temporary, request)  # the path unit must never see a half-written name
        except OSError as error:
            return f"COULD NOT START THE INSTALL: {error.strerror or 'ERROR'}".upper()
        return self.progress(label)

    def draw_progress(self, label: str, text: str, percent: int | None, frame: int, hint: str,
                      with_bar: bool = True) -> None:
        height, width, row = self.frame()
        softwareupdate._centered(self.screen, row, f"INSTALLING {label}", curses.A_BOLD)
        for offset, line in enumerate(textwrap.wrap(text, max(8, width - 8))[: max(1, height - 12)]):
            softwareupdate._centered(self.screen, row + 2 + offset, line)
        if with_bar:
            line = softwareupdate.bar(percent, max(8, min(48, width - 16)), frame)
            softwareupdate._centered(self.screen, row + 6, line + (f"  {percent}%" if percent is not None else ""))
        softwareupdate._centered(self.screen, height - 3, hint)
        self.screen.refresh()

    def progress(self, label: str) -> str:
        """Follow the service until it finishes; returns the text the main screen keeps showing."""
        path = self.run_dir / browser.STATUS_NAME
        started = self.clock()
        note = ""
        frame = 0
        state: dict = {}
        self.screen.timeout(POLL_MS)
        try:
            while True:
                phase = state.get("phase") if isinstance(state.get("phase"), str) else ""
                text = softwareupdate._text(state.get("message"), PHASE_TEXT.get(phase, "WORKING...")) if phase \
                    else "STARTING THE INSTALL..."
                percent = softwareupdate._percent(state.get("percent")) if phase else None
                self.draw_progress(label, text, percent, frame, note)
                frame += 1
                key = self.read_key(self.screen)
                self.keep_awake()  # a long download must not blank or sleep the box
                state = softwareupdate.read_status(path)
                phase = state.get("phase") if isinstance(state.get("phase"), str) else ""
                if phase == "done":
                    return softwareupdate._text(state.get("message"), f"{label} IS INSTALLED")
                if phase == "failed":
                    return self.failure(label, softwareupdate._text(state.get("message"), FAILED))
                if not phase and self.clock() - started >= START_WAIT:
                    return self.failure(label, NOT_STARTED)
                if key == ESC:
                    note = WAIT
        finally:
            self.screen.timeout(NORMAL_MS)

    def failure(self, label: str, message: str) -> str:
        """Show why it stopped until A or B is pressed."""
        while True:
            self.draw_progress(label, message, None, 0, PRESS_A, with_bar=False)
            key = self.read_key(self.screen)
            if key in ENTER_KEYS or key == ESC:
                return message


def show(screen: Any, **injected: Any) -> bool | None:
    """Run the screen on `screen`; `read_key` must be given (see BrowserSetup)."""
    return BrowserSetup(screen, **injected).run()
