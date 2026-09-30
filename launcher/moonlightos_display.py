#!/usr/bin/python3
"""Safe wlr-randr display-mode handling for MoonlightOS."""

from __future__ import annotations

import base64
import os
import pathlib
import re
import subprocess
import tempfile
from dataclasses import dataclass


CONFIG = pathlib.Path("/var/lib/moonlightos/config.ini")
LOG = pathlib.Path("/var/log/moonlightos/display.log")
# Present from just before a saved mode is reapplied until the launcher sees
# its first key press. If it is still there at the next start, that mode
# blacked out the screen, so it is not applied again.
PENDING = pathlib.Path("/var/lib/moonlightos/display-restore.pending")
_unconfirmed = False
OUTPUT_RE = re.compile(r'^(\S+)(?:\s+"(.*)")?$')
MODE_RE = re.compile(
    r"^\s+(\d+)x(\d+)\s+px,\s+([0-9]+(?:\.[0-9]+)?)\s+Hz"
    r"(?:\s+\(([^)]*)\))?\s*$"
)


@dataclass(frozen=True, order=True)
class Mode:
    width: int
    height: int
    refresh_mhz: int
    current: bool = False
    preferred: bool = False

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"

    @property
    def refresh(self) -> str:
        value = self.refresh_mhz / 1000
        return f"{value:.3f}".rstrip("0").rstrip(".")

    @property
    def argument(self) -> str:
        return f"{self.resolution}@{self.refresh}Hz"


@dataclass(frozen=True)
class Output:
    name: str
    description: str
    enabled: bool
    modes: tuple[Mode, ...]

    @property
    def identity(self) -> str:
        if not self.description:
            return ""
        return base64.urlsafe_b64encode(self.description.encode("utf-8")).decode("ascii")

    @property
    def current_mode(self) -> Mode | None:
        return next((mode for mode in self.modes if mode.current), None)


def parse_wlr_randr(text: str) -> list[Output]:
    """Parse compositor-advertised outputs and modes from wlr-randr output."""
    outputs: list[Output] = []
    name: str | None = None
    description = ""
    enabled = False
    modes: list[Mode] = []

    def finish() -> None:
        nonlocal name, description, enabled, modes
        if name is not None:
            unique = {(m.width, m.height, m.refresh_mhz): m for m in modes}
            outputs.append(Output(name, description, enabled, tuple(unique.values())))
        name = None
        description = ""
        enabled = False
        modes = []

    for raw_line in text.splitlines():
        if raw_line and not raw_line[0].isspace():
            finish()
            match = OUTPUT_RE.fullmatch(raw_line.strip())
            if match:
                name, description = match.group(1), match.group(2) or ""
            continue
        if name is None:
            continue
        stripped = raw_line.strip()
        if stripped.startswith("Enabled:"):
            enabled = stripped.split(":", 1)[1].strip().lower() == "yes"
            continue
        match = MODE_RE.match(raw_line)
        if match:
            flags = {part.strip() for part in (match.group(4) or "").split(",")}
            modes.append(
                Mode(
                    int(match.group(1)),
                    int(match.group(2)),
                    round(float(match.group(3)) * 1000),
                    "current" in flags,
                    "preferred" in flags,
                )
            )
    finish()
    return outputs


def query_outputs() -> list[Output]:
    result = subprocess.run(
        ["wlr-randr"], text=True, capture_output=True, check=False, timeout=5
    )
    if result.returncode:
        raise RuntimeError((result.stderr or "wlr-randr failed").strip())
    return parse_wlr_randr(result.stdout)


def active_output(outputs: list[Output]) -> Output | None:
    return next((output for output in outputs if output.enabled and output.current_mode), None)


def find_mode(output: Output, resolution: str, refresh_mhz: int) -> Mode | None:
    return next(
        (
            mode
            for mode in output.modes
            if mode.resolution == resolution and mode.refresh_mhz == refresh_mhz
        ),
        None,
    )


def apply_mode(output: Output, mode: Mode, *, dryrun: bool = False) -> None:
    command = ["wlr-randr", "--output", output.name, "--mode", mode.argument]
    if dryrun:
        command.append("--dryrun")
    result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=8)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "wlr-randr failed").strip())


def valid_output_mode(name: str, identity: str, mode: Mode) -> tuple[Output, Mode] | None:
    for output in query_outputs():
        if not output.enabled or output.name != name or not identity:
            continue
        if output.identity != identity:
            continue
        advertised = find_mode(output, mode.resolution, mode.refresh_mhz)
        if advertised:
            return output, advertised
    return None


def _safe_log_text(value: str) -> str:
    return "".join(char if char.isprintable() and char not in "\r\n" else "?" for char in value)


