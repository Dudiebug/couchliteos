"""The TV interface's Settings, power menu, What's New and update progress, as plain Python.

couchliteos-tv.py (GTK) draws these; everything here can be tested without GTK:

    SettingsModel   Settings as two panes: the categories of the classic SETTINGS_MENU on
                    the left, the focused one's current value and what it holds on the right
    PowerModel      SLEEP / RESTART / SHUT DOWN, with the classic launcher's questions
    UpdateProgress  the SOFTWARE UPDATE progress screen, read from the service's status file
                    with softwareupdate's phase texts
    whats_new       the lines of the one-time What's New screen

Every Settings entry the TV interface does not draw itself opens the classic curses screen in
a foot window of its own: `couchliteos-launcher --screen <name>` runs that one screen and exits
(SCREENS lists the names; the classic launcher's SCREENS maps them to its Settings screens).
Cage shows the newest window on top, and the TV interface reads its rows again when it exits.
The first-run wizard runs the same way (`--screen setup`) before the home screen.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import time
from collections.abc import Callable, Mapping

import couchliteos_background as background
import couchliteos_controls as controls
import couchliteos_display as display
import couchliteos_power as power
import couchliteos_softwareupdate as softwareupdate
import couchliteos_stream as stream
import couchliteos_theme as theme
import couchliteos_tvlayout as tvlayout
import couchliteos_update as update
import couchliteos_whatsnew as whatsnew

RUN = pathlib.Path(os.environ.get("COUCHLITEOS_RUN_DIR", "/run/couchliteos"))
STATE = pathlib.Path(os.environ.get("COUCHLITEOS_STATE_DIR", "/var/lib/couchliteos"))
# The child window: the foot wrapper (font size for the screen, the theme's colours) running the
# classic launcher on one screen. Its title is not the launcher's: Guide brings the TV window back.
FOOT = os.environ.get("COUCHLITEOS_FOOT", "/usr/libexec/couchliteos-foot")
LAUNCHER = os.environ.get("COUCHLITEOS_LAUNCHER", "/usr/libexec/couchliteos-launcher")
CHILD_TITLE = "CouchLiteOS Settings"
# Written by the SOFTWARE UPDATE screen of a child once the install was asked for: the version.
# The child then exits and the TV interface shows the progress (UpdateProgress).
UPDATE_WATCH = "update-watch"
REOPEN_SETUP, REOPEN_DISPLAY = "reopen-setup", "reopen-display"  # the classic launcher's restart markers
SWITCH_INTERFACE = "switch-interface"  # APPEARANCE > INTERFACE changed: the service starts again

# The curses screens `couchliteos-launcher --screen` runs (couchliteos-launcher.py SCREENS).
SCREENS = (
    "display", "appearance", "audio", "bluetooth", "controllers", "network", "sleep", "applications",
    "remote-desktop", "streaming", "tv-control", "software-update", "controls", "setup-wizard",
    "support-file",
    "setup",  # the first-run wizard
    "connect",  # start the Remote Desktop connection --app names: certificate and password first
)

SCREEN, APP, VIEW = "screen", "app", "view"  # a curses screen, an application, the TV interface itself


@dataclasses.dataclass(frozen=True)
class Entry:
    label: str  # the classic SETTINGS_MENU row
    kind: str  # SCREEN, APP or VIEW
    target: str  # the --screen name, the application id, or the view ("active", "updates", "help", "home")
    help: str  # the right pane: what is in there


ENTRIES = (
    Entry("DISPLAY", SCREEN, "display", "RESOLUTION, REFRESH RATE, SCREEN EDGES AND TEXT SIZE"),
    Entry("APPEARANCE", SCREEN, "appearance", "THEME, ACCENT COLOUR AND BACKGROUND"),
    Entry("AUDIO", SCREEN, "audio", "WHERE THE SOUND GOES AND A TEST TONE"),
    Entry("BLUETOOTH", SCREEN, "bluetooth", "PAIR CONTROLLERS, HEADPHONES AND KEYBOARDS"),
    Entry("CONTROLLERS", SCREEN, "controllers", "CONNECTED CONTROLLERS, BATTERY AND A BUTTON TEST"),
    Entry("NETWORK", SCREEN, "network", "WI-FI AND WIRED NETWORK"),
    Entry("SLEEP & SCREEN", SCREEN, "sleep", "WHEN THE SCREEN GOES BLANK AND THE BOX SLEEPS"),
    Entry("APPLICATIONS", SCREEN, "applications", "ADD, HIDE AND ORDER APPLICATIONS. ADD A WEB BROWSER"),
    Entry("REMOTE DESKTOP", SCREEN, "remote-desktop", "SAVED REMOTE DESKTOP CONNECTIONS"),
    Entry("STREAMING", SCREEN, "streaming", "GAMING PCS: PAIR, WAKE, STREAM CHECK AND AUTO-STREAM"),
    Entry("ACTIVE APPLICATIONS", VIEW, "active", "BRING BACK OR CLOSE A RUNNING APPLICATION"),
    Entry("TAILSCALE", APP, "tailscale", "REACH YOUR GAMING PC AWAY FROM HOME"),
    Entry("TV CONTROL", SCREEN, "tv-control", "USE THE TV REMOTE (HDMI-CEC)"),
    Entry("SOFTWARE UPDATE", SCREEN, "software-update", "INSTALL A NEWER COUCHLITEOS"),
    Entry("CHECK FOR UPDATES", VIEW, "updates", "LOOK FOR A NEWER RELEASE AT EVERY START. A / CROSS OR ENTER TURNS IT ON OR OFF"),
    Entry("CONTROLS", SCREEN, "controls", "BUTTONS, MOUSE SPEED AND THE KEYBOARD HOME KEY"),
    Entry("SETUP WIZARD", SCREEN, "setup-wizard", "GO THROUGH THE FIRST-START SETUP AGAIN"),
    Entry("GENERATE SUPPORT FILE", SCREEN, "support-file", "SAVE THE LOGS FOR A BUG REPORT"),
    Entry("SYSTEM DIAGNOSTICS", APP, "system-diagnostics", "CHECKS OF THIS BOX'S HARDWARE AND NETWORK"),
    Entry("HELP", VIEW, "help", "SHORT ANSWERS TO COMMON QUESTIONS, AND THE TOUR AGAIN"),
    Entry("BACK", VIEW, "home", "BACK TO THE HOME SCREEN"),
)
# The home screen's own tiles (couchliteos_home SYSTEM_TILES) that are curses screens.
TILE_SCREENS = {"hosts": "streaming", "software-update": "software-update"}

SETTINGS_HINT = "A / CROSS OR ENTER OPENS  ·  B / CIRCLE OR ESC GOES BACK"
POWER_HINT = "A / CROSS OR ENTER SELECTS  ·  B / CIRCLE OR ESC GOES BACK"
WHATS_NEW_HINT = whatsnew.FOOTER
CONTINUE_HINT = "A / CROSS OR ENTER CONTINUES"
CHILD_FAILED = "COULD NOT OPEN {}: {}"


def child_command(name: str, app: str = "") -> list[str]:
    """The foot window that runs one classic screen."""
    return [FOOT, "--fullscreen", "--title", CHILD_TITLE, LAUNCHER, "--screen", name,
            *(["--app", app] if app else [])]


def setup_due(run: pathlib.Path = RUN, setup_marker: pathlib.Path | None = None) -> bool:
    """Whether the start needs `--screen setup`: setup is not complete, or it restarted mid-way for
    a new picture size. The buttons it used to show once afterwards are the TV's tour now
    (couchliteos_help), drawn by the TV interface itself."""
    setup_marker = setup_marker or STATE / "setup-complete"
    return not setup_marker.exists() or (run / REOPEN_SETUP).exists()


def take_reopen_display(run: pathlib.Path = RUN) -> bool:
    """True once after a child exited to apply SCREEN EDGES / TEXT SIZE: DISPLAY opens again."""
    marker = run / REOPEN_DISPLAY
    if not marker.exists():
        return False
    marker.unlink(missing_ok=True)
    return True


def take_switch_interface(run: pathlib.Path = RUN) -> bool:
    """True once after APPEARANCE > INTERFACE was changed in a screen on top."""
    marker = run / SWITCH_INTERFACE
    if not marker.exists():
        return False
    marker.unlink(missing_ok=True)
    return True


def take_update_watch(run: pathlib.Path = RUN) -> str | None:
    """The version a child's SOFTWARE UPDATE started installing (once), else None."""
    marker = run / UPDATE_WATCH
    try:
        version = marker.read_text(encoding="ascii", errors="replace").strip()[:64]
    except OSError:
        return None
    marker.unlink(missing_ok=True)
    return version or "?"


