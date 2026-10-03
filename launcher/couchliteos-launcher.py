#!/usr/bin/python3
"""Full-screen terminal launcher for the CouchLiteOS appliance."""

from __future__ import annotations

import curses
import dataclasses
import ipaddress
import os
import pathlib
import ssl
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Sequence

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import couchliteos_display as display
import couchliteos_audio as audio
import couchliteos_brightness as brightness
import couchliteos_support as support
import couchliteos_bluetooth as bluetooth
import couchliteos_listview as listview
import couchliteos_apps as apps
import couchliteos_browser as browser
import couchliteos_browsersetup as browsersetup
import couchliteos_power as power
import couchliteos_rdp as rdp
import couchliteos_setup as setup
import couchliteos_stream as stream
import couchliteos_streamcheck as streamcheck
import couchliteos_firmware as firmware
import couchliteos_cec as cec
import couchliteos_controllers as controllers
import couchliteos_pcstatus as pcstatus
import couchliteos_update as update
import couchliteos_softwareupdate as softwareupdate
import couchliteos_errors as errors
import couchliteos_netmenu as netmenu
import couchliteos_confirm as confirmation
import couchliteos_whatsnew as whatsnew
import couchliteos_controls as controls
import couchliteos_input as inputprefs
import couchliteos_padcheck as padcheck
import couchliteos_pads as pads
import couchliteos_screenfit as screenfit
import couchliteos_pointer as pointer
import couchliteos_phone as phone
import couchliteos_theme as themes
import couchliteos_recent as recent


# The tests point both at a scratch directory (testenv.py).
RUN = pathlib.Path(os.environ.get("COUCHLITEOS_RUN_DIR", "/run/couchliteos"))
DATA = pathlib.Path(os.environ.get("COUCHLITEOS_STATE_DIR", "/var/lib/couchliteos"))
HOME_REQUEST = RUN / "home.request"
# Tells gamepad-nav the launcher (not a running app) has focus, so it forwards keys.
LAUNCHER_FOCUS = RUN / "launcher-focus"
# couchliteos_osk.REFOCUSED: the keyboard put the app it typed into back in front.
OSK_REFOCUSED = RUN / "osk-refocused"
SOURCE_MANIFESTS = pathlib.Path(__file__).resolve().parents[1] / "config/apps.d"
FIXED_CONTROLS = (
    ("SETTINGS", "settings"), ("SLEEP", "suspend"), ("REBOOT", "reboot"), ("SHUTDOWN", "poweroff"),
)
WAKE_PC = errors.Action("WAKE PC", "wake-pc")
STREAM_ACTION = "stream-default"  # the first home row: STREAM <PC>, one press to start streaming
STREAM_ROW_MAX = 32  # columns; a long PC name is cut so the menu stays as narrow as SLEEP: NOT SUPPORTED
STREAM_ROW_CHECK_SECONDS = 5  # how often the home screen looks again for a paired PC
SMOOTHER_ROW = "SMOOTHER STREAM (LOWER QUALITY ONE STEP)"
SLEEP_UNSUPPORTED = "SLEEP: NOT SUPPORTED ON THIS PC"
# Shown in SLEEP & SCREEN (at most 76 columns) when a saved sleep timeout is not acted on.
IDLE_SLEEP_NO_WAKE = "IDLE SLEEP OFF: NO CONTROLLER CAN WAKE THIS PC"
SETTINGS_MENU = (
    "DISPLAY",
    "APPEARANCE",
    "AUDIO",
    "BLUETOOTH",
    "CONTROLLERS",
    "NETWORK",
    "SLEEP & SCREEN",
    "APPLICATIONS",
    "REMOTE DESKTOP",
    "STREAMING",
    "ACTIVE APPLICATIONS",
    "TAILSCALE",
    "TV CONTROL",
    "SOFTWARE UPDATE",
    "CHECK FOR UPDATES",
    "CONTROLS",
    "SETUP WIZARD",
    "GENERATE SUPPORT FILE",
    "SYSTEM DIAGNOSTICS",
    "BACK",
)
UPDATE_SUFFIX = "  -  UPDATE AVAILABLE"  # on the SETTINGS row while a newer release is known
STREAM_WINDOW_WORDS = {"moonlight": "moonlight", "chiaki-ng": "chiaki"}  # found in the listed window of a stream
SPINNER = "|/-\\"
SAVE_FAILED = "COULD NOT SAVE: DISK FULL OR READ-ONLY"
ENTER_KEYS = (curses.KEY_ENTER, 10, 13)
# gamepad-nav forwards LB, RB, View/Select, and Menu/Start as F5-F8.
SHORTCUT_KEYS = {curses.KEY_F5: "lb", curses.KEY_F6: "rb", curses.KEY_F7: "view", curses.KEY_F8: "menu"}
SHORTCUT_TAGS = apps.SHORTCUTS  # one set of names on the main menu and in Settings
# TEST REMOTE BUTTONS: the launcher keys gamepad-nav makes of TV remote keys, by the name cec.REMOTE_LABELS uses.
REMOTE_TEST_KEYS = {
    curses.KEY_UP: "KEY_UP", curses.KEY_DOWN: "KEY_DOWN", curses.KEY_LEFT: "KEY_LEFT", curses.KEY_RIGHT: "KEY_RIGHT",
    curses.KEY_ENTER: "KEY_ENTER", 10: "KEY_ENTER", 13: "KEY_ENTER", 27: cec.BACK_KEY, curses.KEY_DC: "KEY_DELETE",
    curses.KEY_F5: "KEY_F5", curses.KEY_F6: "KEY_F6", curses.KEY_F7: "KEY_F7", curses.KEY_F8: "KEY_F8",
}
# gamepad-nav sends Delete for BTN_WEST; X/Triangle (BTN_NORTH) open the keyboard instead.
CLOSE_BUTTON = "Y (XBOX) / SQUARE (PS)"
TEXT_HINT = "X / TRIANGLE KEYBOARD · Y / SQUARE DELETE · A / CROSS OK · B / CIRCLE CANCEL"
# TYPE ON PHONE: SELECT/VIEW (gamepad-nav sends F7 for a tap) or F2 on a keyboard. Y already deletes.
PHONE_KEYS = (curses.KEY_F2, curses.KEY_F7)
PHONE_ROW = "SELECT (VIEW) OR F2: TYPE ON PHONE"
PHONE_HINT = "B / CIRCLE OR ESC CANCELS"
PHONE_POLL_MS = 500
# Hints name controller buttons first; where it fits they add the keyboard key (Enter, Esc).
# F5-F12 are never named: the controller's X, LB, RB, SELECT and START send them.
LIST_HINT = "A / CROSS OR ENTER SELECTS  ·  B / CIRCLE OR ESC GOES BACK"
CONTROLS_HINT = "LEFT/RIGHT OR A / CROSS CHANGES  ·  B / CIRCLE OR ESC GOES BACK"
BUTTONS_HINT = "A / CROSS SELECTS  ·  LEFT/RIGHT MOVES A BUTTON  ·  B / CIRCLE BACK"
FORM_HINT = "A / CROSS EDITS  ·  LEFT/RIGHT TOGGLES  ·  B / CIRCLE OR ESC BACK"
DONE_HINT = "A / CROSS OR B / CIRCLE"
# Settings > DISPLAY > SCREEN EDGES / TEXT SIZE (saved in screen.json, read by the foot wrapper at start).
SCREEN_EDGES_HELP = "PICK THE SMALLEST EDGE WHERE YOU CAN SEE THE WHOLE BORDER."
TEXT_SIZE_HELP = "SMALLER FITS MORE ON THE SCREEN  ·  LARGER IS EASIER TO READ"
SCREEN_CHOICE_HINT = "A / CROSS PICKS  ·  B / CIRCLE OR ESC GOES BACK"
SCREEN_RESTART_MESSAGE = "RESTARTING THE LAUNCHER TO APPLY. THIS TAKES A FEW SECONDS."
SCREEN_NEXT_START_MESSAGE = "SAVED. IT APPLIES THE NEXT TIME COUCHLITEOS STARTS."
# foot sizes its text for the picture size it starts on, so a new picture size restarts it too.
PICTURE_RESTART_MESSAGE = "RESTARTING SO THE TEXT FITS THE NEW PICTURE SIZE. THIS TAKES A FEW SECONDS."
PICTURE_NEXT_START_MESSAGE = "MODE SAVED. THE TEXT SIZE CATCHES UP THE NEXT TIME COUCHLITEOS STARTS."
# systemd gives up on the launcher after 5 starts a minute (StartLimitBurst), so screen-setting
# restarts stop at 3 a minute; the marker sends the restarted launcher back to DISPLAY.
SCREEN_RESTARTS_PER_MINUTE = 3
SUPPORT_EXPORT_TIMEOUT = 180.0
SUPPORT_EXPORT_START_TIMEOUT = 12.0
SUPPORT_EXPORT_POLL_MS = 100
# A result or error stays on the main screen this long unless a button is pressed first.
STATUS_HOLD_SECONDS = 15.0


def application_result() -> apps.LoadResult:
    system_dir = apps.SYSTEM_DIR if apps.SYSTEM_DIR.exists() else SOURCE_MANIFESTS
    result = apps.load_applications(system_dir=system_dir)
    # Firefox and Chrome have no tile until they are installed (ADD A WEB BROWSER).
    return apps.LoadResult(tuple(app for app in result.applications if apps.installed(app)), result.errors)


def set_launcher_focus(held: bool) -> None:
    try:
        if held:
            LAUNCHER_FOCUS.touch()
        else:
            LAUNCHER_FOCUS.unlink(missing_ok=True)
    except OSError:
        pass


def mark_front_app(app: apps.Application) -> None:
    """Name `app` in app-active, as its start did, so Guide follows the app brought to the front."""
    if app.terminal:
        return  # its start leaves the file alone too: gamepad-nav types into a terminal app
    active = RUN / "app-active"
    try:
        active.write_text(app.id + "\n", encoding="ascii")
        os.chmod(active, 0o640)
    except OSError:
        pass


def request_osk(masked: bool = False) -> None:
    if (RUN / "osk-active").exists():  # one keyboard at a time; a second request would queue another
        return
    if masked:
        (RUN / "osk-masked").touch()
    (RUN / "start-osk").touch()


def rdp_application(connection: rdp.Connection) -> apps.Application:
    """Launchable entry for a saved connection that is not pinned to the launcher."""
    return apps.Application(
        id=connection.id, name=connection.name.upper(), kind="rdp",
        connection=connection.id, status_id=connection.id,
    )


def active_rdp_session() -> str | None:
    try:
        return rdp.read_connection_id(RUN / rdp.SESSION.name)
    except (OSError, UnicodeError, ValueError):
        return None


def rdp_log(message: str) -> None:
    display.log(message, pathlib.Path("/var/log/couchliteos/rdp.log"))


# Set by Launcher: blanks the screen when idle and swallows the key that wakes it.
IDLE_GUARD: power.IdleGuard | None = None
# Which running apps the controller drives as a mouse (browsers, web apps).
POINTER_MODES = pointer.Modes()


