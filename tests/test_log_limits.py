"""Logs under /var/log/moonlightos persist across boots, so each must stay bounded."""
import pathlib
import re
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN_APP = ROOT / "scripts" / "moonlightos-run-app"
LAUNCHER_UNIT = ROOT / "services" / "moonlightos-launcher.service"
MAX = 1024  # a small cap keeps the tests fast; both implementations are size-driven


def write_log(path: pathlib.Path, lines: int) -> None:
    path.write_text("".join(f"line {number:06d}\n" for number in range(lines)), encoding="ascii")


class CapBehavior:
    """Shared expectations; subclasses say how the cap is run."""

    def run_cap(self, log: pathlib.Path) -> None:
        raise NotImplementedError

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.directory = pathlib.Path(self._directory.name)
        self.log = self.directory / "app.log"

    def test_small_log_is_left_alone(self):
        write_log(self.log, 10)
        before = self.log.read_bytes()
        self.run_cap(self.log)
        self.assertEqual(self.log.read_bytes(), before)
        self.assertFalse((self.directory / "app.log.1").exists())

    def test_missing_log_is_not_an_error(self):
        self.run_cap(self.log)
        self.assertFalse(self.log.exists() and self.log.stat().st_size)

    def test_big_log_keeps_its_tail_as_the_one_old_copy(self):
        write_log(self.log, 600)  # 7 KiB against a 1 KiB cap
        self.run_cap(self.log)
        old = self.directory / "app.log.1"
        self.assertTrue(old.exists())
        self.assertLessEqual(old.stat().st_size, MAX)
        text = old.read_text(encoding="ascii")
        self.assertTrue(text.endswith("line 000599\n"), text[-40:])
        self.assertNotIn("line 000000", text)
        self.assertLessEqual(self.log.stat().st_size if self.log.exists() else 0, MAX)

    def test_old_copy_is_replaced_not_accumulated(self):
        (self.directory / "app.log.1").write_text("ancient history\n", encoding="ascii")
        write_log(self.log, 600)
        self.run_cap(self.log)
        self.assertNotIn("ancient", (self.directory / "app.log.1").read_text(encoding="ascii"))
        self.assertEqual(sorted(path.name for path in self.directory.iterdir() if path.name.startswith("app.log")), ["app.log", "app.log.1"])

    def test_total_size_after_capping_is_bounded(self):
        write_log(self.log, 5000)
        self.run_cap(self.log)
        total = sum(path.stat().st_size for path in self.directory.iterdir())
        self.assertLessEqual(total, 2 * MAX)


class RunAppCapTest(CapBehavior, unittest.TestCase):
    def function(self) -> str:
        text = RUN_APP.read_text(encoding="utf-8")
        match = re.search(r"^cap_log\(\) \{\n.*?^\}\n", text, re.S | re.M)
        self.assertIsNotNone(match, "moonlightos-run-app must define cap_log")
        return match.group(0)

    def run_cap(self, log: pathlib.Path) -> None:
        script = f'set -Eeuo pipefail\n{self.function()}\ncap_log "$1"\n'
        result = subprocess.run(
            ["bash", "-c", script, "cap", str(log)],
            env={"PATH": "/usr/bin:/bin", "MOONLIGHTOS_LOG_MAX_BYTES": str(MAX)},
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cap_runs_before_the_log_is_opened_for_appending(self):
        text = RUN_APP.read_text(encoding="utf-8")
        call = text.find('cap_log "$LOG_DIR/$APP.log"')
        tee = text.find('tee -a "$LOG_DIR/$APP.log"')
        self.assertGreaterEqual(call, 0)
        self.assertGreaterEqual(tee, 0)
        self.assertLess(call, tee)


class LauncherUnitCapTest(CapBehavior, unittest.TestCase):
    """The launcher's output is tee'd to launcher.log by the unit itself."""

    def pre_command(self) -> str:
        lines = [
            line for line in LAUNCHER_UNIT.read_text(encoding="utf-8").splitlines()
            if line.startswith("ExecStartPre=") and "launcher.log" in line
        ]
        self.assertEqual(len(lines), 1, "the launcher unit must cap launcher.log in ExecStartPre")
        command = lines[0].split("=", 1)[1].lstrip("-")
        match = re.fullmatch(r"/bin/bash (?:-\S+ )*-c '(.*)'", command)
        self.assertIsNotNone(match, command)
        return match.group(1)

    def run_cap(self, log: pathlib.Path) -> None:
        script = self.pre_command()
        # systemd expands these itself before bash sees the command
        script = script.replace("$$", "$").replace("%%", "%")
        self.assertNotIn("%", script.replace("%s", ""))
        script = script.replace("/var/log/moonlightos/launcher.log", str(log))
        script = re.sub(r"\b5242880\b", str(MAX), script)
        result = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, timeout=20,
            env={"PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
