"""couchliteos-boot-time: parsing `systemd-analyze time`, the boot-time log, and the smoke-test gate."""

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("boot_time", str(ROOT / "scripts" / "couchliteos-boot-time"))
spec = importlib.util.spec_from_loader(loader.name, loader)
boot_time = importlib.util.module_from_spec(spec)
loader.exec_module(boot_time)

# Canned `systemd-analyze time` output from the machines CouchLiteOS runs on.
BIOS = (
    "Startup finished in 2.012s (kernel) + 3.456s (initrd) + 8.101s (userspace) = 13.570s \n"
    "multi-user.target reached after 8.050s in userspace.\n"
)
UEFI = (
    "Startup finished in 4.120s (firmware) + 1.502s (loader) + 1.873s (kernel)"
    " + 2.640s (initrd) + 1min 2.250s (userspace) = 1min 12.385s\n"
    "graphical.target reached after 1min 2.100s in userspace.\n"
)
MICRO = "Startup finished in 812ms (kernel) + 950μs (initrd) + 5.5s (userspace) = 6.312s\n"
NOT_FINISHED = (
    "Bootup is not yet finished (org.freedesktop.systemd1.Manager.FinishTimestampMonotonic=0).\n"
    "Please try again later.\n"
)


def completed(stdout, returncode=0):
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


class ParseTest(unittest.TestCase):
    def test_timespans(self):
        self.assertAlmostEqual(boot_time.parse_timespan("13.570s"), 13.57)
        self.assertAlmostEqual(boot_time.parse_timespan("812ms"), 0.812)
        self.assertAlmostEqual(boot_time.parse_timespan("950us"), 0.00095)
        self.assertAlmostEqual(boot_time.parse_timespan("950μs"), 0.00095)
        self.assertAlmostEqual(boot_time.parse_timespan("1min 2.250s"), 62.25)
        self.assertAlmostEqual(boot_time.parse_timespan("1h 2min 3s"), 3723)
        self.assertIsNone(boot_time.parse_timespan("soon"))

    def test_bios_boot_has_no_firmware_or_loader(self):
        stages = boot_time.parse_analyze(BIOS)
        self.assertEqual(set(stages), {"kernel", "initrd", "userspace", "total"})
        self.assertAlmostEqual(stages["kernel"], 2.012)
        self.assertAlmostEqual(stages["initrd"], 3.456)
        self.assertAlmostEqual(stages["userspace"], 8.101)
        self.assertAlmostEqual(stages["total"], 13.57)

    def test_uefi_boot_with_minutes(self):
        stages = boot_time.parse_analyze(UEFI)
        self.assertAlmostEqual(stages["firmware"], 4.12)
        self.assertAlmostEqual(stages["loader"], 1.502)
        self.assertAlmostEqual(stages["userspace"], 62.25)
        self.assertAlmostEqual(stages["total"], 72.385)

    def test_small_units(self):
        stages = boot_time.parse_analyze(MICRO)
        self.assertAlmostEqual(stages["kernel"], 0.812)
        self.assertAlmostEqual(stages["initrd"], 0.00095)
        self.assertAlmostEqual(stages["total"], 6.312)

    def test_unfinished_boot_or_empty_output_gives_nothing(self):
        self.assertEqual(boot_time.parse_analyze(NOT_FINISHED), {})
        self.assertEqual(boot_time.parse_analyze(""), {})

    def test_ready_seconds_take_the_files_age_off_the_time_since_boot(self):
        # Ready at wall 1000.0; read at wall 1003.5 when 15.0 s had passed since boot.
        self.assertAlmostEqual(boot_time.ready_seconds(1000.0, 1003.5, 15.0), 11.5)
        # A wall clock moved in between never gives a time below zero or above the uptime.
        self.assertEqual(boot_time.ready_seconds(1000.0, 5000.0, 15.0), 0.0)
        self.assertEqual(boot_time.ready_seconds(5000.0, 1000.0, 15.0), 15.0)


