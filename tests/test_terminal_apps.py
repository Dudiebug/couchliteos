"""Terminal apps run in a foot window that closes the moment the script exits.

A controller user cannot run commands or press Alt+F4, so the scripts that run
in such a window must keep their last message on screen until Enter (controller
A) or Esc (controller B), and must not advise shell commands.
"""
import fcntl
import os
import pathlib
import pty
import select
import struct
import subprocess
import tempfile
import termios
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
ENROLLMENT = ROOT / "scripts" / "couchliteos-tailscale-enrollment"
DIAGNOSTICS = ROOT / "scripts" / "couchliteos-diagnostics"


class Terminal:
    """Run a script on a pseudo-terminal so that `read` sees a real keyboard."""

    def __init__(self, argv, env, rows=24, cols=80):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.process = subprocess.Popen(
            argv, stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True
        )
        os.close(slave)
        self.output = ""

    def read_until(self, text, timeout=10.0):
        deadline = time.monotonic() + timeout
        while text not in self.output:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"{text!r} not seen in ...{self.output[-600:]!r}")
            if select.select([self.master], [], [], min(remaining, 0.2))[0]:
                try:
                    chunk = os.read(self.master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                self.output += chunk.decode("utf-8", "replace")
        self.assert_contains(text)

    def assert_contains(self, text):
        if text not in self.output:
            raise AssertionError(f"{text!r} not seen in ...{self.output[-600:]!r}")

    def drain(self, seconds=0.4):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if select.select([self.master], [], [], 0.05)[0]:
                try:
                    chunk = os.read(self.master, 4096)
                except OSError:
                    return
                if not chunk:
                    return
                self.output += chunk.decode("utf-8", "replace")

    def send(self, data: bytes):
        os.write(self.master, data)

    def finish(self, timeout=10.0) -> int:
        deadline = time.monotonic() + timeout
        while self.process.poll() is None:
            if time.monotonic() > deadline:
                raise AssertionError(f"still running; output ...{self.output[-600:]!r}")
            self.drain(0.05)
        self.drain(0.1)
        return self.process.returncode

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        os.close(self.master)


class TerminalAppTest(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = pathlib.Path(self._directory.name)
        self.commands = self.root / "bin"
        self.commands.mkdir()
        self.logs = self.root / "log"
        self.logs.mkdir()
        self.terminals = []
        self.addCleanup(lambda: [terminal.close() for terminal in self.terminals])

    def stub(self, name, body):
        path = self.commands / name
        path.write_text("#!/bin/bash\n" + body, encoding="utf-8")
        path.chmod(0o755)

    def environment(self, **extra):
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{self.commands}:{environment['PATH']}",
                "TERM": "xterm",
                "COUCHLITEOS_LOG_DIR": str(self.logs),
            }
        )
        environment.update(extra)
        return environment

    def start(self, argv, rows=24, cols=80, **extra):
        terminal = Terminal(argv, self.environment(**extra), rows=rows, cols=cols)
        self.terminals.append(terminal)
        return terminal

    def assert_still_open(self, terminal):
        terminal.drain(0.4)
        self.assertIsNone(terminal.process.poll(), terminal.output[-600:])


