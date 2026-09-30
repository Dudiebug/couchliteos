#!/usr/bin/python3
"""Background check for a newer MoonlightOS release (GitHub releases, anonymous).

Nothing here may block or break the launcher: the check runs on a daemon
thread, every failure is swallowed, and the answer is cached in a small state
file so a release is asked for at most once a day.
"""

from __future__ import annotations

import configparser
import dataclasses
import json
import os
import pathlib
import re
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable, Iterable

API_URL = "https://api.github.com/repos/Dudiebug/moonlightos/releases/latest"
RELEASES_TEXT = "github.com/Dudiebug/moonlightos/releases"
STATE = pathlib.Path("/var/lib/moonlightos/update-check.ini")
# /etc/moonlightos-version is installed from the overlay; the source tree's
# VERSION file covers running the launcher from a checkout.
VERSION_FILES = (
    pathlib.Path("/etc/moonlightos-version"),
    pathlib.Path(__file__).resolve().parents[1] / "VERSION",
)
CHECK_SECONDS = 24 * 3600.0
# An attempt that failed (usually: the network was not up yet at boot) does not
# count as a check; it is retried after this long.
RETRY_SECONDS = 3600.0
POLL_SECONDS = 60.0
TIMEOUT = 5
MAX_BYTES = 512 * 1024
VERSION_RE = re.compile(
    r"^v?(\d{1,6})(?:\.(\d{1,6}))?(?:\.(\d{1,6}))?(?:-([0-9A-Za-z][0-9A-Za-z.-]{0,31}))?(?:\+[0-9A-Za-z.-]{1,32})?$"
)


class UpdateError(Exception):
    """The release information could not be obtained or understood."""


def parse_version(text: object) -> tuple | None:
    """Sortable key for a version like `0.1.13`, `v0.2.0-rc.1`; None if unparseable."""
    if not isinstance(text, str):
        return None
    match = VERSION_RE.match(text.strip())
    if not match:
        return None
    numbers = tuple(int(part or 0) for part in match.groups()[:3])
    if match.group(4) is None:
        return numbers, (1,)  # a release sorts after any pre-release of it
    # Semver precedence: numeric identifiers sort before alphanumeric ones.
    identifiers = tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part) for part in match.group(4).split(".")
    )
    return numbers, (0, identifiers)


def is_newer(candidate: object, current: object) -> bool:
    new, old = parse_version(candidate), parse_version(current)
    return new is not None and old is not None and new > old


def installed_version(paths: Iterable[pathlib.Path] = VERSION_FILES) -> str:
    for path in paths:
        try:
            value = path.read_text(encoding="ascii", errors="replace").strip()
        except OSError:
            continue
        if parse_version(value) is not None:
            return value
    return ""


@dataclasses.dataclass(frozen=True)
class State:
    enabled: bool = True
    latest: str = ""
    checked_at: float = 0.0  # last successful check
    attempted_at: float = 0.0  # last attempt, successful or not


def _number(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        return 0.0
    return number if 0.0 <= number < 1e12 else 0.0


def load_state(path: pathlib.Path = STATE) -> State:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(path.read_text(encoding="utf-8"))
        section = parser["updates"]
    except (OSError, UnicodeError, configparser.Error, KeyError):
        return State()
    enabled = section.get("enabled", "true").strip().lower()
    latest = section.get("latest", "").strip()
    return State(
        enabled=configparser.ConfigParser.BOOLEAN_STATES.get(enabled, True),
        latest=latest if parse_version(latest) is not None else "",
        checked_at=_number(section.get("checked_at", "0")),
        attempted_at=_number(section.get("attempted_at", "0")),
    )


def save_state(state: State, path: pathlib.Path = STATE) -> None:
    text = (
        "[updates]\n"
        f"enabled = {'true' if state.enabled else 'false'}\n"
        f"latest = {state.latest}\n"
        f"checked_at = {state.checked_at:.0f}\n"
        f"attempted_at = {state.attempted_at:.0f}\n"
    )
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o640)
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def due(state: State, now: float) -> bool:
    if not state.enabled:
        return False
    # A timestamp in the future (the clock was wrong when it was saved) is stale.
    if 0 <= now - state.checked_at < CHECK_SECONDS and state.checked_at:
        return False
    return not (state.attempted_at and 0 <= now - state.attempted_at < RETRY_SECONDS)


def fetch_latest(current: str, opener: Callable[..., object] = urllib.request.urlopen) -> str:
    """Return the latest release tag. Sends no identifiers beyond the version in the User-Agent."""
    request = urllib.request.Request(
        API_URL, headers={"User-Agent": f"MoonlightOS/{current or 'unknown'}", "Accept": "application/vnd.github+json"}
    )
    try:
        with opener(request, timeout=TIMEOUT) as response:
            body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise UpdateError("release answer is too large")
        tag = json.loads(body.decode("utf-8")).get("tag_name")
    except UpdateError:
        raise
    except (OSError, ValueError, AttributeError, UnicodeError) as error:
        raise UpdateError(str(error)) from error
    if parse_version(tag) is None:
        raise UpdateError("release tag is not a version")
    return tag.strip().removeprefix("v")


def notice(state: State, current: str) -> str:
    if state.enabled and is_newer(state.latest, current):
        return f"UPDATE AVAILABLE: {state.latest} — {RELEASES_TEXT}"
    return ""


class Checker:
    """Owns the saved state; `check_if_due` is meant to run off the UI thread."""

    def __init__(
        self,
        current: str | None = None,
        state_path: pathlib.Path = STATE,
        fetch: Callable[[str], str] = fetch_latest,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.current = installed_version() if current is None else current
        self.state_path = state_path
        self.fetch = fetch
        self.clock = clock
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._wake = threading.Event()
        self._state = load_state(state_path)

    @property
    def enabled(self) -> bool:
        return self._state.enabled

    def notice(self) -> str:
        with self._lock:
            return notice(self._state, self.current)

    def _update(self, **changes: object) -> None:
        with self._lock:
            self._state = dataclasses.replace(self._state, **changes)
        # Saving fsyncs, so it happens outside the lock the UI thread reads under.
        with self._save_lock:
            with self._lock:
                state = self._state
            try:
                save_state(state, self.state_path)
            except OSError:
                pass  # read-only or full disk: keep the in-memory state for this session

    def set_enabled(self, enabled: bool) -> None:
        self._update(enabled=enabled)
        self._wake.set()

    def check_if_due(self) -> None:
        now = self.clock()
        with self._lock:
            if not due(self._state, now):
                return
        try:
            latest = self.fetch(self.current)
        except Exception:  # never let a network or parsing problem reach the launcher
            self._update(attempted_at=now)
            return
        self._update(latest=latest, checked_at=now, attempted_at=now)

    def start(self) -> None:
        def loop() -> None:
            while True:
                self.check_if_due()
                self._wake.wait(POLL_SECONDS)
                self._wake.clear()

        threading.Thread(target=loop, name="update-check", daemon=True).start()
