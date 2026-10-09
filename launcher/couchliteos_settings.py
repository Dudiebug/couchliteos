"""Read and write one section of config.ini, leaving every other section alone.

Each feature owns a section ([ui], [theme], [audio], ...). Writes are atomic: the file is
written to a temporary file in the same directory, synced, then renamed over the old one,
so a power cut leaves either the old or the new file, never half of one.
"""

import configparser
import os
import pathlib
import re
import tempfile

CONFIG = pathlib.Path("/var/lib/couchliteos/config.ini")

_HEADER = re.compile(r"\s*\[([^]]+)]\s*\r?\n?")
_SETTING = re.compile(r"\s*[^=:\s][^=:]*[=:]")  # a key, then "=" or ":"


def _valid_key(key: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9_.:-]+", key))


def _valid_value(value: str) -> bool:
    return value.isprintable() and value == value.strip()


def _readable(text: str) -> str:
    """The text without the lines configparser refuses (a line with no "=", a setting before any
    section), so one bad hand-edited line does not hide every other setting in the file."""
    kept: list[str] = []
    in_section = continues = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            kept.append(line)
        elif _HEADER.fullmatch(line):
            kept.append(line)
            in_section, continues = True, False
        elif line[:1].isspace() and continues:  # an indented line goes on with the value above
            kept.append(line)
        elif in_section and _SETTING.match(line):
            kept.append(line)
            continues = True
        else:
            continues = False  # dropped; an indented line after it would join the wrong value
    return "\n".join(kept) + "\n"


def _parse(path: pathlib.Path) -> configparser.ConfigParser:
    """The file's settings, bad lines left out. A missing file has none; OSError, UnicodeError or
    configparser.Error when it cannot be read."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return parser
    parser.read_string(_readable(text), source=str(path))
    return parser


def _section(parser: configparser.ConfigParser, section: str) -> dict:
    if not parser.has_section(section):
        return {}
    return {key: value.strip() for key, value in parser.items(section, raw=True)}


def read_section(section: str, path: pathlib.Path = CONFIG) -> dict:
    """Return the section's keys and values; an unreadable file or missing section is {}.
    A line that is not a setting is skipped, the rest still read."""
    try:
        return _section(_parse(path), section)
    except (OSError, UnicodeError, configparser.Error):
        return {}


def sections(prefix: str, path: pathlib.Path = CONFIG) -> list[str]:
    """Names of the sections that start with prefix, in file order (for [stream:<host>:<app>])."""
    try:
        parser = _parse(path)
    except (OSError, UnicodeError, configparser.Error):
        return []
    return [name for name in parser.sections() if name.startswith(prefix)]


def section_part(text: str, limit: int = 48) -> str:
    """A readable, valid piece of a section name for any text: lower case, other characters as '-'.

    Two names may give the same part ("Half-Life" and "Half Life"); callers that need one
    section per name add a hash of the exact name."""
    part = re.sub(r"[^a-z0-9_.-]+", "-", text.lower()).strip("-.")[:limit].strip("-.")
    return part or "-"


def get_bool(values: dict, key: str, default: bool) -> bool:
    value = values.get(key, "").lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return default


def get_choice(values: dict, key: str, choices, default: str) -> str:
    value = values.get(key, "").lower()
    return value if value in choices else default


def get_int(values: dict, key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(values.get(key, ""))
    except ValueError:
        return default
    return value if low <= value <= high else default


def atomic_write(path: pathlib.Path, data: bytes, default_mode: int = 0o640) -> None:
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = default_mode
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def write_section(section: str, values: dict, path: pathlib.Path = CONFIG) -> None:
    """Replace the whole section with values (in the given order); other sections are kept."""
    if not _valid_key(section.lower()) or section != section.lower():
        raise ValueError(f"bad section name: {section!r}")
    body = [f"[{section}]\n"]
    for key, value in values.items():
        value = str(value)
        if not _valid_key(key) or not _valid_value(value):
            raise ValueError(f"bad setting {key!r} = {value!r}")
        body.append(f"{key} = {value}\n")
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        lines = []
    start = end = None
    for index, line in enumerate(lines):
        match = _HEADER.fullmatch(line)
        if match and match.group(1).strip().lower() == section:
            start = index
            end = next((n for n in range(index + 1, len(lines)) if _HEADER.fullmatch(lines[n])), len(lines))
            break
    if start is None:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += "\n"
        updated = lines + (["\n"] if lines and lines[-1].strip() else []) + body
    else:
        updated = lines[:start] + body + (["\n"] if end < len(lines) else []) + lines[end:]
    atomic_write(path, "".join(updated).encode("utf-8"))


def delete_section(section: str, path: pathlib.Path = CONFIG) -> bool:
    """Remove the section (header and keys); True when it was there. Other sections are kept."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        return False
    for index, line in enumerate(lines):
        match = _HEADER.fullmatch(line)
        if match and match.group(1).strip().lower() == section:
            end = next((n for n in range(index + 1, len(lines)) if _HEADER.fullmatch(lines[n])), len(lines))
            kept = lines[:index] + lines[end:]
            while kept and not kept[-1].strip():
                kept.pop()
            atomic_write(path, "".join(kept).encode("utf-8"))
            return True
    return False


def update_section(section: str, changes: dict, path: pathlib.Path = CONFIG) -> dict:
    """Merge changes into the section and write it; returns the new section.

    Existing entries write_section would refuse (hand-edited: a tab, a space in the key) are
    dropped, or one of them would block every later save of the section. A file that cannot be
    read is never written over (OSError): that would lose the section's other settings."""
    try:
        current = _section(_parse(path), section)
    except (UnicodeError, configparser.Error) as error:
        raise OSError(f"{path.name} cannot be read: {error}") from error
    values = {key: value for key, value in current.items() if _valid_key(key) and _valid_value(value)}
    values.update({key: str(value) for key, value in changes.items()})
    write_section(section, values, path)
    return values
