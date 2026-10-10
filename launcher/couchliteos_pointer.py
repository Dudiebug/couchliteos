#!/usr/bin/python3
"""Which apps the controller drives as a mouse ("controller mouse"), and the flag gamepad-nav reads.

Browsers and web applications have no controller support, so for them the
left stick moves a pointer, the right stick scrolls and A clicks. Games and
streaming clients read the controller themselves and keep it. The choice can
be changed per app from the Guide menu (ACTIVE APPLICATIONS) until the
launcher restarts.
"""

from __future__ import annotations

import os
import pathlib
from typing import Any

FLAG = pathlib.Path(os.environ.get("COUCHLITEOS_RUN_DIR", "/run/couchliteos")) / "pointer-mode"
BROWSER_IDS = frozenset({"firefox", "google-chrome", "lofi-radio"})  # LO-FI RADIO is a page in a browser
# User-added web applications run Chrome in kiosk mode (Settings > APPLICATIONS > ADD WEB APPLICATION).
BROWSER_BINARIES = frozenset({
    "firefox", "firefox-esr", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
})


def default_for(app: Any) -> bool:
    """True for a browser or a web application."""
    if app.id in BROWSER_IDS:
        return True
    if app.kind == "command" and not getattr(app, "terminal", False) and app.command:
        return pathlib.PurePath(app.command).name in BROWSER_BINARIES
    return False


class Modes:
    def __init__(self, flag: pathlib.Path = FLAG) -> None:
        self.flag = flag
        self.choices: dict[str, bool] = {}
        self.front: str | None = None  # the app last brought to the front

    def enabled(self, app: Any) -> bool:
        return self.choices.get(app.id, default_for(app))

    def toggle(self, app: Any) -> bool:
        self.choices[app.id] = not self.enabled(app)
        if self.front == app.id:
            self.write(self.choices[app.id])
        return self.choices[app.id]

    def apply(self, app: Any) -> None:
        """`app` is coming to the front: hand gamepad-nav its choice."""
        self.front = app.id
        self.write(self.enabled(app))

    def write(self, on: bool) -> None:
        try:
            if on:
                self.flag.touch()
            else:
                self.flag.unlink(missing_ok=True)
        except OSError:
            pass  # without /run the pad simply stays a pad
