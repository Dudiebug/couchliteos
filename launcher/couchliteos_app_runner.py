#!/usr/bin/python3
"""Run one validated configured application without a shell."""

from __future__ import annotations

import os
import pathlib
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable

import couchliteos_apps as apps
import couchliteos_rdp as rdp


RUN = pathlib.Path("/run/couchliteos")
REQUEST = RUN / "launch-app.request"
READY_SECONDS = 5.0
FOOT = "/usr/libexec/couchliteos-foot"  # picks the font size for the screen, then runs foot
# sdl-freerdp3 exit statuses (FreeRDP 3.15 client/SDL/SDL3/sdl_freerdp.cpp).
RDP_NORMAL_EXITS = {0, 1, 2, 3, 4, 5, 11}
RDP_FINAL_EXITS = {
    6: "the client ran out of memory",
    7: "the server denied the connection",
    8: "the server denied the connection",
    9: "this account is not allowed to connect",
    10: "the server requires fresh credentials",
    128: "the connection settings were rejected",
    129: "the client ran out of memory",
    132: "authentication failed; check the username, domain, and password",
    133: "security negotiation failed",
    134: "sign-in failed; check the username, domain, and password",
    135: "the account is locked out",
    136: "the client could not prepare the connection",
    138: "the client could not finish connecting",
    143: "the TLS connection failed; the server certificate may have changed",
    144: "this account does not have permission to connect",
    145: "the connection was cancelled",
    148: "the password has expired",
    149: "the password must be changed before signing in",
    150: "the Kerberos server is unreachable",
    151: "the account is disabled",
    152: "the password has expired",
    153: "the client certificate was revoked",
    154: "the password is wrong",
    155: "access was denied",
    156: "the account has a sign-in restriction",
    157: "the account has expired",
    158: "this account may not sign in remotely",
    159: "a username and password are required",
}
RDP_FINAL_EXITS.update({code: "remote desktop licensing failed" for code in range(16, 27)})
# FreeRDP returns negative COMMAND_LINE_ERROR values (-1000..-1006) when it cannot
# parse its arguments or read the password from stdin; as exit statuses these are
# 24..18, which overlap the licensing codes.
RDP_FINAL_EXITS.update({
    code: "the client rejected its settings, could not read the password, or licensing failed"
    for code in range(18, 25)
})
EXIT_FINAL = 65
EXIT_RETRY = 75


class FinalError(Exception):
    """A Remote Desktop failure that retrying cannot fix."""


def atomic_status(path: pathlib.Path, value: str) -> None:
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o640)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value.rstrip("\n") + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def read_request(path: pathlib.Path = REQUEST) -> str:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64:
            raise ValueError("request is not a bounded regular file")
        app_id = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as error:
        raise ValueError(f"cannot read request: {error}") from error
    if not apps.ID_RE.fullmatch(app_id):
        raise ValueError("invalid application id")
    return app_id


def command_vector(app: apps.Application) -> list[str]:
    if app.kind != "command" or not pathlib.PurePath(app.command).is_absolute():
        raise ValueError("application is not a configured command")
    try:
        arguments = shlex.split(app.arguments, posix=True)
    except ValueError as error:
        raise ValueError(f"invalid command arguments: {error}") from error
    command = [app.command, *arguments]
    if app.terminal:
        return [FOOT, "--fullscreen", "--title", app.name, "--", *command]
    return command


def configured_environment(app: apps.Application) -> dict[str, str]:
    environment = os.environ.copy()
    for name, value in app.environment.items():
        if not apps.ENV_RE.fullmatch(name):
            raise ValueError(f"invalid environment name: {name}")
        environment[name] = value
    return environment


def supervise(
    process: subprocess.Popen[bytes],
    ready: pathlib.Path,
    status: pathlib.Path,
    close: pathlib.Path,
    alive: Callable[[], bool] | None = None,
) -> tuple[int, bool]:
    """Mark the process ready after it survives startup and honour close requests.

    When `alive` reports that the process's environment is gone, the process
    group is stopped as if closed, but the close is not reported as requested.
    """
    close_requested = False
    deadline = time.monotonic() + READY_SECONDS
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if process.poll() is None:
        ready.touch(mode=0o640)
        atomic_status(status, "started")
    while process.poll() is None:
        lost = alive is not None and not alive()
        if lost or close.exists():
            close_requested = not lost
            close.unlink(missing_ok=True)
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            break
        time.sleep(0.1)
    return process.wait(), close_requested


