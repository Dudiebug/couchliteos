"""Settings > SOFTWARE UPDATE: look for a newer release, install it, and watch it happen.

The launcher only asks. Installing is the root service's job: this screen touches
/run/couchliteos/update-install (couchliteos-update.path then starts couchliteos-update.service) and
shows what the service writes to /run/couchliteos/update-status.json. While the file is being
downloaded B asks the service to stop (update-cancel); once the install has started it cannot be
stopped safely, so B only says to wait.

The service saves the running system first (couchliteos_snapshot). This screen shows that saved
version, turns SAVE BEFORE UPDATE on or off (config.ini, the launcher's own file) and asks the root
service to delete it (/run/couchliteos/snapshot-delete, couchliteos-snapshot-delete.path) or to
restore it (/run/couchliteos/restore-request, couchliteos-restore-request.path: the service leaves a
request for couchliteos-restore.service and restarts the box, which restores before the launcher).

On the live USB stick the screen offers UPDATE THE INSTALLED SYSTEM when couchliteos-find-installs
found one on the disks (/run/couchliteos/installs.json): /run/couchliteos/disk-update starts
couchliteos-disk-update.service (`couchliteos-updater apply-disk --found`), and the same progress
screen follows it.

A live stick that keeps nothing offers SET UP STORAGE ON THIS STICK:
/run/couchliteos/persist-setup-request starts couchliteos-persist-setup.service, whose progress
(/run/couchliteos/persist-setup.json) shows on this screen.

A live stick with storage shows its own update slots (couchliteos_liveslot): RUNNING FROM STICK, the
updated system and the ISO it was made from, and ROLL BACK when a previous slot exists. ROLL BACK touches
/run/couchliteos/rollback-live, which asks the updater to make the previous slot the one the next boot starts.

Between releases (installed boxes): the last results of couchliteos-security-update and
couchliteos-app-update (/var/lib/couchliteos/{security,app}-update.json), INSTALL ... NOW (their
request files in /run/couchliteos), the [update] auto_security / auto_apps switches, and ROLL BACK
for an app running a downloaded version (/run/couchliteos/app-rollback holds its name).

Everything the screen needs from outside is injected, so the tests use fakes.
"""

from __future__ import annotations

import curses
import json
import os
import pathlib
import textwrap
import time
from typing import Any, Callable

import couchliteos_confirm as confirmation
import couchliteos_listview as listview
import couchliteos_liveslot as liveslot
import couchliteos_settings as settings
import couchliteos_snapshot as snapshot
import couchliteos_update as update

RUN = pathlib.Path(os.environ.get("COUCHLITEOS_RUN_DIR", "/run/couchliteos"))
STATE_DIR = pathlib.Path("/var/lib/couchliteos")
SECURITY_STATE, APP_STATE = "security-update.json", "app-update.json"
SECURITY_REQUEST, APP_REQUEST, ROLLBACK_REQUEST = "security-update-install", "app-update-install", "app-rollback"
UPDATE_NOW = "INSTALL SECURITY FIXES AND APP UPDATES NOW"
UPDATE_STARTED = "STARTED: THEY WAIT WHILE A GAME STREAMS. THE RESULTS SHOW HERE."
AUTO = {"auto_security": "AUTOMATIC SECURITY FIXES", "auto_apps": "AUTOMATIC APP UPDATES"}
ROLL_BACK = "ROLL BACK"
NOT_CHECKED = "NOT CHECKED YET"
STORAGE_REQUEST, STORAGE_STATUS = "persist-setup-request", "persist-setup.json"
SET_UP_STORAGE = "SET UP STORAGE ON THIS STICK"
STORAGE_QUESTION = (
    "KEEP SETTINGS, PAIRINGS AND APPS ON THIS STICK? THE FREE SPACE ON IT BECOMES A DATA AREA; "
    "THE ISO ON IT STAYS. RESTART WHEN IT IS READY."
)
STORAGE_STARTED = "SETTING UP STORAGE..."
MOUNTINFO = pathlib.Path("/proc/self/mountinfo")
STICK_RUNNING = "RUNNING FROM STICK"
STICK_ROLL_BACK = "ROLL BACK"  # the stick's own slots; the same words as the app rollback, checked first
ISO_VERSION_FILE = liveslot.ISO_VERSION_FILE  # on the storage partition, beside live-update/: the ISO's version
LIVE_ROLLBACK = "rollback-live"  # request file for the updater: the previous slot becomes the one GRUB starts