def log(message: str, log_path: pathlib.Path = LOG) -> None:
    from datetime import datetime, timezone

    line = f"{datetime.now(timezone.utc).isoformat()} {_safe_log_text(message)}\n"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(line)
    except OSError:
        pass


def load_saved_display(config_path: pathlib.Path = CONFIG) -> dict[str, str]:
    try:
        lines = config_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    in_display = False
    for line in lines:
        section = re.fullmatch(r"\s*\[([^]]+)]\s*", line)
        if section:
            in_display = section.group(1).strip().lower() == "display"
            continue
        if not in_display or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lower()
        if key in {"output", "identity", "resolution", "refresh_mhz"}:
            values[key] = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", values.get("output", "")):
        return {}
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,512}={0,2}", values.get("identity", "")):
        return {}
    if not re.fullmatch(r"[1-9]\d{1,4}x[1-9]\d{1,4}", values.get("resolution", "")):
        return {}
    if not re.fullmatch(r"[1-9]\d{3,8}", values.get("refresh_mhz", "")):
        return {}
    return values


def save_display(
    output: Output, mode: Mode, config_path: pathlib.Path = CONFIG, pending: pathlib.Path | None = None
) -> None:
    if not output.identity:
        raise RuntimeError("display identity is unavailable; mode was not saved")
    original = config_path.read_text(encoding="utf-8", errors="replace") if config_path.exists() else ""
    lines = original.splitlines(keepends=True)
    replacement = [
        "[display]\n",
        f"output = {output.name}\n",
        f"identity = {output.identity}\n",
        f"resolution = {mode.resolution}\n",
        f"refresh_mhz = {mode.refresh_mhz}\n",
    ]
    start = end = None
    for index, line in enumerate(lines):
        section = re.fullmatch(r"\s*\[([^]]+)]\s*\r?\n?", line)
        if section and section.group(1).strip().lower() == "display":
            start = index
            end = len(lines)
            for following in range(index + 1, len(lines)):
                if re.fullmatch(r"\s*\[[^]]+]\s*\r?\n?", lines[following]):
                    end = following
                    break
            break
    if start is None:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += "\n"
        if lines and lines[-1].strip():
            lines.append("\n")
        updated = lines + replacement
    else:
        updated = lines[:start] + replacement + lines[end:]

    config_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".config.ini.", dir=config_path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.writelines(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, config_path)
        directory_fd = os.open(config_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    # Choosing a mode again is the user's own confirmation; restores may resume.
    (pending or config_path.with_name(PENDING.name)).unlink(missing_ok=True)


def confirm_restore(pending: pathlib.Path = PENDING) -> None:
    """Call on the first key press: the screen is usable, so clear the pending mark."""
    global _unconfirmed
    if _unconfirmed:
        _unconfirmed = False
        try:
            pending.unlink(missing_ok=True)
        except OSError:
            pass


def restore_saved_mode(pending: pathlib.Path = PENDING) -> bool | None:
    """True when the saved mode is active, False when nothing was restored.

    None means it was skipped because the previous restore was never confirmed.
    """
    global _unconfirmed
    saved = load_saved_display()
    if not saved:
        log("No valid saved display mode; keeping compositor default")
        return False
    try:
        requested = Mode(
            *map(int, saved["resolution"].split("x")), int(saved["refresh_mhz"])
        )
        validated = valid_output_mode(saved["output"], saved["identity"], requested)
        if not validated:
            log("Saved output, display identity, or mode is absent; keeping compositor default")
            return False
        output, mode = validated
        if output.current_mode and (
            output.current_mode.width,
            output.current_mode.height,
            output.current_mode.refresh_mhz,
        ) == (mode.width, mode.height, mode.refresh_mhz):
            log(f"Saved display mode already active on {output.name}: {mode.argument}")
            return True
        if pending.exists():
            log("Previous saved-mode restore was never confirmed by input; skipping it")
            return None
        apply_mode(output, mode, dryrun=True)
        validated = valid_output_mode(output.name, output.identity, mode)
        if not validated:
            log("Display changed after dry-run; keeping compositor default")
            return False
        try:
            pending.write_text("pending", encoding="utf-8")
        except OSError as error:
            log(f"Cannot mark the restore pending, so not applying it: {error}")
            return False
        _unconfirmed = True
        try:
            apply_mode(*validated)
        except RuntimeError:
            _unconfirmed = False
            pending.unlink(missing_ok=True)
            raise
        log(f"Restored saved display mode on {output.name}: {mode.argument}")
        return True
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        log(f"Could not restore saved display mode; keeping compositor default: {error}")
        return False
