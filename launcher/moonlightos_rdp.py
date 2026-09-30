#!/usr/bin/python3
"""Saved Remote Desktop connections, certificate pinning, and password handoff.

Connections are ordinary settings owned by the appliance user. Passwords are
never stored here: a prompted password is handed to the session runner through
a 0600 file in RAM-backed /run/moonlightos, and an optionally saved password is
kept in a root-owned 0600 file that only the root helper can read or write.
"""

from __future__ import annotations

import configparser
import dataclasses
import hashlib
import io
import ipaddress
import json
import os
import pathlib
import re
import secrets
import socket
import ssl
import stat
import struct
import tempfile
import time

import moonlightos_apps as apps


DATA = pathlib.Path("/var/lib/moonlightos")
CONNECTIONS = DATA / "rdp" / "connections.ini"
SECRETS_NAME = "rdp-secrets"
RUN = pathlib.Path("/run/moonlightos")
REQUEST = RUN / "rdp.request"
SESSION = RUN / "rdp-session"
HANDOFF = RUN / "rdp-session.secret"
SECRET_REQUEST = RUN / "rdp-secret.request"
SECRET_STATUS = RUN / "rdp-secret.status"
CLIENT = "/usr/bin/sdl-freerdp3"
WAYLAND_APP_ID = "moonlightos-rdp"

DEFAULT_PORT = 3389
MAX_CONNECTIONS = 64
MAX_FILE = 64 * 1024
MAX_PASSWORD = 512
MAX_USERNAME = 256
MAX_DOMAIN = 255
MAX_HOST = 253
RESOLUTION_PRESETS = ("1280x720", "1600x900", "1920x1080", "2560x1440", "3840x2160")
HOST_LABEL = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")
RESOLUTION_RE = re.compile(r"^([1-9][0-9]{2,3})x([1-9][0-9]{2,3})$")
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
REQUEST_ID_RE = re.compile(r"^[0-9a-f]{24}$")
FIELDS = {
    "name", "host", "port", "username", "domain", "resolution",
    "fullscreen", "audio", "clipboard", "save_password", "certificate",
}


class RdpError(ValueError):
    """A connection, request, or secret failed validation."""


@dataclasses.dataclass(frozen=True)
class Connection:
    id: str
    name: str
    host: str
    username: str
    port: int = DEFAULT_PORT
    domain: str = ""
    resolution: str = "native"
    fullscreen: bool = True
    audio: bool = True
    clipboard: bool = True
    save_password: bool = False
    # SHA-256 of the server's TLS certificate (lowercase hex) accepted by the user.
    certificate: str = ""


def _text(value: str, field: str, limit: int, *, required: bool = True, forbidden: str = "") -> str:
    value = value.strip()
    if required and not value:
        raise RdpError(f"{field} is required")
    if len(value) > limit:
        raise RdpError(f"{field} is too long")
    if any(not character.isprintable() or character in forbidden for character in value):
        raise RdpError(f"{field} contains an unsupported character")
    return value


def validate_name(value: str) -> str:
    return _text(value, "display name", apps.MAX_NAME)


def validate_host(value: str) -> str:
    value = _text(value, "host", MAX_HOST).rstrip(".").lower()
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        address = None
    if address is not None:
        if address.version != 4:
            raise RdpError("IPv6 is disabled on MoonlightOS; use an IPv4 address or host name")
        return str(address)
    labels = value.split(".")
    if not all(HOST_LABEL.fullmatch(label) for label in labels) or labels[-1].isdigit():
        raise RdpError("host must be an IPv4 address or DNS name")
    return value


def validate_port(value: object) -> int:
    try:
        port = int(str(value).strip())
    except ValueError as error:
        raise RdpError("port must be a number") from error
    if not 1 <= port <= 65535:
        raise RdpError("port must be between 1 and 65535")
    return port


def validate_username(value: str) -> str:
    return _text(value, "username", MAX_USERNAME)