class AnalyzeTest(unittest.TestCase):
    def test_retries_until_the_boot_has_finished(self):
        answers = [completed(NOT_FINISHED, 1), completed(NOT_FINISHED, 1), completed(BIOS)]
        calls, sleeps = [], []

        def runner(argv, **kwargs):
            calls.append(argv)
            return answers.pop(0)

        stages = boot_time.analyze(runner=runner, sleep=sleeps.append, wait=60)
        self.assertAlmostEqual(stages["total"], 13.57)
        self.assertEqual(calls, [["systemd-analyze", "--no-pager", "time"]] * 3)
        self.assertEqual(sleeps, [boot_time.ANALYZE_RETRY] * 2)

    def test_gives_up_without_failing(self):
        sleeps = []
        stages = boot_time.analyze(runner=lambda argv, **_: completed(NOT_FINISHED, 1),
                                   sleep=sleeps.append, wait=0)
        self.assertEqual(stages, {})
        self.assertEqual(sleeps, [])

        def broken(argv, **_):
            raise subprocess.TimeoutExpired(argv, 15)

        self.assertEqual(boot_time.analyze(runner=broken, sleep=sleeps.append, wait=0), {})
        self.assertEqual(boot_time.analyze(runner=mock.Mock(side_effect=OSError), sleep=sleeps.append, wait=0), {})


class LogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)

    def test_line_names_every_stage_and_marks_the_missing_ones(self):
        line = boot_time.log_line("2026-10-03T12:00:00+00:00", "0.3.0", 11.46,
                                  boot_time.parse_analyze(BIOS))
        self.assertEqual(
            line,
            "2026-10-03T12:00:00+00:00 version=0.3.0 launcher_ready=11.5 firmware=? loader=?"
            " kernel=2.0 initrd=3.5 userspace=8.1 total=13.6",
        )
        self.assertTrue(boot_time.log_line("t", "?", 9.0, {}).endswith("total=?"))

    def test_append_creates_the_log_and_keeps_the_last_boots(self):
        log = self.dir / "logs" / "boot-time.log"
        boot_time.append(log, "first")
        boot_time.append(log, "second")
        self.assertEqual(log.read_text(), "first\nsecond\n")
        log.write_text("".join(f"boot {n}\n" for n in range(boot_time.MAX_LINES)))
        boot_time.append(log, "newest")
        lines = log.read_text().splitlines()
        self.assertEqual(len(lines), boot_time.KEEP_LINES)
        self.assertEqual(lines[-1], "newest")
        self.assertEqual(lines[-2], f"boot {boot_time.MAX_LINES - 1}")

    def test_version_comes_from_build_info(self):
        info = self.dir / "build-info"
        info.write_text("CouchLiteOS: 0.3.0\nBuild profile: general\n")
        self.assertEqual(boot_time.version(info), "0.3.0")
        self.assertEqual(boot_time.version(self.dir / "missing"), "?")

    def test_main_prints_the_marker_first_and_writes_one_line(self):
        ready = self.dir / "launcher-ready"
        ready.touch()
        os.utime(ready, (1000.0, 1000.0))
        log = self.dir / "boot-time.log"
        info = self.dir / "build-info"
        info.write_text("CouchLiteOS: 0.3.0\n")
        order = []
        stdout = io.StringIO()

        def analyze():
            order.append(stdout.getvalue())
            return boot_time.parse_analyze(UEFI)

        with mock.patch.multiple(boot_time, READY=ready, LOG=log, BUILD_INFO=info, analyze=analyze), \
                mock.patch.object(boot_time.time, "time", return_value=1002.0), \
                mock.patch.object(boot_time.time, "clock_gettime", return_value=14.25), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(boot_time.main(), 0)
        # The serial marker is out before waiting for systemd to finish.
        self.assertEqual(order, ["COUCHLITEOS_BOOT_SECONDS=12.2\n"])
        self.assertEqual(stdout.getvalue(), "COUCHLITEOS_BOOT_SECONDS=12.2\n")
        line = log.read_text()
        self.assertRegex(line, r"^\S+ version=0\.3\.0 launcher_ready=12\.2 firmware=4\.1 loader=1\.5"
                               r" kernel=1\.9 initrd=2\.6 userspace=62\.2 total=72\.4\n$")

    def test_main_without_the_launcher_does_nothing(self):
        log = self.dir / "boot-time.log"
        sleeps = []
        with mock.patch.multiple(boot_time, READY=self.dir / "missing", LOG=log, READY_WAIT=5), \
                mock.patch.object(boot_time.time, "sleep", sleeps.append), \
                contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(boot_time.main(), 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertFalse(log.exists())
        self.assertEqual(sleeps, [1] * 5)

    def test_main_waits_for_a_launcher_that_is_slow_to_start(self):
        # The unit starts once the launcher service has, which can be before (or, after a
        # failed first start, long before) the launcher draws its first frame.
        ready = self.dir / "launcher-ready"
        log = self.dir / "boot-time.log"
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 3:
                ready.touch()

        with mock.patch.multiple(boot_time, READY=ready, LOG=log, BUILD_INFO=self.dir / "none",
                                 analyze=lambda: {}), \
                mock.patch.object(boot_time.time, "sleep", sleep), \
                contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(boot_time.main(), 0)
        self.assertEqual(sleeps, [1, 1, 1])
        self.assertRegex(stdout.getvalue(), r"^COUCHLITEOS_BOOT_SECONDS=[0-9.]+\n$")
        self.assertTrue(log.exists())


class UnitTest(unittest.TestCase):
    def test_unit_runs_after_the_launcher_and_does_not_hold_the_boot(self):
        unit = (ROOT / "services/couchliteos-boot-time.service").read_text()
        self.assertRegex(unit, r"(?m)^After=couchliteos-launcher\.service$")
        # Checked when the job starts, a condition skips the unit for the whole boot when the
        # launcher's first start fails; the script waits for launcher-ready instead.
        self.assertNotIn("ConditionPathExists", unit)
        runtime = int(re.search(r"(?m)^RuntimeMaxSec=(\d+)$", unit).group(1))
        self.assertGreater(runtime, boot_time.READY_WAIT + boot_time.ANALYZE_WAIT)
        # A oneshot still running would keep systemd-analyze from ever answering.
        self.assertRegex(unit, r"(?m)^Type=exec$")
        self.assertRegex(unit, r"(?m)^StandardOutput=journal\+console$")


@unittest.skipUnless(shutil.which("bash") and shutil.which("awk"), "needs bash and awk")
class SmokeGateTest(unittest.TestCase):
    SMOKE = ROOT / "tests/qemu-smoke.sh"

    def test_bad_baseline_is_refused_before_anything_starts(self):
        result = subprocess.run(
            ["bash", str(self.SMOKE), "/nonexistent.iso"], capture_output=True, text=True,
            env={**os.environ, "COUCHLITEOS_BOOT_BASELINE": "fast"},
        )
        self.assertEqual(result.returncode, 64)
        self.assertIn("COUCHLITEOS_BOOT_BASELINE", result.stderr)

    def test_fails_only_above_baseline_plus_a_quarter(self):
        text = self.SMOKE.read_text()
        check = re.search(r"awk -v n=\"\$boot_seconds\" -v b=\"\$BASELINE\" '([^']+)'", text).group(1)

        def over(seconds, baseline):
            return subprocess.run(["awk", "-v", f"n={seconds}", "-v", f"b={baseline}", check]).returncode == 0

        self.assertFalse(over("12.0", "10"))
        self.assertFalse(over("12.5", "10"))
        self.assertTrue(over("12.6", "10"))
        self.assertTrue(over("30.1", "20.0"))


if __name__ == "__main__":
    unittest.main()
