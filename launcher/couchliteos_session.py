"""Starting, finding and closing applications: shared by both home screens.

The classic launcher (couchliteos-launcher.py, curses in foot) and the TV interface
(couchliteos-tv.py, GTK) start apps and streams, bring them back to the front and list
what runs the same way, so that logic lives here once. Nothing here draws or reads keys:
a front end subclasses `Session` and supplies the screen parts:

    draw_launching(label, frame)    the "STARTING <APP>" screen, redrawn while an app starts
    launch_wait_begin() / launch_wait() / launch_wait_end()
                                    around the start wait; launch_wait runs about every 0.1 s
                                    (read keys, keep the screen alive)
    show_launch_failure(label, message, app=None, retry=True) -> "retry" | anything else
    prepare_remote_desktop(app)     certificate and password before a Remote Desktop session
    wake_before_moonlight(app, auto)  wake a sleeping gaming PC; False stops the start

The classic launcher reads its own module's RUN, LAUNCHER_FOCUS and POINTER_MODES (the
tests point those at scratch files), so those reach the code here through `run_dir`,
`set_launcher_focus`, `pointer_modes`, `application_result` and `active_rdp_session`.

Root work is asked for with request files in RUN: `start-<app>` (couchliteos-run-app),
`launch-app.request` (couchliteos-configured-app), the Remote Desktop session request,
the stream request, `close-<app>`, and `suspend` / `reboot` / `poweroff`. An app shows it
is up with `<status_id>-ready` and reports `<status_id>-status` ("failed: ...", "exited: ...").
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import time

import couchliteos_apps as apps
import couchliteos_display as display
import couchliteos_pointer as pointer
import couchliteos_rdp as rdp
import couchliteos_recent as recent
import couchliteos_stream as stream

RUN = pathlib.Path(os.environ.get("COUCHLITEOS_RUN_DIR", "/run/couchliteos"))
STATE = pathlib.Path(os.environ.get("COUCHLITEOS_STATE_DIR", "/var/lib/couchliteos"))
SETUP_MARKER = STATE / "setup-complete"  # couchliteos_setup.MARKER (that module needs curses)
HOME_REQUEST_NAME = "home.request"
LAUNCHER_FOCUS_NAME = "launcher-focus"  # gamepad-nav forwards keys while the launcher has focus
SOURCE_MANIFESTS = pathlib.Path(__file__).resolve().parents[1] / "config/apps.d"
LOG = pathlib.Path("/var/log/couchliteos/launcher.log")
SPINNER = "|/-\\"
START_TIMEOUT = 18  # seconds an app has to write its -ready mark
FAILED_FOR = 2.75  # app units retry after two seconds: a failure this long is not recovering
STREAM_WINDOW_WORDS = {"moonlight": "moonlight", "chiaki-ng": "chiaki"}  # found in the listed window of a stream
WINDOW_MATCHES = {
    "firefox": ("app_id:firefox-esr", "app_id:firefox", "title:Mozilla Firefox"),
    "google-chrome": ("app_id:google-chrome", "title:Google Chrome"),
    "moonlight": ("app_id:moonlight", "title:Moonlight"),
    "chiaki-ng": ("app_id:chiaki", "app_id:io.github.streetpea.Chiaki4deck", "title:Chiaki"),
}
POWER_REQUESTS = ("suspend", "reboot", "poweroff")
# Which running apps the controller drives as a mouse (browsers, web apps).
POINTER_MODES = pointer.Modes()


def application_result() -> apps.LoadResult:
    system_dir = apps.SYSTEM_DIR if apps.SYSTEM_DIR.exists() else SOURCE_MANIFESTS
    result = apps.load_applications(system_dir=system_dir)
    # Firefox and Chrome have no tile until they are installed (ADD A WEB BROWSER).
    return apps.LoadResult(tuple(app for app in result.applications if apps.installed(app)), result.errors)


def set_launcher_focus(held: bool, path: pathlib.Path) -> None:
    try:
        if held:
            path.touch()
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def mark_front_app(app: apps.Application, run: pathlib.Path) -> None:
    """Name `app` in app-active, as its start did, so Guide follows the app brought to the front."""
    if app.terminal:
        return  # its start leaves the file alone too: gamepad-nav types into a terminal app
    active = run / "app-active"
    try:
        active.write_text(app.id + "\n", encoding="ascii")
        os.chmod(active, 0o640)
    except OSError:
        pass


def rdp_application(connection: rdp.Connection) -> apps.Application:
    """Launchable entry for a saved connection that is not pinned to the launcher."""
    return apps.Application(
        id=connection.id, name=connection.name.upper(), kind="rdp",
        connection=connection.id, status_id=connection.id,
    )


def active_rdp_session(run: pathlib.Path) -> str | None:
    try:
        return rdp.read_connection_id(run / rdp.SESSION.name)
    except (OSError, UnicodeError, ValueError):
        return None


def read_app_status(app_id: str, run: pathlib.Path) -> str:
    path = run / f"{app_id}-status"
    try:
        if path.is_symlink() or path.stat().st_size > 512:
            return ""
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return lines[0][:240] if lines else ""


def any_app_running(run: pathlib.Path) -> bool:
    """True when any app or stream shows a sign of life in /run/couchliteos, or when that cannot be told."""
    try:
        return any(item.name != "launcher-ready" for item in run.glob("*-ready"))
    except Exception:
        return True  # not knowing means do not restart


def focus_app(
    app: apps.Application, *, run: pathlib.Path, launcher_focus: pathlib.Path,
    pointer_modes: pointer.Modes = POINTER_MODES,
) -> bool:
    """Bring `app`'s window to the front (wlrctl); the controller goes with it."""
    matches = WINDOW_MATCHES.get(app.id)
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
            pointer_modes.apply(app)
            mark_front_app(app, run)
            set_launcher_focus(False, launcher_focus)
            return True
    keyword = STREAM_WINDOW_WORDS.get(app.id)
    if keyword is not None and focus_listed_toplevel(keyword):
        pointer_modes.apply(app)
        mark_front_app(app, run)
        set_launcher_focus(False, launcher_focus)
        return True
    return False


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
    display.log(f"no window for {keyword}; the compositor lists: {lines!r}", LOG)
    return False


