#!/usr/bin/python3
"""Load and safely update CouchLiteOS application manifests."""

from __future__ import annotations

import configparser
import dataclasses
import os
import pathlib
import pwd
import grp
import re
import tempfile
import time
import urllib.parse


SYSTEM_DIR = pathlib.Path("/usr/share/couchliteos/apps.d")
DATA = pathlib.Path(os.environ.get("COUCHLITEOS_STATE_DIR", "/var/lib/couchliteos"))
USER_DIR = DATA / "apps.d"
STATE_FILE = DATA / "apps-state.ini"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RESERVED_IDS = {"settings", "reboot", "poweroff"}
MAX_MANIFEST_SIZE = 16 * 1024
MAX_NAME = 64
MAX_COMMAND = 512
MAX_ARGUMENTS = 2048
MAX_ENV_VALUE = 1024
KINDS = {"request", "command", "rdp", "flatpak"}
# A `kind = flatpak` manifest names a Flathub application (`flatpak = org.example.App`); it
# loads as a command application that runs `flatpak run <id>`, so the launcher and the app
# runner start it like any other command.
FLATPAK = "/usr/bin/flatpak"
FLATPAK_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(\.[A-Za-z_][A-Za-z0-9_-]*){2,}$")
FLATHUB_REPO = pathlib.Path("/usr/share/couchliteos/flathub.flatpakrepo")
# Controller buttons that gamepad-nav forwards to the launcher as F5-F8.
CATEGORIES = {"games", "video", "apps", "network"}
ICON_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
SHORTCUTS = {"lb": "LB / L1", "rb": "RB / R1", "view": "VIEW / SELECT", "menu": "MENU / START"}


class ManifestError(ValueError):
    """A manifest or state file failed validation."""


@dataclasses.dataclass(frozen=True)
class Application:
    id: str
    name: str
    kind: str
    command: str = ""
    arguments: str = ""
    request: str = ""
    # The program the tile needs; while it is missing the tile is hidden (a browser installed on demand).
    binary: str = ""
    connection: str = ""
    shortcut: str = ""
    status_id: str = ""
    terminal: bool = False
    enabled: bool = True
    visible: bool = True
    order: int = 60
    return_to_launcher: bool = True
    environment: dict[str, str] = dataclasses.field(default_factory=dict)
    flatpak: str = ""
    system: bool = False
    path: pathlib.Path | None = None
    # Where the TV interface puts it ("games", "video", "apps" or "network"; "" = by what it is) and
    # its icon (a bundled icon name); the classic launcher ignores both.
    category: str = ""
    icon: str = ""


@dataclasses.dataclass(frozen=True)
class LoadResult:
    applications: tuple[Application, ...]
    errors: tuple[str, ...]


def _scalar(value: str, field: str, limit: int) -> str:
    if "\0" in value or "\n" in value or "\r" in value:
        raise ManifestError(f"{field} contains an invalid character")
    if len(value) > limit:
        raise ManifestError(f"{field} is too long")
    return value


def _parser() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    return parser


def _boolean(section: configparser.SectionProxy, name: str, fallback: bool) -> bool:
    try:
        return section.getboolean(name, fallback=fallback)
    except ValueError as error:
        raise ManifestError(f"{name} must be true or false") from error