def validate_domain(value: str) -> str:
    return _text(value, "domain", MAX_DOMAIN, required=False, forbidden=" \\/@")


def validate_resolution(value: str) -> str:
    value = value.strip().lower()
    if value in {"native", "fit"}:
        return value
    match = RESOLUTION_RE.fullmatch(value)
    if not match:
        raise RdpError("resolution must be native, fit, or WIDTHxHEIGHT")
    width, height = map(int, match.groups())
    if not (640 <= width <= 8192 and 480 <= height <= 8192):
        raise RdpError("custom resolution must be between 640x480 and 8192x8192")
    return f"{width}x{height}"


def normalize_fingerprint(value: str) -> str:
    value = re.sub(r"[\s:]", "", value).lower()
    if value and not FINGERPRINT_RE.fullmatch(value):
        raise RdpError("certificate fingerprint must be a SHA-256 value")
    return value


def validate_password(value: str) -> str:
    if len(value) > MAX_PASSWORD or any(character in value for character in "\0\r\n"):
        raise RdpError("password contains an unsupported character or is too long")
    return value


def validate_id(value: str) -> str:
    if not apps.ID_RE.fullmatch(value) or not value.startswith("rdp-"):
        raise RdpError("invalid connection id")
    return value


def validate(connection: Connection) -> Connection:
    return dataclasses.replace(
        connection,
        id=validate_id(connection.id),
        name=validate_name(connection.name),
        host=validate_host(connection.host),
        port=validate_port(connection.port),
        username=validate_username(connection.username),
        domain=validate_domain(connection.domain),
        resolution=validate_resolution(connection.resolution),
        certificate=normalize_fingerprint(connection.certificate),
    )


def connection_id(name: str, existing: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:28] or "connection"
    return apps.application_id(f"rdp-{base}", existing)


def _bounded_text(path: pathlib.Path, limit: int) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise RdpError(f"{path.name} is not a bounded regular file")
        return stream.read(limit + 1).decode("utf-8")


def _boolean(section: configparser.SectionProxy, name: str, fallback: bool) -> bool:
    try:
        return section.getboolean(name, fallback=fallback)
    except ValueError as error:
        raise RdpError(f"{name} must be true or false") from error


def load_connections(path: pathlib.Path = CONNECTIONS) -> tuple[tuple[Connection, ...], tuple[str, ...]]:
    try:
        raw = _bounded_text(path, MAX_FILE)
    except FileNotFoundError:
        return (), ()
    except (OSError, UnicodeError, RdpError) as error:
        return (), (f"{path.name}: {error}",)
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        parser.read_string(raw)
    except configparser.Error as error:
        return (), (f"{path.name}: invalid INI: {error}",)
    connections: list[Connection] = []
    errors: list[str] = []
    for section_name in parser.sections()[:MAX_CONNECTIONS]:
        section = parser[section_name]
        try:
            unknown = set(section) - FIELDS
            if unknown:
                raise RdpError(f"unsupported field {sorted(unknown)[0]}")
            connections.append(validate(Connection(
                id=section_name,
                name=section.get("name", ""),
                host=section.get("host", ""),
                username=section.get("username", ""),
                port=section.get("port", str(DEFAULT_PORT)),
                domain=section.get("domain", ""),
                resolution=section.get("resolution", "native"),
                fullscreen=_boolean(section, "fullscreen", True),
                audio=_boolean(section, "audio", True),
                clipboard=_boolean(section, "clipboard", True),
                save_password=_boolean(section, "save_password", False),
                certificate=section.get("certificate", ""),
            )))
        except RdpError as error:
            errors.append(f"{section_name}: {error}")
    if len(parser.sections()) > MAX_CONNECTIONS:
        errors.append(f"only the first {MAX_CONNECTIONS} connections were loaded")
    return tuple(connections), tuple(errors)


