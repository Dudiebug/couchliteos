"""Idle screen blanking and automatic sleep for the launcher (OLED protection)."""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

DATA = pathlib.Path(os.environ.get("COUCHLITEOS_STATE_DIR", "/var/lib/couchliteos"))
CONFIG = DATA / "config.ini"
# Present only when QEMU passes the smoke-test flag (scripts/couchliteos-qemu-smoke).
SMOKE_FLAG = pathlib.Path("/sys/firmware/qemu_fw_cfg/by_name/opt/couchliteos.smoke/raw")
NET = pathlib.Path("/sys/class/net")
STATE = pathlib.Path("/sys/power/state")
USB = pathlib.Path("/sys/bus/usb/devices")
# Written as root (by udev, see 75-couchliteos-wakeup.rules): reading a NIC's Wake-on-LAN
# support needs CAP_NET_ADMIN, which the launcher does not have.
WOL_DIR = pathlib.Path("/run/couchliteos-hardware/wake-on-lan")
ETHTOOL = "/usr/sbin/ethtool"
LOGIND_CAN_SUSPEND = (
    "busctl", "--system", "call", "org.freedesktop.login1", "/org/freedesktop/login1",
    "org.freedesktop.login1.Manager", "CanSuspend",
)

BLANK_CHOICES = (0, 2, 5, 10, 15, 30)  # minutes; 0 is off
SLEEP_CHOICES = (0, 15, 30, 60, 120)
DEFAULT_BLANK = 5
DEFAULT_SLEEP = 30
# The "is an application running?" question is only asked after this much idle
# time, and at most this often (it reads every application manifest).
APP_CHECK_AFTER = 30.0
APP_POLL_SECONDS = 5.0
# After a resume the first real key press is swallowed if it comes within this long: the user is
# pressing a button to wake the pad or the TV, not to choose something.
RESUME_SWALLOW_SECONDS = 10.0
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


def suspend_supported(logind: str | None, state: str | None) -> bool:
    """Can this machine suspend? Decided from logind's CanSuspend answer and /sys/power/state.

    "yes" and "challenge" both mean the hardware can ("challenge" only says the asking
    user would have to authenticate; the real suspend runs as root). "na" means it cannot.
    Anything else ("no", or no answer) describes the caller's rights or a missing bus, not
    the machine, so the kernel's own list of sleep states decides. The live image has no
    polkit, so the launcher's non-root CanSuspend call normally ends up here.
    """
    if logind in ("yes", "challenge"):
        return True
    if logind == "na":
        return False
    return bool({"mem", "freeze"} & set((state or "").split()))


