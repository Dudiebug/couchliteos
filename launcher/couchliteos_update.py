#!/usr/bin/python3
"""Background check for a newer CouchLiteOS release (GitHub releases, anonymous).

Nothing here may block or break the launcher: the check runs on a daemon
thread, every failure is swallowed, and the answer is cached in a small state
file. A release is asked for once at every start of the launcher (every boot), then at most
every few hours while the box stays on.
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
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable

# The project is moving from Dudiebug/moonlightos to Dudiebug/couchliteos on GitHub. Until the
# repository is renamed the new name is a 404, so the old one is asked next; after the rename GitHub
# redirects the old name, so either order keeps working.
API_URLS = (
    "https://api.github.com/repos/Dudiebug/couchliteos/releases/latest",
    "https://api.github.com/repos/Dudiebug/moonlightos/releases/latest",  # rename:keep
)
RELEASES_TEXT = "github.com/Dudiebug/couchliteos/releases"
PROFILE_FILE = pathlib.Path("/usr/share/couchliteos/profile.conf")
LIVE_MEDIUM = pathlib.Path("/run/live/medium")
CMDLINE = pathlib.Path("/proc/cmdline")
SUMS_NAME = "SHA256SUMS"
STATE = pathlib.Path(os.environ.get("COUCHLITEOS_STATE_DIR", "/var/lib/couchliteos")) / "update-check.ini"
# config.ini [update] paused_until: written by a restore (couchliteos_updater) so the check does not
# offer the update that was just undone. Settings > SOFTWARE UPDATE still checks when asked.
CONFIG = STATE.parent / "config.ini"
PAUSE_SECONDS = 7 * 86400.0
# Live USB only: the installed systems couchliteos-find-installs.service found on the disks (root writes it).
INSTALLS = pathlib.Path(os.environ.get("COUCHLITEOS_RUN_DIR", "/run/couchliteos")) / "installs.json"
ROUTE4 = pathlib.Path("/proc/net/route")
ROUTE6 = pathlib.Path("/proc/net/ipv6_route")
RTF_UP = 0x1
RTF_REJECT = 0x200
# /etc/couchliteos-version is installed from the overlay; the source tree's
# VERSION file covers running the launcher from a checkout.
VERSION_FILES = (
    pathlib.Path("/etc/couchliteos-version"),
    pathlib.Path(__file__).resolve().parents[1] / "VERSION",
)
# While the box stays on: how long a successful check is good for.
CHECK_SECONDS = 6 * 3600.0
# An attempt that failed (usually: the network was not up yet at boot) does not
# count as a check; it is retried after this long.
RETRY_SECONDS = 600.0
# The check at start-up ignores the last check, but a launcher that keeps restarting
# must not ask GitHub every few seconds.
START_GAP_SECONDS = 60.0
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
    prompted: str = ""  # the release the owner was last offered on the Home screen
    no_file: str = ""  # the latest release, when it has no ISO for this box (yet): Home does not offer it


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
    prompted = section.get("prompted", "").strip()
    no_file = section.get("no_file", "").strip()
    return State(
        enabled=configparser.ConfigParser.BOOLEAN_STATES.get(enabled, True),
        latest=latest if parse_version(latest) is not None else "",
        checked_at=_number(section.get("checked_at", "0")),
        attempted_at=_number(section.get("attempted_at", "0")),
        prompted=prompted if parse_version(prompted) is not None else "",
        no_file=no_file if parse_version(no_file) is not None else "",
    )


def save_state(state: State, path: pathlib.Path = STATE) -> None:
    text = (
        "[updates]\n"
        f"enabled = {'true' if state.enabled else 'false'}\n"
        f"latest = {state.latest}\n"
        f"checked_at = {state.checked_at:.0f}\n"
        f"attempted_at = {state.attempted_at:.0f}\n"
        f"prompted = {state.prompted}\n"
        f"no_file = {state.no_file}\n"
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


def has_default_route(route4: pathlib.Path = ROUTE4, route6: pathlib.Path = ROUTE6) -> bool:
    """True when this machine has a usable default route (IPv4 or IPv6).

    The update check is skipped entirely without one, so a PC that is offline,
    or only on a link-local network, never even tries. The stick moves between
    machines, so this is read at run time on every attempt.
    """
    try:
        for line in route4.read_text(encoding="ascii", errors="replace").splitlines()[1:]:
            # Iface Destination Gateway Flags RefCnt Use Metric Mask ...
            fields = line.split()
            if (
                len(fields) >= 8 and fields[0] != "lo" and int(fields[1], 16) == 0 and int(fields[7], 16) == 0
                and int(fields[3], 16) & RTF_UP and not int(fields[3], 16) & RTF_REJECT
            ):
                return True
    except (OSError, ValueError):
        pass
    try:
        for line in route6.read_text(encoding="ascii", errors="replace").splitlines():
            # dest plen src slen next_hop metric refcnt use flags iface
            fields = line.split()
            if (
                len(fields) >= 10 and fields[-1] != "lo" and int(fields[0], 16) == 0 and int(fields[1], 16) == 0
                and int(fields[8], 16) & RTF_UP and not int(fields[8], 16) & RTF_REJECT
            ):
                return True
    except (OSError, ValueError):
        pass
    return False


def paused_until(path: pathlib.Path = CONFIG) -> float:
    """config.ini [update] paused_until (seconds since the epoch), 0 when there is none."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(path.read_text(encoding="utf-8", errors="replace"))
        return _number(parser.get("update", "paused_until", fallback="0").strip())
    except (OSError, configparser.Error):
        return 0.0


