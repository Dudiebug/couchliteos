#!/usr/bin/env python3
"""Drive the installer entry in a GRUB menu over QEMU's HMP socket."""

from __future__ import annotations

import argparse
import os
import socket
import time


CHAR_KEYS = {
    " ": "spc",
    "/": "slash",
    ".": "dot",
    "-": "minus",
    "_": "shift-minus",
    "=": "equal",
    ":": "shift-semicolon",
    ",": "comma",
}
KEY_CHARS = {value: key for key, value in CHAR_KEYS.items()}


def key_for_char(character: str) -> str:
    if character in CHAR_KEYS:
        return CHAR_KEYS[character]
    if len(character) == 1 and character.isascii() and character.isupper():
        return f"shift-{character.lower()}"
    if len(character) == 1 and character.isascii() and character.isalnum():
        return character
    raise ValueError(f"unsupported HMP character: {character!r}")


def keys_for_text(text: str) -> list[str]:
    return [key_for_char(character) for character in text]


def text_for_keys(keys: list[str]) -> str:
    characters = []
    for key in keys:
        if key in KEY_CHARS:
            characters.append(KEY_CHARS[key])
        elif key.startswith("shift-") and len(key) == 7 and key[-1].isalpha():
            characters.append(key[-1].upper())
        elif len(key) == 1 and key.isalnum():
            characters.append(key)
        else:
            raise ValueError(f"unsupported HMP key: {key!r}")
    return "".join(characters)


def install_commands(
    menu_screenshot: str,
    editor_screenshot: str,
    installer_screenshot: str,
    kernel_append: str,
) -> list[tuple[str, float]]:
    commands = [("sendkey home", 0.25)] * 60
    commands += [("sendkey down", 0.15)] * 3
    commands += [
        (f"screendump {menu_screenshot}", 0.5),
        ("sendkey e", 2.0),
        ("sendkey down", 0.5),
        ("sendkey down", 0.5),
        ("sendkey end", 0.1),
    ]
    commands += [(f"sendkey {key}", 0.03) for key in keys_for_text(kernel_append)]
    commands += [
        (f"screendump {editor_screenshot}", 0.5),
        ("sendkey ctrl-x", 20.0),
        (f"screendump {installer_screenshot}", 0.5),
    ]
    return commands


def connect_monitor(path: str, timeout: float = 15.0) -> socket.socket:
    """Connect to a QEMU HMP monitor: a Unix socket path, or tcp:HOST:PORT."""
    deadline = time.monotonic() + timeout
    while True:
        if path.startswith("tcp:"):
            host, _separator, port = path[4:].rpartition(":")
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            address: object = (host, int(port))
        else:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            address = path
        try:
            client.connect(address)
            return client
        except OSError:
            client.close()
            if time.monotonic() >= deadline:
                raise TimeoutError(f"QEMU monitor did not appear: {path}")
            time.sleep(0.1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("monitor")
    parser.add_argument("menu_screenshot")
    parser.add_argument("editor_screenshot")
    parser.add_argument("installer_screenshot")
    parser.add_argument("kernel_append")
    args = parser.parse_args()

    if any(
        " " in path
        for path in (
            args.menu_screenshot,
            args.editor_screenshot,
            args.installer_screenshot,
        )
    ):
        parser.error("HMP screenshot paths must not contain spaces")

    # Slow hosts may stretch the pauses between keystrokes (default 1).
    scale = int(os.environ.get("COUCHLITEOS_QEMU_TIMEOUT_SCALE", "1"))
    with connect_monitor(args.monitor, timeout=15.0 * scale) as monitor:
        for command, delay in install_commands(
            args.menu_screenshot,
            args.editor_screenshot,
            args.installer_screenshot,
            args.kernel_append,
        ):
            monitor.sendall(f"{command}\n".encode("ascii"))
            time.sleep(delay * scale)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