def has_persistence(mountinfo: pathlib.Path = MOUNTINFO) -> bool:
    """live-boot mounted a persistence area (the same test as couchliteos-persist-setup)."""
    try:
        text = mountinfo.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(" /run/live/persistence/" in line for line in text.splitlines())


def stick_mounts(mountinfo: pathlib.Path = MOUNTINFO) -> list[str]:
    """Where live-boot mounted the stick's storage or its live medium, in mount-table order."""
    try:
        lines = mountinfo.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    points = [row[4].replace("\\040", " ") for row in (line.split() for line in lines) if len(row) > 4]
    return [point for point in points if point == "/run/live/medium" or point.startswith("/run/live/persistence/")]


def iso_version(mount: pathlib.Path) -> str:
    """The version of the ISO the stick was made from, as the installer recorded it; "" when unknown."""
    try:
        value = (mount / ISO_VERSION_FILE).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""
    return value if update.parse_version(value) is not None else ""


def stick_state(mountinfo: pathlib.Path = MOUNTINFO, root: pathlib.Path = pathlib.Path("/")) -> dict[str, str]:
    """The stick's own update slots and the ISO they came from; {} when it has no slots at all.

    Reads only: nothing is mounted here, the mount table just says where live-boot mounted the storage.
    """
    for point in stick_mounts(mountinfo):
        mount = root / point.lstrip("/")
        base = mount / liveslot.SLOT_DIR
        if (base / liveslot.CURRENT).exists() or (base / liveslot.PREVIOUS).exists():
            slots = liveslot.slot_state(base)
            return {"current": slots[liveslot.CURRENT], "previous": slots[liveslot.PREVIOUS], "iso": iso_version(mount)}
    return {}