class EnrollmentWaitsTest(TerminalAppTest):
    def setUp(self):
        super().setUp()
        self.stub("tailscale", "printf '%s\\n' '{\"BackendState\":\"NeedsLogin\"}'\n")
        self.stub("qrencode", "echo '[QR]'\n")

    def enrollment_environment(self, **extra):
        values = {
            "COUCHLITEOS_TAILSCALE_URL_FILE": str(self.root / "auth-url"),
            "COUCHLITEOS_TAILSCALE_POLL_SECONDS": "0.05",
            "COUCHLITEOS_TAILSCALE_URL_WAIT_SECONDS": "2",
        }
        values.update(extra)
        return values

    def test_failed_enrollment_waits_and_names_a_menu_path(self):
        self.stub("systemctl", "exit 0\n")
        terminal = self.start([str(ENROLLMENT)], **self.enrollment_environment())
        terminal.read_until("SYSTEM DIAGNOSTICS")
        self.assertNotIn("couchliteos-tailscale-diagnostics", terminal.output)
        terminal.assert_contains("SETTINGS > SYSTEM DIAGNOSTICS")
        terminal.read_until("ENTER")  # the prompt line can arrive after the message
        self.assert_still_open(terminal)
        terminal.send(b"\n")
        self.assertEqual(terminal.finish(), 1)

    def test_url_timeout_waits_until_escape(self):
        self.stub("systemctl", "exit 1\n")
        terminal = self.start([str(ENROLLMENT)], **self.enrollment_environment())
        terminal.read_until("SYSTEM DIAGNOSTICS")
        self.assertNotIn("local timeout", terminal.output)
        self.assert_still_open(terminal)
        terminal.send(b"\x1b")
        self.assertEqual(terminal.finish(), 1)

    def test_arrow_keys_do_not_close_the_message(self):
        self.stub("systemctl", "exit 0\n")
        terminal = self.start([str(ENROLLMENT)], **self.enrollment_environment())
        terminal.read_until("SYSTEM DIAGNOSTICS")
        terminal.send(b"\x1b[B\x1b[A x")
        self.assert_still_open(terminal)
        terminal.send(b"\r")
        self.assertEqual(terminal.finish(), 1)

    def test_qr_screen_closes_only_on_enter_or_escape(self):
        self.stub("systemctl", "exit 1\n")
        (self.root / "auth-url").write_text(
            "https://login.tailscale.com/a/current\n", encoding="utf-8"
        )
        terminal = self.start(
            [str(ENROLLMENT)],
            **self.enrollment_environment(COUCHLITEOS_TAILSCALE_POLL_SECONDS="1"),
        )
        terminal.read_until("to return")
        terminal.assert_contains("[QR]")
        terminal.send(b"\x1b[B")
        self.assert_still_open(terminal)
        terminal.send(b"\n")
        self.assertEqual(terminal.finish(), 130)

    def test_without_a_terminal_failure_exits_immediately(self):
        self.stub("systemctl", "exit 0\n")
        result = subprocess.run(
            [str(ENROLLMENT)],
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            env=self.environment(**self.enrollment_environment()),
            timeout=10,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("SYSTEM DIAGNOSTICS", result.stderr)


class DiagnosticsViewerTest(TerminalAppTest):
    def setUp(self):
        super().setUp()
        self.calls = self.root / "tailscale-calls"
        self.stub(
            "couchliteos-tailscale-diagnostics",
            f"echo x >> '{self.calls}'\nfor i in $(seq 1 60); do echo \"tailscale line $i\"; done\n",
        )
        for name in (
            "couchliteos-usbip", "journalctl", "systemctl", "clear", "vulkaninfo", "vainfo",
            "wpctl", "aplay", "nmcli", "ip", "lsusb", "lspci", "lscpu", "rfkill", "wlr-randr",
            "systemd-analyze",  # 8 s per call on a busy build VM: the report outran read_until
        ):
            self.stub(name, "exit 0\n")

    def saved_reports(self):
        return sorted(self.logs.glob("diagnostics-*.txt"))

    def test_report_runs_once_and_is_saved(self):
        terminal = self.start([str(DIAGNOSTICS)])
        terminal.read_until("CouchLiteOS diagnostics")
        terminal.drain(0.5)
        self.assertEqual(len(self.calls.read_text().split()), 1)
        (saved,) = self.saved_reports()
        self.assertIn("tailscale line 60", saved.read_text())
        terminal.send(b"\x1b")
        self.assertEqual(terminal.finish(), 0)
        self.assertEqual(len(self.calls.read_text().split()), 1)

    def test_result_stays_on_screen_until_the_user_returns(self):
        terminal = self.start([str(DIAGNOSTICS)])
        terminal.read_until("CouchLiteOS diagnostics")
        terminal.drain(1.0)
        self.assertIsNone(terminal.process.poll(), terminal.output[-600:])
        terminal.send(b"\x1b")
        self.assertEqual(terminal.finish(), 0)

    def test_enter_pages_forward_and_the_end_names_the_saved_file(self):
        terminal = self.start([str(DIAGNOSTICS)], rows=24)
        terminal.read_until("CouchLiteOS diagnostics")
        (saved,) = self.saved_reports()
        total = len(saved.read_text().splitlines())
        self.assertGreater(total, 3 * 21)
        pages = 0
        while terminal.process.poll() is None and pages < total:
            terminal.drain(0.2)
            terminal.send(b"\n")
            pages += 1
        self.assertEqual(terminal.finish(), 0)
        self.assertLess(pages, total // 10, "each Enter must show a whole page")
        self.assertIn(str(saved), terminal.output)
        self.assertIn("tailscale line 60", terminal.output)

    def test_up_arrow_goes_back_a_page(self):
        terminal = self.start([str(DIAGNOSTICS)], rows=24)
        terminal.read_until("CouchLiteOS diagnostics")
        terminal.drain(0.3)
        terminal.send(b"\n")
        terminal.read_until("lines 22-")
        terminal.send(b"\x1b[A")
        terminal.drain(0.4)
        self.assertGreaterEqual(terminal.output.count("lines 1-"), 2, terminal.output)
        terminal.send(b"\x1b")
        self.assertEqual(terminal.finish(), 0)

    def test_without_a_terminal_it_prints_and_exits(self):
        result = subprocess.run(
            [str(DIAGNOSTICS)],
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            env=self.environment(),
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("tailscale line 60", result.stdout)
        self.assertEqual(len(self.calls.read_text().split()), 1)

    def test_only_the_newest_saved_reports_are_kept(self):
        for day in range(1, 9):
            (self.logs / f"diagnostics-2026010{day}-120000.txt").write_text("old", encoding="utf-8")
        (self.logs / "launcher.log").write_text("not a report", encoding="utf-8")
        result = subprocess.run(
            [str(DIAGNOSTICS)],
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            env=self.environment(),
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        reports = self.saved_reports()
        self.assertEqual(len(reports), 5, [report.name for report in reports])
        self.assertIn("tailscale line 60", reports[-1].read_text())
        self.assertFalse((self.logs / "diagnostics-20260101-120000.txt").exists())
        self.assertTrue((self.logs / "diagnostics-20260108-120000.txt").exists())
        self.assertTrue((self.logs / "launcher.log").exists())

    def test_watch_mode_has_a_controller_exit_and_no_alt_f4(self):
        terminal = self.start([str(DIAGNOSTICS), "--watch"])
        terminal.read_until("Refresh")
        self.assertNotIn("Alt+F4", terminal.output)
        terminal.assert_contains("ESC")
        terminal.send(b"\x1b")
        self.assertEqual(terminal.finish(), 0)


if __name__ == "__main__":
    unittest.main()