def due(state: State, now: float, *, start: bool = False, retry: bool = False, paused: float = 0.0) -> bool:
    """True when a release should be asked for now.

    `paused`: no check before this time (after a restore). One further away than PAUSE_SECONDS
    was written by a wrong clock and is ignored.

    `start`: no check has succeeded since the launcher started, so the last check (maybe from before
    the reboot) does not count. `retry`: an attempt already failed in this run."""
    if not state.enabled or 0 < paused - now <= PAUSE_SECONDS:
        return False
    # A timestamp in the future (the clock was wrong when it was saved) is stale.
    if not start and state.checked_at and 0 <= now - state.checked_at < CHECK_SECONDS:
        return False
    gap = RETRY_SECONDS if retry or not start else START_GAP_SECONDS
    return not (state.attempted_at and 0 <= now - state.attempted_at < gap)


@dataclasses.dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int


@dataclasses.dataclass(frozen=True)
class Release:
    version: str
    assets: tuple[Asset, ...] = ()


def _release_json(current: str, opener: Callable[..., object], timeout: float) -> dict:
    """The newest release's JSON. Sends no identifiers beyond the version in the User-Agent."""
    headers = {"User-Agent": f"CouchLiteOS/{current or 'unknown'}", "Accept": "application/vnd.github+json"}
    try:
        for url in API_URLS:
            try:
                with opener(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
                    body = response.read(MAX_BYTES + 1)
                break
            except urllib.error.HTTPError as error:
                if error.code != 404 or url == API_URLS[-1]:
                    raise
        if len(body) > MAX_BYTES:
            raise UpdateError("release answer is too large")
        data = json.loads(body.decode("utf-8"))
        if not isinstance(data, dict):
            raise UpdateError("release answer is not an object")
    except UpdateError:
        raise
    except (OSError, ValueError, AttributeError, UnicodeError) as error:
        raise UpdateError(str(error)) from error
    if parse_version(data.get("tag_name")) is None:
        raise UpdateError("release tag is not a version")
    return data


def fetch_latest(
    current: str, opener: Callable[..., object] = urllib.request.urlopen, timeout: float = TIMEOUT
) -> str:
    """Return the latest release tag without its `v`."""
    return _release_json(current, opener, timeout)["tag_name"].strip().removeprefix("v")


def fetch_release(
    current: str, opener: Callable[..., object] = urllib.request.urlopen, timeout: float = TIMEOUT
) -> Release:
    """The latest release with its downloadable files (malformed entries are ignored)."""
    data = _release_json(current, opener, timeout)
    assets = []
    for entry in data.get("assets") or []:
        if not isinstance(entry, dict):
            continue
        name, url, size = entry.get("name"), entry.get("browser_download_url"), entry.get("size")
        if isinstance(name, str) and isinstance(url, str) and isinstance(size, int) and size >= 0:
            assets.append(Asset(name, url, size))
    return Release(data["tag_name"].strip().removeprefix("v"), tuple(assets))


def iso_name(version: str, suffix: str) -> str:
    """The release asset name build.sh gives this version and profile (`ISO_SUFFIX`)."""
    return f"couchliteos-{version}-{suffix + '-' if suffix else ''}amd64.iso"


def pick_iso(release: Release, suffix: str) -> Asset:
    wanted = iso_name(release.version, suffix)
    for asset in release.assets:
        if asset.name == wanted:
            return asset
    raise UpdateError(f"release {release.version} has no {wanted}")


def not_ready(release: Release, profile: dict[str, str]) -> str:
    """The message when this release profile's ISO is missing but the general one is there.

    The NVIDIA ISO is published some time after the general one; legacy profiles
    (RELEASE=0) never get one, so they keep the plain "no file" message."""
    suffix = profile.get("ISO_SUFFIX", "")
    if not suffix or profile.get("RELEASE") != "1":
        return ""
    names = {asset.name for asset in release.assets}
    if iso_name(release.version, suffix) in names or iso_name(release.version, "") not in names:
        return ""
    return f"THE {suffix.upper()} VERSION OF {release.version} IS NOT READY YET. TRY AGAIN LATER."


def sums_asset(release: Release) -> Asset:
    for asset in release.assets:
        if asset.name == SUMS_NAME:
            return asset
    raise UpdateError(f"release {release.version} has no {SUMS_NAME}")


def read_profile(path: pathlib.Path = PROFILE_FILE) -> dict[str, str]:
    """KEY=value lines of the profile this image was built from (shell quoting stripped)."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return values
    for line in lines:
        match = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line.strip())
        if match:
            values[match.group(1)] = match.group(2).strip().strip("\"'")
    return values


def is_live(medium: pathlib.Path = LIVE_MEDIUM, cmdline: pathlib.Path = CMDLINE) -> bool:
    """True when booted from the live ISO or stick: the running system then is the image."""
    if medium.exists():
        return True
    try:
        return "boot=live" in cmdline.read_text(encoding="ascii", errors="replace").split()
    except OSError:
        return False


def read_installs(path: pathlib.Path = INSTALLS) -> list[dict[str, str]]:
    """[{"device", "version", "profile", "disk"}] (couchliteos_updater.write_installs); [] when unknown."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    keys = ("device", "version", "profile", "disk")
    if not isinstance(data, list):
        return []
    return [
        {key: item[key] for key in keys} for item in data
        if isinstance(item, dict) and all(isinstance(item.get(key), str) for key in keys)
    ]


def disk_notice(current: str, path: pathlib.Path = INSTALLS) -> str:
    """Home on the live USB: the one installed system found is older than this stick."""
    installs = read_installs(path)
    if len(installs) != 1 or not (parse_version(installs[0]["version"]) is None
                                  or is_newer(current, installs[0]["version"])):
        return ""
    return f"INSTALLED SYSTEM {installs[0]['version']} FOUND: UPDATE IT IN SETTINGS > SOFTWARE UPDATE"


def notice(state: State, current: str, installed: bool = False) -> str:
    if state.enabled and is_newer(state.latest, current):
        if installed:  # a box on its disk updates itself
            return f"COUCHLITEOS {state.latest} IS AVAILABLE: SETTINGS > SOFTWARE UPDATE"
        return f"UPDATE AVAILABLE: {state.latest} — {RELEASES_TEXT}"
    return ""


class Checker:
    """Owns the saved state; `check_if_due` is meant to run off the UI thread."""

    def __init__(
        self,
        current: str | None = None,
        state_path: pathlib.Path = STATE,
        fetch: Callable[[str], Release | str] = fetch_release,
        clock: Callable[[], float] = time.time,
        online: Callable[[], bool] = has_default_route,
        live: Callable[[], bool] = is_live,
        profile: dict[str, str] | None = None,
        config_path: pathlib.Path = CONFIG,
    ) -> None:
        self.current = installed_version() if current is None else current
        self.paused_until = paused_until(config_path)  # a restore happens before the launcher starts
        self.suffix = (read_profile() if profile is None else profile).get("ISO_SUFFIX", "")
        self.state_path = state_path
        self.fetch = fetch
        self.clock = clock
        self.online = online
        self.installed = not live()  # fixed for the whole boot
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._wake = threading.Event()
        self._state = load_state(state_path)
        self._checked = False  # a check has succeeded since this launcher started
        self._failed = False  # an attempt has failed since then

    @property
    def enabled(self) -> bool:
        return self._state.enabled

    def notice(self) -> str:
        if not self.installed:  # the live USB offers to update the system on the disk first
            found = disk_notice(self.current)
            if found:
                return found
        with self._lock:
            return notice(self._state, self.current, self.installed)

    def available(self) -> str:
        """The newer release the last check found ("" when none, or checking is off)."""
        with self._lock:
            state = self._state
        return state.latest if state.enabled and is_newer(state.latest, self.current) else ""

    def to_offer(self) -> str:
        """The newer release to offer the owner now: once per release, and never on a live stick."""
        latest = self.available()
        with self._lock:
            prompted, no_file = self._state.prompted, self._state.no_file
        # A release without an ISO for this box (the NVIDIA one comes later) cannot be installed yet.
        return latest if latest and self.installed and latest not in (prompted, no_file) else ""

    def mark_offered(self, version: str) -> None:
        self._update(prompted=version)

    def record(self, latest: str) -> None:
        """A check made elsewhere (SETTINGS > SOFTWARE UPDATE) found `latest`: Home shows it too."""
        if parse_version(latest) is None:
            return
        now = self.clock()
        # The owner is looking at the result right now: Home need not ask about it again.
        self._update(latest=latest, checked_at=now, attempted_at=now, prompted=latest)
        self._checked, self._failed = True, False

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
            if not due(self._state, now, start=not self._checked, retry=self._failed, paused=self.paused_until):
                return
        # Runs before anything is recorded: a boot without a network must not use
        # up the start-up check, so the next poll after the network comes up retries.
        if not self.online():
            return
        try:
            found = self.fetch(self.current)
        except Exception:  # never let a network or parsing problem reach the launcher
            self._failed = True
            self._update(attempted_at=now)
            return
        latest, no_file = found, ""  # a bare version (tests) counts as having a file
        if isinstance(found, Release):
            latest = found.version
            try:
                pick_iso(found, self.suffix)
            except UpdateError:
                no_file = latest
        self._checked, self._failed = True, False
        self._update(latest=latest, checked_at=now, attempted_at=now, no_file=no_file)

    def start(self) -> None:
        def loop() -> None:
            while True:
                self.check_if_due()
                self._wake.wait(POLL_SECONDS)
                self._wake.clear()

        threading.Thread(target=loop, name="update-check", daemon=True).start()