REQUEST, CANCEL, STATUS = "update-install", "update-cancel", "update-status.json"
DELETE_REQUEST, RESTORE_REQUEST = "snapshot-delete", "restore-request"
DISK_REQUEST, INSTALLS = "disk-update", "installs.json"
UPDATE_DISK = "UPDATE THE INSTALLED SYSTEM (KEEPS PAIRINGS AND SETTINGS)"
DISK_DONE = "UPDATE DONE. REMOVE THE USB STICK, THEN RESTART: REBOOT OR POWER > RESTART ON THE HOME SCREEN."
TITLE = "SOFTWARE UPDATE"
CHECK, INSTALL, BACK = "CHECK FOR UPDATES", "INSTALL UPDATE", "BACK"
HINT = "A / CROSS SELECTS  ·  B / CIRCLE GOES BACK"
SAVE_ON, SAVE_OFF, DELETE_SAVED = "SAVE BEFORE UPDATE: ON", "SAVE BEFORE UPDATE: OFF", "DELETE SAVED VERSION"
RESTORE = "RESTORE PREVIOUS VERSION"
CHANNEL = "UPDATE CHANNEL"
BETA_TEXT = "BETA: TEST VERSIONS ARE OFFERED TOO. THEY CAN HAVE BUGS."
NO_SAVED = "SAVED VERSION: NONE"
DELETING = "DELETING THE SAVED VERSION..."
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
NOT_RESTARTED = "THE BOX DID NOT RESTART: CHOOSE REBOOT OR POWER > RESTART ON THE HOME SCREEN"
FAILED = "UPDATE FAILED"
PRESS_A = "PRESS A / CROSS OR ENTER"
CHECK_TIMEOUT = 10     # seconds GitHub gets to answer the check
POLL_MS = 500          # how often the progress screen looks at the status file
START_WAIT = 15        # seconds without any status before the service counts as not started
RESTART_WAIT = 90      # seconds in "restarting" before the screen admits the box did not restart
NORMAL_MS = 1000       # the launcher's usual input timeout, restored afterwards
ENTER_KEYS = (curses.KEY_ENTER, 10, 13)
ESC = 27
PHASE_TEXT = {
    "checking": "CHECKING FOR UPDATES...", "downloading": "DOWNLOADING...", "verifying": "CHECKING THE DOWNLOAD...",
    "saving": "SAVING THE CURRENT VERSION...", "installing": "INSTALLING...", "restarting": "RESTARTING...",
}
PHASE_HINT = {
    "downloading": "B / CIRCLE CANCELS THE DOWNLOAD", "saving": "KEEP THE BOX PLUGGED IN",
    "installing": "KEEP THE BOX PLUGGED IN",
}


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
        record: Callable[[str], None] = lambda _version: None,
        saved: Callable[[], snapshot.Snapshot | None] = snapshot.info,
        save_first: Callable[[], bool] = snapshot.enabled,
        set_save_first: Callable[[bool], None] = snapshot.set_enabled,
        hand_off: Callable[[str], None] | None = None,
        installs: Callable[[], list[dict[str, str]]] | None = None,
        state_dir: pathlib.Path = STATE_DIR, config: pathlib.Path = settings.CONFIG,
        persistent: Callable[[], bool] = has_persistence,
        stick: Callable[[], dict[str, str]] = stick_state,
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
        self.record = record  # tells the Home screen's update notice what this check found
        self.saved = saved
        self.save_first = save_first
        self.set_save_first = set_save_first
        # The TV interface runs this screen on its own (couchliteos-launcher --screen) and draws the
        # progress itself: once the install is asked for, `hand_off(version)` and the screen closes.
        self.hand_off = hand_off
        self.handed_off = False
        self.installs = installs or (lambda: update.read_installs(self.run_dir / INSTALLS))
        self.state_dir = pathlib.Path(state_dir)
        self.config = pathlib.Path(config)
        self.persistent = persistent
        self.stick = stick
        self.release: update.Release | None = None
        self.asset: update.Asset | None = None
        self.result = ""
        self.selected = 0
        self.heading = "UPDATING TO COUCHLITEOS"  # the progress screen's title, before the version

    # -- the main screen -----------------------------------------------------------------------

    def rows(self) -> list[str]:
        if self.live:
            storage = [] if self.persistent() else [SET_UP_STORAGE]
            return ([UPDATE_DISK] if self.found() else []) + storage + self.stick_rows() + [BACK]
        rows = ([INSTALL] if self.release else []) + [CHECK, SAVE_ON if self._save_first() else SAVE_OFF]
        rows += [self.channel_row()]
        saved = self._saved()
        rows += [UPDATE_NOW] + self.auto_rows() + self.rollback_rows()
        return rows + (self.restore_rows(saved) if saved else []) + [BACK]

    def channel_row(self) -> str:
        return f"{CHANNEL}: {update.update_channel(self.current, self.config).upper()}"

    def toggle_channel(self) -> None:
        new = "stable" if update.update_channel(self.current, self.config) == "beta" else "beta"
        try:
            settings.update_section("update", {"channel": new}, self.config)
        except OSError:
            self.result = "COULD NOT SAVE THE SETTING"
            return
        self.release = self.asset = None  # found on the other channel: CHECK again
        self.result = BETA_TEXT if new == "beta" else ""

    # -- the stick's own update slots ------------------------------------------------------------

    def stick_info(self) -> dict[str, str]:
        try:
            return self.stick()
        except Exception:  # noqa: BLE001 - a stick that cannot be read is shown as one with no slots
            return {}

    def stick_rows(self) -> list[str]:
        """ROLL BACK only with storage on the stick and a previous slot to go back to."""
        return [STICK_ROLL_BACK] if self.persistent() and self.stick_info().get("previous") else []

    def stick_lines(self) -> list[str]:
        info = self.stick_info()
        if not info.get("current"):
            return [STICK_RUNNING, f"ISO {self.current or 'UNKNOWN'}"]
        return [STICK_RUNNING, f"UPDATED SYSTEM {info['current']}", f"ISO {info.get('iso') or 'UNKNOWN'}"]

    def roll_back_live(self) -> None:
        previous = self.stick_info().get("previous", "")
        if not previous:
            return
        question = (
            f"GO BACK TO COUCHLITEOS {previous}? THE UPDATED SYSTEM STAYS ON THE STICK AS THE PREVIOUS ONE. "
            "RESTART THE BOX TO FINISH."
        )
        if not self.confirm(self.screen, question):
            return
        try:
            (self.run_dir / LIVE_ROLLBACK).touch()
        except OSError as error:
            self.result = f"COULD NOT START THE ROLL BACK: {error.strerror or 'ERROR'}".upper()
            return
        self.result = f"ROLLING BACK TO COUCHLITEOS {previous}. RESTART THE BOX TO FINISH."

    # -- between releases ------------------------------------------------------------------------

    def auto_rows(self) -> list[str]:
        values = settings.read_section("update", self.config)
        return [f"{label}: {'ON' if settings.get_bool(values, key, True) else 'OFF'}" for key, label in AUTO.items()]

    def downloaded_apps(self) -> list[dict]:
        """Apps the last app-update run found on a downloaded version: those can roll back."""
        apps = read_status(self.state_dir / APP_STATE).get("apps")
        return [row for row in apps if isinstance(row, dict) and row.get("current")
                and row.get("active") == row.get("current") and isinstance(row.get("name"), str)
                ] if isinstance(apps, list) else []

    def rollback_rows(self) -> list[str]:
        return [f"{ROLL_BACK} {row['name'].upper()} {row['current']}" for row in self.downloaded_apps()]

    def between_lines(self) -> list[str]:
        lines = []
        for label, name in (("SECURITY FIXES", SECURITY_STATE), ("APPS", APP_STATE)):
            message = read_status(self.state_dir / name).get("message")
            lines.append(f"{label}: {message if isinstance(message, str) and message else NOT_CHECKED}")
        return lines

    def storage_line(self) -> str:
        message = read_status(self.run_dir / STORAGE_STATUS).get("message")
        return f"STORAGE: {message}" if isinstance(message, str) and message else ""

    def set_up_storage(self) -> None:
        if not self.confirm(self.screen, STORAGE_QUESTION):
            return
        try:
            (self.run_dir / STORAGE_REQUEST).touch()
        except OSError:
            self.result = NOT_STARTED
            return
        self.result = STORAGE_STARTED

    def update_now(self) -> None:
        try:
            for name in (SECURITY_REQUEST, APP_REQUEST):
                (self.run_dir / name).touch()
        except OSError:
            self.result = NOT_STARTED
            return
        self.result = UPDATE_STARTED

    def toggle_auto(self, choice: str) -> None:
        key = next(key for key, label in AUTO.items() if choice.startswith(label))
        try:
            settings.update_section("update", {key: "off" if choice.endswith("ON") else "on"}, self.config)
        except OSError:
            self.result = "COULD NOT SAVE THE SETTING"

    def roll_back(self, choice: str) -> None:
        row = next(row for row in self.downloaded_apps() if choice.startswith(f"{ROLL_BACK} {row['name'].upper()} "))
        name = row["name"].upper()
        if not self.confirm(self.screen, f"GO BACK FROM {name} {row['current']} TO THE VERSION BEFORE IT?"):
            return
        try:
            (self.run_dir / ROLLBACK_REQUEST).write_text(row["name"] + "\n", encoding="ascii")
        except OSError:
            self.result = NOT_STARTED
            return
        self.result = f"ROLLING BACK {name}"

    @staticmethod
    def restore_rows(saved: snapshot.Snapshot) -> list[str]:
        return [DELETE_SAVED, f"{RESTORE} ({saved.version}, SAVED {saved.saved_on()})"]

    def found(self) -> dict[str, str] | None:
        """The one installed system on the disks (live USB), or None (none, several, unreadable)."""
        try:
            installs = self.installs()
        except Exception:  # noqa: BLE001
            return None
        return installs[0] if len(installs) == 1 else None

    def found_line(self, found: dict[str, str]) -> str:
        profile = found.get("profile", "").upper()
        return f"INSTALLED SYSTEM: {found.get('version') or 'UNKNOWN'}" + (f" ({profile})" if profile else "")

    def _saved(self) -> snapshot.Snapshot | None:
        try:
            return self.saved()
        except Exception:  # noqa: BLE001 - an unreadable snapshot is shown as none
            return None

    def _save_first(self) -> bool:
        try:
            return self.save_first()
        except Exception:  # noqa: BLE001
            return True

    def saved_line(self) -> str:
        saved = self._saved()
        return f"SAVED VERSION: {saved.describe()}" if saved else NO_SAVED

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
        lines = [(self.box_line(), 0)] + ([] if self.live else [(self.saved_line(), 0)])
        lines += [] if self.live else [(text, 0) for text in self.between_lines()]
        storage = self.storage_line() if self.live else ""
        lines += [(storage, curses.A_BOLD)] if storage else []
        lines += [("", 0)]
        found = self.found() if self.live else None
        if self.live and self.persistent():
            lines += [(text, 0) for text in self.stick_lines()] + [("", 0)]
        if found:
            lines += [(self.found_line(found), 0)]
        elif self.live:
            lines += [(text, 0) for text in textwrap.wrap(LIVE_TEXT, wrap)] + [("", 0), (update.RELEASES_TEXT, 0)]
        lines += [(text, curses.A_BOLD) for text in textwrap.wrap(self.result, wrap)] if self.result else []
        widest = max([len(text) for text, _attr in lines] + [len(row) + 3 for row in self.rows()])  # ">  " rows
        left = max(2, (width - widest) // 2)
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
                elif choice.startswith(CHANNEL):
                    self.toggle_channel()
                    self.selected = self.rows().index(self.channel_row())
                elif choice in (SAVE_ON, SAVE_OFF):
                    self.toggle_save_first(choice == SAVE_OFF)
                elif choice == DELETE_SAVED:
                    self.delete_saved()
                elif choice.startswith(RESTORE):
                    self.restore_saved()
                elif choice == UPDATE_DISK:
                    self.update_disk()
                elif choice == SET_UP_STORAGE:
                    self.set_up_storage()
                elif choice == UPDATE_NOW:
                    self.update_now()
                elif choice.startswith(tuple(AUTO.values())):
                    self.toggle_auto(choice)
                elif choice == STICK_ROLL_BACK:
                    self.roll_back_live()
                elif choice.startswith(ROLL_BACK):
                    self.roll_back(choice)
                else:
                    self.install()
                    if self.handed_off:
                        return

    # -- the saved version -----------------------------------------------------------------------

    def toggle_save_first(self, on: bool) -> None:
        try:
            self.set_save_first(on)
        except OSError:
            self.result = "COULD NOT SAVE THE SETTING"
            return
        self.result = "" if on else "UPDATES NOW INSTALL WITHOUT SAVING THE CURRENT VERSION FIRST"

    def delete_saved(self) -> None:
        saved = self._saved()
        if saved is None:
            return
        question = (
            f"DELETE THE SAVED VERSION ({saved.version})? THE BOX CAN THEN NOT GO BACK TO IT. "
            "THE NEXT UPDATE SAVES A NEW ONE."
        )
        if not self.confirm(self.screen, question):
            return
        try:
            (self.run_dir / DELETE_REQUEST).touch()
        except OSError as error:
            self.result = f"COULD NOT DELETE THE SAVED VERSION: {error.strerror or 'ERROR'}".upper()
            return
        self.result = DELETING
        self.selected = 0

    def restore_saved(self) -> None:
        saved = self._saved()
        if saved is None:
            return
        if self.apps_running():
            self.result = CLOSE_APPS
            return
        question = (
            f"RESTORE COUCHLITEOS {saved.version}, SAVED {saved.saved_on()}? EVERYTHING CHANGED SINCE THEN IS LOST: "
            "SETTINGS, PAIRINGS AND APPS ADDED LATER. THE BOX RESTARTS AND RESTORES IT. KEEP IT PLUGGED IN."
        )
        if not self.confirm(self.screen, question):
            return
        try:
            (self.run_dir / STATUS).unlink(missing_ok=True)
            (self.run_dir / RESTORE_REQUEST).touch()
        except OSError as error:
            self.result = f"COULD NOT START THE RESTORE: {error.strerror or 'ERROR'}".upper()
            return
        self.heading = "RESTORING COUCHLITEOS"
        try:
            self.result = self.progress(saved.version)
        finally:
            self.heading = "UPDATING TO COUCHLITEOS"
        self.selected = 0

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
        try:
            self.record(release.version)
        except Exception:  # noqa: BLE001 - the Home notice is a courtesy; this screen still answers
            pass
        if not update.is_newer(release.version, self.current):
            self.result = NEWEST
            return
        try:
            asset = update.pick_iso(release, self.profile.get("ISO_SUFFIX", ""))
            update.sums_asset(release)  # the service verifies the download against it
        except update.UpdateError:
            self.result = update.not_ready(release, self.profile) or NO_FILE
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
        if self.hand_off is not None:
            try:
                self.hand_off(self.release.version)
                self.handed_off = True
                return
            except OSError:
                pass  # nobody else will watch it: follow it here
        self.result = self.progress(self.release.version)
        if self.result == NEWEST:  # the service found nothing newer after all
            self.release = self.asset = None

    def update_disk(self) -> None:
        """Live USB: update the system installed on the disk from this stick."""
        found = self.found()
        if found is None:
            return
        if self.apps_running():
            self.result = CLOSE_APPS
            return
        question = (
            f"UPDATE THE {self.found_line(found)} TO COUCHLITEOS {self.current}? IT SAVES THE INSTALLED SYSTEM "
            "FIRST, THEN INSTALLS THIS VERSION. PAIRINGS, WI-FI, BLUETOOTH AND SETTINGS ARE KEPT. "
            "KEEP THE BOX PLUGGED IN."
        )
        if not self.confirm(self.screen, question):
            return
        try:
            (self.run_dir / STATUS).unlink(missing_ok=True)
            (self.run_dir / DISK_REQUEST).touch()
        except OSError as error:
            self.result = f"COULD NOT START THE UPDATE: {error.strerror or 'ERROR'}".upper()
            return
        self.result = self.progress(self.current)

    # -- watching the service --------------------------------------------------------------------

    def draw_progress(
        self, version: str, text: str, percent: int | None, frame: int, hint: str, with_bar: bool = True
    ) -> None:
        height, width, row = self.frame()
        _centered(self.screen, row, f"{self.heading} {version}", curses.A_BOLD)
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
                if phase in ("downloading", "saving", "installing", "checking", "verifying"):
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
                if phase == "updated":  # apply-disk: the stick has to come out before the restart
                    return self.failure(shown, _text(state.get("message"), DISK_DONE))
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
                    elif phase in ("saving", "installing"):
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