def read_manifest(path: pathlib.Path, *, system: bool = False) -> Application:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MANIFEST_SIZE:
            raise ManifestError("manifest is not a bounded regular file")
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ManifestError(f"cannot read manifest: {error}") from error
    if "\0" in raw:
        raise ManifestError("manifest contains NUL")
    parser = _parser()
    try:
        parser.read_string(raw)
    except configparser.Error as error:
        raise ManifestError(f"invalid INI: {error}") from error
    if "app" not in parser:
        raise ManifestError("missing [app]")
    section = parser["app"]
    app_id = _scalar(section.get("id", "").strip(), "id", 32)
    if not ID_RE.fullmatch(app_id):
        raise ManifestError("invalid application id")
    if not system and app_id in RESERVED_IDS:
        raise ManifestError("application id is reserved by the launcher")
    name = _scalar(section.get("name", "").strip(), "name", MAX_NAME)
    if not name:
        raise ManifestError("name is required")
    kind = _scalar(section.get("kind", "").strip(), "kind", 16)
    if kind not in KINDS:
        raise ManifestError("kind must be request, command, rdp, or flatpak")
    command = _scalar(section.get("command", "").strip(), "command", MAX_COMMAND)
    arguments = _scalar(section.get("arguments", "").strip(), "arguments", MAX_ARGUMENTS)
    flatpak = _scalar(section.get("flatpak", "").strip(), "flatpak", 255)
    if kind == "flatpak":
        if command or arguments or section.get("request", "").strip():
            raise ManifestError("flatpak application cannot define command, arguments, or request")
        if not FLATPAK_ID_RE.fullmatch(flatpak):
            raise ManifestError("invalid flatpak application id")
        kind, command, arguments = "command", FLATPAK, f"run {flatpak}"
    elif flatpak:
        raise ManifestError("only flatpak applications can define flatpak")
    request = _scalar(section.get("request", "").strip(), "request", 64)
    binary = _scalar(section.get("binary", "").strip(), "binary", MAX_COMMAND)
    if binary and not all(pathlib.PurePath(item).is_absolute() for item in binaries(binary)):
        raise ManifestError("binary must be an absolute path")
    connection = _scalar(section.get("connection", "").strip(), "connection", 32)
    shortcut = _scalar(section.get("shortcut", "").strip().lower(), "shortcut", 16)
    if shortcut and shortcut not in SHORTCUTS:
        raise ManifestError("unsupported controller shortcut")
    status_id = _scalar(section.get("status_id", app_id).strip(), "status_id", 32)
    if not ID_RE.fullmatch(status_id):
        raise ManifestError("invalid status id")
    if not system and status_id != app_id:
        raise ManifestError("user application status_id must match id")
    if kind != "rdp" and connection:
        raise ManifestError("only rdp applications can define connection")
    if kind == "command":
        if not pathlib.PurePath(command).is_absolute():
            raise ManifestError("command must be an absolute path")
        if request:
            raise ManifestError("command application cannot define request")
    elif kind == "rdp":
        if command or arguments or request:
            raise ManifestError("rdp application cannot define command, arguments, or request")
        if not ID_RE.fullmatch(connection) or connection != app_id:
            raise ManifestError("rdp application id must match its connection id")
        if status_id != app_id:
            raise ManifestError("rdp application status_id must match id")
    else:
        if not system:
            raise ManifestError("user applications must use kind=command")
        if not request.startswith("start-") or not ID_RE.fullmatch(request.removeprefix("start-")):
            raise ManifestError("invalid request name")
    category = _scalar(section.get("category", "").strip().lower(), "category", 16)
    if category and category not in CATEGORIES:
        raise ManifestError("category must be games, video, apps, or network")
    icon = _scalar(section.get("icon", "").strip().lower(), "icon", 48)
    if icon and not ICON_RE.fullmatch(icon):
        raise ManifestError("icon must be an icon name (a-z, 0-9 and -)")
    return_to_launcher = _boolean(section, "return_to_launcher", True)
    if not return_to_launcher:
        raise ManifestError("return_to_launcher=false is not supported")
    try:
        order = section.getint("order", fallback=60)
    except ValueError as error:
        raise ManifestError("order must be an integer") from error
    if not -10000 <= order <= 10000:
        raise ManifestError("order is out of range")
    environment: dict[str, str] = {}
    if "environment" in parser:
        for key, value in parser["environment"].items():
            if not ENV_RE.fullmatch(key):
                raise ManifestError(f"invalid environment name: {key}")
            environment[key] = _scalar(value, f"environment {key}", MAX_ENV_VALUE)
    return Application(
        id=app_id,
        name=name,
        kind=kind,
        command=command,
        arguments=arguments,
        request=request,
        binary=binary,
        connection=connection,
        shortcut=shortcut,
        status_id=status_id,
        terminal=_boolean(section, "terminal", False) and kind != "rdp",
        enabled=_boolean(section, "enabled", True),
        visible=_boolean(section, "visible", True),
        order=order,
        return_to_launcher=True,
        environment=environment,
        flatpak=flatpak,
        system=system,
        path=path,
        category=category,
        icon=icon,
    )


