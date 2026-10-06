#!/usr/bin/python3
"""Settings > AUDIO > MICROPHONE and BLUETOOTH DELAY for the launcher.

The screens draw through the Settings view they are opened from (its draw() and
status line), so they look like every other settings screen.
"""

from __future__ import annotations

import curses
import subprocess
import textwrap
from collections.abc import Callable
from typing import Any

import couchliteos_audio as audio
import couchliteos_bluetooth as bluetooth

ENTER_KEYS = (curses.KEY_ENTER, 10, 13)
LEVEL_STEP = 5  # percent
ERRORS = (OSError, RuntimeError, ValueError, subprocess.SubprocessError)
MIC_HELP_ROW = "ABOUT VOICE CHAT"
MIC_TEST_ROW = f"TEST (RECORD {audio.MIC_TEST_SECONDS} S, THEN PLAY IT BACK)"
# No mic passthrough in Moonlight streams yet: upstream Moonlight and Sunshine do not carry it.
MIC_HELP = (
    "MOONLIGHT STREAMS DO NOT CARRY THE MICROPHONE YET: MOONLIGHT AND SUNSHINE HAVE NO "
    "MIC PASSTHROUGH. FOR VOICE CHAT WHILE YOU STREAM, OPEN DISCORD IN THE BROWSER ON THIS "
    "PC; IT USES THE MICROPHONE CHOSEN HERE. PS5 REMOTE PLAY (CHIAKI-NG) SENDS THE "
    "MICROPHONE TO THE PS5 FOR VOICE CHAT."
)
DELAY_HINT = "WIRED OR HDMI AUDIO IS BEST FOR FAST GAMES"


def output_label(sink: audio.Sink, graph: audio.Graph | None) -> str:
    """An AUDIO OUTPUT row; a Bluetooth output says so, with its codec."""
    row = f"{'*' if sink.default else ' '}  {sink.name}"
    node = next((node for node in graph.sinks() if node.id == sink.id), None) if graph else None
    if node is not None and node.bluetooth:
        row += f"  (BLUETOOTH{' · ' + node.codec if node.codec else ''})"
    return row


def level_bar(percent: int) -> str:
    filled = max(0, min(100, percent)) * 20 // 100
    return f"[{'#' * filled}{'.' * (20 - filled)}]  {percent}%"


def show_text(view: Any, read_key: Callable, title: str, text: str) -> None:
    rows = textwrap.wrap(text, 60) or [""]
    status, view.status = view.status, "A / CROSS OR B / CIRCLE TO RETURN"
    try:
        while True:
            view.draw(title, rows, None)
            if read_key(view.screen) in (*ENTER_KEYS, 27):
                return
    finally:
        view.status = status


def run_mic_test(view: Any, title: str, rows: list[str], selected: int) -> str:
    def progress(stage: str) -> None:
        view.status = "RECORDING: SPEAK NOW" if stage == "recording" else "PLAYING IT BACK"
        view.draw(title, rows, selected)

    try:
        audio.mic_test(progress=progress)
    except ERRORS as error:
        return f"TEST FAILED: {str(error).upper()[:80]}"
    return "TEST DONE. NOTHING HEARD? CHECK THE MUTE AND LEVEL, OR CHOOSE ANOTHER INPUT"


