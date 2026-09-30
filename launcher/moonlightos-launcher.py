#!/usr/bin/python3
"""Full-screen terminal launcher for the MoonlightOS appliance."""

from __future__ import annotations

import curses
import dataclasses
import ipaddress
import os
import pathlib
import shlex
import ssl
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Sequence

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import moonlightos_display as display
import moonlightos_audio as audio
import moonlightos_support as support
import moonlightos_bluetooth as bluetooth
import moonlightos_apps as apps
import moonlightos_power as power
import moonlightos_rdp as rdp
import moonlightos_setup as setup
import moonlightos_stream as stream
import moonlightos_cec as cec
import moonlightos_controllers as controllers
import moonlightos_update as update
import moonlightos_errors as errors


RUN = pathlib.Path("/run/moonlightos")
HOME_REQUEST = RUN / "home.request"
# Tells gamepad-nav the launcher (not a running app) has focus, so it forwards keys.
LAUNCHER_FOCUS = RUN / "launcher-focus"
SOURCE_MANIFESTS = pathlib.Path(__file__).resolve().parents[1] / "config/apps.d"
FIXED_CONTROLS = (
    ("SETTINGS", "settings"), ("SLEEP", "suspend"), ("REBOOT", "reboot"), ("SHUTDOWN", "poweroff"),
)
WAKE_PC = errors.Action("WAKE PC", "wake-pc")
SLEEP_UNSUPPORTED = "SLEEP: NOT SUPPORTED ON THIS PC"
SETTINGS_MENU = (
    "DISPLAY",
    "AUDIO",
    "BLUETOOTH",
    "NETWORK",
    "SLEEP & SCREEN",
    "APPLICATIONS",
    "REMOTE DESKTOP",
    "STREAMING",
    "ACTIVE APPLICATIONS",
    "TAILSCALE",
    "TV CONTROL",
    "CHECK FOR UPDATES",
    "SETUP WIZARD",
    "GENERATE SUPPORT FILE",
    "SYSTEM DIAGNOSTICS",
    "BACK",
)
SPINNER = "|/-\\"
SAVE_FAILED = "COULD NOT SAVE: DISK FULL OR READ-ONLY"
ENTER_KEYS = (curses.KEY_ENTER, 10, 13)
# gamepad-nav forwards LB, RB, View/Select, and Menu/Start as F5-F8.
SHORTCUT_KEYS = {curses.KEY_F5: "lb", curses.KEY_F6: "rb", curses.KEY_F7: "view", curses.KEY_F8: "menu"}
SHORTCUT_TAGS = {"lb": "LB", "rb": "RB", "view": "VIEW", "menu": "MENU"}
# gamepad-nav sends Delete for BTN_WEST; X/Triangle (BTN_NORTH) open the keyboard instead.
CLOSE_BUTTON = "Y (XBOX) / SQUARE (PS)"
TEXT_HINT = "KEYBOARD: X (XBOX) / TRIANGLE (PS) / F12  ·  A/ENTER ACCEPTS  ·  B/ESC CANCELS"
SUPPORT_EXPORT_TIMEOUT = 180.0
SUPPORT_EXPORT_START_TIMEOUT = 12.0
SUPPORT_EXPORT_POLL_MS = 100


def application_result() -> apps.LoadResult:
    system_dir = apps.SYSTEM_DIR if apps.SYSTEM_DIR.exists() else SOURCE_MANIFESTS
    return apps.load_applications(system_dir=system_dir)


def set_launcher_focus(held: bool) -> None:
    try:
        if held:
            LAUNCHER_FOCUS.touch()
        else:
            LAUNCHER_FOCUS.unlink(missing_ok=True)
    except OSError:
        pass


def request_osk(masked: bool = False) -> None:
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
    display.log(message, pathlib.Path("/var/log/moonlightos/rdp.log"))


# Set by Launcher: blanks the screen when idle and swallows the key that wakes it.
IDLE_GUARD: power.IdleGuard | None = None


