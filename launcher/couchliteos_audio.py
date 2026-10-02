#!/usr/bin/python3
"""PipeWire output discovery and selection for the launcher."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import time
from collections.abc import Collection


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


@dataclasses.dataclass(frozen=True)
class ProfileOutput:
    """An output a sound card offers only under another profile.

    A plain HDA card plays on its analog jack or on one HDMI/DisplayPort output
    at a time; the others have no sink until the card's profile is switched.
    """

    device: int
    profile: int
    name: str


def _output_part(profile_name: str) -> str | None:
    return next((part for part in profile_name.split("+") if part.startswith("output:")), None)


def _output_label(description: str) -> str:
    label = description.split(" + ")[0].strip()
    return re.sub(r"\s+Output$", "", label)


def parse_profile_outputs(dump: str) -> list[ProfileOutput]:
    """Outputs that switching an ALSA card's profile would add, from `pw-dump`.

    One entry per output the active profile does not already play on, using the
    highest-priority profile that has it. Profiles reported unavailable (an
    HDMI/DP jack with no TV) are left out.
    """
    outputs: list[ProfileOutput] = []
    for item in json.loads(dump or "[]"):
        if not isinstance(item, dict) or item.get("type") != "PipeWire:Interface:Device":
            continue
        info = item.get("info") or {}
        props = info.get("props") or {}
        if props.get("device.api") != "alsa" or props.get("media.class") != "Audio/Device":
            continue
        params = info.get("params") or {}
        active = (params.get("Profile") or [{}])[0].get("name", "")
        playing = _output_part(active)
        best: dict[str, dict] = {}
        for profile in params.get("EnumProfile") or []:
            output = _output_part(profile.get("name", ""))
            if not output or output == playing or profile.get("available") == "no":
                continue
            if output not in best or profile.get("priority", 0) > best[output].get("priority", 0):
                best[output] = profile
        device_name = props.get("device.description") or props.get("device.nick") or "Sound card"
        for profile in sorted(best.values(), key=lambda p: -p.get("priority", 0)):
            label = _output_label(profile.get("description") or profile["name"])
            outputs.append(ProfileOutput(int(item["id"]), int(profile["index"]), f"{device_name} {label}"))
    return outputs


def query_profile_outputs() -> list[ProfileOutput]:
    """Like parse_profile_outputs on the live graph; empty if PipeWire can't say."""
    try:
        result = subprocess.run(
            ["pw-dump", "--no-colors"], text=True, capture_output=True, check=False, timeout=5,
        )
        if result.returncode:
            return []
        return parse_profile_outputs(result.stdout)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return []


def switch_to(output: ProfileOutput, previous: Collection[int], wait: float = 3.0) -> Sink:
    """Switch the card to `output`'s profile and return the sink that appears.

    The choice is saved, so WirePlumber restores it after a restart and keeps
    it while the output is available (instead of following the TV).
    """
    result = subprocess.run(
        ["pw-cli", "set-param", str(output.device), "Profile",
         json.dumps({"index": output.profile, "save": True})],
        text=True, capture_output=True, check=False, timeout=5,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or "pw-cli set-param failed").strip())
    deadline = time.monotonic() + wait
    while True:
        new = [sink for sink in query_sinks() if sink.id not in previous]
        if new:
            return new[0]
        if time.monotonic() >= deadline:
            raise RuntimeError("the output did not appear")
        time.sleep(0.25)


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


def step_volume(step: int, target: str = DEFAULT_SINK) -> None:
    """Raise or lower the volume by `step` percent, never above 100%, without reading it back
    (one wpctl call: the volume keys step it while held, and nothing shows the level)."""
    _wpctl("set-volume", "-l", "1.0", target, f"{abs(step)}%{'+' if step > 0 else '-'}")


def change_volume(step: int, target: str = DEFAULT_SINK) -> Volume:
    """step_volume(), then the new level for the Guide menu and Settings to show."""
    step_volume(step, target)
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