def focus_launcher() -> None:
    try:
        subprocess.run(
            ["wlrctl", "toplevel", "focus", "title:CouchLiteOS Launcher"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        pass


# The keyboard Home key (Ctrl+Alt+H until it is changed) reaches the launcher as bytes foot
# makes of the chord (Ctrl+Alt+H is ESC then Ctrl+H) while gamepad-nav turns it into a Home
# request. Home is handled like Guide, so those bytes are dropped: an ESC (a chord with Alt)
# waits this long for the request to show up, and the chord's own letter is dropped within
# HOME_CHORD_TAIL (a chord without Alt, such as Super+H, waits for the request the same way).
# Only a request this fresh counts: one left by a Guide press in Settings (which only the
# main menu answers) must not swallow every later B / Esc.
HOME_CHORD_WAIT = 0.05
# Setup ignores Home, so there a missed chord skips a step: wait longer (the request has been
# seen arriving about 0.1 s after foot's bytes). B is Esc too, so only setup pays for this.
HOME_CHORD_SETUP_WAIT = 0.3
HOME_CHORD_POLL = 0.02
HOME_CHORD_FRESH = 0.5
HOME_CHORD_TAIL = 1.0
CTRL_H = 8
_home_chord_until = 0.0
HOME_SETTINGS = inputprefs.Watcher()  # the saved Home key, re-read when config.ini changes


def home_chord_bytes() -> tuple[bool, frozenset[int]]:
    """(foot starts the chord with ESC, the other bytes it sends) for the saved keyboard Home key."""
    names = inputprefs.parse_chord(HOME_SETTINGS.current().keyboard_home)
    keys = [name[4:] for name in names if name not in inputprefs.MODIFIER_FAMILIES]
    families = {inputprefs.chord_family(name) for name in names if name in inputprefs.MODIFIER_FAMILIES}
    if len(keys) != 1:
        return False, frozenset()
    key = keys[0]
    if len(key) != 1:
        return "ALT" in families, frozenset()
    if "CTRL" in families and key.isalpha():
        return "ALT" in families, frozenset({ord(key) & 0x1F})
    return "ALT" in families, frozenset({ord(key.lower()), ord(key.upper())})


def home_chord_pending() -> bool:
    try:
        written = HOME_REQUEST.stat().st_mtime
    except OSError:
        return False
    return abs(time.time() - written) < HOME_CHORD_FRESH


def read_key(screen: curses.window, *, keyboard: bool = True) -> int:
    """The next key. F12 (X / Triangle) opens the on-screen keyboard for the launcher unless
    `keyboard` is False: then the caller gets the key and decides where the typing goes."""
    key = screen.getch()
    if key != -1:
        display.confirm_restore()
    if IDLE_GUARD is not None:
        key = IDLE_GUARD.filter(screen, key)
    if key == curses.KEY_F12 and keyboard:
        request_osk()
        return -1
    return without_home_chord(key)


def without_home_chord(key: int, wait: float = HOME_CHORD_WAIT) -> int:
    """-1 for the bytes foot sends for the keyboard Home key (gamepad-nav has left a Home request
    for it), so the shortcut is not also taken as B / Esc or a typed letter; any other key
    unchanged. An ESC, or the letter of a chord without Alt, waits up to `wait` seconds for the request."""
    global _home_chord_until
    has_esc, tail = home_chord_bytes()
    if key == 27 and has_esc:
        if wait_for_home_request(wait):  # left for the caller's Home check, like a Guide press
            _home_chord_until = time.monotonic() + HOME_CHORD_TAIL
            return -1
    elif key in tail:
        if has_esc:
            if time.monotonic() < _home_chord_until:
                _home_chord_until = 0.0
                return -1
        elif wait_for_home_request(wait):
            return -1
    return key


def wait_for_home_request(wait: float) -> bool:
    waited = 0.0
    while waited < wait and not home_chord_pending():
        step = min(HOME_CHORD_POLL, wait - waited)
        time.sleep(step)
        waited += step
    return home_chord_pending()


def claim_screen_restart(marker: str) -> bool:
    """Count a restart that applies a screen change, and leave `marker` in RUN for the restarted
    launcher. False (no restart) past SCREEN_RESTARTS_PER_MINUTE or when RUN cannot be written."""
    log = RUN / "screen-restarts"
    try:
        now = time.time()
        recent = [stamp for stamp in map(float, log.read_text().split()) if now - 60 < stamp <= now] if log.exists() else []
        if len(recent) >= SCREEN_RESTARTS_PER_MINUTE:
            return False
        log.write_text("".join(f"{stamp}\n" for stamp in [*recent, now]))
        (RUN / marker).touch()
    except (OSError, ValueError):
        return False
    return True


def setup_ui(screen: curses.window) -> setup.CursesUI:
    """The setup wizard's screens, reading keys without the Ctrl+Alt+H chord: the wizard takes
    no Home request (as with Guide), and its ESC must not skip a step."""
    return setup.CursesUI(screen, read_key=lambda: without_home_chord(screen.getch(), HOME_CHORD_SETUP_WAIT))


class KeyboardSource:
    """The keyboards' key presses for the capture screen, read from evdev (watched, never grabbed)."""

    OWN_DEVICE_PREFIX = "CouchLiteOS "  # the pad's own virtual keyboard and the on-screen keyboard are not typed on

    def __init__(self) -> None:
        self.devices: list = []

    def open(self) -> bool:
        try:
            import evdev  # python3-evdev, already part of the image
            import glob
        except ImportError:
            return False
        codes = evdev.ecodes
        for path in glob.glob("/dev/input/event*"):
            try:
                device = evdev.InputDevice(path)
                keys = set(device.capabilities().get(codes.EV_KEY, []))
                if {codes.KEY_A, codes.KEY_LEFTCTRL} <= keys and not device.name.startswith(self.OWN_DEVICE_PREFIX):
                    self.devices.append(device)
                else:
                    device.close()
            except OSError:
                pass
        return bool(self.devices)

    def poll(self, timeout: float) -> list[tuple[str, int]]:
        """(key name, 1 down / 0 up) for what arrived within `timeout` seconds."""
        import select
        from evdev import ecodes

        found: list[tuple[str, int]] = []
        try:
            readable = select.select(self.devices, [], [], timeout)[0]
        except (OSError, ValueError):
            return found
        for device in readable:
            try:
                events = list(device.read())
            except BlockingIOError:
                continue
            except OSError:
                self.devices.remove(device)
                continue
            for event in events:
                if event.type == ecodes.EV_KEY and event.value in (0, 1):
                    name = ecodes.KEY.get(event.code)
                    name = name if isinstance(name, str) else (name or [""])[0]
                    if name.startswith("KEY_"):
                        found.append((name, event.value))
        return found

    def close(self) -> None:
        for device in self.devices:
            try:
                device.close()
            except OSError:
                pass
        self.devices.clear()


def flush_input() -> None:
    """Drop presses queued while the screen was frozen by a slow call."""
    try:
        curses.flushinp()
    except curses.error:  # no terminal (unit tests)
        pass


def move_selection(selected: int, key: int, count: int) -> int:
    if key in (curses.KEY_UP, ord("k")):
        return (selected - 1) % count
    if key in (curses.KEY_DOWN, ord("j")):
        return (selected + 1) % count
    return selected


def indeterminate_progress_bar(width: int, frame: int) -> str:
    """Return a fixed-width, bouncing ASCII progress indicator."""
    width = max(8, width)
    inner_width = width - 2
    segment_width = max(2, min(8, inner_width // 4))
    travel = max(0, inner_width - segment_width)
    if travel:
        cycle = frame % (travel * 2)
        position = cycle if cycle <= travel else travel * 2 - cycle
    else:
        position = 0
    body = (
        " " * position
        + "=" * segment_width
        + " " * (inner_width - position - segment_width)
    )
    return f"[{body}]"


def format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, remaining = divmod(total, 60)
    return f"{minutes:02d}:{remaining:02d}"


# Interfaces that are not the LAN: VPN, container and VM bridges.
VIRTUAL_INTERFACES = ("tailscale", "docker", "virbr", "veth", "br-")
OFFLINE = "OFFLINE - SETTINGS > NETWORK"


def default_route_interfaces(output: str) -> list[str]:
    """Interface names from `ip -4 route show default`, in route order."""
    devices = []
    for line in output.splitlines():
        fields = line.split()
        if fields[:1] == ["default"] and "dev" in fields[:-1]:
            devices.append(fields[fields.index("dev") + 1])
    return devices


def get_ipv4(output: str, preferred: Sequence[str] = ()) -> str:
    """Return the LAN IPv4 from normal or `ip -brief` output.

    Loopback, link-local and virtual interfaces (VPN, Docker, libvirt) are
    skipped; an address on an interface in `preferred` wins over the first.
    """
    found: list[tuple[str, str]] = []
    for line in output.splitlines():
        fields = line.split()
        if not fields:
            continue
        candidates: list[str] = []
        if "inet" in fields:
            index = fields.index("inet") + 1
            if index < len(fields):
                candidates.append(fields[index])
            interface = fields[-1]
        else:
            # `ip -brief -4 address` prints: IFACE STATE ADDRESS/PREFIX ...
            candidates.extend(fields[2:])
            interface = fields[0]
        if interface.startswith(VIRTUAL_INTERFACES):
            continue
        for candidate in candidates:
            value = candidate.split("/", 1)[0]
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if isinstance(address, ipaddress.IPv4Address) and not (
                address.is_loopback or address.is_link_local
            ):
                found.append((interface, str(address)))
    for interface, address in found:
        if interface in preferred:
            return address
    return found[0][1] if found else "NO IPV4"


def network_summary() -> str:
    def ip(*arguments: str) -> str:
        return subprocess.run(
            ["ip", *arguments], text=True, capture_output=True, check=False, timeout=3
        ).stdout

    try:
        addresses = ip("-brief", "-4", "address", "show", "up")
        routes = ip("-4", "route", "show", "default")
    except (OSError, subprocess.SubprocessError):
        return OFFLINE
    address = get_ipv4(addresses, default_route_interfaces(routes))
    return f"{address}  ONLINE" if address != "NO IPV4" else OFFLINE


LIVE_DIR = pathlib.Path("/run/live")
LIVE_WARNING = "LIVE MODE: SETTINGS WILL NOT BE SAVED"


def state_is_persistent(findmnt_output: str) -> bool:
    """Judge `findmnt -n -o SOURCE,FSTYPE,OPTIONS --target /var/lib/couchliteos`.

    Saved settings survive only when that directory is a bind mount from a
    writable disk (live-boot persistence) or sits on an overlay whose upper
    layer lives under /run/live/persistence. The plain live overlay, tmpfs,
    RAM devices and the read-only system image do not count.
    """
    if "/run/live/persistence/" in findmnt_output:
        return True
    fields = findmnt_output.split()
    source, fstype = (fields + ["", ""])[:2]
    return (
        source.startswith("/dev/")
        and not source.startswith(("/dev/ram", "/dev/zram"))
        and fstype not in ("squashfs", "iso9660", "udf")
    )


def live_mode_warning(live_dir: pathlib.Path = LIVE_DIR) -> str:
    """Return the banner text when booted live without working persistence."""
    if not live_dir.exists():
        return ""
    try:
        result = subprocess.run(
            ["findmnt", "-n", "-o", "SOURCE,FSTYPE,OPTIONS", "--target", "/var/lib/couchliteos"],
            text=True, capture_output=True, check=False, timeout=3,
        )
        output = result.stdout
    except (OSError, subprocess.SubprocessError):
        output = ""
    return "" if state_is_persistent(output) else LIVE_WARNING


def bluetooth_summary() -> str:
    try:
        adapter = bluetooth.BluetoothClient().snapshot().get("adapter")
    except bluetooth.BluetoothError:
        return "BLUETOOTH STATUS UNAVAILABLE"
    if not isinstance(adapter, dict):
        return "NO BLUETOOTH ADAPTER"
    return "BLUETOOTH ON" if adapter.get("powered") else "BLUETOOTH OFF"


def display_summary() -> str:
    try:
        output = display.active_output(display.query_outputs())
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return "DISPLAY STATUS UNAVAILABLE"
    if output is None or output.current_mode is None:
        return "NO ACTIVE DISPLAY"
    return f"{output.name}  {output.current_mode.argument}"


def audio_summary() -> str:
    try:
        result = subprocess.run(
            ["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
            text=True, capture_output=True, check=False, timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return "AUDIO STATUS UNAVAILABLE"
    return result.stdout.strip().upper()[:96] if result.returncode == 0 else "AUDIO STATUS UNAVAILABLE"


def controller_summary() -> str:
    identity = DATA / "launcher-controller.id"
    try:
        value = identity.read_text(encoding="ascii").strip()
    except OSError:
        return "NO CONTROLLER IDENTITY SAVED"
    return f"CONTROLLER DETECTED  {value[:64]}"


def configuration_summary(name: str) -> str:
    root = DATA / "home" / ".config"
    try:
        configured = any(name in path.name.casefold() for path in root.iterdir())
    except OSError:
        configured = False
    return f"{name.upper()} {'CONFIGURATION FOUND' if configured else 'NOT CONFIGURED'}"


def tailscale_summary() -> str:
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"], text=True, capture_output=True,
            check=False, timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return "TAILSCALE DISCONNECTED"
    return "TAILSCALE CONNECTED" if result.returncode == 0 else "TAILSCALE DISCONNECTED"


def add_centered(screen: curses.window, row: int, text: str, attr: int = 0) -> None:
    height, width = screen.getmaxyx()
    if not 0 <= row < height or width < 2:
        return
    clipped = text[: max(0, width - 4)]
    column = max(1, (width - len(clipped)) // 2)
    try:
        screen.addstr(row, column, clipped, attr)
    except curses.error:
        pass


def draw_border(screen: curses.window) -> None:
    height, width = screen.getmaxyx()
    if height < 8 or width < 24:
        return
    try:
        screen.border(
            ord("|"), ord("|"), ord("-"), ord("-"),
            ord("+"), ord("+"), ord("+"), ord("+"),
        )
    except curses.error:
        pass


class Launcher:
    def __init__(self, screen: curses.window) -> None:
        self.screen = screen
        self.selected = 0
        self.live_warning = live_mode_warning()
        self.summary = network_summary()
        self._status = self.summary
        self.message_since: float | None = None
        self.last_status_update = time.monotonic()
        self.applications: tuple[apps.Application, ...] = ()
        self.menu: list[tuple[str, str]] = []
        self.pending_stream: tuple[str, str] | None = None  # (target, app) while an auto-stream starts
        self.woke_up = False  # set by on_resume; ACTIVE APPLICATIONS closes on it
        self.stream_by_hand = False  # True while the STREAM row (not the auto-stream) starts it: wording only
        self.stream_row: list[tuple[str, str]] = []  # [] or the STREAM <PC> row, first in self.menu
        self.last_stream_check = time.monotonic()
        # Checked on every start (the USB stick moves between PCs) and again after a resume.
        self.can_sleep = power.can_suspend()
        self.controllers = controllers.Monitor()
        self.pcstatus = pcstatus.Monitor()
        self.padcheck = padcheck.Monitor()
        self.updates = update.Checker()
        # Buttons on error screens (couchliteos_errors). A handler is called with the
        # application that failed (or None); it returns True to ask for another try.
        # register_failure_actions() adds the WAKE PC entry at start-up.
        self.failure_actions: dict[str, Callable[[apps.Application | None], bool]] = {
            errors.NETWORK.id: self.open_network_settings,
            errors.SUPPORT.id: self.save_support_file,
            errors.BLUETOOTH.id: self.open_bluetooth_settings,
            errors.ACTIVE.id: self.open_active_applications,
        }
        # Idle sleep needs something that can wake the box again; without it the saved timeout is ignored.
        self.can_wake = bool(power.wake_sources())
        self.reload_applications()
        global IDLE_GUARD
        IDLE_GUARD = self.idle = power.IdleGuard(
            power.effective_settings(power.load_settings(), self.can_sleep, self.can_wake),
            apps_running=self.apps_running,
            request_sleep=self.request_sleep,
            resumed=self.check_resume,
            home_pending=lambda: HOME_REQUEST.exists(),
            clear_home=lambda: HOME_REQUEST.unlink(missing_ok=True),
            # The QEMU smoke tests must never be blanked or suspended (checked live: the
            # fw_cfg flag may appear after the launcher starts).
            enabled=lambda: not power.smoke_test_active(),
            passive_keys=(-1, curses.KEY_RESIZE),
        )

    @property
    def status(self) -> str:
        return self._status

    @status.setter
    def status(self, value: str) -> None:
        # Whatever sets the status is reporting a result or an error. run() keeps it
        # on screen until a button press or STATUS_HOLD_SECONDS (refresh_status).
        self._status = value
        self.message_since = time.monotonic()

    def release_status(self) -> None:
        if self.message_since is not None:
            self._status, self.message_since = self.summary, None

    def refresh_status(self) -> None:
        now = time.monotonic()
        if self.message_since is not None and now - self.message_since >= STATUS_HOLD_SECONDS:
            self.release_status()
        if self.message_since is None and now - self.last_status_update >= 5:
            self.summary = self._status = network_summary()
            self.last_status_update = now

    def reload_applications(self) -> None:
        result = application_result()
        self.applications = tuple(
            app for app in result.applications if app.visible and app.enabled
        )
        self.stream_row = self.stream_row_items()
        self.last_stream_check = time.monotonic()
        self.menu = self.stream_row + [
            (f"{app.name}  [{SHORTCUT_TAGS[app.shortcut]}]" if app.shortcut else app.name, app.id)
            for app in self.applications
        ] + self.fixed_controls()
        self.selected = min(self.selected, max(0, len(self.menu) - 1))
        if result.errors:
            self.status = f"{len(result.errors)} INVALID APPLICATION(S) SKIPPED"

    def stream_row_items(self) -> list[tuple[str, str]]:
        """The STREAM <PC> row, only with Moonlight enabled and a paired PC to stream to (the one
        Settings > STREAMING chose, else the only one). Anything unreadable means no row."""
        try:
            host = stream.autostream_host(stream.load_hosts(), stream.load_settings())
            if host is None or self.app_by_id("moonlight") is None:
                return []
        except (OSError, ValueError):
            return []
        return [(f"STREAM {host.label}"[:STREAM_ROW_MAX].rstrip(), STREAM_ACTION)]

    def refresh_stream_row(self) -> None:
        """Follow pairing changes (Moonlight pairs in its own window): add or drop the STREAM row,
        keeping the same row selected. Looks at most every STREAM_ROW_CHECK_SECONDS."""
        now = time.monotonic()
        if now - self.last_stream_check < STREAM_ROW_CHECK_SECONDS:
            return
        self.last_stream_check = now
        row = self.stream_row_items()
        if row == self.stream_row:
            return
        self.menu[:len(self.stream_row)] = row
        self.selected = max(0, self.selected + len(row) - len(self.stream_row))
        self.stream_row = row
        self.selected = min(self.selected, max(0, len(self.menu) - 1))

    def fixed_controls(self) -> list[tuple[str, str]]:
        """SETTINGS, SLEEP, REBOOT, SHUTDOWN; SLEEP names its reason when this PC cannot suspend."""
        return [
            (SLEEP_UNSUPPORTED, action) if action == "suspend" and not self.can_sleep else (label, action)
            for label, action in FIXED_CONTROLS
        ]

    def refresh_sleep_support(self) -> None:
        """Ask again whether this PC can suspend; the SLEEP control and the auto-sleep timer follow."""
        self.can_sleep = power.can_suspend()
        self.can_wake = bool(power.wake_sources())
        del self.menu[len(self.stream_row) + len(self.applications):]
        self.menu += self.fixed_controls()
        self.idle.apply(power.effective_settings(power.load_settings(), self.can_sleep, self.can_wake))

    def prepare_session(self) -> None:
        RUN.mkdir(mode=0o750, parents=True, exist_ok=True)
        display_name = os.environ.get("DISPLAY", ":0")
        wayland = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
        if not display_name.startswith(":") or "/" in display_name or "/" in wayland:
            raise RuntimeError("Cage supplied an invalid display environment")
        (RUN / "session.env").write_text(
            f"DISPLAY={display_name}\nWAYLAND_DISPLAY={wayland}\n", encoding="utf-8"
        )
        os.chmod(RUN / "session.env", 0o640)
        set_launcher_focus(False)

    def request(self, name: str) -> None:
        (RUN / name).touch()

    def apps_running(self) -> bool:
        try:
            return bool(self.running_applications())
        except Exception:  # noqa: BLE001 - a failed check must not sleep the box or crash the launcher
            return True

    def request_sleep(self) -> None:
        if not self.can_sleep:
            self.status = "SLEEP IS NOT SUPPORTED ON THIS PC"
            return
        # couchliteos-suspend.path removes the file before suspending.
        try:
            self.request("suspend")
        except OSError as error:
            self.status = f"COULD NOT REQUEST SLEEP: {error}"
            return
        self.status = "GOING TO SLEEP"

    def check_resume(self) -> bool:
        """True once after the system wakes (couchliteos-resume.service drops the marker)."""
        marker = RUN / "resumed"
        if not marker.exists():
            return False
        marker.unlink(missing_ok=True)
        self.on_resume()
        return True

    def on_resume(self) -> None:
        """Called once when the launcher sees the system resume from sleep."""
        (RUN / "suspend").unlink(missing_ok=True)  # a stale request must not suspend again
        HOME_REQUEST.unlink(missing_ok=True)  # the long press that slept us also asked for Home
        self.selected = 0  # not on SLEEP, where the user left it: the first A would sleep the box again
        self.refresh_sleep_support()
        self.status = "RESUMED FROM SLEEP"
        self.last_status_update = time.monotonic()
        focus_launcher()
        self.woke_up = True  # the Guide hold that slept the box opened ACTIVE APPLICATIONS; wake to the main menu
        self.autostream(countdown=15)  # the chosen PC streams again after a wake-up; no-op unless Settings > STREAMING asks

    def draw(self) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        draw_border(self.screen)
        title_row = max(2, height // 8)
        add_centered(self.screen, title_row, "COUCHLITEOS")
        add_centered(self.screen, title_row + 1, self.pcstatus.line())

        available = self.updates.available()
        labels = [
            f"{label}{UPDATE_SUFFIX}" if available and action == "settings" else label
            for label, action in self.menu
        ]
        selected = self.selected
        first_row = max(title_row + 3, height // 3)
        footer = self.footer_lines()
        # The status line is always on screen (it carries results and errors); the footer
        # lines sit directly below it, so each one past the first moves it up a row.
        status_row = height - 4 - max(0, len(footer) - 1)
        head = len(self.stream_row)  # the STREAM row, if any, is its own group above the apps
        after_apps = head + len(self.applications)
        gap_before = sorted({after_apps, after_apps + 1} | ({head} if head else set()))
        if len(labels) + len(gap_before) <= status_row - 1 - first_row:
            # Everything fits: blank rows separate the apps, SETTINGS and the power buttons.
            selected += sum(1 for at in gap_before if at <= self.selected)
            for at in reversed(gap_before):
                labels.insert(at, "")
        menu_left = max(2, (width - max(len(label) for label in labels) - 3) // 2)
        listview.draw_rows(self.screen, labels, selected, first_row, status_row - 1, menu_left)
        add_centered(self.screen, status_row, self.status)
        for offset, (text, attr) in enumerate(reversed(footer)):
            add_centered(self.screen, height - 3 - offset, text, attr)
        self.screen.refresh()

    def offer_update(self) -> None:
        """Once per release, ask on the Home screen whether to open SOFTWARE UPDATE.

        Only while nothing runs and the launcher is in front: never over an app or a stream."""
        version = self.updates.to_offer()
        if not version or HOME_REQUEST.exists() or (RUN / "app-active").exists() or self.any_app_running():
            return
        if (RUN / "osk-active").exists() or (RUN / "start-osk").exists():
            return  # the on-screen keyboard is up or on its way
        guard = IDLE_GUARD
        if guard is not None:
            if guard.timer.blanked:
                return  # the screen is blank: ask when someone is looking
            guard.keep_awake()  # the question is activity: no blanking or sleep while it waits
        self.updates.mark_offered(version)  # before the question: a crash cannot make it come back
        if confirmation.confirm(self.screen, f"COUCHLITEOS {version} IS AVAILABLE. OPEN SOFTWARE UPDATE NOW?"):
            Settings(self.screen, self).run_software_update()
        self.draw()

    def footer_lines(self) -> list[tuple[str, int]]:
        """Extra home-screen lines below the status line: (text, curses attribute)."""
        lines: list[tuple[str, int]] = []
        if self.live_warning:
            lines.append((self.live_warning, curses.A_NORMAL))
        available = self.updates.notice()
        if available:
            lines.append((available, curses.A_BOLD))
        battery = self.controllers.line()
        if battery:
            lines.append((battery, curses.A_REVERSE if self.controllers.low() else curses.A_NORMAL))
        lines += [] if battery else self.padcheck.footer(curses.A_BOLD)  # NO CONTROLLER FOUND
        return lines

    def draw_launching(self, label: str, frame: str) -> None:
        self.screen.erase()
        height, _width = self.screen.getmaxyx()
        draw_border(self.screen)
        add_centered(self.screen, max(2, height // 8), "COUCHLITEOS")
        center = max(6, height // 2 - 1)
        add_centered(self.screen, center, f"STARTING {label}  {frame}")
        add_centered(self.screen, center + 2, "PLEASE WAIT")
        add_centered(self.screen, height - 3, "HOLD SELECT+START (VIEW+MENU) TO COME BACK TO THE LAUNCHER")
        self.screen.refresh()

    def wake_from_failure(self, _app: apps.Application | None = None) -> bool:
        self.wake_pc()  # its own messages when no PC or address is known
        return False  # back on the error screen, where TRY AGAIN is one press away

    def open_network_settings(self, _app: apps.Application | None = None) -> bool:
        self.launch_by_id("network-setup")  # the same screen the setup wizard uses
        return False  # back on the error screen, where TRY AGAIN is one press away

    def save_support_file(self, _app: apps.Application | None = None) -> bool:
        Settings(self.screen, self).generate_support_file()
        return False

    def open_bluetooth_settings(self, _app: apps.Application | None = None) -> bool:
        bluetooth.run_bluetooth(self.screen)
        return False

    def open_active_applications(self, app: apps.Application | None = None) -> bool:
        """ACTIVE APPLICATIONS from an error screen; True (go on) once no other Remote Desktop session is left."""
        self.active_applications()
        session = active_rdp_session()
        return session is None or (app is not None and session == app.connection)

    def show_failure(self, failure: errors.Failure, app: apps.Application | None = None) -> str:
        """Show an error screen with its buttons; returns "retry" or "dismiss".

        The other buttons run their handler (see `failure_actions`) and bring the
        screen back, unless the handler asks for another try by returning True.
        """
        set_launcher_focus(True)  # the dialog is on top of a failed app: keep the controller on it
        if not controllers.bluetooth_present():
            failure = failure.without_bluetooth()  # nothing to open on a PC with no adapter
        while True:
            choice = errors.show(self.screen, failure, read_key=read_key)
            if choice in ("retry", errors.DISMISS.id):
                return choice
            handler = self.failure_actions.get(choice)
            if handler is not None and handler(app):
                return "retry"

    def show_launch_failure(
        self, label: str, message: str, app: apps.Application | None = None, *, retry: bool = True
    ) -> str:
        online = network_summary().endswith("  ONLINE")
        failure = errors.describe_failure(
            label, message, app_id=app.id if app else "", app_kind=app.kind if app else "",
            online=online, retry=retry,
        )
        return self.show_failure(failure, app)

    def draw_wait(self, title: str, detail: str, hint: str) -> None:
        self.screen.erase()
        height, _width = self.screen.getmaxyx()
        draw_border(self.screen)
        add_centered(self.screen, max(2, height // 8), "COUCHLITEOS")
        center = max(6, height // 2 - 1)
        add_centered(self.screen, center, title)
        add_centered(self.screen, center + 2, detail)
        add_centered(self.screen, height - 3, hint)
        self.screen.refresh()

    @staticmethod
    def pressed(key: int) -> bool:
        return key not in (-1, curses.KEY_RESIZE)

    def wake_host(self, host: stream.Host, *, force: bool = False, hint: str = "PRESS ANY BUTTON TO STOP WAITING") -> str:
        """Wake a sleeping gaming PC and wait up to 90 s; any key or button stops the wait.

        `hint` is shown under the wait; it must say what stopping the wait leads to for the caller.

        Returns stream.wake_and_wait's result, or "nonetwork" without waiting when no link is up."""
        if not stream.link_up():
            return "nonetwork"

        def tick(elapsed: float) -> bool:
            self.draw_wait(
                f"WAKING {host.label}...  {SPINNER[int(elapsed * 4) % len(SPINNER)]}",
                f"{int(elapsed)} OF {int(stream.WAKE_TIMEOUT)} SECONDS",
                hint,
            )
            key = self.screen.getch()
            if HOME_REQUEST.exists():  # the Guide/Home button leaves a file instead of a key
                HOME_REQUEST.unlink(missing_ok=True)
                went_home.append(True)
                return True
            return self.pressed(key)

        went_home: list[bool] = []
        self.screen.timeout(100)
        try:
            result = stream.wake_and_wait(host, force=force, tick=tick)
        finally:
            self.screen.timeout(1000)
        return "home" if went_home and result == "cancelled" else result

    def wake_before_moonlight(self, app: apps.Application, auto: bool = False) -> bool:
        """Wake the gaming PC if it is asleep; False when Moonlight must not start.

        A button during the wait starts Moonlight at once, except for an auto-stream (`auto`),
        where it cancels and leaves the user in the launcher.
        The Home button always cancels. A failure to wake never blocks a launch by hand: Moonlight
        opens with the reason on the status line. An auto-stream to a PC that stayed silent for
        the whole wait, or that is on while Sunshine is not answering, would only fail, so that
        is explained and offered WAKE PC instead."""
        try:
            host = stream.default_host(stream.load_hosts(), stream.load_settings())
            if host is None or not host.mac:
                return True
            result = self.wake_host(
                host,
                hint=(
                    f"PRESS ANY BUTTON TO CANCEL {'THE STREAM' if self.stream_by_hand else 'AUTO-STREAM'}"
                    if auto else "PRESS ANY BUTTON TO START MOONLIGHT WITHOUT WAITING"
                ),
            )
            if result == "nonetwork":
                self.draw_wait(stream.NO_NETWORK, "STARTING MOONLIGHT WITHOUT WAKING THE PC", "")
                self.screen.timeout(2000)
                self.screen.getch()
                self.screen.timeout(1000)
        except (OSError, ValueError, curses.error, subprocess.SubprocessError):
            return True
        if result == "home" or (auto and result == "cancelled"):
            self.status = f"{self.stream_word} CANCELLED" if auto else "MOONLIGHT NOT STARTED"
            return False
        if result in ("timeout", "awake") and not auto:
            # Opened by hand: Moonlight still lists the PC and can wake it or pick another one.
            self.draw_wait(StreamingSettings.WAKE_RESULTS[result].format(host.label), "STARTING MOONLIGHT ANYWAY", "")
            self.screen.timeout(2000)
            self.screen.getch()
            self.screen.timeout(1000)
            return True
        if result in ("timeout", "awake"):
            # A Moonlight failure screen carries WAKE PC (register_failure_actions); TRY AGAIN waits again.
            message = StreamingSettings.WAKE_RESULTS[result].format(host.label)
            if self.show_launch_failure(app.name, message, app=app) == "retry":
                return self.wake_before_moonlight(app, auto)
            return False
        return True

    def wake_pc(self) -> None:
        StreamingSettings(self.screen, self).wake_pc()

    def wake_message(self, host: stream.Host) -> str:
        """Wake `host` and say how it went, in the words of SETTINGS > STREAMING > WAKE PC."""
        blocked = stream.wake_block(host)
        if blocked:
            return blocked
        try:
            result = self.wake_host(host, force=True)
        except OSError as error:
            return f"WAKE FAILED: {error}".upper()
        return StreamingSettings.WAKE_RESULTS.get(result, "{}").format(host.label)

    def wait_screen(self, title: str, seconds: float, until=None) -> str:
        """A countdown any key or button cancels: "done" (`until()` turned true), "cancelled", or "timeout"."""
        end = time.monotonic() + seconds
        self.screen.timeout(100)
        try:
            try:
                curses.flushinp()  # keys typed before the countdown must not cancel it
            except curses.error:
                pass
            while True:
                if until is not None and until():
                    return "done"
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return "timeout"
                self.draw_wait(title, f"{int(remaining) + 1} SECONDS", "PRESS ANY BUTTON TO CANCEL")
                if self.pressed(self.screen.getch()) or HOME_REQUEST.exists():
                    HOME_REQUEST.unlink(missing_ok=True)
                    return "cancelled"
        finally:
            self.screen.timeout(1000)

    def autostream(self, countdown: float = 5) -> bool:
        """Start the chosen PC's stream (Settings > STREAMING) after a cancellable `countdown` seconds.

        Runs once setup is complete and nothing else is running; also meant to be called
        after the system resumes from sleep, with a longer countdown (the TV and the pad
        need a few seconds to come back). Any button cancels the countdown, and also the wait
        for a sleeping PC to wake. Returns True when the stream was started."""
        try:
            config = stream.load_settings()
            if not config.autostart or not setup.MARKER.exists() or (RUN / "app-active").exists():
                return False
            host = stream.autostream_host(stream.load_hosts(), config)
        except (OSError, ValueError):
            return False
        if host is None:
            self.status = "STREAMING: THE SAVED PC IS NOT PAIRED ON THIS SYSTEM"
            return False
        if not stream.link_up():
            result = self.wait_screen("WAITING FOR THE NETWORK...", 10, until=stream.link_up)
            if result != "done":
                self.status = "AUTO-STREAM CANCELLED" if result == "cancelled" else f"{stream.NO_NETWORK}; AUTO-STREAM SKIPPED"
                return False
        if self.wait_screen(f"STARTING STREAM TO {host.label}...", countdown) != "timeout":
            self.status = "AUTO-STREAM CANCELLED"
            return False
        return self.start_stream(host, config.app)

    @property
    def stream_word(self) -> str:
        return "STREAM" if self.stream_by_hand else "AUTO-STREAM"

    def start_stream(self, host: stream.Host, app_name: str, *, by_hand: bool = False) -> bool:
        """Start Moonlight streaming `app_name` from `host`, for the auto-stream and the STREAM row.

        `by_hand` (the row) only changes the wording of the wake wait and its failures."""
        app = self.app_by_id("moonlight")
        if app is None:
            self.status = "MOONLIGHT IS UNAVAILABLE"
            return False
        request = RUN / stream.STREAM_REQUEST.name
        self.pending_stream = (host.target, app_name)
        self.stream_by_hand = by_hand
        try:
            try:
                stream.write_stream_request(host.target, app_name, request)
            except (OSError, ValueError) as error:
                self.status = f"{self.stream_word} NOT STARTED: {error}".upper()
                return False
            started = self.launch_app(app, auto=True)
        finally:
            self.pending_stream = None
            self.stream_by_hand = False
            request.unlink(missing_ok=True)
        if started:
            try:
                recent.record(recent.host_key(host), app_name)  # the home screen's GAMES row shows it first
            except (OSError, ValueError):
                pass  # the order is a convenience: never a reason to fail the stream
        return started

    def stream_selected_pc(self) -> bool:
        """The STREAM <PC> row: stream the PC and application chosen in Settings > STREAMING, no countdown."""
        try:
            config = stream.load_settings()
            host = stream.autostream_host(stream.load_hosts(), config)
        except (OSError, ValueError):
            host = None
        if host is None:
            self.status = f"NO PC TO STREAM TO: {stream.PAIR_FIRST}"
            self.last_stream_check = float("-inf")  # the row is out of date: drop it now
            self.refresh_stream_row()
            return False
        return self.start_stream(host, config.app, by_hand=True)

    @staticmethod
    def read_app_status(app_id: str) -> str:
        path = RUN / f"{app_id}-status"
        try:
            if path.is_symlink() or path.stat().st_size > 512:
                return ""
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return lines[0][:240] if lines else ""

    def launch_app(self, app: apps.Application, *, quiet: bool = False, auto: bool = False, wake: bool = True) -> bool:
        """Start an application. `quiet` skips the failure dialog for a hidden one-off
        application whose caller explains a failed start in its own words. `auto` marks an
        auto-stream; `wake=False` skips waking the default PC (pairing may be for another one)."""
        label, app_id = app.name, app.status_id
        ready = RUN / f"{app_id}-ready"
        if ready.exists():
            if self.focus_app(app):
                self.status = f"RESUMED {label}"
                return True
            self.status = f"{label} IS RUNNING BUT HAS NO WINDOW: CLOSE IT UNDER SETTINGS > ACTIVE APPLICATIONS"
            return False
        if app.kind == "command":
            # couchliteos-configured-app.service runs one app at a time; a request
            # queued behind it would start unasked when the running app closes.
            other = next((item for item in self.running_applications()
                          if item.kind == "command" and item.id != app.id), None)
            if other is not None:
                self.show_launch_failure(
                    label, f"{other.name} IS STILL RUNNING. CLOSE IT FIRST: PRESS HOME, THEN CLOSE IT "
                           "IN ACTIVE APPLICATIONS."
                )
                self.status = f"{label} NOT STARTED: {other.name} IS RUNNING"
                return False
        state = RUN / f"{app_id}-status"
        if app.kind == "rdp" and not RemoteDesktopSettings(self.screen, self).prepare_launch(app):
            return False
        if app.id == "moonlight" and wake and not self.wake_before_moonlight(app, auto):
            return False
        if auto and self.pending_stream is not None:
            # Written again after the wake wait (it can outlast REQUEST_MAX_AGE) and before each
            # TRY AGAIN: the Moonlight start consumes the request.
            try:
                stream.write_stream_request(*self.pending_stream, RUN / stream.STREAM_REQUEST.name)
            except (OSError, ValueError) as error:
                self.status = f"{self.stream_word} NOT STARTED: {error}".upper()
                return False
        ready.unlink(missing_ok=True)
        state.unlink(missing_ok=True)
        POINTER_MODES.apply(app)
        set_launcher_focus(False)  # the starting app takes the controller
        if app.kind == "request":
            self.request(app.request)
        elif app.kind == "rdp":
            rdp.write_session_request(app.connection, RUN / rdp.REQUEST.name)
        else:
            apps.atomic_write(RUN / "launch-app.request", app.id + "\n")

        deadline = time.monotonic() + 18
        failure_since: float | None = None
        frame = 0
        self.screen.timeout(100)
        try:
            while time.monotonic() < deadline:
                self.draw_launching(label, SPINNER[frame % len(SPINNER)])
                frame += 1
                if ready.exists():
                    self.status = f"{label} STARTED"
                    return True

                app_state = self.read_app_status(app_id)
                now = time.monotonic()
                if app_state.startswith("exited:"):
                    # It ran and quit on its own before the ready mark (e.g. nmtui).
                    self.status = f"{label} EXITED"
                    return True
                if app_state.startswith("failed:"):
                    if failure_since is None:
                        failure_since = now
                    # App units retry after two seconds. A persistent failure for
                    # longer than that means retries have not recovered startup.
                    if now - failure_since >= 2.75:
                        if not quiet and self.show_launch_failure(label, app_state.removeprefix("failed:").strip(), app=app) == "retry":
                            return self.launch_app(app, quiet=quiet, auto=auto, wake=wake)
                        self.status = f"{label} FAILED TO START"
                        return False
                else:
                    failure_since = None
                read_key(self.screen)  # permits curses to process resize/input state
        finally:
            self.screen.timeout(1000)

        last_state = self.read_app_status(app_id)
        if app.kind == "rdp" and not (RUN / rdp.SESSION.name).exists():
            # The session never picked up the request: do not leave it (or a
            # typed password) waiting in /run for a later start.
            (RUN / rdp.REQUEST.name).unlink(missing_ok=True)
            (RUN / rdp.HANDOFF.name).unlink(missing_ok=True)
        elif app.kind == "request":
            (RUN / app.request).unlink(missing_ok=True)
        elif app.kind == "command":
            # Never leave a request behind to start the app unasked later.
            (RUN / "launch-app.request").unlink(missing_ok=True)
        message = (
            last_state.removeprefix("failed:").strip()
            if last_state.startswith("failed:")
            else "THE APPLICATION DID NOT BECOME READY BEFORE THE STARTUP TIMEOUT"
        )
        if not quiet and self.show_launch_failure(label, message, app=app) == "retry":
            return self.launch_app(app, quiet=quiet, auto=auto, wake=wake)
        self.status = f"{label} START TIMED OUT"
        return False

    def app_by_id(self, app_id: str) -> apps.Application | None:
        return next(
            (app for app in application_result().applications if app.id == app_id and app.enabled),
            None,
        )

    def launch_by_id(self, app_id: str) -> bool:
        app = self.app_by_id(app_id)
        if app is None:
            self.status = f"{app_id.upper()} IS NOT AVAILABLE: CHECK SETTINGS > APPLICATIONS"
            return False
        return self.launch_app(app)

    @staticmethod
    def focus_app(app: apps.Application) -> bool:
        matches = {
            "firefox": ("app_id:firefox-esr", "app_id:firefox", "title:Mozilla Firefox"),
            "google-chrome": ("app_id:google-chrome", "title:Google Chrome"),
            "moonlight": ("app_id:moonlight", "title:Moonlight"),
            "chiaki-ng": ("app_id:chiaki", "app_id:io.github.streetpea.Chiaki4deck", "title:Chiaki"),
        }.get(app.id)
        if matches is None:
            matches = (f"title:{app.name}",)
            if app.kind == "command" and not app.terminal:
                # Windows of user-added apps (Chrome kiosk pages, Steam, ...) are not
                # titled with the stored name, but their app_id follows the binary.
                binary = pathlib.PurePath(app.command).name
                ids = dict.fromkeys((binary.removesuffix("-stable"), binary))
                matches = tuple(f"app_id:{item}" for item in ids) + matches
        if app.kind == "rdp":
            matches = (f"app_id:{rdp.WAYLAND_APP_ID}", f"title:{app.name}")
        for match in matches:
            try:
                result = subprocess.run(
                    ["wlrctl", "toplevel", "focus", match], check=False,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
                )
            except subprocess.TimeoutExpired:
                continue  # one slow answer is not "no window": try the next match
            except (OSError, subprocess.SubprocessError):
                return False
            if result.returncode == 0:
                POINTER_MODES.apply(app)
                mark_front_app(app)
                set_launcher_focus(False)
                return True
        keyword = STREAM_WINDOW_WORDS.get(app.id)
        if keyword is not None and Launcher.focus_listed_toplevel(keyword):
            POINTER_MODES.apply(app)
            mark_front_app(app)
            set_launcher_focus(False)
            return True
        return False

    @staticmethod
    def focus_listed_toplevel(keyword: str) -> bool:
        """A stream window the exact matches missed: take it from what the compositor lists, by whatever
        app id or title it really has. A miss writes the list to the journal, so the names show up there."""
        try:
            listing = subprocess.run(
                ["wlrctl", "toplevel", "list"], check=False, capture_output=True, text=True, timeout=2,
            ).stdout or ""
        except (OSError, subprocess.SubprocessError):
            return False
        lines = [line.strip() for line in listing.splitlines() if line.strip()]
        windows = [line.partition(":")[::2] for line in lines  # wlrctl prints "app_id: title"
                   if "couchliteos" not in line.lower()]
        windows = [(app_id.strip(), title.strip()) for app_id, title in windows]
        # A window whose app id names the client; failing that one with no app id whose title does
        # (never another program's window that merely mentions it, such as a browser tab).
        for by_app_id in (True, False):
            for app_id, title in windows:
                if keyword not in (app_id if by_app_id else (title if not app_id else "")).lower():
                    continue
                for match in (f"app_id:{app_id}" if app_id else "", f"title:{title}" if title else ""):
                    if not match:
                        continue
                    try:
                        result = subprocess.run(
                            ["wlrctl", "toplevel", "focus", match], check=False,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
                        )
                    except (OSError, subprocess.SubprocessError):
                        continue
                    if result.returncode == 0:
                        return True
        # The launcher's own terminal is the screen: the journal is not reachable from here, a log file is.
        display.log(f"no window for {keyword}; the compositor lists: {lines!r}",
                    pathlib.Path("/var/log/couchliteos/launcher.log"))
        return False

    @staticmethod
    def any_app_running() -> bool:
        """True when any app or stream shows a sign of life in /run/couchliteos, or when that cannot be told."""
        try:
            return any(item.name != "launcher-ready" for item in RUN.glob("*-ready"))
        except Exception:
            return True  # not knowing means do not restart

    def running_applications(self) -> list[apps.Application]:
        running = [
            app for app in application_result().applications
            if app.enabled and (RUN / f"{app.status_id}-ready").exists()
        ]
        session = active_rdp_session()
        if session and session not in {app.id for app in running} and (RUN / f"{session}-ready").exists():
            connection = rdp.get_connection(session)
            if connection is not None:
                running.append(rdp_application(connection))
        return running

    @staticmethod
    def volume_row() -> str:
        try:
            volume = audio.get_volume()
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return "VOLUME  UNAVAILABLE"
        return f"VOLUME  {volume.percent}%{'  MUTED' if volume.muted else ''}"

    @staticmethod
    def brightness_row() -> str | None:
        """None on PCs without a backlight this user can change (most desktops and TVs)."""
        percent = brightness.get_percent()
        return None if percent is None else f"BRIGHTNESS  {percent}%"

    def type_into(self, app: apps.Application) -> str | None:
        """Bring `app` to the front and open the on-screen keyboard over it; the text is typed
        into the app when the keyboard closes. None when it worked, else what went wrong."""
        if not self.focus_app(app):
            return f"COULD NOT FOCUS {app.name}: PRESS {CLOSE_BUTTON} TO CLOSE IT, THEN START IT AGAIN"
        request_osk()
        self.status = f"TYPING INTO {app.name}"
        return None

    def active_applications(self) -> None:
        """The Guide / Home menu: resume or close apps, type into one, change the volume and the
        screen brightness, and turn the controller mouse on or off, without closing what is running."""
        selected = 0
        status = ""
        HOME_REQUEST.unlink(missing_ok=True)  # a Guide press made before this screen opened
        OSK_REFOCUSED.unlink(missing_ok=True)
        set_launcher_focus(True)  # the pad drives this menu even with an app (or its mouse) running
        self.woke_up = False
        while True:
            running = self.running_applications()
            front = next((app for app in running if app.id == POINTER_MODES.front), running[0] if running else None)
            rows = [f"{app.name:<32} RUNNING" for app in running]
            volume_index = len(rows)
            rows.append(self.volume_row())
            brightness_text = self.brightness_row()
            brightness_index = len(rows) if brightness_text else -1
            if brightness_text:
                rows.append(brightness_text)
            mouse_index = len(rows) if front else -1
            if front:
                on = "ON" if POINTER_MODES.enabled(front) else "OFF"
                rows.append(f"{'CONTROLLER MOUSE (' + front.name + ')':<32} {on}")
            rows.append("RETURN TO MAIN LAUNCHER")
            selected = min(selected, len(rows) - 1)
            self.screen.erase()
            height, width = self.screen.getmaxyx()
            draw_border(self.screen)
            add_centered(self.screen, max(2, height // 8), "ACTIVE APPLICATIONS")
            first = max(5, height // 3)
            left = max(2, (width - max(map(len, rows), default=1) - 3) // 2)
            for index, row in enumerate(rows):
                marker = ">" if index == selected else " "
                try:
                    self.screen.addnstr(first + index, left, f"{marker}  {row}", width - left - 1)
                except curses.error:
                    pass
            if selected == volume_index:
                hint = ("LEFT / RIGHT CHANGES THE VOLUME  ·  A / CROSS MUTES", "")
            elif selected == brightness_index:
                hint = ("LEFT / RIGHT CHANGES THE SCREEN BRIGHTNESS", "")
            elif selected == mouse_index:
                hint = ("A / CROSS TURNS IT ON OR OFF: LEFT STICK POINTS, A CLICKS, RIGHT STICK SCROLLS", "")
            elif running:
                hint = (f"A / CROSS RESUMES  ·  {CLOSE_BUTTON} CLOSES  ·  B / CIRCLE BACK",
                        "X (XBOX) / TRIANGLE (PS) OPENS THE KEYBOARD AND TYPES INTO THE APP")
            else:
                hint = ("NO MANAGED APPLICATIONS ARE RUNNING", "")
            add_centered(self.screen, height - 4, "" if status else hint[1])
            add_centered(self.screen, height - 3, status or hint[0])
            self.screen.refresh()
            key = read_key(self.screen, keyboard=False)
            if self.woke_up:  # the box slept and woke while this menu was open
                self.woke_up = False
                return
            if HOME_REQUEST.exists():  # Guide again while the menu is open closes it
                HOME_REQUEST.unlink(missing_ok=True)
                return
            if OSK_REFOCUSED.exists():
                # Home was pressed while the keyboard was up; closing it typed into the app and
                # put that app back in front, so the controller goes back to it too.
                OSK_REFOCUSED.unlink(missing_ok=True)
                set_launcher_focus(False)
                return
            if key != -1:
                status = ""
            selected = move_selection(selected, key, len(rows))
            if key == 27 or (key in (curses.KEY_ENTER, 10, 13) and selected == len(rows) - 1):
                return
            if key == curses.KEY_F12:
                target = running[selected] if selected < len(running) else front
                if target is None:
                    # Typed into this menu, letters would move the selection and Enter pick a row.
                    status = "NO APP IS RUNNING TO TYPE INTO"
                    continue
                problem = self.type_into(target)
                if problem is None:
                    return
                status = problem
                continue
            if selected == volume_index:
                try:
                    if key in (curses.KEY_LEFT, curses.KEY_RIGHT):
                        audio.change_volume(-audio.VOLUME_STEP if key == curses.KEY_LEFT else audio.VOLUME_STEP)
                    elif key in (curses.KEY_ENTER, 10, 13):
                        audio.toggle_mute()
                except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                    status = f"VOLUME NOT CHANGED: {error}".upper()
                continue
            if selected == brightness_index:
                if key in (curses.KEY_LEFT, curses.KEY_RIGHT):
                    try:
                        brightness.change(-brightness.STEP if key == curses.KEY_LEFT else brightness.STEP)
                    except OSError as error:
                        status = f"BRIGHTNESS NOT CHANGED: {error.strerror or error}".upper()
                continue
            if selected == mouse_index:
                if key in (curses.KEY_ENTER, 10, 13, curses.KEY_LEFT, curses.KEY_RIGHT):
                    on = POINTER_MODES.toggle(front)
                    status = f"CONTROLLER MOUSE {'ON' if on else 'OFF'} FOR {front.name}"
                continue
            if selected >= len(running):
                continue
            app = running[selected]
            if key in (curses.KEY_ENTER, 10, 13):
                if self.focus_app(app):
                    self.status = f"RESUMED {app.name}"
                    return
                status = f"COULD NOT FOCUS {app.name}: PRESS {CLOSE_BUTTON} TO CLOSE IT, THEN START IT AGAIN"
            elif key in (curses.KEY_DC, ord("x")):
                (RUN / f"close-{app.status_id}").touch()
                status = f"CLOSING {app.name}"

    def activate(self) -> None:
        _label, action = self.menu[self.selected]
        app = next((item for item in self.applications if item.id == action), None)
        if action == STREAM_ACTION:
            self.stream_selected_pc()
        elif app:
            self.launch_app(app)
        elif action == "settings":
            Settings(self.screen, self).run()
            self.reload_applications()
        elif action in {"reboot", "poweroff"}:
            question = "REBOOT NOW?" if action == "reboot" else "SHUT DOWN NOW?"
            running = ", ".join(item.name for item in self.running_applications())
            if running:
                question += f" {running} WILL BE CLOSED."
            if confirmation.confirm(self.screen, question):
                self.status = "REBOOTING..." if action == "reboot" else "SHUTTING DOWN..."
                self.request(action)
        elif action == "suspend":
            # A PC that cannot sleep gets the explanation from request_sleep, not a question.
            # Without a wake source (Settings > SLEEP lists them) only the power button wakes it.
            question = "SLEEP NOW?" if self.can_wake else "SLEEP NOW? NOTHING CONNECTED CAN WAKE THIS PC: USE ITS POWER BUTTON TO WAKE IT."
            if not self.can_sleep or confirmation.confirm(self.screen, question):
                self.request_sleep()

    def setup_wizard(self, *, force: bool = False, resume: bool = False) -> None:
        self.wizard_picture_changed = False
        actions = {
            "text": self.wizard_text,
            "display": self.wizard_display,
            "picture_saved": self.wizard_picture_saved,
            "tone": lambda: self.launch_and_wait("audio-test"),
            "launch": self.launch_and_wait,
            "pair_moonlight": self.pair_moonlight,
            "wake_pc": self.wake_message,
            "browser": self.browser_setup,
            "applications": Settings(self.screen, self).run_applications,
        }
        tv_control = getattr(self, "tv_control_screen", None)
        if callable(tv_control):
            actions["tv"] = tv_control
        statuses = {
            "chiaki-ng": lambda: configuration_summary("chiaki"),
            "tailscale": tailscale_summary,
            "applications": lambda: f"{len(application_result().applications)} APPLICATIONS CONFIGURED",
        }
        setup.SetupWizard(
            setup_ui(self.screen), actions, setup.System(),
            bluetooth_client=bluetooth.BluetoothClient(), statuses=statuses,
        ).run(force=force, resume=resume)
        HOME_REQUEST.unlink(missing_ok=True)  # setup takes no Home request, as with Guide: drop a leftover one
        self.reload_applications()

    def browser_setup(self) -> bool | None:
        """ADD A WEB BROWSER (setup and Settings > APPLICATIONS): True once one was installed there."""
        def keep_awake() -> None:  # a long download must not blank or sleep the box
            if IDLE_GUARD is not None:
                IDLE_GUARD.keep_awake()

        try:
            return browsersetup.show(self.screen, read_key=read_key, keep_awake=keep_awake)
        except Exception:  # noqa: BLE001 - a broken screen must not take the launcher down
            self.status = "COULD NOT OPEN ADD A WEB BROWSER"
            return False
        finally:
            self.reload_applications()

    def wizard_text(self, title: str, prompt: str, limit: int, *, masked: bool = False) -> str | None:
        request_osk(masked)
        return ApplicationsSettings(self.screen, self).text_input(title, prompt, limit, masked=masked)

    def wizard_display(self, resolution: str, refresh_mhz: int) -> bool:
        """Offer a mode through the existing 15 second preview; True only if it was confirmed."""
        settings = Settings(self.screen, self)
        settings.in_wizard = True  # setup restarts for a new picture size itself, after saving its progress
        if not settings.refresh_outputs():
            return False
        before = settings.output.current_mode if settings.output is not None else None
        settings.resolution, settings.refresh_mhz = resolution, refresh_mhz
        settings.apply_preview()
        # Settings overwrites its status text after a rollback, so compare the real mode instead.
        current = settings.output.current_mode if settings.output is not None else None
        confirmed = current is not None and (current.resolution, current.refresh_mhz) == (resolution, refresh_mhz)
        if confirmed and (before is None or before.resolution != resolution):
            self.wizard_picture_changed = True
        return confirmed

    def wizard_picture_saved(self) -> None:
        """Setup saved its picture answer: after a new picture size, restart so foot picks a text
        size that fits it, and setup carries on at sound. A running app or the rate limit means no restart."""
        if not getattr(self, "wizard_picture_changed", False):
            return
        self.wizard_picture_changed = False
        if self.any_app_running() or not claim_screen_restart("reopen-setup"):
            return
        setup_ui(self.screen).status("DISPLAY AND SOUND", [PICTURE_RESTART_MESSAGE])
        time.sleep(1)  # long enough to read before the screen goes dark
        sys.exit(0)

    def launch_and_wait(
        self, app_id: str, *, lines: list[str] | None = None, big: str | None = None,
        patience: float | None = None, quiet: bool = False,
    ) -> bool:
        """Start an application and return when it exits (HOME shows the running applications).

        With `patience` the application is closed after that many seconds or when ESC is pressed,
        for one-off commands that have no window of their own to close.
        """
        app = self.app_by_id(app_id)
        # Setup and pairing: never wake the default PC first, the one being paired may be another.
        options = {"quiet": True, "wake": False} if quiet else {"wake": False}
        if app is None or not self.launch_app(app, **options):
            return False
        ready = RUN / f"{app.status_id}-ready"
        ui = setup_ui(self.screen)
        shown = lines or [f"{app.name} IS OPEN.", "CLOSE IT OR PRESS THE HOME BUTTON WHEN YOU ARE DONE."]
        started = time.monotonic()
        closing_since: float | None = None
        while ready.exists():
            if HOME_REQUEST.exists():
                HOME_REQUEST.unlink(missing_ok=True)
                self.active_applications()
                continue
            ui.status("SETUP", shown, big=big)
            key = read_key(self.screen)
            if closing_since is not None:
                if time.monotonic() - closing_since > 8:
                    break
            elif patience is not None and (key == 27 or time.monotonic() - started >= patience):
                (RUN / f"close-{app.status_id}").touch()
                closing_since = time.monotonic()
        return True

    def pair_moonlight(self, host: str, pin: str) -> bool:
        """Run `moonlight pair` with the wizard's PIN as a hidden one-off application."""
        appdir = "/opt/couchliteos/apps/moonlight"
        app = apps.Application(
            id="moonlight-pair", name="MOONLIGHT PAIRING", kind="command",
            command=f"{appdir}/usr/bin/moonlight", arguments=setup.moonlight_pair_arguments(host, pin),
            status_id="moonlight-pair", visible=False,
            environment={"QT_QPA_PLATFORM": "xcb", "APPDIR": appdir, "LD_LIBRARY_PATH": f"{appdir}/usr/lib"},
        )
        try:
            apps.write_user_application(app)
        except (OSError, apps.ManifestError) as error:
            self.status = f"COULD NOT START PAIRING: {error}"
            return False
        try:
            return self.launch_and_wait(
                app.id, big=pin, patience=180, quiet=True,
                lines=[
                    f"ON THE GAMING PC OPEN HTTPS://{setup.sunshine_web_address(host)}, CLICK THE PIN TAB AND TYPE:",
                    "THIS SCREEN CLOSES BY ITSELF WHEN PAIRING ENDS. B OR ESC CANCELS.",
                ],
            )
        finally:
            try:
                apps.delete_user_application(app.id)
            except (OSError, apps.ManifestError):
                pass

    def run(self) -> None:
        self.prepare_session()
        curses.curs_set(0)
        self.screen.keypad(True)
        self.screen.timeout(1000)
        try:
            curses.use_default_colors()
        except curses.error:
            pass

        self.controllers.start()
        self.pcstatus.start()
        self.updates.start()
        self.draw()
        (RUN / "launcher-ready").touch()
        if display.restore_saved_mode() is None:
            self.status = "SAVED DISPLAY MODE SKIPPED — CHOOSE IT AGAIN IN SETTINGS > DISPLAY"
            self.last_status_update = time.monotonic() + 25  # keep it up for 30 s
        self.draw()
        whatsnew.show_once(self.screen, lambda: read_key(self.screen))  # before the wizard: only upgraders see it
        if (RUN / "reopen-setup").exists():  # restarted for a new picture size during setup
            (RUN / "reopen-setup").unlink(missing_ok=True)
            self.setup_wizard(resume=True)
        else:
            self.setup_wizard()
        controls.show_once(self.screen)
        if (RUN / "reopen-display").exists():  # restarted to apply SCREEN EDGES / TEXT SIZE
            (RUN / "reopen-display").unlink(missing_ok=True)
            Settings(self.screen, self).run_display()
        else:
            self.autostream()
        self.draw()
        while True:
            key = read_key(self.screen)
            if key not in (-1, curses.KEY_RESIZE):
                self.release_status()  # a press dismisses the message that was being held
            if HOME_REQUEST.exists() or key == curses.KEY_HOME:
                HOME_REQUEST.unlink(missing_ok=True)
                set_launcher_focus(True)  # gamepad-nav forwards keys while an app runs
                self.active_applications()
                self.draw()
                continue
            if key == -1:  # idle: nobody is mid-press
                self.offer_update()
            if not (self.selected == 0 and key in (curses.KEY_UP, ord("k"))):  # never wrap up onto SHUTDOWN
                self.selected = move_selection(self.selected, key, len(self.menu))
            if key in (curses.KEY_ENTER, 10, 13):
                self.activate()
            elif key in SHORTCUT_KEYS:
                app = next((item for item in self.applications if item.shortcut == SHORTCUT_KEYS[key]), None)
                if app is None:
                    self.status = f"NO BUTTON USES THE {SHORTCUT_TAGS[SHORTCUT_KEYS[key]]} SHORTCUT"
                else:
                    self.launch_app(app)
            elif key == curses.KEY_RESIZE:
                pass

            self.refresh_status()
            self.refresh_stream_row()
            self.draw()


class Settings:
    def __init__(self, screen: curses.window, launcher: Launcher) -> None:
        self.screen = screen
        self.launcher = launcher
        self.selected = 0
        self.status = ""
        self.output: display.Output | None = None
        self.original_mode: display.Mode | None = None
        self.resolution = ""
        self.refresh_mhz = 0
        self.in_wizard = False  # setup's own restart handles a new picture size there

    def refresh_outputs(self) -> bool:
        try:
            current = display.active_output(display.query_outputs())
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            self.status = f"DISPLAY QUERY FAILED: {error}. CHECK THE CABLE AND OPEN DISPLAY SETTINGS AGAIN"
            return False
        if current is None or current.current_mode is None:
            self.status = "NO ACTIVE DISPLAY: CHECK THE HDMI OR DISPLAYPORT CABLE AND THE TV INPUT"
            return False
        self.output = current
        self.original_mode = current.current_mode
        self.resolution = current.current_mode.resolution
        self.refresh_mhz = current.current_mode.refresh_mhz
        self.status = f"CURRENT: {self.resolution} @ {current.current_mode.refresh} HZ"
        return True

    def resolutions(self) -> list[str]:
        if self.output is None:
            return []
        return list(dict.fromkeys(mode.resolution for mode in self.output.modes))

    def native_resolution(self) -> str:
        if self.output is None:
            return ""
        return next((mode.resolution for mode in self.output.modes if mode.preferred), "")

    def refresh_rates(self) -> list[int]:
        if self.output is None:
            return []
        return list(
            dict.fromkeys(
                mode.refresh_mhz
                for mode in self.output.modes
                if mode.resolution == self.resolution
            )
        )

    def draw(
        self, title: str = "SETTINGS", rows: list[str] | None = None, selected: int | None = None, hint: str = "",
    ) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if height >= 8 and width >= 24:
            try:
                self.screen.border()
            except curses.error:
                pass
        add_centered(self.screen, max(2, height // 8), title)
        if rows is None:
            rows = [self.menu_label(item) for item in SETTINGS_MENU]
            selected = self.selected
        left = max(2, (width - max((len(item) for item in rows), default=1) - 3) // 2)
        listview.draw_rows(self.screen, rows, selected, max(5, height // 3), height - 4, left)
        add_centered(self.screen, height - 3, self.status or hint)
        self.screen.refresh()

    def menu_label(self, item: str) -> str:
        if item == "CHECK FOR UPDATES":
            return f"{item}  {'ON' if self.launcher.updates.enabled else 'OFF'}"
        if item == "SOFTWARE UPDATE" and self.launcher.updates.available():
            return f"{item}  -  {self.launcher.updates.available()} AVAILABLE"
        return item

    def toggle_updates(self) -> None:
        enabled = not self.launcher.updates.enabled
        self.launcher.updates.set_enabled(enabled)
        if not enabled:
            self.status = "UPDATE CHECK OFF: NOTHING IS SENT"
        elif not self.launcher.updates.online():
            self.status = "UPDATE CHECK ON: WAITS UNTIL THIS PC IS ONLINE"
        else:
            self.status = "UPDATE CHECK ON: LOOKS FOR A NEWER RELEASE AT EVERY START"

    def choose(self, title: str, choices: list[tuple[str, object]], current: object) -> object | None:
        if not choices:
            self.status = "NO ADVERTISED CHOICES"
            return None
        selected = next((index for index, item in enumerate(choices) if item[1] == current), 0)
        while True:
            self.draw(title, [label for label, _value in choices], selected)
            key = read_key(self.screen)
            selected = move_selection(selected, key, len(choices))
            if key in (curses.KEY_ENTER, 10, 13):
                return choices[selected][1]
            if key == 27:
                return None

    def show_message(self, title: str, message: str) -> None:
        while True:
            _height, width = self.screen.getmaxyx()
            rows = textwrap.wrap(message, width=max(8, width - 8)) or [""]
            self.status = f"{DONE_HINT} RETURNS TO SETTINGS"
            self.draw(title, rows, None)
            if read_key(self.screen) in (curses.KEY_ENTER, 10, 13, 27):
                return

    def show_error(
        self, title: str, message: str, *, retry: bool = True, hint: str = "", actions: tuple[errors.Action, ...] = ()
    ) -> bool:
        """An error with buttons (TRY AGAIN when `retry`, then BACK); True when TRY AGAIN was chosen."""
        failure = errors.simple_failure(title, message, *actions, hint=hint, retry=retry)
        return self.launcher.show_failure(failure) == "retry"

    def draw_support_progress(
        self,
        destination: support.Destination,
        frame: int,
        message: str,
        elapsed: float,
    ) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        draw_border(self.screen)
        title_row = max(2, height // 8)
        add_centered(self.screen, title_row, "EXPORTING SUPPORT FILE")

        stage_rows = textwrap.wrap(
            (message or "COLLECTING SUPPORT INFORMATION").upper(),
            width=max(8, width - 8),
        )[:2]
        stage_row = max(title_row + 3, height // 2 - 4)
        spinner = SPINNER[frame % len(SPINNER)]
        for index, row in enumerate(stage_rows):
            prefix = f"{spinner}  " if index == 0 else ""
            add_centered(self.screen, stage_row + index, prefix + row)

        bar_width = min(50, max(8, width - 12))
        add_centered(
            self.screen,
            stage_row + len(stage_rows) + 1,
            indeterminate_progress_bar(bar_width, frame),
        )

        destination_rows = textwrap.wrap(
            f"USB: {destination.display_name}", width=max(8, width - 8)
        )[:2]
        destination_row = stage_row + len(stage_rows) + 3
        for index, row in enumerate(destination_rows):
            add_centered(self.screen, destination_row + index, row)
        add_centered(
            self.screen,
            destination_row + len(destination_rows) + 1,
            f"ELAPSED {format_elapsed(elapsed)}",
        )
        add_centered(self.screen, height - 3, "PLEASE WAIT - DO NOT REMOVE USB DRIVE")
        self.screen.refresh()

    def rollback(self, old_output: display.Output, old_mode: display.Mode) -> None:
        try:
            validated = display.valid_output_mode(old_output.name, old_output.identity, old_mode)
            if not validated:
                display.log("Rollback skipped because the original display or mode disappeared")
                self.status = "DISPLAY CHANGED; COMPOSITOR DEFAULT RETAINED"
                return
            display.apply_mode(validated[0], validated[1], dryrun=True)
            validated = display.valid_output_mode(old_output.name, old_output.identity, old_mode)
            if not validated:
                raise RuntimeError("original display changed after rollback dry-run")
            display.apply_mode(*validated)
            display.log(f"Rolled back preview on {old_output.name} to {old_mode.argument}")
            self.status = "DISPLAY MODE ROLLED BACK"
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            display.log(f"Display rollback failed: {error}")
            self.status = f"ROLLBACK FAILED: {error}. IF THE PICTURE IS WRONG, REBOOT"

    def apply_preview(self) -> None:
        if self.output is None or self.original_mode is None:
            self.status = "NO ACTIVE DISPLAY MODE: GO BACK AND OPEN DISPLAY SETTINGS AGAIN"
            return
        requested = display.Mode(
            *map(int, self.resolution.split("x")), self.refresh_mhz
        )
        old_output, old_mode = self.output, self.original_mode
        try:
            validated = display.valid_output_mode(old_output.name, old_output.identity, requested)
            if not validated:
                raise RuntimeError("selected output or advertised mode is no longer available")
            display.apply_mode(validated[0], validated[1], dryrun=True)
            validated = display.valid_output_mode(old_output.name, old_output.identity, requested)
            if not validated:
                raise RuntimeError("display changed after dry-run")
            display.apply_mode(*validated)
            display.log(f"Previewing {requested.argument} on {old_output.name}")
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            display.log(f"Display preview rejected: {error}")
            self.status = f"MODE NOT APPLIED: {error}. PICK ANOTHER RESOLUTION OR REFRESH RATE"
            return

        deadline = time.monotonic() + 15
        old_timeout = 250
        self.screen.timeout(old_timeout)
        confirmed = False
        # Presses queued while the mode switched (a double-tapped A) never saw this
        # screen, and a saved mode is reapplied on every boot: drop them, and make
        # the safe choice the default.
        curses.flushinp()
        selected = 0
        while time.monotonic() < deadline:
            seconds = max(1, int(deadline - time.monotonic() + 0.999))
            self.status = f"SELECT KEEP TO SAVE; OTHERWISE REVERTS IN {seconds}S"
            self.draw(f"CONFIRM DISPLAY MODE {requested.argument}", ["REVERT", "KEEP THIS MODE"], selected)
            key = read_key(self.screen)
            selected = move_selection(selected, key, 2)
            if key in (curses.KEY_ENTER, 10, 13):
                confirmed = selected == 1
                break
            if key == 27:
                break
        self.screen.timeout(1000)

        if not confirmed:
            self.rollback(old_output, old_mode)
            self.refresh_outputs()
            return
        self.finish_preview(old_output, old_mode, requested)

    def finish_preview(self, old_output: display.Output, old_mode: display.Mode, requested: display.Mode) -> None:
        try:
            validated = display.valid_output_mode(old_output.name, old_output.identity, requested)
            if not validated or validated[0].current_mode is None:
                raise RuntimeError("display disappeared before confirmation")
            current = validated[0].current_mode
            if (current.width, current.height, current.refresh_mhz) != (
                requested.width,
                requested.height,
                requested.refresh_mhz,
            ):
                raise RuntimeError("compositor did not retain the requested mode")
            try:
                display.save_display(validated[0], requested)
            except (OSError, RuntimeError) as error:
                # The user confirmed a working mode: keep it for this session.
                display.log(f"Confirmed {requested.argument} but could not save it: {error}")
                self.refresh_outputs()
                reason = getattr(error, "strerror", None) or str(error)
                self.status = f"MODE APPLIED BUT NOT SAVED: {reason.upper()[:60]}"
                return
            display.log(f"Confirmed and saved {requested.argument} on {old_output.name}")
            self.status = "DISPLAY MODE CONFIRMED AND SAVED"
            self.refresh_outputs()
            if (requested.width, requested.height) != (old_mode.width, old_mode.height) and not self.in_wizard:
                self.restart_for_picture_size()
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            display.log(f"Display confirmation failed: {error}")
            self.rollback(old_output, old_mode)

    def generate_support_file(self) -> None:
        try:
            destinations = support.discover_destinations()
        except (OSError, subprocess.SubprocessError) as error:
            failure = f"USB DESTINATION CHECK FAILED: {error}"
            self.status = failure
            if self.show_error("SUPPORT EXPORT FAILED", failure, hint="RECONNECT THE USB DRIVE, THEN TRY AGAIN."):
                return self.generate_support_file()
            return
        if not destinations:
            failure = support.no_destination_message()  # says whether no drive or only the boot drive is plugged in
            self.status = "SUPPORT FILE NOT SAVED: PLUG IN A USB DRIVE AND TRY AGAIN"
            if self.show_error("USB DRIVE NOT FOUND", failure):
                return self.generate_support_file()
            return
        destination = destinations[0]
        if len(destinations) > 1:
            selected = self.choose(
                "SELECT SUPPORT DESTINATION",
                [(item.display_name, item) for item in destinations],
                destination,
            )
            if selected is None:
                self.status = "SUPPORT EXPORT CANCELLED"
                return
            destination = selected
        try:
            request_id = support.submit_request(destination)
        except OSError as error:
            failure = f"COULD NOT START THE SUPPORT EXPORT: {error}"
            self.status = f"EXPORT FAILED: {error}"
            if self.show_error("SUPPORT EXPORT FAILED", failure):
                return self.generate_support_file()
            return

        started_at = time.monotonic()
        start_deadline = started_at + SUPPORT_EXPORT_START_TIMEOUT
        deadline = started_at + SUPPORT_EXPORT_TIMEOUT
        frame = 0
        message = "WAITING FOR EXPORT SERVICE"
        self.screen.timeout(SUPPORT_EXPORT_POLL_MS)
        try:
            while True:
                now = time.monotonic()
                state = support.read_status(request_id)
                if state:
                    export_state = state.get("state", "")
                    if export_state == "success":
                        destination_name = state.get("destination", "") or destination.display_name
                        result_message = state.get("message", "") or "SUPPORT FILE CREATED"
                        self.screen.timeout(1000)
                        self.show_message(
                            "SUPPORT FILE CREATED",
                            f"{result_message}. SAVED: {destination_name}",
                        )
                        self.status = f"SAVED: {destination_name}"
                        return
                    if export_state == "failed":
                        failure = state.get("message", "") or "THE EXPORTER REPORTED AN UNKNOWN FAILURE"
                        self.screen.timeout(1000)
                        self.status = f"EXPORT FAILED: {failure}"
                        if self.show_error("SUPPORT EXPORT FAILED", failure):
                            return self.generate_support_file()
                        return
                    if export_state == "working":
                        message = state.get("message", "") or "COLLECTING SUPPORT INFORMATION"
                elif now >= start_deadline:
                    failure = (
                        "THE EXPORT SERVICE DID NOT REPORT STARTUP. THE FILE WAS NOT CREATED. "
                        "REBOOT COUCHLITEOS AND TRY AGAIN; IF IT FAILS AGAIN, RUN SYSTEM DIAGNOSTICS."
                    )
                    self.screen.timeout(1000)
                    self.status = "EXPORT FAILED: SERVICE DID NOT START - REBOOT, THEN TRY AGAIN"
                    self.show_error("SUPPORT EXPORT FAILED", failure, retry=False)  # the message says reboot first
                    return

                if now >= deadline:
                    failure = (
                        "THE EXPORT DID NOT REPORT COMPLETION WITHIN 3 MINUTES. "
                        "DO NOT REMOVE THE USB DRIVE WHILE ITS ACTIVITY LIGHT IS FLASHING. "
                        "REBOOT COUCHLITEOS BEFORE TRYING AGAIN."
                    )
                    self.screen.timeout(1000)
                    self.status = "SUPPORT EXPORT TIMED OUT - REBOOT BEFORE TRYING AGAIN"
                    self.show_error("SUPPORT EXPORT TIMED OUT", failure, retry=False)
                    return

                self.draw_support_progress(destination, frame, message, now - started_at)
                frame += 1
                read_key(self.screen)  # process resize/input state; export cannot be cancelled safely
        finally:
            self.screen.timeout(1000)

    def launch(self, app_id: str) -> None:
        """Start an app and show the launcher's result (e.g. UNAVAILABLE) on this screen."""
        self.launcher.launch_by_id(app_id)
        self.status = self.launcher.status

    def run_network(self) -> None:
        text = netmenu.open(self.screen, self.launcher.wizard_text,
                            lambda: self.launch("network-setup"), network_summary)
        if text:
            self.status = text

    def run_software_update(self) -> None:
        def keep_awake() -> None:  # a long download must not blank or sleep the box
            if IDLE_GUARD is not None:
                IDLE_GUARD.keep_awake()

        try:
            softwareupdate.show(
                self.screen, read_key=read_key, apps_running=self.launcher.apps_running, keep_awake=keep_awake,
                record=self.launcher.updates.record)
        except Exception:  # noqa: BLE001 - a broken screen must not take the launcher down
            self.status = "COULD NOT OPEN SOFTWARE UPDATE"

    def activate(self) -> bool:
        actions = {
            "DISPLAY": self.run_display,
            "APPEARANCE": self.run_appearance,
            "AUDIO": self.run_audio,
            "BLUETOOTH": lambda: bluetooth.run_bluetooth(self.screen),
            "CONTROLLERS": lambda: pads.run(self.screen, read_key),
            "NETWORK": self.run_network,
            "SLEEP & SCREEN": self.run_sleep_settings,
            "APPLICATIONS": self.run_applications,
            "REMOTE DESKTOP": self.run_remote_desktop,
            "STREAMING": self.run_streaming,
            "ACTIVE APPLICATIONS": self.launcher.active_applications,
            "TAILSCALE": lambda: self.launch("tailscale"),
            "TV CONTROL": self.run_tv_control,
            "SOFTWARE UPDATE": self.run_software_update,
            "CHECK FOR UPDATES": self.toggle_updates,
            "CONTROLS": self.run_controls,
            "SETUP WIZARD": lambda: self.launcher.setup_wizard(force=True),
            "GENERATE SUPPORT FILE": self.generate_support_file,
            "SYSTEM DIAGNOSTICS": lambda: self.launch("system-diagnostics"),
        }
        action = actions.get(SETTINGS_MENU[self.selected])
        if action is None:
            return False
        action()
        return True

    def run_display(self) -> None:
        self.refresh_outputs()
        selected = 0
        notice = ""  # what the last screen-edge or text-size change did, shown until the next press
        while True:
            refresh = f"{self.refresh_mhz / 1000:g} HZ" if self.refresh_mhz else "UNAVAILABLE"
            screen_settings = screenfit.load()
            rows = [
                f"RESOLUTION  {self.resolution or 'UNAVAILABLE'}", f"REFRESH RATE  {refresh}", "APPLY DISPLAY MODE",
                f"SCREEN EDGES  {screen_settings.edges}%", f"TEXT SIZE  {screen_settings.text.upper()}", "BACK",
            ]
            status = self.status
            self.status = notice or {3: SCREEN_EDGES_HELP, 4: TEXT_SIZE_HELP}.get(selected) or status
            self.draw("DISPLAY SETTINGS", rows, selected)
            self.status, notice = status, ""
            key = read_key(self.screen)
            selected = move_selection(selected, key, len(rows))
            if key == 27 or (key in (curses.KEY_ENTER, 10, 13) and selected == len(rows) - 1):
                return
            if key not in (curses.KEY_ENTER, 10, 13):
                continue
            if selected == 0:
                resolutions = self.resolutions()
                native = self.native_resolution()
                chosen = self.choose(
                    "RESOLUTION",
                    [(f"{item}  (NATIVE)" if item == native else item, item) for item in resolutions],
                    self.resolution,
                )
                if isinstance(chosen, str):
                    self.resolution = chosen
                    rates = self.refresh_rates()
                    if self.refresh_mhz not in rates and rates:
                        self.refresh_mhz = rates[0]
            elif selected == 1:
                rates = self.refresh_rates()
                chosen = self.choose(
                    "REFRESH RATE",
                    [(f"{value / 1000:g} HZ", value) for value in rates],
                    self.refresh_mhz,
                )
                if isinstance(chosen, int):
                    self.refresh_mhz = chosen
            elif selected == 2:
                self.apply_preview()
            elif selected == 3:
                self.status = SCREEN_CHOICE_HINT
                chosen = self.choose(
                    "SCREEN EDGES", [(f"{value}%", value) for value in screenfit.EDGE_CHOICES], screen_settings.edges
                )
                self.status = status
                if isinstance(chosen, int):
                    notice = self.change_screen_setting("edges", chosen, rows, selected)
            else:
                self.status = SCREEN_CHOICE_HINT
                chosen = self.choose(
                    "TEXT SIZE", [(value.upper(), value) for value in screenfit.TEXT_CHOICES], screen_settings.text
                )
                self.status = status
                if isinstance(chosen, str):
                    notice = self.change_screen_setting("text", chosen, rows, selected)

    def run_appearance(self) -> None:
        """THEME and ACCENT; a change recolours the launcher and the keyboard at once."""
        selected = 0
        notice = ""
        while True:
            available, problems = themes.available()
            name, accent = themes.load_choice()
            chosen = available.get(name) or available.get(themes.DEFAULT) or themes.FALLBACK
            rows = [f"THEME  {chosen.label}", f"ACCENT  {accent.upper() or 'THEME DEFAULT'}", "BACK"]
            status = self.status
            # A bad user theme is listed nowhere else: say so until something is chosen.
            self.status = notice or (f"SKIPPED {problems[0]}".upper()[:76] if problems else status)
            self.draw("APPEARANCE", rows, selected)
            self.status, notice = status, ""
            key = read_key(self.screen)
            selected = move_selection(selected, key, len(rows))
            if key == 27 or (key in ENTER_KEYS and selected == len(rows) - 1):
                return
            if key not in ENTER_KEYS:
                continue
            if selected == 0:
                value = self.choose("THEME", [(theme.label, theme.name) for theme in available.values()], chosen.name)
                if isinstance(value, str):
                    notice = self.change_theme(value, accent)
            else:
                choices = [("THEME DEFAULT", ""), *((label.upper(), label) for label, _colour in themes.ACCENTS)]
                value = self.choose("ACCENT", choices, accent)
                if isinstance(value, str):
                    notice = self.change_theme(chosen.name, value)

    def change_theme(self, name: str, accent: str) -> str:
        """Save the theme choice and recolour the running foot windows; returns what to tell the user."""
        try:
            themes.save_choice(name, accent)
        except OSError as error:
            return f"NOT SAVED: {error}".upper()
        try:
            themes.apply(themes.current())
        except OSError:
            return "SAVED: THE NEW COLOURS SHOW AFTER A RESTART"
        return ""

    def restart_for_picture_size(self) -> None:
        """foot sized its text for the old picture size: restart into DISPLAY so it fits the new one."""
        if self.launcher.any_app_running() or not claim_screen_restart("reopen-display"):
            self.status = PICTURE_NEXT_START_MESSAGE
            return
        self.status = PICTURE_RESTART_MESSAGE
        self.draw("DISPLAY SETTINGS", [], 0)
        time.sleep(1)  # long enough to read before the screen goes dark
        sys.exit(0)

    def change_screen_setting(self, field: str, value: object, rows: list[str], selected: int) -> str:
        """Save a screen-edge or text-size choice and apply it; returns what to tell the user.

        foot reads the setting as it starts, so applying means a restart: exit and let
        systemd bring cage, foot and the launcher back (Restart=always). That would
        kill any running app or stream, so then it only applies the next time.
        """
        current = screenfit.load()
        changed = dataclasses.replace(current, **{field: value})
        if changed == current:
            return ""
        try:
            screenfit.save(changed)
        except (OSError, ValueError) as error:
            return f"NOT SAVED: {error}".upper()
        if self.launcher.any_app_running() or not claim_screen_restart("reopen-display"):
            return SCREEN_NEXT_START_MESSAGE
        self.status = SCREEN_RESTART_MESSAGE
        self.draw("DISPLAY SETTINGS", rows, selected)
        time.sleep(1)  # long enough to read before the screen goes dark
        sys.exit(0)

    def run_audio(self) -> None:
        selected = 0
        result = ""  # outcome of the last change, shown on the redraw
        explained = False  # the "nothing to play on" screen is shown once, not on every redraw
        while True:
            failure: errors.Failure | None = None
            try:
                sinks = audio.query_sinks()
                # Outputs the sound card has only under another profile (the
                # speaker while a TV has HDMI, a second HDMI/DP port).
                others = audio.query_profile_outputs()
                rows = [f"{'*' if sink.default else ' '}  {sink.name}" for sink in sinks]
                rows += [f"   {output.name}" for output in others]
                if sinks:
                    try:
                        volume = audio.get_volume()
                        filled = volume.percent * 20 // 100
                        rows.append(f"VOLUME  [{'#' * filled}{'.' * (20 - filled)}]  {volume.percent}%")
                        rows.append(f"MUTE  {'ON' if volume.muted else 'OFF'}")
                    except (OSError, RuntimeError, subprocess.SubprocessError):
                        rows += ["VOLUME  UNAVAILABLE", "MUTE  UNAVAILABLE"]
                rows.append("BACK")
                self.status = result or (
                    "* IS THE CURRENT DEFAULT OUTPUT" if sinks else
                    "CHOOSE AN OUTPUT" if others else "NO AUDIO OUTPUTS AVAILABLE"
                )
                if not sinks and not others:
                    failure = errors.no_sound_output(bluetooth=controllers.bluetooth_present())
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                sinks = []
                others = []
                rows = ["BACK"]
                self.status = f"AUDIO QUERY FAILED: {error}"
                failure = errors.problem("AUDIO QUERY FAILED", str(error))
            if failure is None:
                explained = False
            elif not explained:
                explained = True
                if self.launcher.show_failure(failure) == "retry":
                    explained = False
                    continue
            selected = min(selected, len(rows) - 1)
            self.draw("AUDIO OUTPUT", rows, selected)
            key = read_key(self.screen)
            selected = move_selection(selected, key, len(rows))
            if key == 27 or (key in ENTER_KEYS and selected == len(rows) - 1):
                return
            if key not in (curses.KEY_LEFT, curses.KEY_RIGHT, *ENTER_KEYS):
                continue
            if selected < len(sinks) + len(others):
                if key not in ENTER_KEYS:
                    continue
                try:
                    if selected < len(sinks):
                        sink = sinks[selected]
                    else:
                        self.status = "SWITCHING OUTPUT..."
                        self.draw("AUDIO OUTPUT", rows, selected)
                        sink = audio.switch_to(others[selected - len(sinks)], {s.id for s in sinks})
                    audio.set_default(sink.id)
                    result = f"DEFAULT OUTPUT: {sink.name}"
                except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                    result = f"OUTPUT NOT CHANGED: {error}. TRY ANOTHER OUTPUT"
                    continue
                try:
                    note = audio.ensure_audible(sink.id)
                except (OSError, RuntimeError, subprocess.SubprocessError):
                    note = "VOLUME NOT CHECKED"
                if note:
                    result += f" ({note})"
                continue
            try:
                if selected == len(sinks) + len(others):
                    step = -audio.VOLUME_STEP if key == curses.KEY_LEFT else audio.VOLUME_STEP
                    result = f"VOLUME {audio.change_volume(step).percent}%"
                else:
                    result = "MUTED" if audio.toggle_mute().muted else "UNMUTED"
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                result = f"VOLUME NOT CHANGED: {error}"

    def run_controls(self) -> None:
        """The Home shortcut and pointer speeds (LEFT/RIGHT or A changes one), and the button help."""
        selected = 0
        notice = ""  # what the last change did, shown until the next press
        while True:
            settings = inputprefs.load_settings()
            rows = [*inputprefs.rows(settings), controls.TITLE, controls.KEYS_TITLE, "BACK"]
            field = inputprefs.FIELDS[selected] if selected < len(inputprefs.FIELDS) else ""
            status = self.status
            self.status = notice or inputprefs.HELP.get(field) or status
            self.draw("CONTROLS", rows, selected, CONTROLS_HINT)
            self.status, notice = status, ""
            key = read_key(self.screen)
            selected = move_selection(selected, key, len(rows))
            if key == 27 or (key in ENTER_KEYS and selected == len(rows) - 1):
                return
            if key in ENTER_KEYS and selected == len(rows) - 3:
                controls.show(self.screen)
            elif key in ENTER_KEYS and selected == len(rows) - 2:
                controls.show_keys(self.screen)
            elif field == "keyboard_home":
                if key in ENTER_KEYS:  # picked by pressing it, not cycled with LEFT/RIGHT
                    notice = self.capture_home_key()
            elif selected < len(inputprefs.FIELDS) and key in (curses.KEY_LEFT, curses.KEY_RIGHT, *ENTER_KEYS):
                step = -1 if key == curses.KEY_LEFT else 1
                _settings, notice = inputprefs.change(settings, inputprefs.FIELDS[selected], step)

    CAPTURE_HINT = "B / CIRCLE OR ESC: CANCEL  ·  Y / SQUARE: OFF  ·  X / TRIANGLE: RESET"
    CAPTURE_POLL_MS = 50
    CAPTURE_QUIET = 0.4  # seconds after a keyboard press before a pad button or lone Esc counts

    def draw_capture(self, lines: list[str], hint: str) -> None:
        self.screen.erase()
        height, _width = self.screen.getmaxyx()
        draw_border(self.screen)
        add_centered(self.screen, max(2, height // 8), "KEYBOARD HOME KEY")
        center = max(6, height // 2 - 2)
        for offset, line in enumerate(lines):
            add_centered(self.screen, center + 2 * offset, line)
        add_centered(self.screen, height - 3, hint)
        self.screen.refresh()

    def capture_home_key(self, source: KeyboardSource | None = None) -> str:
        """Settings > CONTROLS > KEYBOARD HOME KEY: hold the keys, let go, confirm. What to tell the user."""
        source = source or KeyboardSource()
        if not source.open():
            return "NO KEYBOARD FOUND"
        capture = inputprefs.ChordCapture()
        message = ""
        last_press = 0.0
        settings = inputprefs.load_settings()

        def take(events) -> str | None:
            """Feed the key events; the notice to leave with once a chord, Esc, Delete or F12 settles it."""
            nonlocal message, last_press
            for name, value in events:
                last_press = time.monotonic()
                chord = capture.feed(name, value)
                if chord is None:
                    continue
                if chord == {"KEY_ESC"}:
                    return "NOT CHANGED"
                if chord == {"KEY_DELETE"}:  # a keyboard's Y / SQUARE: its curses key is held back by the quiet period
                    return self.save_home_key(settings, "")
                if chord == {"KEY_F12"}:  # a keyboard's X / TRIANGLE
                    return self.save_home_key(settings, inputprefs.DEFAULT_CHORD)
                message = inputprefs.validate_chord(chord) or ""
                if not message:
                    return self.confirm_home_key(source, settings, chord)
            return None

        old_timeout = 1000  # the Settings screens' wait; a short one here so a release shows within a blink
        self.screen.timeout(self.CAPTURE_POLL_MS)
        try:
            while True:
                settings = inputprefs.load_settings()
                lines = ["HOLD THE KEYS YOU WANT, THEN LET GO", f"NOW: {inputprefs.chord_label(settings.keyboard_home)}"]
                self.draw_capture([*lines, message] if message else lines, self.CAPTURE_HINT)
                notice = take(source.poll(0.05))
                if notice is not None:
                    return notice
                key = read_key(self.screen, keyboard=False)
                # foot writes the ESC of an Alt chord before the key events were read: look again first
                notice = take(source.poll(0))
                if notice is not None:
                    return notice
                if getattr(source, "devices", [None]) == []:
                    return "KEYBOARD DISCONNECTED"
                if key == -1 or capture.held or time.monotonic() - last_press < self.CAPTURE_QUIET:
                    continue  # nothing, or bytes of a keyboard chord still arriving
                if key == 27:
                    return "NOT CHANGED"
                if key == curses.KEY_DC:
                    return self.save_home_key(settings, "")
                if key == curses.KEY_F12:
                    return self.save_home_key(settings, inputprefs.DEFAULT_CHORD)
        finally:
            self.screen.timeout(old_timeout)
            source.close()
            flush_input()
            HOME_REQUEST.unlink(missing_ok=True)  # the old key, pressed while capturing, asked for Home

    def confirm_home_key(self, source: KeyboardSource, settings: inputprefs.Settings, chord: frozenset[str]) -> str:
        text = inputprefs.canonical_chord(chord)
        label = inputprefs.chord_label(text)
        lines = [f"SET THE HOME KEY TO {label}?"]
        if "SUPER" in label.split("+"):
            lines.append("A STREAM'S REMOTE PC ALSO GETS THE SUPER KEY")
        until = time.monotonic() + self.CAPTURE_QUIET
        while time.monotonic() < until:  # the chord's own bytes arrive at curses now: drop them
            source.poll(0.05)
            read_key(self.screen, keyboard=False)
        while True:
            self.draw_capture(lines, "A / CROSS OR ENTER: YES  ·  B / CIRCLE OR ESC: NO")
            source.poll(0.05)
            key = read_key(self.screen, keyboard=False)
            if key in ENTER_KEYS:
                return self.save_home_key(settings, text)
            if key == 27:
                return "NOT CHANGED"

    @staticmethod
    def save_home_key(settings: inputprefs.Settings, text: str) -> str:
        try:
            inputprefs.save_settings(dataclasses.replace(settings, keyboard_home=text))
        except OSError as error:
            return f"COULD NOT SAVE: {error}".upper()
        return f"{inputprefs.ROW_KEYBOARD}  {inputprefs.chord_label(text)}"

    def run_sleep_settings(self) -> None:
        settings = power.load_settings()  # as saved: its sleep value may belong to another PC
        can_sleep = self.launcher.can_sleep
        wol, mac = power.wake_on_lan()
        wake_on_lan = {
            "supported": f"MAC {mac}",
            "unsupported": "NOT SUPPORTED BY THIS NETWORK ADAPTER",
            "none": "NO WIRED NETWORK ADAPTER",
            "unknown": "ADAPTER NOT CHECKED",
        }[wol]
        sources = power.wake_sources()
        can_wake = self.launcher.can_wake = bool(sources)
        wake_from = ", ".join(sources) or "NO USB BLUETOOTH ADAPTER OR KEYBOARD FOUND"

        def note() -> str:
            # A timeout is saved but not acted on: say so, on this screen, where it was set.
            return IDLE_SLEEP_NO_WAKE if can_sleep and not can_wake and settings.sleep else ""

        self.status = note()
        selected = 0
        while True:
            rows = [
                f"BLANK SCREEN AFTER  {power.minutes_label(settings.blank)}",
                f"SLEEP AFTER  {power.minutes_label(settings.sleep) if can_sleep else 'NOT SUPPORTED ON THIS PC'}",
                f"WAKE-ON-LAN  {wake_on_lan}",
                f"WAKE FROM  {wake_from}",
                "BACK",
            ]
            self.draw("SLEEP & SCREEN", rows, selected)
            key = read_key(self.screen)
            selected = move_selection(selected, key, len(rows))
            if key == 27 or (key in ENTER_KEYS and selected == 4):
                return
            if key not in ENTER_KEYS:
                continue
            if selected == 1 and not can_sleep:
                self.status = "SLEEP IS NOT SUPPORTED ON THIS PC"
                continue
            if selected in (2, 3):
                self.status = "INFORMATION ONLY"
                continue
            field, choices = ("blank", power.BLANK_CHOICES) if selected == 0 else ("sleep", power.SLEEP_CHOICES)
            chosen = self.choose(
                rows[selected].split("  ")[0],
                [(power.minutes_label(value), value) for value in choices],
                getattr(settings, field),
            )
            if chosen is None:
                continue
            settings = dataclasses.replace(settings, **{field: chosen})
            try:
                power.save_settings(settings)
                self.status = note()
            except OSError as error:
                self.status = f"COULD NOT SAVE: {error}"
            self.launcher.idle.apply(power.effective_settings(settings, can_sleep, can_wake))

    def run_applications(self) -> None:
        ApplicationsSettings(self.screen, self.launcher).run()
        self.launcher.reload_applications()

    def run_remote_desktop(self) -> None:
        RemoteDesktopSettings(self.screen, self.launcher).run()
        self.launcher.reload_applications()

    def run_streaming(self) -> None:
        StreamingSettings(self.screen, self.launcher).run()

    def run_tv_control(self) -> None:
        """HDMI-CEC: what the TV reports, and three switches that only work with a CEC adapter."""

        def ask_tv() -> cec.Status:
            self.status = ""
            self.draw("TV CONTROL", ["ASKING THE TV..."], None)
            return cec.query_status()

        status = ask_tv()
        settings = cec.load_settings()
        can_sleep = cec.suspend_supported()
        selected = 0
        while True:
            info = cec.status_lines(status)
            actions = (
                cec.toggle_rows(settings, status.usable, can_sleep) + ["REFRESH"]
                + ([cec.REMOTE_TEST_ROW] if status.adapter is not None else []) + ["BACK"]
            )
            selected = min(selected, len(actions) - 1)
            self.draw(
                "TV CONTROL", info + [""] + actions, len(info) + 1 + selected, hint=cec.row_hint(actions[selected]),
            )
            key = read_key(self.screen)
            selected = move_selection(selected, key, len(actions))
            if key == 27 or (key in ENTER_KEYS and actions[selected] == "BACK"):
                return
            if key not in ENTER_KEYS:
                continue
            if actions[selected] == "REFRESH":
                status = ask_tv()
            elif actions[selected] == cec.REMOTE_TEST_ROW:
                if status.usable:
                    self.run_remote_test()
                else:
                    self.status = "NOT AVAILABLE: TV NOT CONNECTED"
            elif not status.usable:
                self.status = "NOT AVAILABLE: " + ("NO CEC ADAPTER FOUND" if status.adapter is None else "TV NOT CONNECTED")
            elif selected in (1, 2) and not can_sleep:
                self.status = "NOT AVAILABLE: SUSPEND NOT SUPPORTED ON THIS PC"
            else:
                changed = cec.toggled(settings, selected)
                try:
                    cec.save_settings(changed)
                except OSError as error:
                    self.status = f"NOT SAVED: {error}"
                else:
                    settings, self.status = changed, "SAVED"

    def run_remote_test(self) -> None:
        """TEST REMOTE BUTTONS: name each key as the TV sends it; closes after 10 s of quiet or BACK twice."""
        test = cec.RemoteTest(time.monotonic())
        self.status = ""
        flush_input()
        self.screen.timeout(100)
        try:
            while True:
                now = time.monotonic()
                if test.done(now):
                    return
                self.draw(cec.REMOTE_TEST_ROW, test.rows(), None, hint=test.hint(now))
                key = read_key(self.screen)
                now = time.monotonic()
                if HOME_REQUEST.exists():  # HOME or MENU on the remote (and Guide) leave a file, not a key
                    HOME_REQUEST.unlink(missing_ok=True)
                    test.press(cec.HOME_KEY, 0, now)
                if key not in (-1, curses.KEY_RESIZE):
                    test.press(REMOTE_TEST_KEYS.get(key), key, now)
        finally:
            self.screen.timeout(1000)

    def run(self) -> None:
        self.refresh_outputs()
        while True:
            self.draw()
            key = read_key(self.screen)
            self.selected = move_selection(self.selected, key, len(SETTINGS_MENU))
            if key in (curses.KEY_ENTER, 10, 13) and not self.activate():
                return
            if key == 27:
                return


class ApplicationsSettings:
    def __init__(self, screen: curses.window, launcher: Launcher) -> None:
        self.screen = screen
        self.launcher = launcher
        self.selected = 0
        self.status = ""

    def result(self) -> apps.LoadResult:
        return application_result()

    hint = BUTTONS_HINT  # the footer when no message is showing; subclasses and callers may replace it

    def draw(self, title: str, rows: list[str], selected: int | None = None, hint: str = "") -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        draw_border(self.screen)
        add_centered(self.screen, max(2, height // 8), title)
        left = max(2, (width - max((len(row) for row in rows), default=1) - 3) // 2)
        listview.draw_rows(self.screen, rows, selected, max(5, height // 4), height - 4, left)
        add_centered(self.screen, height - 3, self.status or hint or self.hint)
        self.screen.refresh()

    def discard_changes(self) -> bool:
        """B was pressed on work in progress: YES throws it away, NO (the default) keeps editing."""
        return confirmation.confirm(self.screen, "DISCARD CHANGES?")

    def text_input(
        self, title: str, prompt: str, limit: int, *, initial: str = "", masked: bool = False,
        unsaved: bool = False,
    ) -> str | None:
        """`unsaved`: earlier answers of the same form are lost if this is cancelled, so ask first."""
        value = initial[:limit]
        previous_status, self.status = self.status, TEXT_HINT
        self.screen.timeout(-1)
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            while True:
                shown = ("*" * len(value) if masked else value)[-max(1, self.screen.getmaxyx()[1] - 12):] or "_"
                self.draw(title, [prompt, shown, "", PHONE_ROW], None)
                key = self.screen.get_wch()
                if isinstance(key, str):
                    if key in {"\n", "\r"}:
                        return value
                    if key == "\x1b":
                        if not unsaved or self.discard_changes():
                            return None
                    elif key in {"\b", "\x7f"}:
                        value = value[:-1]
                    elif key.isprintable() and key not in "\r\n" and len(value) < limit:
                        value += key
                elif key == curses.KEY_F12:
                    request_osk(masked)
                elif key in PHONE_KEYS:
                    typed = self.phone_input(title, prompt, limit, masked=masked)
                    if typed is not None:  # it fills the field; the user still confirms with A / Enter
                        value = typed[:limit]
                    self.screen.timeout(-1)
                elif key in (curses.KEY_BACKSPACE, curses.KEY_DC):  # Y/Square arrives as KEY_DC
                    value = value[:-1]
        finally:
            self.status = previous_status
            self.screen.timeout(1000)
            try:
                curses.curs_set(0)
            except curses.error:
                pass

    def phone_input(self, title: str, prompt: str, limit: int, *, masked: bool = False) -> str | None:
        """TYPE ON PHONE: show a QR code and URL until the phone sends the text, B cancels or it expires."""
        interfaces = phone.lan_interfaces()
        if not interfaces:
            self.status = "TYPE ON PHONE NEEDS A HOME NETWORK CONNECTION"
            return None
        try:
            session = phone.Session(
                str(interfaces[0].ip), time.monotonic, networks=[interface.network for interface in interfaces],
                title=title, prompt=prompt, limit=limit, masked=masked,
            )
        except OSError:
            self.status = "COULD NOT START TYPE ON PHONE"
            return None
        try:
            code = phone.qr_lines(session.url)
            self.screen.timeout(PHONE_POLL_MS)
            while True:
                typed = session.poll()
                if typed is not None:
                    self.status = TEXT_HINT
                    return typed
                if session.expired():
                    self.status = "TYPE ON PHONE TIMED OUT. PRESS SELECT (VIEW) OR F2 TO TRY AGAIN"
                    return None
                self.draw_phone(title, session.url, code)
                try:
                    key = self.screen.get_wch()
                except curses.error:  # no key before the poll timeout
                    continue
                if key in ("\x1b", 27):
                    self.status = TEXT_HINT
                    return None
        finally:
            session.close()

    def draw_phone(self, title: str, url: str, code: list[str]) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        draw_border(self.screen)
        add_centered(self.screen, 1, title)
        add_centered(self.screen, 2, "SCAN WITH A PHONE ON THE SAME NETWORK, OR OPEN:")
        add_centered(self.screen, 3, url)
        top = 5
        if len(code) <= height - top - 3 and code and max(len(line) for line in code) <= width - 4:
            column = max(1, (width - max(len(line) for line in code)) // 2)
            for offset, line in enumerate(code):
                try:
                    self.screen.addstr(top + offset, column, line)
                except curses.error:
                    pass
        add_centered(self.screen, height - 3, "THE TEXT FILLS THE FIELD; YOU STILL CONFIRM IT HERE")
        add_centered(self.screen, height - 2, PHONE_HINT)
        self.screen.refresh()

    def yes_no(self, title: str, prompt: str, *, unsaved: bool = False) -> bool | None:
        selected = 0
        while True:
            rows = [prompt, "YES", "NO"]
            self.draw(title, rows, selected + 1)
            key = read_key(self.screen)
            selected = move_selection(selected, key, 2)
            if key in (curses.KEY_ENTER, 10, 13):
                return selected == 0
            if key == 27 and (not unsaved or self.discard_changes()):
                return None

    def _write_user(self, app: apps.Application) -> None:
        system_dir = apps.SYSTEM_DIR if apps.SYSTEM_DIR.exists() else SOURCE_MANIFESTS
        apps.write_user_application(app, system_dir=system_dir)

    def add_command(self) -> None:
        name = self.text_input("ADD COMMAND APPLICATION", "NAME", apps.MAX_NAME)
        if not name:
            return
        command = self.text_input("ADD COMMAND APPLICATION", "ABSOLUTE COMMAND", apps.MAX_COMMAND, unsaved=True)
        if command is None:
            return
        arguments = self.text_input(
            "ADD COMMAND APPLICATION", "ARGUMENTS (OPTIONAL)", apps.MAX_ARGUMENTS, unsaved=True
        )
        if arguments is None:
            return
        terminal = self.yes_no("ADD COMMAND APPLICATION", "RUN IN TERMINAL?", unsaved=True)
        if terminal is None:
            return
        environment_text = self.text_input(
            "ADD COMMAND APPLICATION", "ENVIRONMENT KEY=value;OTHER=value (OPTIONAL)", 2048, unsaved=True
        )
        if environment_text is None:
            return
        try:
            result = self.result()
            app_id = apps.application_id(name, {item.id for item in result.applications})
            order = max([50, *(item.order for item in result.applications if item.visible)]) + 10
            self._write_user(
                apps.Application(
                    id=app_id, name=name.strip().upper(), kind="command", command=command.strip(),
                    arguments=arguments, status_id=app_id, terminal=terminal, order=order,
                    environment=apps.parse_environment(environment_text),
                )
            )
            self.status = f"ADDED {name.strip().upper()}"
        except (OSError, apps.ManifestError) as error:
            self.status = f"APPLICATION NOT ADDED: {error}"

    def add_web(self) -> None:
        if not browser.installed_browsers():  # a web app opens in an installed browser
            self.launcher.browser_setup()
            if not browser.installed_browsers():
                self.status = "ADD A WEB BROWSER FIRST"
                return
        name = self.text_input("ADD WEB APPLICATION", "NAME", apps.MAX_NAME)
        if not name:
            return
        url = self.text_input("ADD WEB APPLICATION", "HTTP:// OR HTTPS:// URL", 2048, unsaved=True)
        if url is None:
            return
        try:
            url = apps.validate_web_url(url)
            kiosk = browser.kiosk_command(url)
            if kiosk is None:
                self.status = "ADD A WEB BROWSER FIRST"
                return
            result = self.result()
            app_id = apps.application_id(name, {item.id for item in result.applications})
            order = max([50, *(item.order for item in result.applications if item.visible)]) + 10
            self._write_user(
                apps.Application(
                    id=app_id, name=name.strip().upper(), kind="command",
                    command=kiosk[0], arguments=kiosk[1], status_id=app_id, order=order,
                )
            )
            self.status = f"ADDED {name.strip().upper()}"
        except (OSError, apps.ManifestError) as error:
            self.status = f"WEB APPLICATION NOT ADDED: {error}"

    def edit(self, app: apps.Application) -> None:
        while True:
            rows = ["DISABLE" if app.enabled else "ENABLE"]
            if not app.system:
                rows.append("DELETE")
            rows.append("BACK")
            selected = 0
            while True:
                self.draw(app.name, rows, selected)
                key = read_key(self.screen)
                selected = move_selection(selected, key, len(rows))
                if key == 27:
                    return
                if key in (curses.KEY_ENTER, 10, 13):
                    break
            choice = rows[selected]
            result = self.result()
            current = list(result.applications)
            index = next((number for number, item in enumerate(current) if item.id == app.id), -1)
            if index < 0 or choice == "BACK":
                return
            try:
                if choice in {"ENABLE", "DISABLE"}:
                    current[index] = dataclasses.replace(current[index], enabled=choice == "ENABLE")
                elif choice == "DELETE":
                    system_dir = apps.SYSTEM_DIR if apps.SYSTEM_DIR.exists() else SOURCE_MANIFESTS
                    apps.delete_user_application(app.id, system_dir=system_dir)
                    self.status = f"DELETED {app.name}"
                    self.launcher.reload_applications()
                    return
                apps.write_state(current)
                self.launcher.reload_applications()
                app = next(item for item in self.result().applications if item.id == app.id)
                self.status = f"UPDATED {app.name}"
            except (OSError, apps.ManifestError) as error:
                self.status = f"APPLICATION NOT UPDATED: {error}"

    def move(self, visible: list[apps.Application], direction: int) -> None:
        target = self.selected + direction
        if not 0 <= target < len(visible):
            self.status = "APPLICATION IS ALREADY AT THE EDGE"
            return
        visible[self.selected], visible[target] = visible[target], visible[self.selected]
        order_by_id = {item.id: (number + 1) * 10 for number, item in enumerate(visible)}
        current = [
            dataclasses.replace(item, order=order_by_id.get(item.id, item.order))
            for item in self.result().applications
        ]
        apps.write_state(current)
        self.selected = target
        self.launcher.reload_applications()
        self.status = f"MOVED {visible[target].name}"

    def run(self) -> None:
        while True:
            result = self.result()
            visible = [app for app in result.applications if app.visible]
            rows = [f"{app.name:<28} {'ENABLED' if app.enabled else 'DISABLED'}" for app in visible]
            rows += ["ADD COMMAND APPLICATION", "ADD WEB APPLICATION", "ADD A WEB BROWSER", "BACK"]
            self.selected = min(self.selected, len(rows) - 1)
            if result.errors:
                self.status = f"{len(result.errors)} INVALID APPLICATION(S) SKIPPED"
            self.draw("EDIT APPLICATIONS", rows, self.selected)
            key = read_key(self.screen)
            self.selected = move_selection(self.selected, key, len(rows))
            if self.selected < len(visible) and key in (curses.KEY_LEFT, ord("h"), curses.KEY_RIGHT, ord("l")):
                try:
                    self.move(visible, -1 if key in (curses.KEY_LEFT, ord("h")) else 1)
                except (OSError, apps.ManifestError) as error:
                    self.status = f"APPLICATION NOT MOVED: {error}"
                continue
            if key == 27:
                return
            if key not in (curses.KEY_ENTER, 10, 13):
                continue
            if self.selected < len(visible):
                self.edit(visible[self.selected])
            elif self.selected == len(visible):
                self.add_command()
            elif self.selected == len(visible) + 1:
                self.add_web()
            elif self.selected == len(visible) + 2:
                self.launcher.browser_setup()
            else:
                return


class RemoteDesktopSettings(ApplicationsSettings):
    """Saved RDP connections, their launcher buttons, and the connect flow."""

    hint = LIST_HINT  # its lists have no LEFT/RIGHT action

    def menu(self, title: str, rows: list[str], selected: int = 0, first: int = 0) -> int | None:
        """Return the chosen row index; rows before `first` are information only."""
        selected = max(first, min(selected, len(rows) - 1))
        while True:
            self.draw(title, rows, selected)
            key = read_key(self.screen)
            if key in (curses.KEY_UP, ord("k")):
                selected = selected - 1 if selected > first else len(rows) - 1
            elif key in (curses.KEY_DOWN, ord("j")):
                selected = selected + 1 if selected < len(rows) - 1 else first
            elif key in ENTER_KEYS:
                return selected
            elif key == 27:
                return None

    def message(self, title: str, message: str) -> None:
        _height, width = self.screen.getmaxyx()
        rows = [line for part in message.split("\n") for line in (textwrap.wrap(part, width=max(8, width - 10)) or [""])]
        self.menu(title, rows + ["OK"], len(rows), len(rows))

    @staticmethod
    def pinned() -> dict[str, apps.Application]:
        return {app.connection: app for app in application_result().applications if app.kind == "rdp"}

    @staticmethod
    def _system_dir() -> pathlib.Path:
        return apps.SYSTEM_DIR if apps.SYSTEM_DIR.exists() else SOURCE_MANIFESTS

    def run(self) -> None:
        selected = 0
        while True:
            connections, errors = rdp.load_connections()
            pinned = self.pinned()
            rows = [
                f"{item.name[:28]:<28} {item.host}:{item.port}{'  PINNED' if item.id in pinned else ''}"
                for item in connections
            ] + ["ADD CONNECTION", "BACK"]
            self.status = f"{len(errors)} INVALID CONNECTION(S) SKIPPED" if errors else (self.status or "")
            choice = self.menu("REMOTE DESKTOP", rows, selected)
            if choice is None or choice == len(rows) - 1:
                return
            selected = choice
            if choice < len(connections):
                self.connection_menu(connections[choice].id)
            else:
                self.edit_connection(None)

    @staticmethod
    def row_action(row: str) -> str:
        """Name of a menu row, the same before and after it changes (PIN/UNPIN, labels)."""
        action = row.split("  ", 1)[0]
        return "PIN TO LAUNCHER" if action == "UNPIN FROM LAUNCHER" else action

    def connection_menu(self, connection_id: str) -> None:
        selected = 0
        last = ""
        while True:
            connection = rdp.get_connection(connection_id)
            if connection is None:
                return
            app = self.pinned().get(connection_id)
            rows = ["CONNECT", "EDIT CONNECTION", "UNPIN FROM LAUNCHER" if app else "PIN TO LAUNCHER"]
            if app:
                shortcut = apps.SHORTCUTS.get(app.shortcut, "NONE")
                rows += [f"BUTTON LABEL  {app.name}", "BUTTON POSITION", f"CONTROLLER SHORTCUT  {shortcut}"]
            if connection.save_password:
                rows.append("FORGET SAVED PASSWORD")
            if connection.certificate:
                rows.append("FORGET CERTIFICATE")
            rows += ["DELETE CONNECTION", "BACK"]
            # The rows change after an action. Keep the cursor on the row it was on; if
            # that row is gone (FORGET ...), step up rather than onto the row that slid in.
            names = [self.row_action(row) for row in rows]
            selected = names.index(last) if last in names else max(0, min(selected - bool(last), len(rows) - 1))
            choice = self.menu(connection.name.upper(), rows, selected)
            if choice is None or rows[choice] == "BACK":
                return
            selected = choice
            last = names[choice]
            action = rows[choice].split("  ", 1)[0]
            try:
                if action == "CONNECT":
                    self.launcher.launch_app(app or rdp_application(connection))
                    return
                if action == "EDIT CONNECTION":
                    self.edit_connection(connection)
                elif action == "PIN TO LAUNCHER":
                    self.pin(connection)
                elif action == "UNPIN FROM LAUNCHER" and app:
                    apps.delete_user_application(app.id, system_dir=self._system_dir())
                    self.status = f"UNPINNED {app.name}"
                elif action == "BUTTON LABEL" and app:
                    label = self.text_input("BUTTON LABEL", "LAUNCHER BUTTON LABEL", apps.MAX_NAME, initial=app.name)
                    if label and label.strip():
                        self._write_user(dataclasses.replace(app, name=label.strip().upper()))
                        self.status = f"LABEL SET TO {label.strip().upper()}"
                elif action == "BUTTON POSITION" and app:
                    self.reposition(app.id)
                elif action == "CONTROLLER SHORTCUT" and app:
                    self.assign_shortcut(app)
                elif action == "FORGET SAVED PASSWORD":
                    self.forget_password(connection)
                elif action == "FORGET CERTIFICATE":
                    if self.yes_no("FORGET CERTIFICATE", "VERIFY THE FINGERPRINT AGAIN ON THE NEXT CONNECTION?"):
                        rdp.upsert_connection(dataclasses.replace(connection, certificate=""))
                        rdp_log(f"certificate pin cleared for {connection.id}")
                        self.status = "CERTIFICATE FORGOTTEN"
                elif action == "DELETE CONNECTION":
                    if self.delete(connection, app):
                        return
            except (OSError, apps.ManifestError, rdp.RdpError) as error:
                self.status = f"NOT CHANGED: {error}"
            self.launcher.reload_applications()

    def resolution_choice(self, current: str) -> str | None:
        rows = ["NATIVE  (THIS DISPLAY'S RESOLUTION)", "FIT  (FOLLOW THE WINDOW SIZE)"]
        rows += [f"CUSTOM  {item}" for item in rdp.RESOLUTION_PRESETS] + ["CUSTOM  TYPE A SIZE", "BACK"]
        values: list[str | None] = ["native", "fit", *rdp.RESOLUTION_PRESETS, "type", None]
        choice = self.menu("RESOLUTION", rows, values.index(current) if current in values else 0)
        if choice is None or values[choice] is None:
            return None
        if values[choice] != "type":
            return values[choice]
        typed = self.text_input("CUSTOM RESOLUTION", "WIDTHxHEIGHT, FOR EXAMPLE 1920x1080", 9)
        return typed.strip().lower() if typed else None

    @staticmethod
    def resolution_label(value: str) -> str:
        return {"native": "NATIVE", "fit": "FIT"}.get(value, f"CUSTOM {value}")

    def edit_connection(self, connection: rdp.Connection | None) -> None:
        title = "ADD CONNECTION" if connection is None else f"EDIT {connection.name.upper()}"
        values: dict[str, object] = dataclasses.asdict(
            connection or rdp.Connection(id="", name="", host="", username="")
        )
        values["port"] = str(values["port"])
        new_password: str | None = None
        saved_values = dict(values)
        selected = 0
        on_off = {True: "ON", False: "OFF"}
        while True:
            rows = [
                f"DISPLAY NAME   {values['name'] or '(REQUIRED)'}",
                f"HOST           {values['host'] or '(REQUIRED)'}",
                f"PORT           {values['port']}",
                f"USERNAME       {values['username'] or '(REQUIRED)'}",
                f"DOMAIN         {values['domain'] or '(NONE)'}",
                f"RESOLUTION     {self.resolution_label(str(values['resolution']))}",
                f"FULLSCREEN     {on_off[bool(values['fullscreen'])]}",
                f"AUDIO          {on_off[bool(values['audio'])]}",
                f"CLIPBOARD      {on_off[bool(values['clipboard'])]}",
                "SAVE PASSWORD  " + (
                    "ON  (STORED UNENCRYPTED)" if values["save_password"] else "OFF  (ASK WHEN CONNECTING)"
                ),
                "SAVE CONNECTION",
                "CANCEL",
            ]
            self.draw(title, rows, selected, FORM_HINT)
            key = read_key(self.screen)
            selected = move_selection(selected, key, len(rows))
            changed = values != saved_values or new_password is not None
            if key == 27:
                if changed and not self.discard_changes():
                    continue
                return
            toggles = {6: "fullscreen", 7: "audio", 8: "clipboard"}
            if selected in toggles and key in (curses.KEY_LEFT, curses.KEY_RIGHT, *ENTER_KEYS):
                values[toggles[selected]] = not values[toggles[selected]]
                continue
            if key not in ENTER_KEYS:
                continue
            fields = {
                0: ("name", "DISPLAY NAME", apps.MAX_NAME),
                1: ("host", "HOST NAME OR IPV4 ADDRESS", rdp.MAX_HOST),
                2: ("port", "PORT (DEFAULT 3389)", 5),
                3: ("username", "USERNAME", rdp.MAX_USERNAME),
                4: ("domain", "DOMAIN (OPTIONAL)", rdp.MAX_DOMAIN),
            }
            if selected in fields:
                name, prompt, limit = fields[selected]
                typed = self.text_input(title, prompt, limit, initial=str(values[name]))
                if typed is not None:
                    values[name] = typed.strip()
            elif selected == 5:
                chosen = self.resolution_choice(str(values["resolution"]))
                if chosen:
                    try:
                        values["resolution"] = rdp.validate_resolution(chosen)
                    except rdp.RdpError as error:
                        self.status = str(error).upper()
            elif selected == 9:
                if values["save_password"]:
                    values["save_password"] = False
                    new_password = None
                elif self.yes_no(
                    "SAVE PASSWORD",
                    "THE PASSWORD WILL BE STORED UNENCRYPTED IN A ROOT-ONLY FILE. SAVE IT?",
                ):
                    typed = self.text_input(title, "PASSWORD TO SAVE", rdp.MAX_PASSWORD, masked=True)
                    if typed is not None:
                        values["save_password"] = True
                        new_password = typed
            elif selected == 10:
                if self.save_connection(connection, values, new_password):
                    return
            elif not changed or self.discard_changes():
                return

    def upsert(self, connection: rdp.Connection) -> bool:
        """Save a connection; a full or read-only disk is reported, not raised."""
        try:
            rdp.upsert_connection(connection)
        except OSError:
            self.status = SAVE_FAILED
            return False
        return True

    def save_connection(
        self, original: rdp.Connection | None, values: dict[str, object], new_password: str | None
    ) -> bool:
        try:
            connections, _errors = rdp.load_connections()
            taken = {item.id for item in connections} | {item.id for item in application_result().applications}
            identifier = original.id if original else rdp.connection_id(str(values["name"]), taken)
            certificate = original.certificate if original else ""
            if original and (original.host, original.port) != (
                rdp.validate_host(str(values["host"])), rdp.validate_port(values["port"])
            ):
                certificate = ""  # a different server must be verified again
            updated = rdp.validate(rdp.Connection(
                id=identifier, name=str(values["name"]), host=str(values["host"]),
                port=rdp.validate_port(values["port"]), username=str(values["username"]),
                domain=str(values["domain"]), resolution=str(values["resolution"]),
                fullscreen=bool(values["fullscreen"]), audio=bool(values["audio"]),
                clipboard=bool(values["clipboard"]),
                save_password=bool(values["save_password"]) and (
                    new_password is not None or bool(original and original.save_password)
                ),
                certificate=certificate,
            ))
            if updated.save_password and new_password is not None:
                rdp.validate_password(new_password)
            if not self.upsert(updated):
                return False
        except (OSError, rdp.RdpError, apps.ManifestError) as error:
            self.status = f"NOT SAVED: {error}".upper()
            return False
        message = f"SAVED {updated.name.upper()}"
        if (
            original and original.save_password and updated.save_password and new_password is None
            and rdp.secret_binding(original) != rdp.secret_binding(updated)
        ):
            # A saved password belongs to one server and account; never reuse it.
            updated = dataclasses.replace(updated, save_password=False)
            if not self.upsert(updated):
                return False
            message = "SAVED; SERVER OR ACCOUNT CHANGED, SO SAVE THE PASSWORD AGAIN"
        if updated.save_password and new_password is not None:
            ok, detail = self.password_request("set", updated.id, new_password)
            if not ok:
                cleared = self.upsert(dataclasses.replace(updated, save_password=False))
                self.password_request("delete", updated.id)
                if not cleared:
                    return False
                message = f"SAVED; PASSWORD NOT STORED: {detail}".upper()
        elif original and original.save_password and not updated.save_password:
            ok, detail = self.password_request("delete", updated.id)
            if not ok:
                message = f"SAVED; OLD PASSWORD NOT REMOVED: {detail}".upper()
        self.status = message
        return True

    def password_request(self, operation: str, connection_id: str, password: str = "") -> tuple[bool, str]:
        self.draw("REMOTE DESKTOP", ["UPDATING THE SAVED PASSWORD", "PLEASE WAIT"], None)
        request = RUN / rdp.SECRET_REQUEST.name
        request_id = rdp.submit_secret_request(operation, connection_id, password, request)
        ok, detail = rdp.wait_secret_status(request_id, path=RUN / rdp.SECRET_STATUS.name)
        flush_input()  # up to 10 s of frozen screen: drop the presses made meanwhile
        if not ok:
            request.unlink(missing_ok=True)  # never leave a password waiting in /run
        return ok, detail

    def forget_password(self, connection: rdp.Connection) -> None:
        ok, detail = self.password_request("delete", connection.id)
        if ok:
            rdp.upsert_connection(dataclasses.replace(connection, save_password=False))
            self.status = "SAVED PASSWORD REMOVED"
        else:
            self.status = f"PASSWORD NOT REMOVED: {detail}".upper()

    def pin(self, connection: rdp.Connection) -> None:
        result = application_result()
        order = max([50, *(item.order for item in result.applications if item.visible)]) + 10
        self._write_user(apps.Application(
            id=connection.id, name=connection.name.upper(), kind="rdp", connection=connection.id,
            status_id=connection.id, order=order,
        ))
        self.status = f"PINNED {connection.name.upper()}"

    def assign_shortcut(self, app: apps.Application) -> None:
        options = [("NONE", "")] + [(label, value) for value, label in apps.SHORTCUTS.items()]
        chosen = self.choose_shortcut(options, app.shortcut)
        if chosen is None:
            return
        for other in application_result().applications:
            if chosen and other.shortcut == chosen and other.id != app.id and not other.system:
                self._write_user(dataclasses.replace(other, shortcut=""))
        self._write_user(dataclasses.replace(app, shortcut=chosen))
        self.status = f"SHORTCUT {apps.SHORTCUTS.get(chosen, 'NONE')} ASSIGNED TO {app.name}" if chosen else "SHORTCUT REMOVED"

    def choose_shortcut(self, options: list[tuple[str, str]], current: str) -> str | None:
        rows = [label for label, _value in options]
        index = next((number for number, (_label, value) in enumerate(options) if value == current), 0)
        self.status = "A / CROSS PICKS  ·  THEN PRESS THAT BUTTON ON THE MAIN SCREEN"
        choice = self.menu("CONTROLLER SHORTCUT", rows, index)
        self.status = ""
        return None if choice is None else options[choice][1]

    def reposition(self, app_id: str) -> None:
        self.status = f"UP/DOWN MOVES THE BUTTON  ·  {DONE_HINT} WHEN DONE"
        while True:
            visible = [item for item in application_result().applications if item.visible]
            index = next((number for number, item in enumerate(visible) if item.id == app_id), None)
            if index is None:
                return
            self.draw("BUTTON POSITION", [item.name for item in visible], index)
            key = read_key(self.screen)
            if key in ENTER_KEYS or key == 27:
                self.status = f"POSITION {index + 1} OF {len(visible)}"
                return
            direction = -1 if key in (curses.KEY_UP, curses.KEY_LEFT) else 1 if key in (curses.KEY_DOWN, curses.KEY_RIGHT) else 0
            target = index + direction
            if direction and 0 <= target < len(visible):
                self.selected = index
                self.move(visible, direction)
                self.status = f"UP/DOWN MOVES THE BUTTON  ·  {DONE_HINT} WHEN DONE"

    def delete(self, connection: rdp.Connection, app: apps.Application | None) -> bool:
        if not self.yes_no("DELETE CONNECTION", f"DELETE {connection.name.upper()}?"):
            return False
        # Always ask: a failed earlier save can leave a secret behind.
        ok, detail = self.password_request("delete", connection.id)
        if not ok and connection.save_password:
            self.status = f"NOT DELETED: SAVED PASSWORD COULD NOT BE REMOVED: {detail}".upper()
            return False
        if app:
            apps.delete_user_application(app.id, system_dir=self._system_dir())
        rdp.remove_connection(connection.id)
        self.status = f"DELETED {connection.name.upper()}"
        return True

    def prepare_launch(self, app: apps.Application) -> bool:
        """Verify the server certificate and obtain a password before starting a session."""
        label = app.name
        connection = rdp.get_connection(app.connection)
        if connection is None:
            self.launcher.show_failure(errors.connection_missing(label), app)
            return False
        session = active_rdp_session()
        if session and session != connection.id:
            # Also covers a session that is starting or reconnecting after a crash.
            busy = errors.remote_session_busy(label, open_now=(RUN / f"{session}-ready").exists())
            if self.launcher.show_failure(busy, app) == "retry":
                return self.prepare_launch(app)
            return False
        self.status = "CONNECTING...  CAN TAKE 15 SECONDS IF THE PC IS ASLEEP"
        self.draw(label, [f"CONNECTING TO {connection.host}:{connection.port}"], None)
        try:
            try:
                fingerprint = rdp.probe_certificate(connection.host, connection.port)
            finally:
                self.status = ""
                # The screen was frozen meanwhile: presses made then are not answers to what comes next.
                flush_input()
        except (OSError, ssl.SSLError, ValueError) as error:
            unreachable = f"COULD NOT REACH {connection.host}:{connection.port}: {error}"
            if self.launcher.show_launch_failure(label, unreachable, app=app) == "retry":
                return self.prepare_launch(app)
            return False
        if not connection.certificate:
            if not self.confirm_certificate(connection, fingerprint):
                self.launcher.status = "CONNECTION CANCELLED"
                return False
            connection = dataclasses.replace(connection, certificate=fingerprint)
            if not self.upsert(connection):
                # Without the pin the session would trust whatever answers next time.
                self.launcher.show_launch_failure(label, SAVE_FAILED)
                return False
            rdp_log(f"certificate trusted on first use for {connection.id} {connection.host}:{connection.port} sha256={fingerprint}")
        elif connection.certificate != fingerprint:
            rdp_log(
                f"certificate changed for {connection.id} {connection.host}:{connection.port} "
                f"pinned={connection.certificate} presented={fingerprint}; connection blocked"
            )
            self.message(
                "CERTIFICATE CHANGED - CONNECTION BLOCKED",
                f"THE SERVER AT {connection.host}:{connection.port} PRESENTED A DIFFERENT CERTIFICATE.\n"
                "SAVED:  " + "  ".join(rdp.fingerprint_lines(connection.certificate)) + "\n"
                "NOW:    " + "  ".join(rdp.fingerprint_lines(fingerprint)) + "\n"
                "THIS CAN MEAN SOMEONE IS INTERCEPTING THE CONNECTION. IF THE SERVER CERTIFICATE WAS "
                "REPLACED ON PURPOSE, VERIFY THE NEW FINGERPRINT ON THE SERVER, THEN USE SETTINGS > "
                "REMOTE DESKTOP > FORGET CERTIFICATE.",
            )
            self.launcher.status = f"{label} BLOCKED: CERTIFICATE CHANGED"
            return False
        handoff = RUN / rdp.HANDOFF.name
        if connection.save_password:
            # The root helper copies the saved password into the handoff, but only
            # while the host, port, username, and domain match what it was saved for.
            handoff.unlink(missing_ok=True)
            ok, detail = self.password_request("stage", connection.id)
            if not ok:
                self.launcher.show_failure(errors.saved_password_unusable(label, detail), app)
                return False
            return True
        password = self.text_input(
            f"CONNECT TO {connection.name.upper()}", f"PASSWORD FOR {connection.username}", rdp.MAX_PASSWORD,
            masked=True,
        )
        if password is None:
            self.launcher.status = "CONNECTION CANCELLED"
            return False
        rdp.write_handoff(connection.id, password, handoff)
        return True

    def confirm_certificate(self, connection: rdp.Connection, fingerprint: str) -> bool:
        rows = [
            f"FIRST CONNECTION TO {connection.host}:{connection.port}",
            "COMPARE THIS SHA-256 CERTIFICATE FINGERPRINT WITH THE SERVER:",
            "",
            *rdp.fingerprint_lines(fingerprint),
            "",
            "TRUST AND CONNECT",
            "CANCEL",
        ]
        self.status = "ACCEPT ONLY IF THE FINGERPRINT MATCHES"
        choice = self.menu("VERIFY SERVER CERTIFICATE", rows, len(rows) - 1, len(rows) - 2)
        self.status = ""
        return choice == len(rows) - 2


class StreamingSettings(RemoteDesktopSettings):
    """Couch to game: start a stream at boot, wake the gaming PC, and tune the stream."""

    TITLE = "STREAMING"
    WAKE_RESULTS = {
        "up": "{} IS ALREADY AWAKE AND ANSWERING.",
        "woke": "{} IS AWAKE.",
        "awake": "{} IS ON BUT SUNSHINE IS NOT ANSWERING. START SUNSHINE ON THE PC, THEN TRY AGAIN.",
        "timeout": f"{{}} DID NOT ANSWER WITHIN {int(stream.WAKE_TIMEOUT)} SECONDS. IT MAY STILL BE STARTING; TRY MOONLIGHT IN A MOMENT.",
        "cancelled": "STOPPED WAITING. THE WAKE REQUEST WAS SENT AND {} MAY STILL BE STARTING.",
        "home": "STOPPED WAITING. THE WAKE REQUEST WAS SENT AND {} MAY STILL BE STARTING.",
        "sent": "WAKE REQUEST SENT TO {}. IT CAN TAKE A MINUTE TO START.",
        "noaddr": "WAKE REQUEST NOT SENT: NO ADDRESS IS KNOWN FOR {}.",
        "nonetwork": "NO NETWORK. CONNECT ETHERNET OR WI-FI, THEN TRY AGAIN.",
        "nomac": stream.NO_MAC,
    }

    @staticmethod
    def ident(host: stream.Host) -> str:
        return host.uuid or host.name

    def rows(self, hosts: list[stream.Host], config: stream.StreamSettings) -> list[str]:
        host = stream.autostream_host(hosts, config)
        pc = host.label if host else ("NOT PAIRED ON THIS SYSTEM" if config.host else "NOT CHOSEN")
        if any(item.mac for item in hosts):
            wake = "WAKE PC"
        else:
            wake = "WAKE PC  UNAVAILABLE: " + ("NO PC ADDRESS LEARNED YET" if hosts else "NO PC PAIRED")
        return [
            f"AUTO-STREAM AT STARTUP  {stream.autostart_label(hosts, config)}",
            f"PC  {pc}",
            f"APPLICATION  {config.app}",
            wake,
            "OPTIMIZE STREAM SETTINGS",
            "PAIR ANOTHER GAMING PC" if hosts else "PAIR A GAMING PC",
            streamcheck.TITLE,
            SMOOTHER_ROW,
            *([f"{firmware.TITLE}  {firmware.state_label()}"] if firmware.applies() else []),
            "BACK",
        ]

    def run(self) -> None:
        selected = 0
        self.status = "ENTER CHANGES A SETTING  ·  ESC RETURNS"
        while True:
            hosts, config = stream.load_hosts(), stream.load_settings()
            rows = self.rows(hosts, config)
            choice = self.menu(self.TITLE, rows, selected)
            if choice is None or choice == len(rows) - 1:
                return
            selected = choice
            if choice == 0:
                self.toggle_autostart(config, hosts)
            elif choice == 1:
                self.choose_host(config, hosts)
            elif choice == 2:
                self.choose_app(config, stream.autostream_host(hosts, config))
            elif choice == 3:
                self.wake_pc()
            elif choice == 4:
                self.optimize()
            elif choice == 5:
                self.pair_pc()
            elif choice == 6:
                self.stream_check()
            elif rows[choice].startswith(firmware.TITLE):
                self.decoder_firmware()
            else:
                self.smoother()

    def save(self, config: stream.StreamSettings) -> bool:
        try:
            stream.save_settings(config)
        except (OSError, ValueError) as error:
            self.message(self.TITLE, f"NOT SAVED: {error}".upper())
            return False
        return True

    def pick_host(self, hosts: list[stream.Host], title: str = "CHOOSE A PC") -> stream.Host | None:
        choice = self.menu(title, [host.label for host in hosts] + ["BACK"])
        return hosts[choice] if choice is not None and choice < len(hosts) else None

    def toggle_autostart(self, config: stream.StreamSettings, hosts: list[stream.Host]) -> None:
        if config.autostart:
            self.save(dataclasses.replace(config, autostart=False))  # the chosen PC stays saved
            return
        if not hosts:
            self.message("AUTO-STREAM AT STARTUP", stream.PAIR_FIRST)
            return
        host = stream.autostream_host(hosts, config) or self.pick_host(hosts)
        if host is not None:
            self.save(dataclasses.replace(config, autostart=True, host=self.ident(host)))

    def choose_host(self, config: stream.StreamSettings, hosts: list[stream.Host]) -> None:
        if not hosts:
            self.message(self.TITLE, stream.PAIR_FIRST)
            return
        host = self.pick_host(hosts)
        if host is not None:
            self.save(dataclasses.replace(config, host=self.ident(host)))

    def choose_app(self, config: stream.StreamSettings, host: stream.Host | None) -> None:
        names = list(host.apps) if host else []
        choice = self.menu("APPLICATION TO STREAM", names + ["TYPE A NAME", "BACK"])
        if choice is None or choice == len(names) + 1:
            return
        if choice < len(names):
            name = names[choice]
        else:
            typed = self.text_input(self.TITLE, "APPLICATION NAME, AS MOONLIGHT SHOWS IT", stream.APP_MAX, initial=config.app)
            if typed is None:
                return
            name = typed.strip()
        if stream.valid_app(name):
            self.save(dataclasses.replace(config, app=name))
        else:
            self.message(self.TITLE, "NOT SAVED: THE NAME IS EMPTY, TOO LONG, OR STARTS WITH A DASH.")

    def wake_pc(self) -> None:
        hosts, config = stream.load_hosts(), stream.load_settings()
        if not hosts:
            self.message("WAKE PC", stream.PAIR_FIRST)
            return
        host = stream.default_host(hosts, config) or self.pick_host(hosts, "WAKE WHICH PC?")
        if host is not None:
            self.wake(host)

    def wake(self, host: stream.Host) -> None:
        """Wake `host` (WAKE PC, and STREAM CHECK's button) and say how it went."""
        self.message("WAKE PC", self.launcher.wake_message(host))

    def stream_check(self) -> None:
        """Settings > STREAMING > STREAM CHECK: is the network path to the gaming PC good enough, and what to change.

        B cancels the check. A PC that does not answer offers WAKE PC (the wake flow above) when a MAC is
        known, and the check runs again after it."""
        title = streamcheck.TITLE
        hosts, config = stream.load_hosts(), stream.load_settings()
        host = stream.default_host(hosts, config) if hosts else None
        if hosts and host is None:
            host = self.pick_host(hosts, "CHECK WHICH PC?")
            if host is None:
                return
        problem = streamcheck.unavailable(hosts, host, stream.link_up())
        if problem:
            self.message(title, problem)
            return

        def home_pressed() -> bool:  # the Guide/Home button leaves a file instead of a key
            if HOME_REQUEST.exists():
                HOME_REQUEST.unlink(missing_ok=True)
                return True
            return False

        while True:
            runner = streamcheck.Runner(host)
            if not streamcheck.wait(self.screen, runner, read_key, home_pressed):
                return
            if runner.result is None:
                self.message(title, streamcheck.CHECK_FAILED)
                return
            choice = streamcheck.show_result(self.screen, runner.result, read_key)
            if choice == streamcheck.WAKE:
                self.wake(host)
            elif choice != streamcheck.AGAIN:
                return

    def optimize(self) -> None:
        title = "OPTIMIZE STREAM SETTINGS"
        if stream.moonlight_running(RUN):
            self.message(title, "CLOSE MOONLIGHT FIRST. IT WOULD OVERWRITE THE NEW SETTINGS WHEN IT EXITS.")
            return
        try:
            output = display.active_output(display.query_outputs())
        except (OSError, RuntimeError, subprocess.SubprocessError):
            output = None
        mode = output.current_mode if output else None
        if mode is None:
            self.message(title, "NO DISPLAY MODE WAS DETECTED. NOTHING WAS CHANGED.")
            return
        if not stream.link_up():
            self.message(
                title, "NO NETWORK LINK. CONNECT ETHERNET OR WI-FI SO IT CAN BE MEASURED. NOTHING WAS CHANGED."
            )
            return
        host = stream.default_host(stream.load_hosts(), stream.load_settings())
        addresses = host.lan_addresses() if host else []
        self.draw(title, ["MEASURING THE NETWORK...", "THIS TAKES ABOUT TEN SECONDS"], None)
        # Check the PC first: a sleeping one loses every ping, which is not the network's fault.
        asleep = bool(addresses) and stream.probe(host) == "down"
        network = stream.measure_network(addresses[0][0] if addresses else None, pc_asleep=asleep)
        plan = stream.plan_settings(mode.width, mode.height, mode.refresh_mhz, network, stream.video_decode())
        try:
            stream.apply_plan(plan, run_dir=RUN)
        except (OSError, stream.StreamError) as error:
            self.message(title, f"NOT CHANGED: {error}".upper())
            return
        self.message("STREAM SETTINGS APPLIED", "\n".join(stream.summary_lines(mode.argument, plan, network)))

    def pair_pc(self) -> None:
        """The wizard's own pairing step; which PC auto-stream and WAKE PC use stays in the PC row."""
        title = "PAIR A GAMING PC"
        if not stream.link_up():
            self.message(title, "NO NETWORK. CONNECT ETHERNET OR WI-FI FIRST: SETTINGS > NETWORK.")
            return
        if stream.moonlight_running(RUN):
            self.message(title, "CLOSE MOONLIGHT FIRST: HOLD SELECT+START, THEN CLOSE IT IN ACTIVE APPLICATIONS.")
            return
        before = {self.ident(host) for host in stream.load_hosts()}
        actions = {
            "text": self.launcher.wizard_text,
            "launch": self.launcher.launch_and_wait,
            "pair_moonlight": self.launcher.pair_moonlight,
            "wake_pc": self.launcher.wake_message,
        }
        step = setup.SetupWizard(setup_ui(self.screen), actions, setup.System()).step_streaming()
        after = {self.ident(host) for host in stream.load_hosts()}
        if not after - before:
            if step != setup.DONE:  # DONE with nothing new: an already paired PC was picked (USE IT)
                self.message(title, "NO NEW GAMING PC WAS PAIRED. NOTHING WAS CHANGED.")
            return
        count = f"{len(after)} GAMING PC{'S' if len(after) != 1 else ''}"
        self.message(
            title, f"PAIRED. THIS SYSTEM NOW KNOWS {count}.\n"
            'CHOOSE THE ONE FOR AUTO-STREAM AND WAKE PC IN THE "PC" ROW.'
        )

    def smoother(self) -> None:
        title = "SMOOTHER STREAM"
        try:
            old, new = stream.lower_bitrate(run_dir=RUN)
        except (OSError, stream.StreamError) as error:
            self.message(title, f"NOT CHANGED: {error}".upper())
            return
        if new is None:
            self.message(
                title, f"ALREADY AT THE LOWEST USEFUL QUALITY ({old / 1000:g} MBPS). NOTHING WAS CHANGED.\n"
                "TRY A NETWORK CABLE, OR 5 GHZ WI-FI CLOSER TO THE ROUTER."
            )
            return
        self.message(
            title, f"STREAM QUALITY LOWERED: {old / 1000:g} MBPS -> {new / 1000:g} MBPS.\n"
            "STILL STUTTERING? PICK SMOOTHER STREAM AGAIN, OR USE A NETWORK CABLE.\n"
            "TO UNDO, PICK OPTIMIZE STREAM SETTINGS."
        )

    def decoder_firmware(self) -> None:
        """Settings > STREAMING > VIDEO DECODER FIRMWARE (nouveau PCs only): fetch, retest or remove it."""
        title = firmware.TITLE
        if firmware.busy(RUN):  # hidden with B earlier: show that job instead of starting another
            self.message(title, self.wait_for_firmware(title))
            return
        _height, width = self.screen.getmaxyx()
        about = [line for part in firmware.ABOUT.split("\n") for line in (textwrap.wrap(part, width=max(8, width - 10)) or [""])]
        state = firmware.state_label()
        actions = ["TEST AGAIN", "REMOVE FIRMWARE", "BACK"] if firmware.installed() else ["DOWNLOAD FROM NVIDIA.COM", "BACK"]
        rows = about + ["", f"NOW: {state}"] + actions
        choice = self.menu(title, rows, len(rows) - len(actions), len(rows) - len(actions))
        if choice is None or rows[choice] == "BACK":
            return
        action = "remove" if rows[choice] == "REMOVE FIRMWARE" else "install"
        try:
            firmware.submit(action, RUN)
        except OSError as error:
            self.message(title, f"COULD NOT START: {error}".upper())
            return
        self.message(title, self.wait_for_firmware(title))

    def wait_for_firmware(self, title: str) -> str:
        """Show the helper's progress until it is done; B only hides the wait, the work goes on."""
        started = time.monotonic()
        status = None
        self.screen.timeout(200)
        try:
            frame = 0
            while True:
                status = firmware.read_status(RUN)
                if status is not None and status[0] != "running":
                    return status[1]
                waited = time.monotonic() - started
                if status is None and waited > firmware.START_TIMEOUT:
                    return "THE FIRMWARE SERVICE DID NOT START. RESTART THE PC AND TRY AGAIN."
                if waited > firmware.RUN_TIMEOUT:
                    return "STILL WORKING AFTER 20 MINUTES. CHECK BACK IN SETTINGS > STREAMING."
                message = status[1] if status else "STARTING..."
                self.draw(title, [f"{message} {SPINNER[frame % len(SPINNER)]}", "", "B / CIRCLE HIDES THIS; IT KEEPS GOING"], None)
                frame += 1
                if read_key(self.screen) == 27:
                    return "STILL WORKING IN THE BACKGROUND. CHECK BACK IN SETTINGS > STREAMING."
        finally:
            self.screen.timeout(1000)


def register_failure_actions(launcher: Launcher) -> None:
    """WAKE PC on Moonlight failure screens: the button and its handler, registered together."""
    errors.register_action(WAKE_PC, app_ids=("moonlight",))
    launcher.failure_actions[WAKE_PC.id] = launcher.wake_from_failure


def connect_tv_control(launcher: Launcher) -> None:
    """Give the setup wizard feat/cec's TV CONTROL screen (the same one Settings opens)."""
    launcher.tv_control_screen = lambda: Settings(launcher.screen, launcher).run_tv_control()


def main(screen: curses.window) -> None:
    curses.set_escdelay(25)  # the controller's B sends a bare Esc; don't wait 1 s for a sequence
    launcher = Launcher(screen)
    register_failure_actions(launcher)
    connect_tv_control(launcher)
    launcher.run()


if __name__ == "__main__":
    curses.wrapper(main)