def focus_launcher() -> None:
    try:
        subprocess.run(
            ["wlrctl", "toplevel", "focus", "title:MoonlightOS Launcher"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def read_key(screen: curses.window) -> int:
    key = screen.getch()
    if key != -1:
        display.confirm_restore()
    if IDLE_GUARD is not None:
        key = IDLE_GUARD.filter(screen, key)
    if key == curses.KEY_F12:
        request_osk()
        return -1
    return key


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
    """Judge `findmnt -n -o SOURCE,FSTYPE,OPTIONS --target /var/lib/moonlightos`.

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
            ["findmnt", "-n", "-o", "SOURCE,FSTYPE,OPTIONS", "--target", "/var/lib/moonlightos"],
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
    identity = pathlib.Path("/var/lib/moonlightos/launcher-controller.id")
    try:
        value = identity.read_text(encoding="ascii").strip()
    except OSError:
        return "NO CONTROLLER IDENTITY SAVED"
    return f"CONTROLLER DETECTED  {value[:64]}"


def configuration_summary(name: str) -> str:
    root = pathlib.Path("/var/lib/moonlightos/home/.config")
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
        self.status = network_summary()
        self.live_warning = live_mode_warning()
        self.last_status_update = time.monotonic()
        self.applications: tuple[apps.Application, ...] = ()
        self.menu: list[tuple[str, str]] = []
        # Checked on every start (the USB stick moves between PCs) and again after a resume.
        self.can_sleep = power.can_suspend()
        self.controllers = controllers.Monitor()
        self.updates = update.Checker()
        # Buttons on error screens (moonlightos_errors). A handler is called with the
        # application that failed (or None); it returns True to ask for another try.
        # register_failure_actions() adds the WAKE PC entry at start-up.
        self.failure_actions: dict[str, Callable[[apps.Application | None], bool]] = {
            errors.NETWORK.id: self.open_network_settings,
            errors.SUPPORT.id: self.save_support_file,
            errors.BLUETOOTH.id: self.open_bluetooth_settings,
        }
        self.reload_applications()
        global IDLE_GUARD
        IDLE_GUARD = self.idle = power.IdleGuard(
            power.effective_settings(power.load_settings(), self.can_sleep),
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

    def reload_applications(self) -> None:
        result = application_result()
        self.applications = tuple(
            app for app in result.applications if app.visible and app.enabled
        )
        self.menu = [
            (f"{app.name}  [{SHORTCUT_TAGS[app.shortcut]}]" if app.shortcut else app.name, app.id)
            for app in self.applications
        ] + self.fixed_controls()
        self.selected = min(self.selected, max(0, len(self.menu) - 1))
        if result.errors:
            self.status = f"{len(result.errors)} INVALID APPLICATION(S) SKIPPED"

    def fixed_controls(self) -> list[tuple[str, str]]:
        """SETTINGS, SLEEP, REBOOT, SHUTDOWN; SLEEP names its reason when this PC cannot suspend."""
        return [
            (SLEEP_UNSUPPORTED, action) if action == "suspend" and not self.can_sleep else (label, action)
            for label, action in FIXED_CONTROLS
        ]

    def refresh_sleep_support(self) -> None:
        """Ask again whether this PC can suspend; the SLEEP control and the auto-sleep timer follow."""
        self.can_sleep = power.can_suspend()
        del self.menu[len(self.applications):]
        self.menu += self.fixed_controls()
        self.idle.apply(power.effective_settings(power.load_settings(), self.can_sleep))

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
        # moonlightos-suspend.path removes the file before suspending.
        try:
            self.request("suspend")
        except OSError as error:
            self.status = f"COULD NOT REQUEST SLEEP: {error}"
            return
        self.status = "GOING TO SLEEP"

    def check_resume(self) -> bool:
        """True once after the system wakes (moonlightos-resume.service drops the marker)."""
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
        self.refresh_sleep_support()
        self.status = "RESUMED FROM SLEEP"
        self.last_status_update = time.monotonic()
        focus_launcher()
        self.autostream()  # the chosen PC streams again after a wake-up; no-op unless Settings > STREAMING asks

    def draw(self) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        draw_border(self.screen)
        title_row = max(2, height // 8)
        add_centered(self.screen, title_row, "MOONLIGHTOS")

        rows = []
        row = max(title_row + 3, height // 3)
        gap_before = {len(self.applications), len(self.applications) + 1}
        for index, (label, _action) in enumerate(self.menu):
            if index in gap_before:
                row += 1
            rows.append((row, label))
            row += 1

        menu_width = max(len(label) for _row, label in rows) + 3
        menu_left = max(2, (width - menu_width) // 2)
        for index, (menu_row, label) in enumerate(rows):
            if menu_row >= height - 3:
                break
            marker = ">" if index == self.selected else " "
            disabled = label == SLEEP_UNSUPPORTED
            try:
                self.screen.addnstr(
                    menu_row, menu_left, f"{marker}  {label}", max(1, width - menu_left - 1),
                    curses.A_DIM if disabled else curses.A_NORMAL,
                )
            except curses.error:
                pass

        footer = self.footer_lines()
        add_centered(self.screen, max(row + 2, height - 4 - len(footer)), self.status)
        for offset, (text, attr) in enumerate(reversed(footer)):
            add_centered(self.screen, height - 3 - offset, text, attr)
        self.screen.refresh()

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
        return lines

    def draw_launching(self, label: str, frame: str) -> None:
        self.screen.erase()
        height, _width = self.screen.getmaxyx()
        draw_border(self.screen)
        add_centered(self.screen, max(2, height // 8), "MOONLIGHTOS")
        center = max(6, height // 2 - 1)
        add_centered(self.screen, center, f"STARTING {label}  {frame}")
        add_centered(self.screen, center + 2, "PLEASE WAIT")
        add_centered(self.screen, height - 3, "EXIT THE APP TO RETURN")
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

    def show_failure(self, failure: errors.Failure, app: apps.Application | None = None) -> str:
        """Show an error screen with its buttons; returns "retry" or "dismiss".

        The other buttons run their handler (see `failure_actions`) and bring the
        screen back, unless the handler asks for another try by returning True.
        """
        set_launcher_focus(True)  # the dialog is on top of a failed app: keep the controller on it
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
        add_centered(self.screen, max(2, height // 8), "MOONLIGHTOS")
        center = max(6, height // 2 - 1)
        add_centered(self.screen, center, title)
        add_centered(self.screen, center + 2, detail)
        add_centered(self.screen, height - 3, hint)
        self.screen.refresh()

    @staticmethod
    def pressed(key: int) -> bool:
        return key not in (-1, curses.KEY_RESIZE)

    def wake_host(self, host: stream.Host, *, force: bool = False) -> str:
        """Wake a sleeping gaming PC and wait up to 30 s; any key or button stops the wait.

        Returns stream.wake_and_wait's result, or "nonetwork" without waiting when no link is up."""
        if not stream.link_up():
            return "nonetwork"

        def tick(elapsed: float) -> bool:
            self.draw_wait(
                f"WAKING {host.label}...  {SPINNER[int(elapsed * 4) % len(SPINNER)]}",
                f"{int(elapsed)} OF {int(stream.WAKE_TIMEOUT)} SECONDS",
                "PRESS ANY BUTTON TO START WITHOUT WAITING",
            )
            return self.pressed(self.screen.getch())

        self.screen.timeout(100)
        try:
            return stream.wake_and_wait(host, force=force, tick=tick)
        finally:
            self.screen.timeout(1000)

    def wake_before_moonlight(self) -> None:
        """Wake the gaming PC if it is asleep. Never blocks or fails the Moonlight launch."""
        try:
            host = stream.default_host(stream.load_hosts(), stream.load_settings())
            if host is None or not host.mac:
                return
            if self.wake_host(host) == "nonetwork":
                self.draw_wait(stream.NO_NETWORK, "STARTING MOONLIGHT WITHOUT WAKING THE PC", "")
                self.screen.timeout(2000)
                self.screen.getch()
                self.screen.timeout(1000)
        except (OSError, ValueError, curses.error, subprocess.SubprocessError):
            pass

    def wake_pc(self) -> None:
        StreamingSettings(self.screen, self).wake_pc()

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

    def autostream(self) -> bool:
        """Start the chosen PC's stream (Settings > STREAMING) after a cancellable 5 s countdown.

        Runs once setup is complete and nothing else is running; also meant to be called
        after the system resumes from sleep. Returns True when the stream was started."""
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
        if self.wait_screen(f"STARTING STREAM TO {host.label}...", 5) != "timeout":
            self.status = "AUTO-STREAM CANCELLED"
            return False
        app = self.app_by_id("moonlight")
        if app is None:
            self.status = "MOONLIGHT IS UNAVAILABLE"
            return False
        request = RUN / stream.STREAM_REQUEST.name
        try:
            stream.write_stream_request(host.target, config.app, request)
        except (OSError, ValueError) as error:
            self.status = f"AUTO-STREAM NOT STARTED: {error}".upper()
            return False
        try:
            return self.launch_app(app)
        finally:
            request.unlink(missing_ok=True)

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

    def launch_app(self, app: apps.Application) -> bool:
        label, app_id = app.name, app.status_id
        ready = RUN / f"{app_id}-ready"
        if ready.exists():
            if self.focus_app(app):
                self.status = f"RESUMED {label}"
                return True
            self.status = f"{label} IS RUNNING BUT HAS NO WINDOW: CLOSE IT UNDER SETTINGS > ACTIVE APPLICATIONS"
            return False
        if app.kind == "command":
            # moonlightos-configured-app.service runs one app at a time; a request
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
        if app.id == "moonlight":
            self.wake_before_moonlight()
        ready.unlink(missing_ok=True)
        state.unlink(missing_ok=True)
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
                        if self.show_launch_failure(label, app_state.removeprefix("failed:").strip(), app=app) == "retry":
                            return self.launch_app(app)
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
        if self.show_launch_failure(label, message, app=app) == "retry":
            return self.launch_app(app)
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
            except (OSError, subprocess.SubprocessError):
                return False
            if result.returncode == 0:
                set_launcher_focus(False)
                return True
        return False

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

    def active_applications(self) -> None:
        selected = 0
        status = f"A/ENTER RESUMES  ·  {CLOSE_BUTTON} CLOSES"
        HOME_REQUEST.unlink(missing_ok=True)  # a Guide press made before this screen opened
        while True:
            running = self.running_applications()
            rows = [f"{app.name:<32} RUNNING" for app in running] + ["RETURN TO MAIN LAUNCHER"]
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
            add_centered(self.screen, height - 3, status if running else "NO MANAGED APPLICATIONS ARE RUNNING")
            self.screen.refresh()
            key = read_key(self.screen)
            if HOME_REQUEST.exists():  # Guide again while the menu is open closes it
                HOME_REQUEST.unlink(missing_ok=True)
                return
            selected = move_selection(selected, key, len(rows))
            if key == 27 or (key in (curses.KEY_ENTER, 10, 13) and selected == len(running)):
                return
            if selected >= len(running):
                continue
            app = running[selected]
            if key in (curses.KEY_ENTER, 10, 13):
                if self.focus_app(app):
                    self.status = f"RESUMED {app.name}"
                    return
                status = f"COULD NOT FOCUS {app.name}: PRESS X TO CLOSE IT, THEN START IT AGAIN"
            elif key in (curses.KEY_DC, ord("x")):
                (RUN / f"close-{app.status_id}").touch()
                status = f"CLOSING {app.name}"

    def activate(self) -> None:
        _label, action = self.menu[self.selected]
        app = next((item for item in self.applications if item.id == action), None)
        if app:
            self.launch_app(app)
        elif action == "settings":
            Settings(self.screen, self).run()
            self.reload_applications()
        elif action in {"reboot", "poweroff"}:
            self.request(action)
        elif action == "suspend":
            self.request_sleep()

    def setup_wizard(self, *, force: bool = False) -> None:
        settings = Settings(self.screen, self)
        actions = {
            "osk": request_osk,
            "network": lambda: self.launch_by_id("network-setup"),
            "bluetooth": lambda: bluetooth.run_bluetooth(self.screen),
            "display": settings.run_display,
            "audio": lambda: self.launch_by_id("audio-test"),
            "moonlight": lambda: self.launch_by_id("moonlight"),
            "chiaki-ng": lambda: self.launch_by_id("chiaki-ng"),
            "tailscale": lambda: self.launch_by_id("tailscale"),
            "applications": settings.run_applications,
        }
        statuses = {
            "network": network_summary,
            "bluetooth": bluetooth_summary,
            "display": display_summary,
            "audio": audio_summary,
            "controller": controller_summary,
            "moonlight": lambda: configuration_summary("moonlight"),
            "chiaki-ng": lambda: configuration_summary("chiaki"),
            "tailscale": tailscale_summary,
            "applications": lambda: f"{len(application_result().applications)} APPLICATIONS CONFIGURED",
        }
        setup.SetupWizard(self.screen, actions, statuses).run(force=force)
        self.reload_applications()

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
        self.updates.start()
        self.draw()
        (RUN / "launcher-ready").touch()
        if display.restore_saved_mode() is None:
            self.status = "SAVED DISPLAY MODE SKIPPED — CHOOSE IT AGAIN IN SETTINGS > DISPLAY"
            self.last_status_update = time.monotonic() + 25  # keep it up for 30 s
        self.draw()
        self.setup_wizard()
        self.autostream()
        self.draw()
        while True:
            key = read_key(self.screen)
            if HOME_REQUEST.exists() or key == curses.KEY_HOME:
                HOME_REQUEST.unlink(missing_ok=True)
                set_launcher_focus(True)  # gamepad-nav forwards keys while an app runs
                self.active_applications()
                self.draw()
                continue
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

            now = time.monotonic()
            if now - self.last_status_update >= 5:
                self.status = network_summary()
                self.last_status_update = now
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

    def draw(self, title: str = "SETTINGS", rows: list[str] | None = None, selected: int | None = None) -> None:
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
        row = max(5, height // 3)
        left = max(2, (width - max((len(item) for item in rows), default=1) - 3) // 2)
        for index, label in enumerate(rows):
            marker = ">" if index == selected else " "
            if row + index >= height - 4:
                break
            try:
                self.screen.addnstr(row + index, left, f"{marker}  {label}", width - left - 1)
            except curses.error:
                pass
        add_centered(self.screen, height - 3, self.status)
        self.screen.refresh()

    def menu_label(self, item: str) -> str:
        if item == "CHECK FOR UPDATES":
            return f"{item}  {'ON' if self.launcher.updates.enabled else 'OFF'}"
        return item

    def toggle_updates(self) -> None:
        enabled = not self.launcher.updates.enabled
        self.launcher.updates.set_enabled(enabled)
        if not enabled:
            self.status = "UPDATE CHECK OFF: NOTHING IS SENT"
        elif not self.launcher.updates.online():
            self.status = "UPDATE CHECK ON: WAITS UNTIL THIS PC IS ONLINE"
        else:
            self.status = "UPDATE CHECK ON: LOOKS FOR A NEWER RELEASE ONCE A DAY"

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
            self.status = "ENTER OR ESC RETURNS TO SETTINGS"
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
            failure = "CONNECT A WRITABLE REMOVABLE USB DRIVE AND TRY AGAIN"
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
                        "REBOOT MOONLIGHTOS AND TRY AGAIN; IF IT FAILS AGAIN, RUN SYSTEM DIAGNOSTICS."
                    )
                    self.screen.timeout(1000)
                    self.status = "EXPORT FAILED: SERVICE DID NOT START - REBOOT, THEN TRY AGAIN"
                    self.show_error("SUPPORT EXPORT FAILED", failure, retry=False)  # the message says reboot first
                    return

                if now >= deadline:
                    failure = (
                        "THE EXPORT DID NOT REPORT COMPLETION WITHIN 3 MINUTES. "
                        "DO NOT REMOVE THE USB DRIVE WHILE ITS ACTIVITY LIGHT IS FLASHING. "
                        "REBOOT MOONLIGHTOS BEFORE TRYING AGAIN."
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

    def activate(self) -> bool:
        actions = {
            "DISPLAY": self.run_display,
            "AUDIO": self.run_audio,
            "BLUETOOTH": lambda: bluetooth.run_bluetooth(self.screen),
            "NETWORK": lambda: self.launch("network-setup"),
            "SLEEP & SCREEN": self.run_sleep_settings,
            "APPLICATIONS": self.run_applications,
            "REMOTE DESKTOP": self.run_remote_desktop,
            "STREAMING": self.run_streaming,
            "ACTIVE APPLICATIONS": self.launcher.active_applications,
            "TAILSCALE": lambda: self.launch("tailscale"),
            "TV CONTROL": self.run_tv_control,
            "CHECK FOR UPDATES": self.toggle_updates,
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
        while True:
            refresh = f"{self.refresh_mhz / 1000:g} HZ" if self.refresh_mhz else "UNAVAILABLE"
            rows = [f"RESOLUTION  {self.resolution or 'UNAVAILABLE'}", f"REFRESH RATE  {refresh}", "APPLY DISPLAY MODE", "BACK"]
            self.draw("DISPLAY SETTINGS", rows, selected)
            key = read_key(self.screen)
            selected = move_selection(selected, key, len(rows))
            if key == 27 or (key in (curses.KEY_ENTER, 10, 13) and selected == 3):
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
            else:
                self.apply_preview()

    def run_audio(self) -> None:
        selected = 0
        result = ""  # outcome of the last change, shown on the redraw
        explained = False  # the "nothing to play on" screen is shown once, not on every redraw
        while True:
            failure: errors.Failure | None = None
            try:
                sinks = audio.query_sinks()
                rows = [f"{'*' if sink.default else ' '}  {sink.name}" for sink in sinks]
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
                    "* IS THE CURRENT DEFAULT OUTPUT" if sinks else "NO AUDIO OUTPUTS AVAILABLE"
                )
                if not sinks:
                    failure = errors.simple_failure(
                        "NO SOUND OUTPUT FOUND",
                        "NO SOUND DEVICE IS AVAILABLE. CHECK THAT THE TV OR SPEAKERS ARE ON AND CONNECTED, "
                        "OR PAIR A BLUETOOTH SPEAKER OR HEADSET.",
                        errors.BLUETOOTH, retry=True,
                    )
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                sinks = []
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
            if selected < len(sinks):
                if key not in ENTER_KEYS:
                    continue
                try:
                    audio.set_default(sinks[selected].id)
                    result = f"DEFAULT OUTPUT: {sinks[selected].name}"
                except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                    result = f"OUTPUT NOT CHANGED: {error}. TRY ANOTHER OUTPUT"
                    continue
                try:
                    note = audio.ensure_audible(sinks[selected].id)
                except (OSError, RuntimeError, subprocess.SubprocessError):
                    note = "VOLUME NOT CHECKED"
                if note:
                    result += f" ({note})"
                continue
            try:
                if selected == len(sinks):
                    step = -audio.VOLUME_STEP if key == curses.KEY_LEFT else audio.VOLUME_STEP
                    result = f"VOLUME {audio.change_volume(step).percent}%"
                else:
                    result = "MUTED" if audio.toggle_mute().muted else "UNMUTED"
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                result = f"VOLUME NOT CHANGED: {error}"

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
        wake_from = ", ".join(power.wake_sources()) or "NO USB BLUETOOTH ADAPTER OR KEYBOARD FOUND"
        self.status = ""
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
                self.status = ""
            except OSError as error:
                self.status = f"COULD NOT SAVE: {error}"
            self.launcher.idle.apply(power.effective_settings(settings, can_sleep))

    def run_applications(self) -> None:
        ApplicationsSettings(self.screen, self.launcher).run()
        self.launcher.reload_applications()

    def run_remote_desktop(self) -> None:
        RemoteDesktopSettings(self.screen, self.launcher).run()
        self.launcher.reload_applications()

    def run_streaming(self) -> None:
        StreamingSettings(self.screen, self.launcher).run()

    def run_tv_control(self) -> None:
        """HDMI-CEC: what the TV reports, and two switches that only work with a CEC adapter."""

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
            actions = cec.toggle_rows(settings, status.usable, can_sleep) + ["REFRESH", "BACK"]
            self.draw("TV CONTROL", info + [""] + actions, len(info) + 1 + selected)
            key = read_key(self.screen)
            selected = move_selection(selected, key, len(actions))
            if key == 27 or (key in ENTER_KEYS and selected == len(actions) - 1):
                return
            if key not in ENTER_KEYS:
                continue
            if selected == len(actions) - 2:
                status = ask_tv()
            elif not status.usable:
                self.status = "NOT AVAILABLE: " + ("NO CEC ADAPTER FOUND" if status.adapter is None else "TV NOT CONNECTED")
            elif selected == 1 and not can_sleep:
                self.status = "NOT AVAILABLE: SUSPEND NOT SUPPORTED ON THIS PC"
            else:
                changed = cec.toggled(settings, selected)
                try:
                    cec.save_settings(changed)
                except OSError as error:
                    self.status = f"NOT SAVED: {error}"
                else:
                    settings, self.status = changed, "SAVED"

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

    def draw(self, title: str, rows: list[str], selected: int | None = None) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        draw_border(self.screen)
        add_centered(self.screen, max(2, height // 8), title)
        first = max(5, height // 4)
        left = max(2, (width - max((len(row) for row in rows), default=1) - 3) // 2)
        count = max(1, height - first - 4)
        offset = 0 if selected is None else min(max(0, selected - count + 1), max(0, len(rows) - count))
        for index, row in enumerate(rows[offset:offset + count], start=offset):
            marker = ">" if index == selected else " "
            try:
                self.screen.addnstr(first + index - offset, left, f"{marker}  {row}", max(1, width - left - 1))
            except curses.error:
                pass
        add_centered(self.screen, height - 3, self.status or "LEFT/RIGHT MOVES  ·  ENTER EDITS  ·  F12 KEYBOARD")
        self.screen.refresh()

    def text_input(
        self, title: str, prompt: str, limit: int, *, initial: str = "", masked: bool = False
    ) -> str | None:
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
                self.draw(title, [prompt, shown], None)
                key = self.screen.get_wch()
                if isinstance(key, str):
                    if key in {"\n", "\r"}:
                        return value
                    if key == "\x1b":
                        return None
                    if key in {"\b", "\x7f"}:
                        value = value[:-1]
                    elif key.isprintable() and key not in "\r\n" and len(value) < limit:
                        value += key
                elif key == curses.KEY_F12:
                    request_osk(masked)
                elif key in (curses.KEY_BACKSPACE,):
                    value = value[:-1]
        finally:
            self.status = previous_status
            self.screen.timeout(1000)
            try:
                curses.curs_set(0)
            except curses.error:
                pass

    def yes_no(self, title: str, prompt: str) -> bool | None:
        selected = 0
        while True:
            rows = [prompt, "YES", "NO"]
            self.draw(title, rows, selected + 1)
            key = read_key(self.screen)
            selected = move_selection(selected, key, 2)
            if key in (curses.KEY_ENTER, 10, 13):
                return selected == 0
            if key == 27:
                return None

    def _write_user(self, app: apps.Application) -> None:
        system_dir = apps.SYSTEM_DIR if apps.SYSTEM_DIR.exists() else SOURCE_MANIFESTS
        apps.write_user_application(app, system_dir=system_dir)

    def add_command(self) -> None:
        name = self.text_input("ADD COMMAND APPLICATION", "NAME", apps.MAX_NAME)
        if not name:
            return
        command = self.text_input("ADD COMMAND APPLICATION", "ABSOLUTE COMMAND", apps.MAX_COMMAND)
        if command is None:
            return
        arguments = self.text_input("ADD COMMAND APPLICATION", "ARGUMENTS (OPTIONAL)", apps.MAX_ARGUMENTS)
        if arguments is None:
            return
        terminal = self.yes_no("ADD COMMAND APPLICATION", "RUN IN TERMINAL?")
        if terminal is None:
            return
        environment_text = self.text_input(
            "ADD COMMAND APPLICATION", "ENVIRONMENT KEY=value;OTHER=value (OPTIONAL)", 2048
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
        name = self.text_input("ADD WEB APPLICATION", "NAME", apps.MAX_NAME)
        if not name:
            return
        url = self.text_input("ADD WEB APPLICATION", "HTTP:// OR HTTPS:// URL", 2048)
        if url is None:
            return
        try:
            url = apps.validate_web_url(url)
            result = self.result()
            app_id = apps.application_id(name, {item.id for item in result.applications})
            order = max([50, *(item.order for item in result.applications if item.visible)]) + 10
            self._write_user(
                apps.Application(
                    id=app_id, name=name.strip().upper(), kind="command",
                    command="/usr/bin/google-chrome-stable",
                    arguments=shlex.join(["--ozone-platform=wayland", "--kiosk", "--no-first-run", url]),
                    status_id=app_id, order=order,
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
            rows += ["ADD COMMAND APPLICATION", "ADD WEB APPLICATION", "BACK"]
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
            else:
                return


class RemoteDesktopSettings(ApplicationsSettings):
    """Saved RDP connections, their launcher buttons, and the connect flow."""

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

    def connection_menu(self, connection_id: str) -> None:
        selected = 0
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
            choice = self.menu(connection.name.upper(), rows, selected)
            if choice is None or rows[choice] == "BACK":
                return
            selected = choice
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
            self.draw(title, rows, selected)
            key = read_key(self.screen)
            selected = move_selection(selected, key, len(rows))
            if key == 27:
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
            else:
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
        self.status = "PRESS THE BUTTON ON THE MAIN LAUNCHER SCREEN (KEYBOARD: F5-F8)"
        choice = self.menu("CONTROLLER SHORTCUT", rows, index)
        self.status = ""
        return None if choice is None else options[choice][1]

    def reposition(self, app_id: str) -> None:
        self.status = "UP/DOWN MOVES THE BUTTON  ·  ENTER OR ESC FINISHES"
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
                self.status = "UP/DOWN MOVES THE BUTTON  ·  ENTER OR ESC FINISHES"

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
            self.launcher.show_launch_failure(
                label, "THE SAVED CONNECTION NO LONGER EXISTS. CHECK SETTINGS > REMOTE DESKTOP.", app=app, retry=False
            )
            return False
        session = active_rdp_session()
        if session and session != connection.id:
            # Also covers a session that is starting or reconnecting after a crash.
            if self.launcher.show_launch_failure(
                label,
                "ANOTHER REMOTE DESKTOP SESSION IS OPEN OR RECONNECTING. PRESS HOME/GUIDE AND CLOSE "
                f"IT WITH {CLOSE_BUTTON}, OR WAIT FOR IT TO END.",
                app=app,
            ) == "retry":
                return self.prepare_launch(app)
            return False
        self.status = "PLEASE WAIT"
        self.draw(label, [f"CHECKING {connection.host}:{connection.port}"], None)
        try:
            fingerprint = rdp.probe_certificate(connection.host, connection.port)
        except (OSError, ssl.SSLError, ValueError) as error:
            unreachable = f"COULD NOT REACH {connection.host}:{connection.port}: {error}"
            if self.launcher.show_launch_failure(label, unreachable, app=app) == "retry":
                return self.prepare_launch(app)
            return False
        finally:
            self.status = ""
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
                self.launcher.show_launch_failure(
                    label, f"THE SAVED PASSWORD COULD NOT BE USED: {detail}".upper(), app=app, retry=False
                )
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
        "timeout": "{} DID NOT ANSWER WITHIN 30 SECONDS. IT MAY STILL BE STARTING; TRY MOONLIGHT IN A MOMENT.",
        "cancelled": "STOPPED WAITING. THE WAKE REQUEST WAS SENT AND {} MAY STILL BE STARTING.",
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
            else:
                self.optimize()

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
        if host is None:
            return
        blocked = stream.wake_block(host)
        if blocked:
            self.message("WAKE PC", blocked)
            return
        try:
            result = self.launcher.wake_host(host, force=True)
        except OSError as error:
            self.message("WAKE PC", f"WAKE FAILED: {error}".upper())
            return
        self.message("WAKE PC", self.WAKE_RESULTS.get(result, "{}").format(host.label))

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
        network = stream.measure_network(addresses[0][0] if addresses else None)
        plan = stream.plan_settings(mode.width, mode.height, mode.refresh_mhz, network)
        try:
            stream.apply_plan(plan, run_dir=RUN)
        except (OSError, stream.StreamError) as error:
            self.message(title, f"NOT CHANGED: {error}".upper())
            return
        self.message("STREAM SETTINGS APPLIED", "\n".join(stream.summary_lines(mode.argument, plan, network)))


def register_failure_actions(launcher: Launcher) -> None:
    """WAKE PC on Moonlight failure screens: the button and its handler, registered together."""
    errors.register_action(WAKE_PC, app_ids=("moonlight",))
    launcher.failure_actions[WAKE_PC.id] = launcher.wake_from_failure


def main(screen: curses.window) -> None:
    curses.set_escdelay(25)  # the controller's B sends a bare Esc; don't wait 1 s for a sequence
    launcher = Launcher(screen)
    register_failure_actions(launcher)
    launcher.run()


if __name__ == "__main__":
    curses.wrapper(main)
