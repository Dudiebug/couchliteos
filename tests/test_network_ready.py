"""couchliteos-network-ready against a fake `ip`: it waits for a default route and an address."""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "couchliteos-network-ready"

# Answers `ip -4 route show default` and `ip -brief -4 address show up`. The route
# appears on the ROUTE_AFTER-th route query (1 = immediately); ADDRESS is the
# address listing. Every query is logged.
FAKE_IP = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_DIR/ip.log"
if [ "$1 $2 $3" = "-4 route show" ]; then
  count=$(cat "$FAKE_DIR/routes" 2>/dev/null || echo 0)
  count=$((count + 1))
  echo "$count" > "$FAKE_DIR/routes"
  if [ "$count" -ge "$ROUTE_AFTER" ]; then
    echo "default via 192.168.1.1 dev enp3s0 proto dhcp metric 100"
  fi
else
  printf '%b' "$ADDRESS"
fi
"""
FAKE_SLEEP = """#!/bin/sh
echo "$*" >> "$FAKE_DIR/sleep.log"
exec "$REAL_SLEEP" "$SLEEP_FOR"
"""
UP = "lo               UNKNOWN        127.0.0.1/8\\nenp3s0           UP             192.168.1.5/24\\n"


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bash"), "needs Linux and bash")
class NetworkReadyTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = pathlib.Path(self.temporary.name)
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        for name, text in (("ip", FAKE_IP), ("sleep", FAKE_SLEEP)):
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        self.path = f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"

    def run_script(self, route_after=1, address=UP, sleep_for="0"):
        environment = {
            "PATH": self.path,
            "FAKE_DIR": str(self.base),
            "ROUTE_AFTER": str(route_after),
            "ADDRESS": address,
            "REAL_SLEEP": shutil.which("sleep") or "/bin/sleep",
            "SLEEP_FOR": sleep_for,
        }
        return subprocess.run(["bash", str(SCRIPT)], env=environment, capture_output=True, text=True, timeout=60)

    def sleeps(self):
        log = self.base / "sleep.log"
        return log.read_text().split() if log.exists() else []

    def test_ready_network_is_reported_at_once(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertRegex(lines[0], r"^\d{4}-\d\d-\d\dT\S+ wired network ready$")
        self.assertIn("enp3s0           UP             192.168.1.5/24", lines)
        self.assertEqual(self.sleeps(), [])
        self.assertEqual(result.stderr, "")

    def test_waits_one_second_at_a_time_for_the_default_route(self):
        result = self.run_script(route_after=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("wired network ready", result.stdout)
        self.assertEqual(self.sleeps(), ["1", "1"])
        queries = (self.base / "ip.log").read_text().splitlines()
        self.assertEqual(queries.count("-4 route show default"), 3)

    def test_timeout_never_blocks_the_boot(self):
        # A route but only loopback: not ready. The script gives up after 15 s and still
        # exits 0 so the launcher starts. (Each fake sleep really waits 0.5 s.)
        result = self.run_script(address="lo               UNKNOWN        127.0.0.1/8\\n", sleep_for="0.5")
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("wired network ready", result.stdout)
        self.assertRegex(result.stderr, r"network readiness timed out; launcher will still start\n$")
        self.assertGreater(len(self.sleeps()), 1)


if __name__ == "__main__":
    unittest.main()
