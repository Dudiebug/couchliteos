#!/usr/bin/python3
"""Settings > STREAMING > VIDEO DECODER FIRMWARE: ask the root helper for NVIDIA's decoder firmware.

couchliteos-nvidia-firmware (a root service started by nvidia-firmware.request)
does the download, the checks and the install; this module only offers it on
nouveau PCs, writes the request, and reads the helper's status back.
"""

from __future__ import annotations

import json
import os
import pathlib
import time

RUN = pathlib.Path("/run/couchliteos")
REQUEST = RUN / "nvidia-firmware.request"
STATUS = RUN / "nvidia-firmware.status"
HARDWARE_ENV = pathlib.Path("/run/couchliteos-hardware/hardware.env")
STORE = pathlib.Path("/var/lib/couchliteos/firmware")
TITLE = "VIDEO DECODER FIRMWARE"
ACTIONS = ("install", "remove")
START_TIMEOUT = 15.0  # seconds for the service to answer before the launcher says it did not start
RUN_TIMEOUT = 20 * 60.0  # the service's own limit is 15 minutes
ABOUT = (
    "THIS PC'S NVIDIA GRAPHICS USE THE OPEN NOUVEAU DRIVER, WHICH NEEDS NVIDIA'S OWN FIRMWARE TO DECODE VIDEO. "
    "COUCHLITEOS IS NOT ALLOWED TO INCLUDE IT, SO STREAMS USE SOFTWARE DECODING.\n"
    "DOWNLOAD FETCHES NVIDIA'S 325.15 DRIVER (27 MB) FROM NVIDIA.COM, CHECKS IT, TAKES OUT ONLY THE VIDEO "
    "DECODER FIRMWARE AND KEEPS IT ON THIS USB DRIVE. NVIDIA'S LICENSE APPLIES.\n"
    "EXPERIMENTAL: IF THE DECODER DOES NOT START, STREAMS KEEP USING SOFTWARE DECODING."
)


def hardware(path: pathlib.Path | None = None) -> dict[str, str]:
    values = {}
    try:
        for line in (path or HARDWARE_ENV).read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    except (OSError, UnicodeDecodeError):
        pass
    return values


def applies(path: pathlib.Path | None = None) -> bool:
    """Only nouveau PCs can use it (Kepler and two late Fermi chips decode with it)."""
    return hardware(path).get("COUCHLITEOS_GPU_DRIVER") == "nouveau"


def installed(store: pathlib.Path | None = None) -> bool:
    """All three blobs are there (the helper also checks their hashes before it trusts them)."""
    return all(((store or STORE) / "nouveau" / name).is_file() for name in ("nve0_bsp", "nve0_vp", "nvc0_ppp"))


def verified(store: pathlib.Path | None = None, hardware_env: pathlib.Path | None = None) -> bool:
    gpu = hardware(hardware_env).get("COUCHLITEOS_GPU_ID", "")
    try:
        return bool(gpu) and gpu in ((store or STORE) / "verified").read_text(encoding="ascii").split()
    except (OSError, UnicodeDecodeError):
        return False


def state_label(store: pathlib.Path | None = None, hardware_env: pathlib.Path | None = None) -> str:
    if not installed(store):
        return "OFF (SOFTWARE DECODING)"
    return "ON" if verified(store, hardware_env) else "SAVED, NOT WORKING YET"


def submit(action: str, run_dir: pathlib.Path | None = None) -> None:
    if action not in ACTIONS:
        raise ValueError(action)
    run = run_dir or RUN
    (run / STATUS.name).unlink(missing_ok=True)
    temporary = run / f".{REQUEST.name}.tmp"
    temporary.write_text(action + "\n", encoding="ascii")
    os.replace(temporary, run / REQUEST.name)


def busy(run_dir: pathlib.Path | None = None, now: float | None = None) -> bool:
    """A job is still running: a new request now would be answered by the old job's status.
    A "running" status older than the service's 15-minute limit is a job systemd stopped."""
    status = read_status(run_dir)
    if status is None or status[0] != "running":
        return False
    try:
        age = (time.time() if now is None else now) - ((run_dir or RUN) / STATUS.name).stat().st_mtime
    except OSError:
        return False
    return age < 16 * 60


def read_status(run_dir: pathlib.Path | None = None) -> tuple[str, str] | None:
    """(state, message) where state is running, done or failed; None before the helper wrote any."""
    try:
        data = json.loads(((run_dir or RUN) / STATUS.name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    state, message = data.get("state"), data.get("message")
    if state not in ("running", "done", "failed") or not isinstance(message, str):
        return None
    return state, message[:400]