def serialize(connections: list[Connection] | tuple[Connection, ...]) -> str:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    for item in connections:
        item = validate(item)
        parser[item.id] = {
            "name": item.name,
            "host": item.host,
            "port": str(item.port),
            "username": item.username,
            "domain": item.domain,
            "resolution": item.resolution,
            "fullscreen": str(item.fullscreen).lower(),
            "audio": str(item.audio).lower(),
            "clipboard": str(item.clipboard).lower(),
            "save_password": str(item.save_password).lower(),
            "certificate": item.certificate,
        }
    output = io.StringIO()
    parser.write(output, space_around_delimiters=True)
    return output.getvalue()


def write_connections(
    connections: list[Connection] | tuple[Connection, ...], path: pathlib.Path = CONNECTIONS
) -> None:
    if len(connections) > MAX_CONNECTIONS:
        raise RdpError(f"at most {MAX_CONNECTIONS} connections can be saved")
    if len({item.id for item in connections}) != len(connections):
        raise RdpError("duplicate connection id")
    apps.atomic_write(path, serialize(connections))


def get_connection(connection_id_value: str, path: pathlib.Path = CONNECTIONS) -> Connection | None:
    connections, _errors = load_connections(path)
    return next((item for item in connections if item.id == connection_id_value), None)


def upsert_connection(connection: Connection, path: pathlib.Path = CONNECTIONS) -> None:
    connection = validate(connection)
    connections, errors = load_connections(path)
    if errors and path.exists() and not connections:
        raise RdpError("saved connections are unreadable; refusing to overwrite them")
    updated = [connection if item.id == connection.id else item for item in connections]
    if connection.id not in {item.id for item in connections}:
        updated.append(connection)
    write_connections(updated, path)


def remove_connection(connection_id_value: str, path: pathlib.Path = CONNECTIONS) -> None:
    connections, _errors = load_connections(path)
    write_connections([item for item in connections if item.id != connection_id_value], path)


def _display_size(native_size: str | None) -> str | None:
    if native_size and RESOLUTION_RE.fullmatch(native_size):
        return native_size
    return None


def freerdp_arguments(connection: Connection, native_size: str | None = None) -> list[str]:
    """Return the argv for one validated connection; no secret is ever included."""
    connection = validate(connection)
    if not connection.certificate:
        raise RdpError("the server certificate has not been verified")
    vector = [
        CLIENT,
        f"/v:{connection.host}",
        f"/port:{connection.port}",
        f"/u:{connection.username}",
    ]
    if connection.domain:
        vector.append(f"/d:{connection.domain}")
    vector += [
        # The password is read from stdin during argument parsing.
        "/from-stdin:force",
        # Only the fingerprint the user accepted is trusted; never prompt.
        f"/cert:deny,fingerprint:sha256:{connection.certificate}",
        # Legacy RDP security has no TLS certificate to pin.
        "/sec:rdp:off",
        f"/title:{connection.name}",
        "/log-level:WARN",
        "+auto-reconnect",
    ]
    size = _display_size(native_size)
    if connection.resolution == "native":
        if size:
            vector.append(f"/size:{size}")
        elif not connection.fullscreen:
            vector.append("+dynamic-resolution")
    elif connection.resolution == "fit":
        vector.append("+dynamic-resolution")
    else:
        vector += [f"/size:{connection.resolution}", "/smart-sizing"]
    if connection.fullscreen:
        vector.append("/f")
    vector.append("/sound:sys:pulse" if connection.audio else "/audio-mode:1")
    vector.append("/clipboard:files-to:off" if connection.clipboard else "-clipboard")
    return vector


def client_environment(base: dict[str, str]) -> dict[str, str]:
    environment = {key: value for key, value in base.items() if key != "DISPLAY"}
    # SDL3 must use Cage's Wayland socket; there is no Xwayland fallback.
    environment.update({
        "SDL_VIDEO_DRIVER": "wayland",
        "SDL_VIDEODRIVER": "wayland",
        "SDL_APP_ID": WAYLAND_APP_ID,
    })
    return environment