def run_microphone(view: Any, read_key: Callable, move_selection: Callable) -> None:
    """Choose the input, set its level, mute it, and TEST it."""
    title = "MICROPHONE"
    selected = 0
    result = ""
    while True:
        sources: list[audio.Sink] = []
        try:
            sources = audio.query_sources()
            graph = audio.query_graph()
            choices = audio.mic_choices(sources, graph, audio.chosen_mic())
        except ERRORS as error:
            choices = []
            result = result or f"MICROPHONES UNAVAILABLE: {str(error).upper()[:60]}"
        rows = [f"{'*' if choice.default else ' '}  {choice.label}" for choice in choices]
        actions: list[str] = ["choose"] * len(rows)
        if sources:
            try:
                volume = audio.get_mic_level()
                rows += [f"LEVEL  {level_bar(volume.percent)}", f"MUTE  {'ON' if volume.muted else 'OFF'}"]
            except ERRORS:
                rows += ["LEVEL  UNAVAILABLE", "MUTE  UNAVAILABLE"]
            actions += ["level", "mute"]
            rows.append(MIC_TEST_ROW)
            actions.append("test")
        rows += [MIC_HELP_ROW, "BACK"]
        actions += ["help", "back"]
        view.status = result or ("* IS THE MICROPHONE IN USE" if choices else "NO MICROPHONE FOUND. PLUG ONE IN")
        selected = min(selected, len(rows) - 1)
        view.draw(title, rows, selected)
        key = read_key(view.screen)
        selected = move_selection(selected, key, len(rows))
        action = actions[selected]
        if key == 27 or (key in ENTER_KEYS and action == "back"):
            return
        if key not in (curses.KEY_LEFT, curses.KEY_RIGHT, *ENTER_KEYS):
            continue
        result = ""
        try:
            if action == "choose" and key in ENTER_KEYS:
                choice = choices[selected]
                audio.choose_mic(choice)
                result = f"MICROPHONE: {choice.label}" + (
                    " (USED ONLY WHILE SOMETHING RECORDS)" if choice.source_id is None else ""
                )
            elif action == "level":
                step = -LEVEL_STEP if key == curses.KEY_LEFT else LEVEL_STEP
                result = f"MIC LEVEL {audio.change_mic_level(step).percent}%"
            elif action == "mute" and key in ENTER_KEYS:
                result = "MIC MUTED" if audio.toggle_mic_mute().muted else "MIC ON"
            elif action == "test" and key in ENTER_KEYS:
                result = run_mic_test(view, title, rows, selected)
            elif action == "help" and key in ENTER_KEYS:
                show_text(view, read_key, "VOICE CHAT", MIC_HELP)
        except ERRORS as error:
            result = f"NOT CHANGED: {str(error).upper()[:80]}"


def audio_devices(client: bluetooth.BluetoothClient | None = None) -> list[dict[str, Any]]:
    """Paired Bluetooth headphones, headsets and speakers."""
    snapshot = (client or bluetooth.BluetoothClient()).snapshot()
    devices = [
        device for device in bluetooth.named_devices(list(snapshot.get("devices") or []))
        if device.get("paired") and bluetooth.is_audio(device)
    ]
    return sorted(devices, key=lambda device: bluetooth.safe_text(device.get("alias") or "").casefold())


def run_bluetooth_delay(
    view: Any, read_key: Callable, move_selection: Callable, client: bluetooth.BluetoothClient | None = None
) -> None:
    """Shift each Bluetooth audio device's sound to match the picture; saved per device."""
    title = "BLUETOOTH DELAY"
    selected = 0
    result = ""
    while True:
        try:
            devices = audio_devices(client)
        except bluetooth.BluetoothError as error:
            devices = []
            result = result or str(error)
        labels = bluetooth.device_labels(devices)
        delays = [audio.load_delay(str(device.get("address") or "")) for device in devices]
        rows = [f"{label:<32} {delay:>3} MS" for label, delay in zip(labels, delays)] + ["BACK"]
        view.status = result or (
            f"LEFT/RIGHT CHANGES IT  ·  {DELAY_HINT}" if devices else "NO BLUETOOTH AUDIO DEVICE IS PAIRED"
        )
        selected = min(selected, len(rows) - 1)
        view.draw(title, rows, selected)
        key = read_key(view.screen)
        selected = move_selection(selected, key, len(rows))
        if key == 27 or (key in ENTER_KEYS and selected == len(rows) - 1):
            return
        if selected >= len(devices) or key not in (curses.KEY_LEFT, curses.KEY_RIGHT):
            continue
        device = devices[selected]
        address = str(device.get("address") or "")
        step = -audio.DELAY_STEP if key == curses.KEY_LEFT else audio.DELAY_STEP
        try:
            value = audio.save_delay(address, delays[selected] + step)
        except ERRORS as error:
            result = f"COULD NOT SAVE: {str(error).upper()[:60]}"
            continue
        result = f"{labels[selected]}: {value} MS"
        if device.get("connected"):
            graph = audio.query_graph()
            try:
                if graph is None or not audio.apply_delay(graph, address, value):
                    result += " (APPLIED WHEN IT CONNECTS)"
            except ERRORS:
                result += " (APPLIED WHEN IT CONNECTS)"
        else:
            result += " (APPLIED WHEN IT CONNECTS)"
