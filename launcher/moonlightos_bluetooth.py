#!/usr/bin/python3
"""Controller-friendly Bluetooth screens for the MoonlightOS launcher."""

from __future__ import annotations

import collections
import curses
import json
import pathlib
import re
import socket
import time
from typing import Any

import moonlightos_confirm


SOCKET_PATH = pathlib.Path("/run/moonlightos-bluetooth/control.sock")
START_OSK = pathlib.Path("/run/moonlightos/start-osk")
OSK_ACTIVE = pathlib.Path("/run/moonlightos/osk-active")
ENTER_KEYS = (curses.KEY_ENTER, 10, 13)
SPINNER = "|/-\\"
MAX_RESPONSE = 65536
ADDRESS_RE = re.compile(r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$")


SERVICE_HINT = "WAIT A FEW SECONDS AND OPEN BLUETOOTH AGAIN; IF IT STAYS DOWN, REBOOT."


class BluetoothError(RuntimeError):
    """A bounded local Bluetooth request failed."""


def safe_text(value: object, limit: int = 96) -> str:
    text = "".join(
        character if character.isprintable() and character not in "\r\n" else "?"
        for character in str(value)
    )
    return text[:limit]


def address_suffix(address: str) -> str:
    if not ADDRESS_RE.fullmatch(address):
        return "??:??"
    return ":".join(address.split(":")[-2:])


def has_useful_name(device: dict[str, Any]) -> bool:
    alias = safe_text(device.get("alias") or "").strip()
    if not alias:
        return False
    compact = re.sub(r"[^0-9A-Fa-f]", "", alias)
    return not (len(compact) == 12 and all(character in "0123456789abcdefABCDEF" for character in compact))


def named_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [device for device in devices if has_useful_name(device)]


def device_labels(devices: list[dict[str, Any]]) -> list[str]:
    """Return stable labels and disambiguate duplicate useful aliases."""
    aliases = [safe_text(item.get("alias") or "").strip() for item in devices]
    counts = collections.Counter(alias.casefold() for alias in aliases if alias)
    labels: list[str] = []
    for device, alias in zip(devices, aliases):
        suffix = address_suffix(str(device.get("address") or ""))
        if counts[alias.casefold()] > 1:
            labels.append(f"{alias} · {suffix}")
        else:
            labels.append(alias)
    return labels


def move_selection(selected: int, key: int, count: int) -> int:
    if count < 1:
        return 0
    if key in (curses.KEY_UP, ord("k")):
        return (selected - 1) % count
    if key in (curses.KEY_DOWN, ord("j")):
        return (selected + 1) % count
    return selected


class BluetoothClient:
    def __init__(self, path: pathlib.Path = SOCKET_PATH) -> None:
        self.path = path

    def request(self, command: str, **fields: object) -> dict[str, Any]:
        payload = json.dumps({"command": command, **fields}, separators=(",", ":"))
        encoded = (payload + "\n").encode("utf-8")
        if len(encoded) > 8192:
            raise BluetoothError("BLUETOOTH REQUEST IS TOO LARGE")

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(0.4)
        try:
            client.connect(str(self.path))
            client.settimeout(1.0)
            client.sendall(encoded)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_RESPONSE:
                    raise BluetoothError("BLUETOOTH SERVICE RESPONSE IS TOO LARGE")
                if b"\n" in chunk:
                    break
        except (OSError, socket.timeout) as error:
            raise BluetoothError("BLUETOOTH SERVICE UNAVAILABLE") from error
        finally:
            client.close()

        raw = b"".join(chunks).split(b"\n", 1)[0]
        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BluetoothError("BLUETOOTH SERVICE RETURNED AN INVALID RESPONSE") from error
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            raise BluetoothError("BLUETOOTH SERVICE RETURNED AN INVALID RESPONSE")
        if not response["ok"]:
            raise BluetoothError(safe_text(response.get("error") or "BLUETOOTH REQUEST FAILED", 240))
        return response

    def snapshot(self) -> dict[str, Any]:
        response = self.request("snapshot")
        if not isinstance(response.get("devices", []), list):
            raise BluetoothError("BLUETOOTH SERVICE RETURNED AN INVALID DEVICE LIST")
        return response


class BluetoothMenu:
    def __init__(self, screen: curses.window, client: BluetoothClient | None = None) -> None:
        self.screen = screen
        self.client = client or BluetoothClient()
        self.status = ""

    def _getch(self) -> int:
        key = self.screen.getch()
        if key == curses.KEY_F12:
            if not OSK_ACTIVE.exists():  # one keyboard at a time
                START_OSK.touch()
            return -1
        return key

    @staticmethod
    def _center(width: int, text: str) -> tuple[int, str]:
        clipped = safe_text(text, max(0, width - 4))
        return max(1, (width - len(clipped)) // 2), clipped

    def draw(
        self,
        title: str,
        rows: list[str],
        selected: int | None = None,
        *,
        details: list[str] | None = None,
        footer: str = "",
    ) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if height >= 8 and width >= 24:
            try:
                self.screen.border()
            except curses.error:
                pass

        def centered(row: int, value: str) -> None:
            if not 0 <= row < height:
                return
            column, clipped = self._center(width, value)
            try:
                self.screen.addstr(row, column, clipped)
            except curses.error:
                pass

        centered(max(2, height // 8), title)
        details = details or []
        detail_row = max(4, height // 8 + 2)
        for offset, value in enumerate(details):
            centered(detail_row + offset, value)

        first_row = max(detail_row + len(details) + 2, height // 3)
        left = max(2, (width - max((len(row) for row in rows), default=1) - 3) // 2)
        for index, label in enumerate(rows):
            if first_row + index >= height - 4:
                break
            marker = ">" if index == selected else " "
            try:
                self.screen.addnstr(
                    first_row + index,
                    left,
                    f"{marker}  {safe_text(label, 120)}",
                    max(1, width - left - 1),
                )
            except curses.error:
                pass
        centered(height - 3, footer or self.status)
        self.screen.refresh()

    def message(self, title: str, message: str) -> None:
        self.screen.timeout(1000)
        rows = [safe_text(message[index:index + 64], 64) for index in range(0, len(message), 64)]
        rows = rows or [""]
        while True:
            self.draw(title, rows, None, footer="ENTER OR ESC TO RETURN")
            if self._getch() in (*ENTER_KEYS, 27):
                return

    def _request(self, command: str, **fields: object) -> dict[str, Any] | None:
        try:
            return self.client.request(command, **fields)
        except BluetoothError as error:
            self.message("BLUETOOTH ERROR", f"{error}. TRY AGAIN IN A MOMENT; IF IT KEEPS FAILING, REBOOT.")
            return None

    def _stop_scan_quietly(self) -> None:
        try:
            self.client.request("stop_scan")
        except BluetoothError:
            pass

    def _service_unavailable(self) -> bool:
        selected = 0
        while True:
            self.draw(
                "BLUETOOTH SERVICE UNAVAILABLE",
                ["RETRY", "BACK"],
                selected,
                footer="THE LAUNCHER IS STILL AVAILABLE",
            )
            key = self._getch()
            selected = move_selection(selected, key, 2)
            if key in ENTER_KEYS:
                return selected == 0
            if key == 27:
                return False

    def _no_adapter(self) -> bool:
        selected = 0
        while True:
            self.draw(
                "BLUETOOTH",
                ["REFRESH", "BACK"],
                selected,
                details=["NO BLUETOOTH ADAPTER FOUND", "PLUG IN A USB BLUETOOTH ADAPTER, THEN CHOOSE REFRESH"],
            )
            key = self._getch()
            selected = move_selection(selected, key, 2)
            if key in ENTER_KEYS:
                return selected == 0
            if key == 27:
                return False

    def _confirm(self, title: str, device: dict[str, Any]) -> bool:
        selected = 0
        alias = device_labels([device])[0]
        while True:
            self.draw(title, ["PAIR", "CANCEL"], selected, details=[alias])
            key = self._getch()
            selected = move_selection(selected, key, 2)
            if key in ENTER_KEYS:
                return selected == 0
            if key == 27:
                return False

    def _numeric_input(self, title: str, prompt_id: str, as_number: bool) -> bool:
        keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "DEL", "0", "OK"]
        selected = 0
        value = ""
        while True:
            cells = [
                f">{item:^3}<" if index == selected else f"[{item:^3}]"
                for index, item in enumerate(keys)
            ]
            grid = ["  ".join(cells[row:row + 3]) for row in range(0, 12, 3)]
            self.draw(title, grid, None, details=[f"CODE: {value or '_'}"], footer="ARROWS SELECT  ENTER ACCEPTS  ESC CANCELS")
            key = self._getch()
            if (
                ord("0") <= key <= ord("9")
                or (not as_number and (ord("A") <= key <= ord("Z") or ord("a") <= key <= ord("z")))
            ) and len(value) < 16:
                value += chr(key)
                continue
            if key in (curses.KEY_BACKSPACE, 8, 127):
                value = value[:-1]
                continue
            row, column = divmod(selected, 3)
            if key == curses.KEY_LEFT:
                column = (column - 1) % 3
            elif key == curses.KEY_RIGHT:
                column = (column + 1) % 3
            elif key == curses.KEY_UP:
                row = (row - 1) % 4
            elif key == curses.KEY_DOWN:
                row = (row + 1) % 4
            selected = row * 3 + column
            if key in ENTER_KEYS:
                chosen = keys[selected]
                if chosen == "DEL":
                    value = value[:-1]
                elif chosen == "OK":
                    if value:
                        reply: object = int(value) if as_number else value
                        return self._request(
                            "agent_reply", prompt_id=prompt_id, accepted=True, value=reply
                        ) is not None
                elif len(value) < 16:
                    value += chosen
            elif key == 27:
                self._request("agent_reply", prompt_id=prompt_id, accepted=False)
                return False

    def _answer_prompt(self, prompt: dict[str, Any]) -> bool:
        prompt_id = str(prompt.get("id") or "")
        kind = str(prompt.get("kind") or "")
        if kind in {"pin_code", "passkey"}:
            return self._numeric_input(
                "ENTER PASSKEY" if kind == "passkey" else "ENTER PIN",
                prompt_id,
                kind == "passkey",
            )
        if kind == "display_passkey":
            return True
        code = str(prompt.get("passkey") or "")
        title = "CONFIRM BLUETOOTH CODE" if kind == "confirmation" else "AUTHORIZE BLUETOOTH"
        details = [code] if code else [safe_text(prompt.get("device_alias") or "BLUETOOTH DEVICE")]
        selected = 0
        while True:
            self.draw(title, ["CONFIRM", "REJECT"], selected, details=details)
            key = self._getch()
            selected = move_selection(selected, key, 2)
            if key in ENTER_KEYS:
                accepted = selected == 0
                self._request("agent_reply", prompt_id=prompt_id, accepted=accepted)
                return accepted
            if key == 27:
                self._request("agent_reply", prompt_id=prompt_id, accepted=False)
                return False

    def _wait_operation(
        self,
        operation_id: str,
        title: str,
        *,
        cancellable_pairing: bool = False,
    ) -> tuple[bool, dict[str, Any] | None]:
        frame = 0
        handled_prompt = ""
        self.screen.timeout(100)
        try:
            while True:
                try:
                    snapshot = self.client.snapshot()
                except BluetoothError as error:
                    self.message("BLUETOOTH SERVICE UNAVAILABLE", f"{error}. {SERVICE_HINT}")
                    return False, None
                operations = snapshot.get("operations") or []
                operation = next(
                    (item for item in operations if str(item.get("id")) == operation_id), None
                )
                if operation:
                    state = str(operation.get("state") or "")
                    if state in {"completed", "paired"}:
                        if state == "paired" and operation.get("error"):
                            self.message("DEVICE PAIRED", safe_text(operation["error"], 240))
                        return True, operation
                    if state in {"failed", "cancelled"}:
                        self.message(
                            f"{title} FAILED",
                            safe_text(operation.get("error") or "THE DEVICE STOPPED RESPONDING", 240),
                        )
                        return False, operation

                if snapshot.get("adapter") is None:
                    self.message("BLUETOOTH ADAPTER REMOVED", "RETURNING TO BLUETOOTH SETTINGS")
                    return False, None

                shown_code = ""
                prompt = snapshot.get("prompt")
                if isinstance(prompt, dict) and str(prompt.get("operation_id") or "") == operation_id:
                    if str(prompt.get("kind")) == "display_passkey":
                        # The device is waiting for this code to be typed on it,
                        # so keep it on screen for as long as the prompt lasts.
                        shown_code = str(prompt.get("passkey") or "")
                    else:
                        prompt_id = str(prompt.get("id") or "")
                        if prompt_id and prompt_id != handled_prompt:
                            handled_prompt = prompt_id
                            if not self._answer_prompt(prompt):
                                if cancellable_pairing:
                                    self._request("cancel_pairing", operation_id=operation_id)
                                return False, operation

                footer = "ESC CANCELS" if cancellable_pairing else "PLEASE WAIT"
                if shown_code:
                    self.draw(
                        "BLUETOOTH PASSKEY",
                        [shown_code],
                        None,
                        details=["ENTER THIS CODE ON THE DEVICE"],
                        footer=footer,
                    )
                else:
                    self.draw(
                        title,
                        [f"{SPINNER[frame % len(SPINNER)]}  PLEASE WAIT"],
                        None,
                        footer=footer,
                    )
                frame += 1
                key = self._getch()
                if key == 27 and cancellable_pairing:
                    self._request("cancel_pairing", operation_id=operation_id)
                    return False, operation
        finally:
            self.screen.timeout(1000)

    def _pair(self, device: dict[str, Any]) -> None:
        if not self._confirm("PAIR DEVICE?", device):
            return
        response = self._request("pair", device=str(device.get("path") or ""))
        if not response:
            return
        operation_id = str(response.get("operation_id") or "")
        if operation_id:
            self._wait_operation(operation_id, "PAIRING", cancellable_pairing=True)

    def _run_device_action(self, command: str, path: str, title: str) -> bool:
        response = self._request(command, device=path)
        if not response:
            return False
        operation_id = str(response.get("operation_id") or "")
        if not operation_id:
            return True
        success, _operation = self._wait_operation(operation_id, title)
        return success

    def _device_screen(self, path: str) -> None:
        selected = 0
        while True:
            try:
                snapshot = self.client.snapshot()
            except BluetoothError as error:
                self.message("BLUETOOTH SERVICE UNAVAILABLE", f"{error}. {SERVICE_HINT}")
                return
            if snapshot.get("adapter") is None:
                self.message("BLUETOOTH ADAPTER REMOVED", "RETURNING TO BLUETOOTH SETTINGS")
                return
            device = next((item for item in snapshot.get("devices") or [] if item.get("path") == path), None)
            if not device:
                self.message("DEVICE NO LONGER AVAILABLE", "RETURNING TO BLUETOOTH SETTINGS")
                return
            actions: list[tuple[str, str]] = []
            if device.get("audio") and device.get("connected"):
                actions.append(("USE FOR AUDIO", "use_audio"))
            actions.append(("DISCONNECT", "disconnect") if device.get("connected") else ("CONNECT", "connect"))
            actions.extend((("FORGET DEVICE", "forget"), ("BACK", "back")))
            selected = min(selected, len(actions) - 1)
            details = [
                f"STATUS                         {'CONNECTED' if device.get('connected') else 'DISCONNECTED'}",
                f"PAIRED                         {'YES' if device.get('paired') else 'NO'}",
                f"TRUSTED                        {'YES' if device.get('trusted') else 'NO'}",
            ]
            self.draw(device_labels([device])[0], [item[0] for item in actions], selected, details=details)
            key = self._getch()
            selected = move_selection(selected, key, len(actions))
            if key == 27:
                return
            if key not in ENTER_KEYS:
                continue
            action = actions[selected][1]
            if action == "back":
                return
            if action == "forget":
                # A controller that is forgotten stays dead until it is paired again.
                if moonlightos_confirm.confirm(
                    self.screen,
                    f"FORGET {device_labels([device])[0]}? IF IT IS A CONTROLLER IT WILL STOP "
                    "WORKING UNTIL YOU PAIR IT AGAIN.",
                ) and self._run_device_action(action, path, "FORGETTING DEVICE"):
                    return
            else:
                self._run_device_action(action, path, action.replace("_", " ").upper())

    def run(self) -> None:
        selected = 0
        discovery_requested = False
        last_scan_request = 0.0
        self.screen.timeout(100)
        try:
            while True:
                try:
                    snapshot = self.client.snapshot()
                except BluetoothError:
                    discovery_requested = False
                    self.screen.timeout(1000)
                    if self._service_unavailable():
                        self.screen.timeout(100)
                        continue
                    return
                self.status = safe_text(snapshot.get("error") or "", 240)
                adapter = snapshot.get("adapter")
                if not isinstance(adapter, dict):
                    discovery_requested = False
                    self.screen.timeout(1000)
                    if self._no_adapter():
                        self.screen.timeout(100)
                        continue
                    return

                if not adapter.get("powered"):
                    discovery_requested = False
                    actions = [("TURN BLUETOOTH ON", "power_on"), ("BACK", "back")]
                    selected = min(selected, 1)
                    self.draw("BLUETOOTH", [item[0] for item in actions], selected, details=["OFF"])
                else:
                    if not discovery_requested or (
                        not adapter.get("discovering")
                        and time.monotonic() - last_scan_request >= 1.0
                    ):
                        discovery_requested = self._request("start_scan") is not None
                        last_scan_request = time.monotonic()
                    devices = named_devices(list(snapshot.get("devices") or []))
                    devices.sort(
                        key=lambda item: (
                            not bool(item.get("paired")),
                            not bool(item.get("connected")),
                            safe_text(item.get("alias") or "").casefold(),
                        )
                    )
                    labels = device_labels(devices)
                    actions = [("RESCAN", "rescan")]
                    actions.extend(
                        (
                            f"{label:<32} "
                            f"{'CONNECTED' if device.get('connected') else ('PAIRED' if device.get('paired') else 'AVAILABLE')}",
                            str(device.get("path") or ""),
                        )
                        for label, device in zip(labels, devices)
                    )
                    actions.extend((("TURN BLUETOOTH OFF", "power_off"), ("BACK", "back")))
                    selected = min(selected, len(actions) - 1)
                    scanning = "SCANNING" if adapter.get("discovering") or discovery_requested else "SCAN STOPPED"
                    self.draw("BLUETOOTH", [item[0] for item in actions], selected, details=[f"ON  ·  {scanning}"])

                key = self._getch()
                selected = move_selection(selected, key, len(actions))
                if key == 27:
                    return
                if key not in ENTER_KEYS:
                    continue
                action = actions[selected][1]
                if action == "back":
                    return
                if action == "power_on":
                    self._request("set_power", powered=True)
                elif action == "power_off":
                    # The off state is saved, so a Bluetooth controller would stay dead after a reboot.
                    if moonlightos_confirm.confirm(
                        self.screen, "TURN OFF BLUETOOTH? BLUETOOTH CONTROLLERS WILL DISCONNECT."
                    ):
                        self._stop_scan_quietly()
                        discovery_requested = False
                        self._request("set_power", powered=False)
                elif action == "rescan":
                    self._stop_scan_quietly()
                    discovery_requested = self._request("start_scan") is not None
                    last_scan_request = time.monotonic()
                else:
                    device = next((item for item in snapshot.get("devices") or [] if item.get("path") == action), None)
                    if device:
                        self._stop_scan_quietly()
                        discovery_requested = False
                        if device.get("paired"):
                            self._device_screen(action)
                        else:
                            self._pair(device)
        finally:
            self._stop_scan_quietly()
            self.screen.timeout(1000)


def run_bluetooth(screen: curses.window, client: BluetoothClient | None = None) -> None:
    BluetoothMenu(screen, client).run()