def write_update_watch(version: str, run: pathlib.Path = RUN) -> None:
    (run / UPDATE_WATCH).write_text(f"{version}\n", encoding="ascii")


# ---------------------------------------------------------------------- Settings


def _safe(source: Callable[[], str]) -> str:
    try:
        value = source()
    except Exception:  # noqa: BLE001 - a value that cannot be read is left blank, never fatal
        return ""
    return value.upper() if isinstance(value, str) else ""


def display_value() -> str:
    output = display.active_output(display.query_outputs())
    if output is None or output.current_mode is None:
        return "NO ACTIVE DISPLAY"
    return f"{output.current_mode.resolution} @ {output.current_mode.refresh} HZ"


def appearance_value() -> str:
    name, accent = theme.load_choice()
    chosen = theme.chosen()
    return (f"{chosen.label if chosen else name or theme.DEFAULT}  ·  ACCENT {theme.accent_label(accent)}"
            f"  ·  {background.label(background.load())}")


def sleep_value(can_sleep: bool) -> str:
    settings = power.load_settings()
    asleep = power.minutes_label(settings.sleep) if can_sleep else "NOT SUPPORTED"
    return f"BLANK AFTER {power.minutes_label(settings.blank)}  ·  SLEEP AFTER {asleep}"


def streaming_value() -> str:
    config = stream.load_settings()
    host = stream.default_host(stream.load_hosts(), config)
    pc = host.label if host else "NO GAMING PC PAIRED"
    return f"{pc}  ·  AUTO-STREAM {'ON' if config.autostart else 'OFF'}"