def _read_state(path: pathlib.Path) -> tuple[dict[str, dict[str, str]], list[str]]:
    if not path.exists():
        return {}, []
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MANIFEST_SIZE:
            raise ManifestError("state is not a bounded regular file")
        parser = _parser()
        parser.read_string(path.read_text(encoding="utf-8"))
        state: dict[str, dict[str, str]] = {}
        for app_id in parser.sections():
            if not ID_RE.fullmatch(app_id):
                raise ManifestError(f"invalid state id: {app_id}")
            values = dict(parser[app_id])
            if set(values) - {"enabled", "order"}:
                raise ManifestError(f"unsupported state field for {app_id}")
            state[app_id] = values
        return state, []
    except (OSError, UnicodeError, configparser.Error, ManifestError) as error:
        return {}, [f"{path.name}: {error}"]


def load_applications(
    system_dir: pathlib.Path = SYSTEM_DIR,
    user_dir: pathlib.Path = USER_DIR,
    state_file: pathlib.Path = STATE_FILE,
) -> LoadResult:
    loaded: dict[str, Application] = {}
    errors: list[str] = []
    for directory, system in ((system_dir, True), (user_dir, False)):
        try:
            # Dotfiles are temporary files (for example a validation file left by
            # a crash), never manifests.
            paths = sorted(item for item in directory.glob("*.ini") if not item.name.startswith("."))
        except OSError as error:
            errors.append(f"{directory}: {error}")
            continue
        for path in paths:
            try:
                app = read_manifest(path, system=system)
                if app.id in loaded:
                    raise ManifestError(f"duplicate application id: {app.id}")
                loaded[app.id] = app
            except ManifestError as error:
                errors.append(f"{path.name}: {error}")
    state, state_errors = _read_state(state_file)
    errors.extend(state_errors)
    applications: list[Application] = []
    for app in loaded.values():
        override = state.get(app.id, {})
        try:
            enabled = app.enabled
            if "enabled" in override:
                enabled = configparser.ConfigParser.BOOLEAN_STATES[override["enabled"].lower()]
            order = int(override.get("order", app.order))
            if not -10000 <= order <= 10000:
                raise ValueError
            applications.append(dataclasses.replace(app, enabled=enabled, order=order))
        except (KeyError, ValueError):
            errors.append(f"{state_file.name}: invalid override for {app.id}")
            applications.append(app)
    applications.sort(key=lambda item: (item.order, item.name.casefold(), item.id))
    return LoadResult(tuple(applications), tuple(errors))


# cached_applications: (system_dir, user_dir, state_file) -> (their stamps, the result).
_LOADED: dict[tuple[pathlib.Path, ...], tuple[tuple, LoadResult]] = {}
# A change this recent is not trusted to show in the next one's stamp (a filesystem with coarse
# timestamps): until it is older, every call parses again.
SETTLED = 2.0


def _stamp(path: pathlib.Path) -> tuple[int, int, int, int] | None:
    try:
        status = path.stat()
    except OSError:
        return None
    return status.st_mtime_ns, status.st_ctime_ns, status.st_size, status.st_ino


