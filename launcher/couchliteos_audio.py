#!/usr/bin/python3
"""PipeWire output discovery and selection for the launcher."""

from __future__ import annotations

import dataclasses
import os
import re
import subprocess


@dataclasses.dataclass(frozen=True)
class Sink:
    id: int
    name: str
    default: bool = False


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
SINK_RE = re.compile(r"^[\s│├└─]*(\*)?\s*(\d+)\.\s+(.+?)(?:\s+\[[^]]*\])?\s*$")


def parse_sinks(output: str) -> list[Sink]:
    sinks: list[Sink] = []
    in_sinks = False
    for raw in output.splitlines():
        line = ANSI_RE.sub("", raw)
        if re.search(r"\bSinks:\s*$", line):
            in_sinks = True
            continue
        if in_sinks and re.match(r"^[\s│├└─]*[A-Za-z][A-Za-z ]+:\s*$", line):
            break
        if not in_sinks:
            continue
        match = SINK_RE.match(line)
        if match:
            name = re.sub(r"\s+\[[^]]*\]\s*$", "", match.group(3)).strip()
            sinks.append(Sink(int(match.group(2)), name, bool(match.group(1))))
    return sinks


def query_sinks() -> list[Sink]:
    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    result = subprocess.run(
        ["wpctl", "status"], text=True, capture_output=True, check=False,
        timeout=5, env=environment,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or "wpctl status failed").strip())
    return parse_sinks(result.stdout)


def set_default(sink_id: int) -> None:
    result = subprocess.run(
        ["wpctl", "set-default", str(sink_id)], text=True, capture_output=True,
        check=False, timeout=5,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or "wpctl set-default failed").strip())


DEFAULT_SINK = "@DEFAULT_AUDIO_SINK@"
VOLUME_STEP = 5  # percent
SANE_VOLUME = 0.5
VOLUME_RE = re.compile(r"Volume:\s*(\d+(?:\.\d+)?)(\s+\[MUTED\])?")


@dataclasses.dataclass(frozen=True)
class Volume:
    percent: int
    muted: bool


def parse_volume(output: str) -> Volume:
    """Parse `wpctl get-volume` output such as "Volume: 0.50 [MUTED]"."""
    match = VOLUME_RE.search(output)
    if not match:
        raise RuntimeError("wpctl reported no volume")
    return Volume(round(float(match.group(1)) * 100), bool(match.group(2)))


def _wpctl(*arguments: str) -> str:
    result = subprocess.run(
        ["wpctl", *arguments], text=True, capture_output=True, check=False, timeout=5,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or f"wpctl {arguments[0]} failed").strip())
    return result.stdout


def get_volume(target: str = DEFAULT_SINK) -> Volume:
    return parse_volume(_wpctl("get-volume", target))


def change_volume(step: int, target: str = DEFAULT_SINK) -> Volume:
    """Raise or lower the volume by `step` percent, never above 100%."""
    _wpctl("set-volume", "-l", "1.0", target, f"{abs(step)}%{'+' if step > 0 else '-'}")
    return get_volume(target)


def toggle_mute(target: str = DEFAULT_SINK) -> Volume:
    _wpctl("set-mute", target, "toggle")
    return get_volume(target)


def ensure_audible(sink_id: int) -> str:
    """Unmute a newly chosen output and lift it off 0%; return what was changed."""
    target = str(sink_id)
    volume = get_volume(target)
    changes = []
    if volume.muted:
        _wpctl("set-mute", target, "0")
        changes.append("UNMUTED")
    if volume.percent == 0:
        _wpctl("set-volume", target, str(SANE_VOLUME))
        changes.append(f"VOLUME SET TO {round(SANE_VOLUME * 100)}%")
    return ", ".join(changes)
