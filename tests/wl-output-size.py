#!/usr/bin/env python3
"""Set every output of the running Wayland compositor to WIDTHxHEIGHT (test-only).

tests/tv-headless.sh runs the TV interface in a headless Cage, whose one output starts at
1280x720; this asks for another size through wlr-output-management (which Cage supports),
speaking the Wayland wire protocol directly, so no wlr-randr or pywayland is needed.
Usage: wl-output-size.py 1920 1080
"""

from __future__ import annotations

import os
import socket
import struct
import sys

MANAGER = "zwlr_output_manager_v1"


class Connection:
    def __init__(self) -> None:
        name = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
        path = name if name.startswith("/") else os.path.join(os.environ["XDG_RUNTIME_DIR"], name)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(path)
        self.last_id = 1  # wl_display
        self.buffer = b""

    def new_id(self) -> int:
        self.last_id += 1
        return self.last_id

    def send(self, obj: int, opcode: int, payload: bytes = b"") -> None:
        self.sock.sendall(struct.pack("<II", obj, (8 + len(payload)) << 16 | opcode) + payload)

    def event(self) -> tuple[int, int, bytes]:
        while True:
            if len(self.buffer) >= 8:
                obj, word = struct.unpack_from("<II", self.buffer)
                size = word >> 16
                if len(self.buffer) >= size:
                    body, self.buffer = self.buffer[8:size], self.buffer[size:]
                    if obj == 1 and word & 0xFFFF == 0:  # wl_display.error
                        raise SystemExit(f"wayland error: {body!r}")
                    return obj, word & 0xFFFF, body
            chunk = self.sock.recv(65536)
            if not chunk:
                raise SystemExit("the compositor closed the connection")
            self.buffer += chunk


def string(text: str) -> bytes:
    data = text.encode() + b"\0"
    return struct.pack("<I", len(data)) + data + b"\0" * (-len(data) % 4)


def read_string(body: bytes, offset: int) -> tuple[str, int]:
    (length,) = struct.unpack_from("<I", body, offset)
    text = body[offset + 4:offset + 4 + length - 1].decode()
    return text, offset + 4 + length + (-length % 4)


def main(argv: list[str]) -> int:
    width, height = int(argv[1]), int(argv[2])
    wl = Connection()
    registry, sync = wl.new_id(), wl.new_id()
    wl.send(1, 1, struct.pack("<I", registry))  # wl_display.get_registry
    wl.send(1, 0, struct.pack("<I", sync))  # wl_display.sync
    found = None
    while True:
        obj, opcode, body = wl.event()
        if obj == registry and opcode == 0:  # global
            (name,) = struct.unpack_from("<I", body)
            interface, _offset = read_string(body, 4)
            if interface == MANAGER:
                found = name
        elif obj == sync:
            break
    if found is None:
        print(f"wl-output-size: the compositor has no {MANAGER}", file=sys.stderr)
        return 1
    manager = wl.new_id()
    wl.send(registry, 0, struct.pack("<I", found) + string(MANAGER) + struct.pack("<II", 1, manager))
    heads: list[int] = []
    while True:
        obj, opcode, body = wl.event()
        if obj == manager and opcode == 0:  # head
            heads.append(struct.unpack_from("<I", body)[0])
        elif obj == manager and opcode == 1:  # done
            (serial,) = struct.unpack_from("<I", body)
            break
    # Cage 0.1.5 asserts when an enabled output is enabled again: switch it off first, then on
    # at the new size. The second configuration needs the `done` serial the first one caused.
    for enable in (False, True):
        configuration = wl.new_id()
        wl.send(manager, 0, struct.pack("<II", configuration, serial))  # create_configuration
        for head in heads:
            if enable:
                configured = wl.new_id()
                wl.send(configuration, 0, struct.pack("<II", configured, head))  # enable_head
                wl.send(configured, 1, struct.pack("<iii", width, height, 0))  # set_custom_mode
            else:
                wl.send(configuration, 1, struct.pack("<I", head))  # disable_head
        wl.send(configuration, 2)  # apply
        result = fresh = None
        while result is None or (not enable and result == 0 and fresh is None):
            obj, opcode, body = wl.event()
            if obj == configuration:
                result = opcode
            elif obj == manager and opcode == 1:
                fresh = serial = struct.unpack_from("<I", body)[0]
        if result != 0:  # succeeded
            print(f"wl-output-size: {width}x{height} was {'refused' if result == 1 else 'cancelled'}",
                  file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