def run(request_path: pathlib.Path = REQUEST) -> int:
    app_id = "configured-app"
    status = RUN / f"{app_id}-status"
    ready = RUN / f"{app_id}-ready"
    active = RUN / "app-active"
    close = RUN / f"close-{app_id}"
    process: subprocess.Popen[bytes] | None = None
    received_signal = 0

    def stop(signum: int, _frame: object) -> None:
        nonlocal received_signal
        received_signal = signum
        if process and process.poll() is None:
            try:
                os.killpg(process.pid, signum)
            except ProcessLookupError:
                pass

    previous = {signum: signal.signal(signum, stop) for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        try:
            app_id = read_request(request_path)
        finally:
            request_path.unlink(missing_ok=True)
        status = RUN / f"{app_id}-status"
        ready = RUN / f"{app_id}-ready"
        result = apps.load_applications()
        app = next((item for item in result.applications if item.id == app_id), None)
        if app is None or not app.enabled:
            raise ValueError("requested application is missing or disabled")
        # Reload the selected file itself so the runner never trusts launcher state.
        if app.path is None:
            raise ValueError("requested application has no manifest")
        app = apps.read_manifest(app.path, system=app.system)
        if app.id != app_id:
            raise ValueError("application manifest changed during launch")
        status = RUN / f"{app.status_id}-status"
        ready = RUN / f"{app.status_id}-ready"
        close = RUN / f"close-{app.status_id}"
        close.unlink(missing_ok=True)
        ready.unlink(missing_ok=True)
        atomic_status(status, "starting")
        if not pathlib.Path(app.command).is_file() or not os.access(app.command, os.X_OK):
            raise FileNotFoundError("application executable is missing")
        vector = command_vector(app)
        if not app.terminal:
            # app-active silences gamepad-nav, but a terminal app (nmtui, a
            # "Press ENTER" prompt) is driven by the keys it forwards.
            active.write_text(app.id + "\n", encoding="ascii")
            os.chmod(active, 0o640)
        process = subprocess.Popen(
            vector,
            env=configured_environment(app),
            start_new_session=True,
        )
        rc, close_requested = supervise(process, ready, status, close)
        if close_requested:
            rc = 0
        if received_signal:
            rc = 128 + received_signal
        if ready.exists() or rc == 0:  # only a nonzero status is a failed start
            atomic_status(status, f"exited: status {rc}")
        else:
            atomic_status(status, f"failed: exited before the application became ready (status {rc})")
        return rc
    except (OSError, ValueError, apps.ManifestError) as error:
        atomic_status(status, f"failed: {error}")
        print(f"Configured application failed: {error}", file=sys.stderr)
        return 66
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        ready.unlink(missing_ok=True)
        close.unlink(missing_ok=True)
        try:
            if active.read_text(encoding="ascii").strip() == app_id:
                active.unlink()
        except OSError:
            pass


def session_connection(request_path: pathlib.Path, session_path: pathlib.Path) -> str:
    """Consume a new launcher request, or reuse the session after a crash restart."""
    try:
        connection_id = rdp.read_connection_id(request_path)
    except (OSError, UnicodeError, ValueError) as error:
        request_path.unlink(missing_ok=True)
        raise FinalError(f"invalid Remote Desktop request: {error}") from error
    if connection_id is not None:
        apps.atomic_write(session_path, connection_id + "\n")
        request_path.unlink(missing_ok=True)
        return connection_id
    connection_id = rdp.read_connection_id(session_path)
    if connection_id is None:
        raise FinalError("no Remote Desktop connection was requested")
    return connection_id


def native_size() -> str | None:
    try:
        import couchliteos_display as display

        output = display.active_output(display.query_outputs())
    except Exception:  # noqa: BLE001 - the size is only a preference
        return None
    if output is None:
        return None
    mode = output.current_mode or next((item for item in output.modes if item.preferred), None)
    return mode.resolution if mode else None


def compositor_watch(environment: dict[str, str]) -> Callable[[], bool] | None:
    """Report whether the Wayland socket the client connected to still exists.

    SDL3 clients may spin instead of exiting when Cage restarts, so a replaced
    or missing socket ends the session and systemd reconnects to the new one.
    """
    runtime, display = environment.get("XDG_RUNTIME_DIR", ""), environment.get("WAYLAND_DISPLAY", "")
    if not runtime or not display:
        return None
    socket_path = pathlib.Path(display if display.startswith("/") else f"{runtime}/{display}")
    try:
        original = socket_path.stat().st_ino
    except OSError:
        return None

    def alive() -> bool:
        try:
            return socket_path.stat().st_ino == original
        except OSError:
            return False

    return alive


def rdp_result(rc: int) -> tuple[int, str]:
    """Map a client exit status to the unit exit status and a launcher status line."""
    if rc in RDP_NORMAL_EXITS:
        return 0, "exited: disconnected"
    if rc in RDP_FINAL_EXITS:
        return EXIT_FINAL, f"failed: {RDP_FINAL_EXITS[rc]}"
    if rc < 0:
        return EXIT_RETRY, f"failed: the client stopped (signal {-rc}); reconnecting"
    return EXIT_RETRY, f"failed: the connection was lost (status {rc}); reconnecting"


def run_rdp(run_dir: pathlib.Path | None = None, connections: pathlib.Path = rdp.CONNECTIONS) -> int:
    run_dir = run_dir or RUN
    connection_id = "rdp"
    status = run_dir / "rdp-status"
    ready = run_dir / "rdp-ready"
    close = run_dir / "close-rdp"
    active = run_dir / "app-active"
    process: subprocess.Popen[bytes] | None = None
    received_signal = 0
    finished = False

    def stop(signum: int, _frame: object) -> None:
        nonlocal received_signal
        received_signal = signum
        if process and process.poll() is None:
            try:
                os.killpg(process.pid, signum)
            except ProcessLookupError:
                pass
            # sdl-freerdp3 may ignore SIGTERM; the close path escalates to SIGKILL.
            try:
                close.touch()
            except OSError:
                pass

    previous = {signum: signal.signal(signum, stop) for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        connection_id = session_connection(run_dir / rdp.REQUEST.name, run_dir / rdp.SESSION.name)
        status = run_dir / f"{connection_id}-status"
        ready = run_dir / f"{connection_id}-ready"
        close = run_dir / f"close-{connection_id}"
        close.unlink(missing_ok=True)
        ready.unlink(missing_ok=True)
        atomic_status(status, "starting")
        connection = rdp.get_connection(connection_id, connections)
        if connection is None:
            raise FinalError("the saved connection no longer exists")
        if not connection.certificate:
            raise FinalError("the server certificate has not been verified; connect from the launcher")
        handoff = rdp.read_handoff(run_dir / rdp.HANDOFF.name)
        if handoff is None:
            raise FinalError(
                "the saved password is missing; save it again in Settings"
                if connection.save_password
                else "a password is required; connect again from the launcher"
            )
        handoff_id, password = handoff
        if handoff_id != connection_id:
            raise FinalError("the password was given for another connection; connect again from the launcher")
        if not os.access(rdp.CLIENT, os.X_OK):
            raise FinalError("the Remote Desktop client is not installed")
        vector = rdp.freerdp_arguments(connection, native_size())
        environment = rdp.client_environment(dict(os.environ))
        active.write_text(connection_id + "\n", encoding="ascii")
        os.chmod(active, 0o640)
        process = subprocess.Popen(
            vector,
            env=environment,
            stdin=subprocess.PIPE,
            start_new_session=True,
        )
        # /from-stdin:force reads exactly one password line before connecting.
        try:
            if process.stdin is not None:
                process.stdin.write((password + "\n").encode("utf-8"))
                process.stdin.close()
        except BrokenPipeError:
            pass
        password = ""
        rc, close_requested = supervise(process, ready, status, close, compositor_watch(environment))
        if received_signal:
            finished = True
            atomic_status(status, "exited: stopped")
            return 128 + received_signal
        code, message = rdp_result(0 if close_requested else rc)
        atomic_status(status, message)
        finished = code != EXIT_RETRY
        return code
    except (FinalError, OSError, ValueError, apps.ManifestError) as error:
        finished = True
        atomic_status(status, f"failed: {error}")
        print(f"Remote Desktop session failed: {error}", file=sys.stderr)
        return EXIT_FINAL
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        ready.unlink(missing_ok=True)
        close.unlink(missing_ok=True)
        try:
            if active.read_text(encoding="ascii").strip() == connection_id:
                active.unlink()
        except OSError:
            pass
        if finished:
            rdp.clear_session_state(run_dir)


def cleanup_rdp(run_dir: pathlib.Path | None = None) -> int:
    """OnFailure handler: forget a session whose restarts are exhausted.

    A pending rdp.request is left alone: it is a newer launch, and the re-armed
    path unit starts it.
    """
    run_dir = run_dir or RUN
    try:
        connection_id = rdp.read_connection_id(run_dir / rdp.SESSION.name)
    except (OSError, UnicodeError, ValueError):
        connection_id = None
    # The pending request's own password (same connection id) must outlive the cleanup.
    try:
        pending = rdp.read_connection_id(run_dir / rdp.REQUEST.name)
        handoff = rdp.read_handoff(run_dir / rdp.HANDOFF.name)
        keep_handoff = pending is not None and handoff is not None and handoff[0] == pending
    except (OSError, UnicodeError, ValueError):
        keep_handoff = False
    rdp.clear_session_state(run_dir, keep_handoff)
    if connection_id:
        (run_dir / f"{connection_id}-ready").unlink(missing_ok=True)
        status = run_dir / f"{connection_id}-status"
        try:
            if status.read_text(encoding="utf-8").rstrip().endswith("reconnecting"):
                atomic_status(status, "failed: the connection was lost and could not be restored")
        except OSError:
            pass
        try:
            if (run_dir / "app-active").read_text(encoding="ascii").strip() == connection_id:
                (run_dir / "app-active").unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--rdp"]:
        raise SystemExit(run_rdp())
    if sys.argv[1:] == ["--rdp-cleanup"]:
        raise SystemExit(cleanup_rdp())
    raise SystemExit(run())