def update_value(checker) -> str:
    available = checker.available()
    return f"{available} AVAILABLE" if available else f"THIS BOX: COUCHLITEOS {checker.current or 'UNKNOWN'}"


def value_sources(
    *, updates, controllers, running: Callable[[], list], applications: Callable[[], int],
    can_sleep: Callable[[], bool], network: Callable[[], str],
) -> dict[str, Callable[[], str]]:
    """The right pane's current value for each entry that has one."""
    return {
        "DISPLAY": display_value,
        "APPEARANCE": appearance_value,
        "CONTROLLERS": lambda: controllers.line() or "NO CONTROLLER CONNECTED",
        "NETWORK": network,
        "SLEEP & SCREEN": lambda: sleep_value(can_sleep()),
        "APPLICATIONS": lambda: f"{applications()} APPLICATIONS",
        "STREAMING": streaming_value,
        "ACTIVE APPLICATIONS": lambda: f"{len(running())} RUNNING" if running() else "NOTHING RUNNING",
        "SOFTWARE UPDATE": lambda: update_value(updates),
        "CHECK FOR UPDATES": lambda: "ON" if updates.enabled else "OFF",
    }


def toggle_updates(checker) -> str:
    """CHECK FOR UPDATES: the classic launcher's switch and its words."""
    enabled = not checker.enabled
    checker.set_enabled(enabled)
    if not enabled:
        return "UPDATE CHECK OFF: NOTHING IS SENT"
    if not checker.online():
        return "UPDATE CHECK ON: WAITS UNTIL THIS PC IS ONLINE"
    return "UPDATE CHECK ON: LOOKS FOR A NEWER RELEASE AT EVERY START"