def _read_exact(connection: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise RdpError("server closed the connection during negotiation")
        data += chunk
    return data


def negotiation_request() -> bytes:
    # TPKT + X.224 Connection Request + RDP_NEG_REQ asking for TLS, NLA, or NLA-EX.
    return struct.pack(">BBH", 3, 0, 19) + bytes((14, 0xE0, 0, 0, 0, 0, 0)) + struct.pack(
        "<BBHI", 1, 0, 8, 0x0000000B
    )


def parse_negotiation_response(header: bytes, body: bytes) -> int:
    if len(header) != 4 or header[0] != 3:
        raise RdpError("server did not answer as an RDP server")
    if len(body) < 7 or body[1] & 0xF0 != 0xD0:
        raise RdpError("server refused the RDP connection request")
    if len(body) < 15:
        raise RdpError("server only offers legacy RDP security; TLS is required")
    kind, _flags, _length, value = struct.unpack("<BBHI", body[7:15])
    if kind == 3:
        raise RdpError(f"server rejected TLS negotiation (code {value})")
    if kind != 2 or value == 0:
        raise RdpError("server only offers legacy RDP security; TLS is required")
    return value


def probe_certificate(host: str, port: int, timeout: float = 6.0) -> str:
    """Return the SHA-256 fingerprint of the certificate the server presents."""
    host = validate_host(host)
    port = validate_port(port)
    with socket.create_connection((host, port), timeout=timeout) as raw:
        raw.settimeout(timeout)
        raw.sendall(negotiation_request())
        header = _read_exact(raw, 4)
        length = struct.unpack(">H", header[2:4])[0]
        if not 11 <= length <= 256:
            raise RdpError("server sent an invalid negotiation response")
        parse_negotiation_response(header, _read_exact(raw, length - 4))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # Nothing is trusted here: the user compares this fingerprint and pins it.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            context.set_ciphers("DEFAULT:@SECLEVEL=0")
        except ssl.SSLError:
            pass
        server_name = None if re.fullmatch(r"[0-9.]+", host) else host
        with context.wrap_socket(raw, server_hostname=server_name) as tls:
            certificate = tls.getpeercert(binary_form=True)
    if not certificate:
        raise RdpError("server did not present a TLS certificate")
    return hashlib.sha256(certificate).hexdigest()


def fingerprint_lines(value: str, per_line: int = 8) -> list[str]:
    value = normalize_fingerprint(value)
    pairs = [value[index:index + 2].upper() for index in range(0, len(value), 2)]
    return [":".join(pairs[index:index + per_line]) for index in range(0, len(pairs), per_line)]


def _atomic_private(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def read_owned(path: pathlib.Path, limit: int, owner_uid: int | None = None) -> str | None:
    """Read a small regular file without following links, checking its owner.

    Without an explicit owner, files owned by this process or by the appliance
    account (which apps.atomic_write assigns) are accepted.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise RdpError(f"{path.name} is not a bounded regular file")
        expected = {os.getuid(), apps._owner()[0]} if owner_uid is None else {owner_uid}
        if info.st_uid not in expected:
            raise RdpError(f"{path.name} has an unexpected owner")
        return stream.read(limit + 1).decode("utf-8")


def _handoff_text(connection_id_value: str, password: str) -> str:
    return f"{validate_id(connection_id_value)}\n{validate_password(password)}"


def write_handoff(connection_id_value: str, password: str, path: pathlib.Path = HANDOFF) -> None:
    """Hand one connection's password to the session runner (RAM-backed, 0600)."""
    _atomic_private(path, _handoff_text(connection_id_value, password))


def read_handoff(path: pathlib.Path = HANDOFF, owner_uid: int | None = None) -> tuple[str, str] | None:
    """Return (connection id, password); the id binds the password to one session."""
    value = read_owned(path, MAX_PASSWORD * 4 + 64, owner_uid)
    if value is None:
        return None
    connection_id_value, separator, password = value.partition("\n")
    if not separator:
        raise RdpError("password handoff has no connection id")
    return validate_id(connection_id_value), validate_password(password)


def write_session_request(connection_id_value: str, path: pathlib.Path = REQUEST) -> None:
    apps.atomic_write(path, validate_id(connection_id_value) + "\n")


def read_connection_id(path: pathlib.Path, owner_uid: int | None = None) -> str | None:
    value = read_owned(path, 64, owner_uid)
    return None if value is None else validate_id(value.strip())


def clear_session_state(run: pathlib.Path = RUN, keep_handoff: bool = False) -> None:
    for name in (SESSION.name,) if keep_handoff else (HANDOFF.name, SESSION.name):
        (run / name).unlink(missing_ok=True)


SECRET_OPERATIONS = {"set", "delete", "stage"}


def submit_secret_request(
    operation: str, connection_id_value: str, password: str = "", path: pathlib.Path = SECRET_REQUEST
) -> str:
    """Ask the root helper to save, delete, or stage (hand off) a saved password."""
    if operation not in SECRET_OPERATIONS:
        raise RdpError("unsupported password operation")
    request_id = secrets.token_hex(12)
    payload: dict[str, str] = {"request_id": request_id, "op": operation, "id": validate_id(connection_id_value)}
    if operation == "set":
        payload["password"] = validate_password(password)
    _atomic_private(path, json.dumps(payload, separators=(",", ":")) + "\n")
    return request_id


def parse_secret_request(text: str) -> dict[str, str]:
    payload = json.loads(text)
    if not isinstance(payload, dict) or set(payload) - {"request_id", "op", "id", "password"}:
        raise RdpError("invalid password request structure")
    if not REQUEST_ID_RE.fullmatch(str(payload.get("request_id", ""))):
        raise RdpError("invalid password request id")
    operation = payload.get("op")
    if operation not in SECRET_OPERATIONS:
        raise RdpError("invalid password request operation")
    validate_id(str(payload.get("id", "")))
    if operation == "set":
        if not isinstance(payload.get("password"), str):
            raise RdpError("password request has no password")
        validate_password(payload["password"])
    elif "password" in payload:
        raise RdpError(f"{operation} request must not carry a password")
    return payload


def read_secret_status(request_id: str, path: pathlib.Path = SECRET_STATUS) -> dict[str, str] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("request_id") != request_id:
        return None
    return {key: str(payload.get(key, "")) for key in ("request_id", "state", "message")}


def wait_secret_status(request_id: str, timeout: float = 10.0, path: pathlib.Path = SECRET_STATUS) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = read_secret_status(request_id, path)
        if state and state["state"] in {"success", "failed"}:
            return state["state"] == "success", state["message"]
        time.sleep(0.1)
    return False, "the password service did not respond"


def secret_binding(connection: Connection) -> dict[str, str]:
    """Server settings a saved password belongs to; staging refuses any change."""
    connection = validate(connection)
    return {
        "host": connection.host,
        "port": str(connection.port),
        "username": connection.username,
        "domain": connection.domain,
    }


class SecretStore:
    """Root-only saved passwords in DATA/rdp-secrets, opened without following links.

    Each file holds the password and the server settings it was saved for, so a
    connection edited to point elsewhere cannot receive it.
    """

    def __init__(self, data: pathlib.Path = DATA, owner_uid: int = 0) -> None:
        self.data = data
        self.owner_uid = owner_uid

    def _directory(self) -> int:
        parent = os.open(self.data, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            try:
                os.mkdir(SECRETS_NAME, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
            directory = os.open(
                SECRETS_NAME, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent
            )
        finally:
            os.close(parent)
        info = os.fstat(directory)
        if info.st_uid != self.owner_uid:
            if os.geteuid() != 0:
                os.close(directory)
                raise RdpError("saved-password directory has an unexpected owner")
            # A recursive ownership change (for example systemd StateDirectory=)
            # must never leave saved passwords readable by the appliance user.
            os.fchmod(directory, 0o700)
            os.fchown(directory, self.owner_uid, 0)
            for name in os.listdir(directory):
                try:
                    entry = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
                except OSError:
                    os.unlink(name, dir_fd=directory)  # links and other oddities are discarded
                    continue
                try:
                    os.fchown(entry, self.owner_uid, 0)
                    os.fchmod(entry, 0o600)
                finally:
                    os.close(entry)
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.fchmod(directory, 0o700)
        return directory

    def store(self, connection: Connection, password: str) -> None:
        name = validate_id(connection.id)
        record = {"password": validate_password(password), **secret_binding(connection)}
        data = json.dumps(record, separators=(",", ":")).encode("utf-8")
        directory = self._directory()
        temporary = f".{name}.{secrets.token_hex(6)}"
        try:
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                dir_fd=directory,
            )
            try:
                os.fchmod(descriptor, 0o600)
                os.write(descriptor, data)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)

    def read(self, connection_id_value: str) -> dict[str, str] | None:
        """Return the stored record (password plus its server binding)."""
        name = validate_id(connection_id_value)
        directory = self._directory()
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        except FileNotFoundError:
            return None
        finally:
            os.close(directory)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner_uid or info.st_size > MAX_FILE:
                raise RdpError("saved password file is not a private regular file")
            try:
                record = json.loads(stream.read(MAX_FILE + 1).decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as error:
                raise RdpError("saved password file is unreadable") from error
        if not isinstance(record, dict) or set(record) != {"password", "host", "port", "username", "domain"}:
            raise RdpError("saved password file is unreadable")
        record = {key: str(value) for key, value in record.items()}
        validate_password(record["password"])
        return record

    def password_for(self, connection: Connection) -> str | None:
        """The saved password, only if it was saved for these exact server settings."""
        record = self.read(connection.id)
        if record is None:
            return None
        if {key: record[key] for key in ("host", "port", "username", "domain")} != secret_binding(connection):
            raise RdpError("the saved password belongs to different server settings; save it again")
        return record["password"]

    def delete(self, connection_id_value: str) -> None:
        name = validate_id(connection_id_value)
        directory = self._directory()
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass
        finally:
            os.close(directory)


def stage_handoff(
    connection_id_value: str, password: str, run: pathlib.Path = RUN, uid: int | None = None, gid: int | None = None
) -> None:
    """Root side: place a saved password where the unprivileged runner reads it."""
    data = _handoff_text(connection_id_value, password).encode("utf-8")
    directory = os.open(run, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    temporary = f".{HANDOFF.name}.{secrets.token_hex(6)}"
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
            dir_fd=directory,
        )
        try:
            if uid is not None and gid is not None:
                os.fchown(descriptor, uid, gid)
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.rename(temporary, HANDOFF.name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def sanitized_summary(connections: tuple[Connection, ...], errors: tuple[str, ...] = ()) -> str:
    rows = []
    for item in connections:
        rows.append(
            f"{item.id}: host={item.host}:{item.port} resolution={item.resolution} "
            f"fullscreen={'yes' if item.fullscreen else 'no'} audio={'yes' if item.audio else 'no'} "
            f"clipboard={'yes' if item.clipboard else 'no'} "
            f"password={'saved' if item.save_password else 'prompt'} "
            f"certificate={'pinned' if item.certificate else 'not-verified'}"
        )
    if errors:
        rows.append(f"invalid connections skipped: {len(errors)}")
    return "\n".join(rows) + ("\n" if rows else "No saved Remote Desktop connections.\n")


if __name__ == "__main__":
    print(sanitized_summary(*load_connections()), end="")