def logind_can_suspend(run=subprocess.run) -> str | None:
    try:
        result = run(LOGIND_CAN_SUSPEND, capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    match = re.fullmatch(r's "(yes|no|na|challenge)"\s*', result.stdout or "")
    return match.group(1) if match else None


def can_suspend(run=subprocess.run, state_path: pathlib.Path = STATE) -> bool:
    try:
        state = state_path.read_text()
    except OSError:
        state = None
    return suspend_supported(logind_can_suspend(run), state)


def effective_settings(settings: Settings, suspend_ok: bool, wake_ok: bool = True) -> Settings:
    """What to act on. A sleep timeout saved on a PC that cannot suspend, or that has nothing
    able to wake it again (see wake_sources), is ignored here, not erased, so it applies again
    when the stick goes back to a PC where it works."""
    return settings if suspend_ok and wake_ok else replace(settings, sleep=0)


def wired_adapters(root: pathlib.Path = NET) -> list[tuple[str, str]]:
    """(interface, MAC) of each physical Ethernet adapter."""
    try:
        names = sorted(path.name for path in root.iterdir())
    except OSError:
        return []
    found = []
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
            found.append((name, address.upper()))
    return found


def wired_mac(root: pathlib.Path = NET) -> str | None:
    adapters = wired_adapters(root)
    return adapters[0][1] if adapters else None


def wol_magic_supported(ethtool_output: str) -> bool:
    """True when ethtool lists magic packet ('g') under "Supports Wake-on"."""
    match = re.search(r"^\s*Supports Wake-on:[ \t]*(\S+)", ethtool_output, re.MULTILINE)
    return bool(match) and "g" in match.group(1)


def _wol_file(state: pathlib.Path, mac: str) -> pathlib.Path:
    return state / mac.lower().replace(":", "-")


def probe_wake_on_lan(root: pathlib.Path = NET, state: pathlib.Path = WOL_DIR, run=subprocess.run) -> None:
    """Run as root: record, per adapter MAC, whether it can wake on a magic packet."""
    for name, mac in wired_adapters(root):
        try:
            result = run([ETHTOOL, name], capture_output=True, text=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode != 0:
            continue
        state.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".wol.", dir=state)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("magic\n" if wol_magic_supported(result.stdout) else "none\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, _wol_file(state, mac))


def wake_on_lan(root: pathlib.Path = NET, state: pathlib.Path = WOL_DIR) -> tuple[str, str | None]:
    """("supported", MAC) for the first wired adapter that can wake on a magic packet;
    otherwise "unsupported", "unknown" (not probed) or "none" (no wired adapter), with no MAC."""
    adapters = wired_adapters(root)
    if not adapters:
        return "none", None
    answers = []
    for _name, mac in adapters:
        try:
            answers.append((mac, _wol_file(state, mac).read_text().strip()))
        except OSError:
            pass
    for mac, answer in answers:
        if answer == "magic":
            return "supported", mac
    return ("unsupported", None) if answers else ("unknown", None)


def wake_sources(root: pathlib.Path = USB) -> list[str]:
    """USB Bluetooth adapters and keyboards whose wakeup is switched on (by the udev rule)."""
    try:
        names = sorted(path.name for path in root.iterdir())
    except OSError:
        return []
    found = set()

    def read(path: pathlib.Path) -> str:
        try:
            return path.read_text().strip().lower()
        except OSError:
            return ""

    for name in names:
        entry = root / name
        if ":" in name:
            klass, protocol = read(entry / "bInterfaceClass"), read(entry / "bInterfaceProtocol")
        else:
            klass, protocol = read(entry / "bDeviceClass"), ""
        kind = "BLUETOOTH ADAPTER" if klass == "e0" else "USB KEYBOARD" if (klass, protocol) == ("03", "01") else None
        if kind and read(root / name.split(":")[0] / "power" / "wakeup") == "enabled":
            found.add(kind)
    return [kind for kind in ("BLUETOOTH ADAPTER", "USB KEYBOARD") if kind in found]


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
    when idle long enough, and reports a resume from sleep (swallowing the first
    real key shortly after it).
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
        self.swallow_until = float("-inf")
        self.timer = IdleTimer(settings, clock())

    def apply(self, settings: Settings) -> None:
        self.timer.settings = settings
        self.timer.reset(self.clock())

    def keep_awake(self) -> None:
        """A screen that is busy on its own (an update downloading) counts as activity."""
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

    def _resume_wake(self, now: float, key: int) -> int:
        # The marker is usually seen on an idle tick, before anyone touches a button; the key that
        # comes later (the pad reconnecting, then A) must not act on the screen that was left open.
        # A key that arrives with the marker is itself the one swallowed.
        if key in self.passive_keys:
            self.swallow_until = now + RESUME_SWALLOW_SECONDS
        return self._wake(now)

    def filter(self, screen, key: int) -> int:
        now = self.clock()
        if self.resumed():
            return self._resume_wake(now, key)
        if not self.enabled():
            return key
        if key not in self.passive_keys and now < self.swallow_until:
            self.swallow_until = float("-inf")
            return self._wake(now)
        if key not in self.passive_keys or self.home_pending():
            return -1 if self.timer.activity(now) else key
        self._poll(screen)
        while self.timer.blanked:
            key = screen.getch()
            now = self.clock()
            if self.resumed():
                return self._resume_wake(now, key)
            if key not in self.passive_keys:
                return self._wake(now)
            if self.home_pending():
                self.clear_home()
                return self._wake(now)
            self._poll(screen)
        return -1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["wake-on-lan"]:
        probe_wake_on_lan()
        return 0
    print("usage: couchliteos_power.py wake-on-lan", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
