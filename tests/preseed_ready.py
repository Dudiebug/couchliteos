#!/usr/bin/env python3
"""Wait until http://127.0.0.1:PORT/installer-preseed.cfg serves this run's preseed file
(PORT: COUCHLITEOS_PRESEED_PORT, 8000 by default).

A preseed server left behind by a killed run can keep port 8000: the new server then cannot
bind, the installer gets the old server's answer (often 404) and waits at a prompt until the
QEMU timeout. Checking the served bytes catches that in seconds.
"""
import os
import sys
import time
import urllib.request

PORT = os.environ.get("COUCHLITEOS_PRESEED_PORT", "8000")
URL = f"http://127.0.0.1:{PORT}/installer-preseed.cfg"


def served() -> bytes | None:
    try:
        with urllib.request.urlopen(URL, timeout=2) as response:
            return response.read()
    except OSError:
        return None


def main(argv: list[str]) -> int:
    expected = open(argv[1], "rb").read()
    deadline = time.monotonic() + float(argv[2] if len(argv) > 2 else 10)
    while time.monotonic() < deadline:
        if served() == expected:
            return 0
        time.sleep(0.2)
    print(f"{URL} does not serve {argv[1]}: is another preseed server still holding port {PORT}?",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