def cached_applications(
    system_dir: pathlib.Path = SYSTEM_DIR,
    user_dir: pathlib.Path = USER_DIR,
    state_file: pathlib.Path = STATE_FILE,
    *,
    clock=time.time,
) -> LoadResult:
    """load_applications, parsed again only when a manifest directory or the state file changed.

    The home screen asks every second and on every key: then it costs three stats. Manifests and
    the state file are written by replacing them (atomic_write), which changes the directory's
    modification time and the state file's own."""
    key = (system_dir, user_dir, state_file)
    stamps = tuple(_stamp(path) for path in key)
    cached = _LOADED.get(key)
    if cached is not None and cached[0] == stamps:
        return cached[1]
    result = load_applications(system_dir, user_dir, state_file)
    newest = max((stamp[0] for stamp in stamps if stamp is not None), default=0) / 1e9
    if clock() - newest >= SETTLED:
        _LOADED[key] = (stamps, result)
    else:
        _LOADED.pop(key, None)
    return result


def binaries(binary: str) -> list[str]:
    """A manifest's `binary`: one path, or alternatives split by "|" (LO-FI RADIO runs in either browser)."""
    return [item.strip() for item in binary.split("|") if item.strip()]


def installed(app: Application, root: pathlib.Path = pathlib.Path("/")) -> bool:
    """False when the app names a `binary` that is not there (Firefox or Chrome before it is installed);
    with alternatives, when none of them is."""
    return not app.binary or any((root / item.lstrip("/")).exists() for item in binaries(app.binary))


def visible_applications(**kwargs: object) -> LoadResult:
    result = load_applications(**kwargs)
    return LoadResult(
        tuple(app for app in result.applications if app.enabled and app.visible), result.errors
    )


def _owner() -> tuple[int, int]:
    try:
        account = pwd.getpwnam("couchliteos")
        return account.pw_uid, grp.getgrnam("couchliteos").gr_gid
    except KeyError:
        return os.getuid(), os.getgid()


def atomic_write(path: pathlib.Path, content: str, mode: int = 0o640) -> None:
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        # fdopen first: it owns the descriptor, so a failing fchmod or fchown still closes it.
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            uid, gid = _owner()
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), uid, gid)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def serialize(app: Application) -> str:
    parser = _parser()
    flatpak = bool(app.flatpak)
    parser["app"] = {
        "id": app.id,
        "name": app.name,
        "kind": "flatpak" if flatpak else app.kind,
        "command": "" if flatpak else app.command,
        "arguments": "" if flatpak else app.arguments,
        "request": app.request,
        "status_id": app.status_id,
        "terminal": str(app.terminal).lower(),
        "enabled": str(app.enabled).lower(),
        "visible": str(app.visible).lower(),
        "order": str(app.order),
        "return_to_launcher": "true",
    }
    if app.binary:
        parser["app"]["binary"] = app.binary
    if app.connection:
        parser["app"]["connection"] = app.connection
    if app.category:
        parser["app"]["category"] = app.category
    if app.icon:
        parser["app"]["icon"] = app.icon
    if flatpak:
        parser["app"]["flatpak"] = app.flatpak
    if app.shortcut:
        parser["app"]["shortcut"] = app.shortcut
    if app.environment:
        parser["environment"] = app.environment
    from io import StringIO

    output = StringIO()
    parser.write(output, space_around_delimiters=True)
    return output.getvalue()


def write_user_application(
    app: Application,
    *,
    system_dir: pathlib.Path = SYSTEM_DIR,
    user_dir: pathlib.Path = USER_DIR,
) -> pathlib.Path:
    validated_text = serialize(dataclasses.replace(app, system=False, path=None))
    system_ids = {item.id for item in load_applications(system_dir, pathlib.Path("/nonexistent")).applications}
    if app.id in system_ids:
        raise ManifestError("user application id collides with a system application")
    user_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".validate-app.", suffix=".tmp", dir=user_dir)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(validated_text)
        read_manifest(pathlib.Path(temporary))
    finally:
        pathlib.Path(temporary).unlink(missing_ok=True)
    destination = user_dir / f"{app.id}.ini"
    atomic_write(destination, validated_text)
    return destination


