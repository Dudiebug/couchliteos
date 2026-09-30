#!/usr/bin/python3
"""Load and safely update MoonlightOS application manifests."""

from __future__ import annotations

import configparser
import dataclasses
import os
import pathlib
import pwd
import grp
import re
import tempfile
import urllib.parse


SYSTEM_DIR = pathlib.Path("/usr/share/moonlightos/apps.d")
USER_DIR = pathlib.Path("/var/lib/moonlightos/apps.d")
STATE_FILE = pathlib.Path("/var/lib/moonlightos/apps-state.ini")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RESERVED_IDS = {"settings", "reboot", "poweroff"}
MAX_MANIFEST_SIZE = 16 * 1024
MAX_NAME = 64
MAX_COMMAND = 512
MAX_ARGUMENTS = 2048
MAX_ENV_VALUE = 1024
KINDS = {"request", "command", "rdp"}
# Controller buttons that gamepad-nav forwards to the launcher as F5-F8.
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
    connection: str = ""
    shortcut: str = ""
    status_id: str = ""
    terminal: bool = False
    enabled: bool = True
    visible: bool = True
    order: int = 60
    return_to_launcher: bool = True
    environment: dict[str, str] = dataclasses.field(default_factory=dict)
    system: bool = False
    path: pathlib.Path | None = None


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
        raise ManifestError("kind must be request, command, or rdp")
    command = _scalar(section.get("command", "").strip(), "command", MAX_COMMAND)
    arguments = _scalar(section.get("arguments", "").strip(), "arguments", MAX_ARGUMENTS)
    request = _scalar(section.get("request", "").strip(), "request", 64)
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
        connection=connection,
        shortcut=shortcut,
        status_id=status_id,
        terminal=_boolean(section, "terminal", False) and kind != "rdp",
        enabled=_boolean(section, "enabled", True),
        visible=_boolean(section, "visible", True),
        order=order,
        return_to_launcher=True,
        environment=environment,
        system=system,
        path=path,
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


def visible_applications(**kwargs: object) -> LoadResult:
    result = load_applications(**kwargs)
    return LoadResult(
        tuple(app for app in result.applications if app.enabled and app.visible), result.errors
    )


def _owner() -> tuple[int, int]:
    try:
        account = pwd.getpwnam("moonlightos")
        return account.pw_uid, grp.getgrnam("moonlightos").gr_gid
    except KeyError:
        return os.getuid(), os.getgid()


def atomic_write(path: pathlib.Path, content: str, mode: int = 0o640) -> None:
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        uid, gid = _owner()
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, uid, gid)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
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
    parser["app"] = {
        "id": app.id,
        "name": app.name,
        "kind": app.kind,
        "command": app.command,
        "arguments": app.arguments,
        "request": app.request,
        "status_id": app.status_id,
        "terminal": str(app.terminal).lower(),
        "enabled": str(app.enabled).lower(),
        "visible": str(app.visible).lower(),
        "order": str(app.order),
        "return_to_launcher": "true",
    }
    if app.connection:
        parser["app"]["connection"] = app.connection
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
        if "=" not in item:
            raise ManifestError("environment values must use KEY=value")
        key, item_value = item.split("=", 1)
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
