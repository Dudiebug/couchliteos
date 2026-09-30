"""Idle screen blanking and automatic sleep for the launcher (OLED protection)."""

from __future__ import annotations

import os
import pathlib
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass

CONFIG = pathlib.Path("/var/lib/moonlightos/config.ini")
# Present only when QEMU passes the smoke-test flag (scripts/moonlightos-qemu-smoke).
SMOKE_FLAG = pathlib.Path("/sys/firmware/qemu_fw_cfg/by_name/opt/moonlightos.smoke/raw")
NET = pathlib.Path("/sys/class/net")

BLANK_CHOICES = (0, 2, 5, 10, 15, 30)  # minutes; 0 is off
SLEEP_CHOICES = (0, 15, 30, 60, 120)
DEFAULT_BLANK = 5
DEFAULT_SLEEP = 30
# The "is an application running?" question is only asked after this much idle
# time, and at most this often (it reads every application manifest).
APP_CHECK_AFTER = 30.0
APP_POLL_SECONDS = 5.0
BLANK = "blank"
SLEEP = "sleep"


@dataclass(frozen=True)
class Settings:
    blank: int = DEFAULT_BLANK
    sleep: int = DEFAULT_SLEEP


def minutes_label(minutes: int) -> str:
    if minutes == 0:
        return "OFF"
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} HOUR" if hours == 1 else f"{hours} HOURS"
    return f"{minutes} MIN"


def _section_header(line: str) -> str | None:
    match = re.fullmatch(r"\s*\[([^]]+)]\s*", line.rstrip("\r\n"))
    return match.group(1).strip().lower() if match else None


def load_settings(path: pathlib.Path = CONFIG) -> Settings:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return Settings()
    values: dict[str, str] = {}
    in_power = False
    for line in lines:
        header = _section_header(line)
        if header is not None:
            in_power = header == "power"
        elif in_power and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip().lower()] = value.strip()

    def pick(key: str, choices: tuple[int, ...], default: int) -> int:
        try:
            value = int(values[key])
        except (KeyError, ValueError):
            return default
        return value if value in choices else default

    return Settings(
        blank=pick("blank_minutes", BLANK_CHOICES, DEFAULT_BLANK),
        sleep=pick("sleep_minutes", SLEEP_CHOICES, DEFAULT_SLEEP),
    )


def save_settings(settings: Settings, path: pathlib.Path = CONFIG) -> None:
    """Replace the [power] section of config.ini, leaving every other section alone."""
    try:
        original = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        original = []
    kept: list[str] = []
    in_power = False
    for line in original:
        header = _section_header(line)
        if header is not None:
            in_power = header == "power"
        if not in_power:
            kept.append(line)
    if kept and not kept[-1].endswith("\n"):
        kept[-1] += "\n"
    if kept and kept[-1].strip():
        kept.append("\n")
    kept += ["[power]\n", f"blank_minutes = {settings.blank}\n", f"sleep_minutes = {settings.sleep}\n"]
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".config.ini.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.writelines(kept)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def smoke_test_active(flag: pathlib.Path = SMOKE_FLAG) -> bool:
    return flag.exists()


def wired_mac(root: pathlib.Path = NET) -> str | None:
    """MAC of the first physical Ethernet adapter, for waking the box with Wake-on-LAN."""
    try:
        names = sorted(path.name for path in root.iterdir())
    except OSError:
        return None
    for name in names:
        nic = root / name
        try:
            if (nic / "type").read_text().strip() != "1":
                continue
            if not (nic / "device").exists() or (nic / "wireless").exists():
                continue
            address = (nic / "address").read_text().strip().lower()
        except OSError:
            continue
        if re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", address) and address != "00:00:00:00:00:00":
            return address.upper()
    return None


class IdleTimer:
    """Decides when an idle launcher should blank the screen and then sleep."""

    def __init__(self, settings: Settings, now: float) -> None:
        self.settings = settings
        self.reset(now)

    def reset(self, now: float) -> None:
        self.idle_since = now
        self.last_check = float("-inf")
        self.blanked = False
        self.slept = False

    def activity(self, now: float) -> bool:
        """Note input. Returns True when the screen was blank (the key only wakes it)."""
        was_blank = self.blanked
        self.reset(now)
        return was_blank

    def poll(self, now: float, apps_running: Callable[[], bool]) -> str | None:
        pending = []
        if self.settings.blank and not self.blanked:
            pending.append((BLANK, self.settings.blank * 60.0))
        if self.settings.sleep and not self.slept:
            pending.append((SLEEP, self.settings.sleep * 60.0))
        if not pending:
            return None
        idle = now - self.idle_since
        if idle >= APP_CHECK_AFTER and now - self.last_check >= APP_POLL_SECONDS:
            self.last_check = now
            if apps_running():
                # Not idle while something is running; the timers restart when it exits.
                self.reset(now)
                self.last_check = now
                return None
        for action, seconds in pending:
            if idle >= seconds:
                if action == BLANK:
                    self.blanked = True
                else:
                    self.slept = True
                return action
        return None


class IdleGuard:
    """Sits between curses input and the launcher screens.

    Blanks the screen when idle, swallows the key that wakes it, asks for sleep
    when idle long enough, and reports a resume from sleep.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        apps_running: Callable[[], bool],
        request_sleep: Callable[[], None],
        resumed: Callable[[], bool],
        home_pending: Callable[[], bool],
        clear_home: Callable[[], None],
        clock: Callable[[], float] = time.monotonic,
        enabled: Callable[[], bool] = lambda: True,
        passive_keys: tuple[int, ...] = (-1,),
    ) -> None:
        self.apps_running = apps_running
        self.request_sleep = request_sleep
        self.resumed = resumed
        self.home_pending = home_pending
        self.clear_home = clear_home
        self.clock = clock
        self.enabled = enabled
        self.passive_keys = passive_keys
        self.timer = IdleTimer(settings, clock())

    def apply(self, settings: Settings) -> None:
        self.timer.settings = settings
        self.timer.reset(self.clock())

    def _poll(self, screen) -> None:
        action = self.timer.poll(self.clock(), self.apps_running)
        if action == BLANK:
            screen.erase()
            screen.refresh()
        elif action == SLEEP:
            self.request_sleep()

    def _wake(self, now: float) -> int:
        self.timer.activity(now)
        return -1  # the caller redraws; the waking key is not acted on

    def filter(self, screen, key: int) -> int:
        now = self.clock()
        if self.resumed():
            return self._wake(now)
        if not self.enabled():
            return key
        if key not in self.passive_keys or self.home_pending():
            return -1 if self.timer.activity(now) else key
        self._poll(screen)
        while self.timer.blanked:
            key = screen.getch()
            now = self.clock()
            if self.resumed():
                return self._wake(now)
            if key not in self.passive_keys:
                return self._wake(now)
            if self.home_pending():
                self.clear_home()
                return self._wake(now)
            self._poll(screen)
        return -1