def focus_launcher(title: str = "CouchLiteOS Launcher") -> None:
    """Bring the home screen (either front end: both windows carry this title) to the front."""
    try:
        subprocess.run(
            ["wlrctl", "toplevel", "focus", f"title:{title}"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        pass


class Session:
    """Start, resume and close applications and streams. Subclass it and supply the screen parts."""

    status = ""  # the result line a front end shows; the classic launcher makes this a property
    pending_stream: tuple[str, str] | None = None  # (target, app) while an auto-stream starts
    stream_by_hand = False  # True while a press (not the auto-stream) starts it: wording only

    # ------------------------------------------------------------------ environment

    @property
    def run_dir(self) -> pathlib.Path:
        return RUN

    @property
    def pointer_modes(self) -> pointer.Modes:
        return POINTER_MODES

    @property
    def home_request(self) -> pathlib.Path:
        return self.run_dir / HOME_REQUEST_NAME

    def set_launcher_focus(self, held: bool) -> None:
        set_launcher_focus(held, self.run_dir / LAUNCHER_FOCUS_NAME)

    def application_result(self) -> apps.LoadResult:
        return application_result()

    def active_rdp_session(self) -> str | None:
        return active_rdp_session(self.run_dir)

    def focus_app(self, app: apps.Application) -> bool:
        return focus_app(app, run=self.run_dir, launcher_focus=self.run_dir / LAUNCHER_FOCUS_NAME,
                         pointer_modes=self.pointer_modes)

    def read_app_status(self, app_id: str) -> str:
        return read_app_status(app_id, self.run_dir)

    def any_app_running(self) -> bool:
        return any_app_running(self.run_dir)

    # ------------------------------------------------------------------ screen parts (front ends override)

    def draw_launching(self, label: str, frame: str) -> None:
        pass

    def launch_wait_begin(self) -> None:
        pass

    def launch_wait(self) -> None:
        time.sleep(0.1)

    def launch_wait_end(self) -> None:
        pass

    def show_launch_failure(
        self, label: str, message: str, app: apps.Application | None = None, *, retry: bool = True
    ) -> str:
        self.status = f"{label}: {message}".upper()
        return "dismiss"

    def prepare_remote_desktop(self, app: apps.Application) -> bool:
        return True

    def wake_before_moonlight(self, app: apps.Application, auto: bool = False) -> bool:
        return True

    # ------------------------------------------------------------------ requests

    def prepare_session(self) -> None:
        """Tell the app units which Wayland display to use (session.env); the home screen starts
        without the controller hand-off marker a previous run may have left."""
        run = self.run_dir
        run.mkdir(mode=0o750, parents=True, exist_ok=True)
        display_name = os.environ.get("DISPLAY", ":0")
        wayland = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
        if not display_name.startswith(":") or "/" in display_name or "/" in wayland:
            raise RuntimeError("Cage supplied an invalid display environment")
        (run / "session.env").write_text(
            f"DISPLAY={display_name}\nWAYLAND_DISPLAY={wayland}\n", encoding="utf-8"
        )
        os.chmod(run / "session.env", 0o640)
        self.set_launcher_focus(False)

    def request(self, name: str) -> None:
        (self.run_dir / name).touch()

    def close_app(self, app: apps.Application) -> None:
        """Ask couchliteos-run-app (or the session unit) to close `app`."""
        (self.run_dir / f"close-{app.status_id}").touch()

    @property
    def stream_word(self) -> str:
        return "STREAM" if self.stream_by_hand else "AUTO-STREAM"

    def app_by_id(self, app_id: str) -> apps.Application | None:
        return next(
            (app for app in self.application_result().applications if app.id == app_id and app.enabled),
            None,
        )

    def launch_by_id(self, app_id: str) -> bool:
        app = self.app_by_id(app_id)
        if app is None:
            self.status = f"{app_id.upper()} IS NOT AVAILABLE: CHECK SETTINGS > APPLICATIONS"
            return False
        return self.launch_app(app)

    def running_applications(self) -> list[apps.Application]:
        running = [
            app for app in self.application_result().applications
            if app.enabled and (self.run_dir / f"{app.status_id}-ready").exists()
        ]
        session = self.active_rdp_session()
        if session and session not in {app.id for app in running} and (self.run_dir / f"{session}-ready").exists():
            connection = rdp.get_connection(session)
            if connection is not None:
                running.append(rdp_application(connection))
        return running

    def start_stream(self, host: stream.Host, app_name: str, *, by_hand: bool = False) -> bool:
        """Start Moonlight streaming `app_name` from `host`, for the auto-stream and the STREAM row.

        `by_hand` (the row) only changes the wording of the wake wait and its failures."""
        app = self.app_by_id("moonlight")
        if app is None:
            self.status = "MOONLIGHT IS UNAVAILABLE"
            return False
        request = self.run_dir / stream.STREAM_REQUEST.name
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

    def launch_app(self, app: apps.Application, *, quiet: bool = False, auto: bool = False, wake: bool = True) -> bool:
        """Start an application. `quiet` skips the failure dialog for a hidden one-off
        application whose caller explains a failed start in its own words. `auto` marks an
        auto-stream; `wake=False` skips waking the default PC (pairing may be for another one)."""
        run = self.run_dir
        label, app_id = app.name, app.status_id
        ready = run / f"{app_id}-ready"
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
        state = run / f"{app_id}-status"
        if app.kind == "rdp" and not self.prepare_remote_desktop(app):
            return False
        if app.id == "moonlight" and wake and not self.wake_before_moonlight(app, auto):
            return False
        if auto and self.pending_stream is not None:
            # Written again after the wake wait (it can outlast REQUEST_MAX_AGE) and before each
            # TRY AGAIN: the Moonlight start consumes the request.
            try:
                stream.write_stream_request(*self.pending_stream, run / stream.STREAM_REQUEST.name)
            except (OSError, ValueError) as error:
                self.status = f"{self.stream_word} NOT STARTED: {error}".upper()
                return False
        ready.unlink(missing_ok=True)
        state.unlink(missing_ok=True)
        self.pointer_modes.apply(app)
        self.set_launcher_focus(False)  # the starting app takes the controller
        if app.kind == "request":
            self.request(app.request)
        elif app.kind == "rdp":
            rdp.write_session_request(app.connection, run / rdp.REQUEST.name)
        else:
            apps.atomic_write(run / "launch-app.request", app.id + "\n")

        deadline = time.monotonic() + START_TIMEOUT
        failure_since: float | None = None
        frame = 0
        self.launch_wait_begin()
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
                    if now - failure_since >= FAILED_FOR:
                        if not quiet and self.show_launch_failure(label, app_state.removeprefix("failed:").strip(), app=app) == "retry":
                            return self.launch_app(app, quiet=quiet, auto=auto, wake=wake)
                        self.status = f"{label} FAILED TO START"
                        return False
                else:
                    failure_since = None
                self.launch_wait()
        finally:
            self.launch_wait_end()

        last_state = self.read_app_status(app_id)
        if app.kind == "rdp" and not (run / rdp.SESSION.name).exists():
            # The session never picked up the request: do not leave it (or a
            # typed password) waiting in /run for a later start.
            (run / rdp.REQUEST.name).unlink(missing_ok=True)
            (run / rdp.HANDOFF.name).unlink(missing_ok=True)
        elif app.kind == "request":
            (run / app.request).unlink(missing_ok=True)
        elif app.kind == "command":
            # Never leave a request behind to start the app unasked later.
            (run / "launch-app.request").unlink(missing_ok=True)
        message = (
            last_state.removeprefix("failed:").strip()
            if last_state.startswith("failed:")
            else "THE APPLICATION DID NOT BECOME READY BEFORE THE STARTUP TIMEOUT"
        )
        if not quiet and self.show_launch_failure(label, message, app=app) == "retry":
            return self.launch_app(app, quiet=quiet, auto=auto, wake=wake)
        self.status = f"{label} START TIMED OUT"
        return False

    def autostream_target(self) -> tuple[stream.Host, str] | None:
        """(PC, app) when Settings > STREAMING starts a stream by itself and it should start now:
        setup is complete and nothing else runs. A saved PC that is no longer paired says so in `status`."""
        try:
            config = stream.load_settings()
            if not config.autostart or not SETUP_MARKER.exists() or (self.run_dir / "app-active").exists():
                return None
            host = stream.autostream_host(stream.load_hosts(), config)
        except (OSError, ValueError):
            return None
        if host is None:
            self.status = "STREAMING: THE SAVED PC IS NOT PAIRED ON THIS SYSTEM"
            return None
        return host, config.app

    # ------------------------------------------------------------------ the 1 s tick

    def take_home_request(self) -> bool:
        """True once per Guide / Home press (gamepad-nav leaves home.request)."""
        if not self.home_request.exists():
            return False
        self.home_request.unlink(missing_ok=True)
        return True

    def take_resumed(self) -> bool:
        """True once after the system wakes from sleep (couchliteos-resume.service drops the marker);
        a stale sleep request and the Home press that slept the box are dropped with it."""
        marker = self.run_dir / "resumed"
        if not marker.exists():
            return False
        marker.unlink(missing_ok=True)
        (self.run_dir / "suspend").unlink(missing_ok=True)  # a stale request must not suspend again
        self.home_request.unlink(missing_ok=True)
        return True

    def may_offer_update(self, blanked: bool = False) -> bool:
        """Whether the home screen may ask about a new release now: never over an app, a stream,
        the on-screen keyboard, a pending Home press or a blank screen."""
        run = self.run_dir
        if blanked or self.home_request.exists() or (run / "app-active").exists() or self.any_app_running():
            return False
        return not ((run / "osk-active").exists() or (run / "start-osk").exists())