def delete_user_application(
    app_id: str,
    *,
    system_dir: pathlib.Path = SYSTEM_DIR,
    user_dir: pathlib.Path = USER_DIR,
) -> None:
    if not ID_RE.fullmatch(app_id):
        raise ManifestError("invalid application id")
    system_ids = {item.id for item in load_applications(system_dir, pathlib.Path("/nonexistent")).applications}
    if app_id in system_ids:
        raise ManifestError("system applications cannot be deleted")
    path = user_dir / f"{app_id}.ini"
    if path.is_symlink():
        raise ManifestError("refusing to delete a symlink")
    path.unlink()


def write_state(
    applications: list[Application] | tuple[Application, ...],
    path: pathlib.Path = STATE_FILE,
) -> None:
    parser = _parser()
    for app in applications:
        parser[app.id] = {"enabled": str(app.enabled).lower(), "order": str(app.order)}
    from io import StringIO

    output = StringIO()
    parser.write(output, space_around_delimiters=True)
    atomic_write(path, output.getvalue())


def parse_environment(value: str) -> dict[str, str]:
    environment: dict[str, str] = {}
    if not value.strip():
        return environment
    for item in value.split(";"):
        if not item.strip():  # "A=1;" or "A=1; ; B=2"
            continue
        if "=" not in item:
            raise ManifestError("environment values must use KEY=value")
        key, item_value = item.split("=", 1)
        key = key.strip()  # "A=1; B=2"
        if not ENV_RE.fullmatch(key):
            raise ManifestError(f"invalid environment name: {key}")
        environment[key] = _scalar(item_value, f"environment {key}", MAX_ENV_VALUE)
    return environment


def validate_web_url(value: str) -> str:
    value = _scalar(value.strip(), "URL", 2048)
    if any(character in value for character in ("`", "${", "$(")) or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ManifestError("URL contains unsupported characters")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ManifestError("URL must be an http:// or https:// address without credentials")
    return value


def application_id(name: str, existing: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:32] or "custom-app"
    candidate = base
    number = 2
    while candidate in existing | RESERVED_IDS:
        suffix = f"-{number}"
        candidate = base[: 32 - len(suffix)].rstrip("-") + suffix
        number += 1
    return candidate


def sanitized_summary(result: LoadResult) -> str:
    rows = []
    for app in result.applications:
        exists = "n/a" if app.kind != "command" else ("yes" if pathlib.Path(app.command).exists() else "no")
        rows.append(
            f"{app.id}: enabled={'yes' if app.enabled else 'no'} order={app.order} "
            f"kind={app.kind} command_exists={exists}"
        )
    if result.errors:
        rows.append(f"invalid manifests skipped: {len(result.errors)}")
    return "\n".join(rows) + ("\n" if rows else "")


if __name__ == "__main__":
    print(sanitized_summary(load_applications()), end="")


def flatpak_application(flatpak_id: str, name: str, existing: set[str]) -> Application:
    """A user application that starts an installed Flatpak (for INSTALL FROM FLATHUB)."""
    if not FLATPAK_ID_RE.fullmatch(flatpak_id) or len(flatpak_id) > 255:
        raise ManifestError("invalid flatpak application id")
    name = _scalar(name.strip(), "name", MAX_NAME)
    if not name:
        raise ManifestError("name is required")
    app_id = application_id(name, existing)
    return Application(
        id=app_id, name=name, kind="command", command=FLATPAK, arguments=f"run {flatpak_id}",
        status_id=app_id, flatpak=flatpak_id,
    )


def flathub_remote_command(system: bool = True) -> list[str]:
    """Add Flathub when the image's build-time remote is missing (an older stick's state).

    The shipped .flatpakrepo carries Flathub's signing key, so this needs no network.
    """
    scope = "--system" if system else "--user"
    return [FLATPAK, "remote-add", scope, "--if-not-exists", "flathub", str(FLATHUB_REPO)]