class SettingsModel:
    """Settings as two panes. `values` maps an entry's label to its current value (read by
    `refresh`, when Settings opens and when a screen opened from it closes)."""

    def __init__(self, values: Mapping[str, Callable[[], str]] | None = None,
                 entries: tuple[Entry, ...] = ENTRIES) -> None:
        self.entries = entries
        self.sources = dict(values or {})
        self.values: dict[str, str] = {}
        self.focus = 0

    def refresh(self) -> None:
        self.values = {label: _safe(source) for label, source in self.sources.items()}

    def rows(self) -> list[str]:
        return [entry.label for entry in self.entries]

    def focused(self) -> Entry:
        return self.entries[self.focus]

    def move(self, dy: int) -> bool:
        focus = max(0, min(len(self.entries) - 1, self.focus + dy))
        moved, self.focus = focus != self.focus, focus
        return moved

    def detail(self) -> tuple[str, str, str]:
        """(title, value, help) for the right pane."""
        entry = self.focused()
        return entry.label, self.values.get(entry.label, ""), entry.help

    def activate(self) -> tuple[str, str]:
        entry = self.focused()
        return entry.kind, entry.target

    def visible(self, slots: int) -> range:
        """The rows on screen: a long list scrolls so the focus stays in the middle."""
        return tvlayout.visible(len(self.entries), self.focus, max(1, slots), anchor=max(1, slots) // 2)


# ---------------------------------------------------------------------- power


@dataclasses.dataclass(frozen=True)
class PowerChoice:
    label: str
    request: str  # "suspend", "reboot", "poweroff", or "" for BACK
    question: str  # "" when nothing is asked


class PowerModel:
    """SLEEP, RESTART, SHUT DOWN and BACK, asked the way the classic home screen asks."""

    def __init__(self, can_sleep: bool, can_wake: bool, running: list[str] | tuple[str, ...] = ()) -> None:
        closed = f" {', '.join(running)} WILL BE CLOSED." if running else ""
        sleep_question = ("SLEEP NOW?" if can_wake else
                          "SLEEP NOW? NOTHING CONNECTED CAN WAKE THIS PC: USE ITS POWER BUTTON TO WAKE IT.")
        self.can_sleep = can_sleep
        self.choices = (
            PowerChoice("SLEEP" if can_sleep else "SLEEP: NOT SUPPORTED ON THIS PC", "suspend",
                        sleep_question if can_sleep else ""),
            PowerChoice("RESTART", "reboot", f"RESTART NOW?{closed}"),
            PowerChoice("SHUT DOWN", "poweroff", f"SHUT DOWN NOW?{closed}"),
            PowerChoice("BACK", "", ""),
        )
        self.focus = 0

    def rows(self) -> list[str]:
        return [choice.label for choice in self.choices]

    def move(self, dy: int) -> bool:
        focus = max(0, min(len(self.choices) - 1, self.focus + dy))
        moved, self.focus = focus != self.focus, focus
        return moved

    def focused(self) -> PowerChoice:
        return self.choices[self.focus]

    @staticmethod
    def done(request: str) -> str:
        return {"suspend": "GOING TO SLEEP", "reboot": "RESTARTING...", "poweroff": "SHUTTING DOWN..."}[request]


# ---------------------------------------------------------------------- update progress

ACTIVE_PHASES = ("checking", "downloading", "verifying", "saving", "installing", "restarting")


@dataclasses.dataclass(frozen=True)
class Progress:
    title: str
    text: str
    percent: int | None  # None: unknown (a moving bar), or no bar when `bar` is False
    hint: str
    bar: bool = True
    finished: str = ""  # set once it ended: the result line the screen leaves behind


def update_running(run: pathlib.Path = RUN) -> bool:
    """An update the service is working on now (the TV interface restarted mid-way)."""
    phase = softwareupdate.read_status(run / softwareupdate.STATUS).get("phase")
    return (run / softwareupdate.REQUEST).exists() or phase in ACTIVE_PHASES


def _text(value: object, fallback: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _percent(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, min(100, int(value)))


class UpdateProgress:
    """softwareupdate.SoftwareUpdate.progress, one poll at a time: call `poll` about twice a second,
    `back` for B / Esc, `dismiss` for A on a failure."""

    def __init__(self, version: str, run: pathlib.Path = RUN, clock: Callable[[], float] = time.monotonic) -> None:
        self.version = version
        self.path = run / softwareupdate.STATUS
        self.run = run
        self.clock = clock
        self.started = clock()
        self.restarting_since: float | None = None
        self.cancelling = False
        self.note = ""
        self.last_phase = ""
        self.failure = ""  # the message shown until A or B
        self.result = ""  # set when it ended
        self.state: dict = {}

    def phase(self) -> str:
        phase = self.state.get("phase")
        return phase if isinstance(phase, str) else ""

    def poll(self) -> Progress:
        if not self.failure and not self.result:
            self.state = softwareupdate.read_status(self.path)
            phase = self.phase()
            if phase != self.last_phase:
                self.last_phase, self.note = phase, ""
            now = self.clock()
            if phase == "failed":
                self.failure = _text(self.state.get("message"), softwareupdate.FAILED)
            elif phase == "cancelled":
                self.result = softwareupdate.CANCELLED
            elif phase == "uptodate":
                self.result = softwareupdate.NEWEST
            elif not phase and now - self.started >= softwareupdate.START_WAIT:
                self.failure = softwareupdate.NOT_STARTED
            elif phase == "restarting":
                self.restarting_since = now if self.restarting_since is None else self.restarting_since
                if now - self.restarting_since >= softwareupdate.RESTART_WAIT:
                    self.failure = softwareupdate.NOT_RESTARTED
        return self.view()

    def view(self) -> Progress:
        shown = _text(self.state.get("version"), self.version)
        title = f"UPDATING TO COUCHLITEOS {shown}".strip()
        if self.result:
            return Progress(title, self.result, None, "", bar=False, finished=self.result)
        if self.failure:
            return Progress(title, self.failure, None, CONTINUE_HINT, bar=False)
        phase = self.phase()
        text = softwareupdate.PHASE_TEXT.get(phase, "WORKING...") if phase else "STARTING THE UPDATE SERVICE..."
        if phase in ("downloading", "saving", "installing", "checking", "verifying"):
            text = _text(self.state.get("message"), text).upper()
        hint = ("CANCELLING..." if self.cancelling and phase == "downloading"
                else self.note or softwareupdate.PHASE_HINT.get(phase, ""))
        if hint.startswith("B / CIRCLE"):
            hint = hint.replace("B / CIRCLE", "B / CIRCLE OR ESC")
        return Progress(title, text, _percent(self.state.get("percent")) if phase else None, hint)

    def back(self) -> None:
        """B / Esc: cancel a download; later phases cannot be stopped, so only say so. On a failure it dismisses."""
        if self.failure:
            self.dismiss()
            return
        phase = self.phase()
        if phase == "downloading" and not self.cancelling:
            self.cancelling = True
            try:
                (self.run / softwareupdate.CANCEL).touch()
            except OSError:
                self.cancelling = False
                self.note = "COULD NOT CANCEL"
        elif phase in ("saving", "installing"):
            self.note = "INSTALLING: PLEASE WAIT"
        elif phase not in ("downloading", "restarting"):
            self.note = "PLEASE WAIT"

    def dismiss(self) -> None:
        if self.failure:
            self.result, self.failure = self.failure, ""


# ---------------------------------------------------------------------- What's New


def whats_new(version: str, seen: str = "") -> tuple[str, list[str]]:
    """(title, lines) of the What's New screen, as couchliteos_whatsnew draws it in curses."""
    renamed, releases = whatsnew.notes(version, seen)
    lines = [whatsnew.RENAME_NOTICE] if renamed else []
    for release, features in releases:
        if len(releases) > 1:
            lines.append(f"NEW IN {release}:")
        lines.extend(f"·  {feature}" for feature in features)
    if whatsnew.skipped_releases(version, seen):
        lines.append(whatsnew.EARLIER)
    return f"WHAT'S NEW IN {version.upper()}", lines


# ---------------------------------------------------------------------- look


def list_slots(layout: tvlayout.Layout) -> int:
    """How many Settings rows fit between the title and the hint."""
    usable = layout.height - 2 * layout.margin_y - layout.px(160)
    return max(5, usable // layout.px(58))


def stylesheet(colours: theme.Theme, layout: tvlayout.Layout) -> str:
    """CSS for these screens, added to tvlayout.stylesheet."""
    c = {field: f"#{value}" for field, value in colours.colours.items()}
    f = layout.fonts
    radius = layout.px(14)
    return f"""
.tv-pane {{ background-color: {c['surface']}; border-radius: {radius}px; padding: {layout.px(28)}px; }}
.tv-pane .tv-value {{ font-size: {f['body']}px; font-weight: bold; color: {c['accent']}; }}
.tv-pane .tv-help {{ font-size: {f['body']}px; color: {c['muted']}; }}
.tv-list .tv-item {{ font-size: {f['body']}px; }}
.tv-line {{ font-size: {f['body']}px; color: {c['text']}; }}
progressbar trough {{ min-height: {layout.px(24)}px; background-color: {c['surface']}; border: none; border-radius: {radius}px; }}
progressbar progress {{ min-height: {layout.px(24)}px; background-color: {c['accent']}; border-radius: {radius}px; }}
"""
