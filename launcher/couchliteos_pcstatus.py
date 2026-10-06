"""The one-line "is my gaming PC ready?" status under the title of the main screen.

It is empty when no gaming PC is paired: not everyone streams from one.

The launcher only ever reads a string (Monitor.line). The network probe
(stream.probe: a TCP connect to Sunshine, never ping, never Wake-on-LAN) runs in a
daemon thread every INTERVAL seconds, and never while an application or stream is
running. Any error leaves the line empty, so the screen looks as it always did.
"""

from __future__ import annotations

import os
import pathlib
import threading
import unicodedata
from collections.abc import Callable

import couchliteos_stream as stream

RUN = pathlib.Path(os.environ.get("COUCHLITEOS_RUN_DIR", "/run/couchliteos"))
INTERVAL = 20.0  # seconds between probes
RETRY = 2.0  # while an app runs, look again this soon so the line is fresh when it ends
LABEL_CELLS = 24  # columns of the PC name; the longest line is then 72 of 76
ENDINGS = {
    "up": "READY",
    "awake": "IS ON BUT SUNSHINE IS NOT ANSWERING",
    "down": "ASLEEP - STARTING MOONLIGHT WAKES IT",
}
NOT_FOUND = "NOT FOUND - IS IT TURNED ON?"  # "down", and Moonlight cannot wake it either


def app_active(run: pathlib.Path = RUN) -> bool:
    """An application (Moonlight included) is running or starting."""
    return (run / "app-active").exists() or (run / "moonlight-ready").exists()


def pick_host(hosts: list[stream.Host], settings: stream.StreamSettings) -> stream.Host | None | int:
    """The default PC, else None (no PC paired) or the count when it would be a guess."""
    if not hosts:
        return None
    return stream.default_host(hosts, settings) or len(hosts)


def label(name: str) -> str:
    """The PC name in capitals: printable characters only, at most LABEL_CELLS columns."""
    out: list[str] = []
    cells = 0
    for char in name.upper():
        if not char.isprintable():
            continue
        width = 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if cells + width > LABEL_CELLS:
            break
        out.append(char)
        cells += width
    return "".join(out)


def status_text(
    hosts: list[stream.Host], host: stream.Host | None | int, result: str | None,
    link_up: bool, wake_block: str = "",
) -> str:
    """The line for a probe `result` ("up", "awake", "down", "unknown" or None = no result yet)."""
    if not link_up:
        return ""  # the status line already says OFFLINE
    if not hosts:
        return ""  # no gaming PC is fine: this box is also a PlayStation Remote Play client or a desktop
    if isinstance(host, int):
        return f"GAMING PCS: {host} PAIRED"
    if host is None:
        return ""
    ending = NOT_FOUND if result == "down" and wake_block else ENDINGS.get(result or "")
    return f"GAMING PC: {label(host.name)} {ending}" if ending else ""


class Monitor:
    """Keeps the latest status line, refreshed off the UI thread."""

    def __init__(
        self,
        load: Callable[[], list[stream.Host]] = stream.load_hosts,
        settings: Callable[[], stream.StreamSettings] = stream.load_settings,
        probe: Callable[[stream.Host], str] = stream.probe,
        link: Callable[[], bool] = stream.link_up,
        app_active: Callable[[], bool] = app_active,
        interval: float = INTERVAL,
    ) -> None:
        self.load = load
        self.settings = settings
        self.probe = probe
        self.link = link
        self.app_active = app_active
        self.interval = interval
        self._text = ""
        self._stop = threading.Event()

    def _compute(self) -> str:
        if not self.link():
            return ""
        hosts = self.load()  # re-read every cycle: the stick moves between PCs
        chosen = pick_host(hosts, self.settings())
        if not isinstance(chosen, stream.Host):
            return status_text(hosts, chosen, None, True)
        return status_text(hosts, chosen, self.probe(chosen), True, stream.wake_block(chosen))

    def refresh(self) -> bool:
        """One cycle. False when it did nothing because an app is running (last text kept)."""
        try:
            if self.app_active():
                return False
            text = self._compute()
            if self.app_active():  # a stream started while we probed: that result is moot
                return False
            self._text = text
        except Exception:  # any failure means no line; it must never reach the launcher
            self._text = ""
        return True

    def line(self) -> str:
        return self._text

    def start(self) -> None:
        def loop() -> None:
            while True:
                wait = self.interval if self.refresh() else min(self.interval, RETRY)
                if self._stop.wait(wait):
                    return

        threading.Thread(target=loop, name="pc-status", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
