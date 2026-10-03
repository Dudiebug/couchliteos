import testenv  # noqa: F401  (first: scratch run and state directories)
import contextlib
import io
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

import couchliteos_power as power

MINUTE = 60.0
RESIZE = 410


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = pathlib.Path(self.directory.name) / "config.ini"

    def test_defaults_are_five_minutes_to_blank_and_thirty_to_sleep(self):
        self.assertEqual(power.load_settings(self.path), power.Settings(blank=5, sleep=30))
        self.assertEqual(power.BLANK_CHOICES, (0, 2, 5, 10, 15, 30))
        self.assertEqual(power.SLEEP_CHOICES, (0, 15, 30, 60, 120))

    def test_saved_values_round_trip_and_other_sections_survive(self):
        self.path.write_text("[display]\noutput = HDMI-A-1\n\n[power]\nblank_minutes = 2\n\n[moonlight]\nfps = 60\n")
        power.save_settings(power.Settings(blank=0, sleep=120), self.path)
        text = self.path.read_text()
        self.assertEqual(power.load_settings(self.path), power.Settings(blank=0, sleep=120))
        self.assertIn("[display]\noutput = HDMI-A-1\n", text)
        self.assertIn("[moonlight]\nfps = 60\n", text)
        self.assertEqual(text.count("[power]"), 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)

    def test_save_creates_a_missing_file(self):
        power.save_settings(power.Settings(blank=15, sleep=0), self.path)
        self.assertEqual(power.load_settings(self.path), power.Settings(blank=15, sleep=0))

    def test_invalid_values_fall_back_per_field(self):
        self.path.write_text("[power]\nblank_minutes = 7\nsleep_minutes = 60\n")
        self.assertEqual(power.load_settings(self.path), power.Settings(blank=5, sleep=60))
        self.path.write_text("[power]\nblank_minutes = abc\nsleep_minutes = -1\n")
        self.assertEqual(power.load_settings(self.path), power.Settings())

    def test_labels(self):
        self.assertEqual(power.minutes_label(0), "OFF")
        self.assertEqual(power.minutes_label(30), "30 MIN")
        self.assertEqual(power.minutes_label(120), "2 HOURS")
        self.assertEqual(power.minutes_label(60), "1 HOUR")


class IdleTimerTest(unittest.TestCase):
    def timer(self, blank=5, sleep=30, now=1000.0):
        return power.IdleTimer(power.Settings(blank=blank, sleep=sleep), now), now

    def test_blank_then_sleep_each_fire_once(self):
        timer, start = self.timer()
        idle = lambda: False
        self.assertIsNone(timer.poll(start + 4 * MINUTE, idle))
        self.assertEqual(timer.poll(start + 5 * MINUTE, idle), power.BLANK)
        self.assertTrue(timer.blanked)
        self.assertIsNone(timer.poll(start + 6 * MINUTE, idle))
        self.assertIsNone(timer.poll(start + 29 * MINUTE, idle))
        self.assertEqual(timer.poll(start + 30 * MINUTE, idle), power.SLEEP)
        self.assertIsNone(timer.poll(start + 31 * MINUTE, idle))

    def test_activity_reports_whether_the_screen_was_blank_and_restarts_the_timers(self):
        timer, start = self.timer()
        self.assertFalse(timer.activity(start + MINUTE))
        self.assertEqual(timer.poll(start + 5 * MINUTE, lambda: False), None)
        self.assertEqual(timer.poll(start + 6 * MINUTE, lambda: False), power.BLANK)
        self.assertTrue(timer.activity(start + 7 * MINUTE))
        self.assertFalse(timer.blanked)
        self.assertIsNone(timer.poll(start + 11 * MINUTE, lambda: False))
        self.assertEqual(timer.poll(start + 12 * MINUTE, lambda: False), power.BLANK)
        # A fresh sleep timer after activity too.
        self.assertIsNone(timer.poll(start + 36 * MINUTE, lambda: False))
        self.assertEqual(timer.poll(start + 37 * MINUTE, lambda: False), power.SLEEP)

    def test_a_running_application_holds_both_timers_until_it_exits(self):
        timer, start = self.timer()
        running = True
        for seconds in range(5, 120 * 60, 5):
            self.assertIsNone(timer.poll(start + seconds, lambda: running))
        self.assertFalse(timer.blanked)
        running = False
        exited = start + 120 * MINUTE
        self.assertIsNone(timer.poll(exited + 4 * MINUTE, lambda: running))
        self.assertEqual(timer.poll(exited + 5 * MINUTE + power.APP_POLL_SECONDS, lambda: running), power.BLANK)

    def test_application_check_is_skipped_when_nothing_can_fire_and_rate_limited(self):
        timer, start = self.timer(blank=0, sleep=0)
        checker = mock.Mock(return_value=False)
        self.assertIsNone(timer.poll(start + 500 * MINUTE, checker))
        checker.assert_not_called()
        timer, start = self.timer()
        checker = mock.Mock(return_value=False)
        timer.poll(start + 10.0, checker)  # too early to matter
        checker.assert_not_called()
        for seconds in range(40, 60):
            timer.poll(start + seconds, checker)
        self.assertLessEqual(checker.call_count, 4)
        self.assertGreaterEqual(checker.call_count, 1)

    def test_off_settings(self):
        timer, start = self.timer(blank=0, sleep=30)
        self.assertIsNone(timer.poll(start + 29 * MINUTE, lambda: False))
        self.assertEqual(timer.poll(start + 30 * MINUTE, lambda: False), power.SLEEP)
        timer, start = self.timer(blank=10, sleep=0)
        self.assertEqual(timer.poll(start + 10 * MINUTE, lambda: False), power.BLANK)
        self.assertIsNone(timer.poll(start + 1000 * MINUTE, lambda: False))

    def test_sleep_before_blank_is_honoured(self):
        timer, start = self.timer(blank=30, sleep=15)
        self.assertEqual(timer.poll(start + 15 * MINUTE, lambda: False), power.SLEEP)


class Clock:
    def __init__(self):
        self.now = 5000.0

    def __call__(self):
        return self.now


class FakeScreen:
    """getch returns scripted (seconds_to_advance, key) pairs."""

    def __init__(self, clock, script=()):
        self.clock = clock
        self.script = list(script)
        self.calls = []

    def erase(self):
        self.calls.append("erase")

    def refresh(self):
        self.calls.append("refresh")

    def getch(self):
        seconds, key = self.script.pop(0)
        self.clock.now += seconds
        return key


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.running = False
        self.home = False
        self.resume = False
        self.sleeps = 0
        self.cleared = 0

    def guard(self, blank=5, sleep=30, enabled=True):
        return power.IdleGuard(
            power.Settings(blank=blank, sleep=sleep),
            apps_running=lambda: self.running,
            request_sleep=self.request_sleep,
            resumed=self.consume_resume,
            home_pending=lambda: self.home,
            clear_home=self.clear_home,
            clock=self.clock,
            enabled=lambda: enabled,
            passive_keys=(-1, RESIZE),
        )

    def request_sleep(self):
        self.sleeps += 1

    def consume_resume(self):
        resumed, self.resume = self.resume, False
        return resumed

    def clear_home(self):
        self.home = False
        self.cleared += 1

    def test_keys_pass_through_while_awake(self):
        guard, screen = self.guard(), FakeScreen(self.clock)
        self.assertEqual(guard.filter(screen, 66), 66)
        self.assertEqual(guard.filter(screen, -1), -1)
        self.assertEqual(screen.calls, [])

    def test_blank_holds_the_screen_black_and_swallows_the_waking_key(self):
        guard = self.guard()
        screen = FakeScreen(self.clock, [(1.0, -1), (1.0, RESIZE), (1.0, 10)])
        self.clock.now += 5 * MINUTE
        self.assertEqual(guard.filter(screen, -1), -1)
        self.assertEqual(screen.calls, ["erase", "refresh"])
        self.assertEqual(screen.script, [])  # RESIZE and timeouts were not activity; ENTER woke it
        self.assertFalse(guard.timer.blanked)
        self.assertEqual(self.sleeps, 0)
        # The next Enter is a normal key again.
        self.assertEqual(guard.filter(screen, 10), 10)

    def test_a_screen_that_keeps_itself_awake_never_blanks_or_sleeps(self):
        guard, screen = self.guard(), FakeScreen(self.clock, [(1.0, 10)])
        for _ in range(10):  # ten minutes of an update downloading, each tick calling keep_awake
            self.clock.now += MINUTE
            guard.keep_awake()
            self.assertEqual(guard.filter(screen, -1), -1)
        self.assertEqual(screen.calls, [])
        self.assertEqual(self.sleeps, 0)
        self.clock.now += 5 * MINUTE  # and the normal timer starts again from the last tick
        guard.filter(screen, -1)
        self.assertEqual(screen.calls[:2], ["erase", "refresh"])

    def test_key_press_before_the_blank_deadline_just_restarts_the_timer(self):
        guard, screen = self.guard(), FakeScreen(self.clock)
        self.clock.now += 4 * MINUTE
        self.assertEqual(guard.filter(screen, 258), 258)
        self.clock.now += 4 * MINUTE
        self.assertEqual(guard.filter(screen, -1), -1)
        self.assertEqual(screen.calls, [])

    def test_resize_is_not_activity(self):
        guard, screen = self.guard(), FakeScreen(self.clock, [(1.0, 10)])
        self.clock.now += 4 * MINUTE
        guard.filter(screen, RESIZE)
        self.clock.now += MINUTE
        guard.filter(screen, -1)
        self.assertEqual(screen.calls[:2], ["erase", "refresh"])

    def test_sleep_is_requested_once_while_blank(self):
        guard = self.guard()
        screen = FakeScreen(self.clock, [(25 * MINUTE, -1), (MINUTE, -1), (MINUTE, 10)])
        self.clock.now += 5 * MINUTE
        guard.filter(screen, -1)
        self.assertEqual(self.sleeps, 1)
        self.assertFalse(guard.timer.blanked)

    def test_pending_home_request_is_activity_but_is_left_for_the_launcher_while_awake(self):
        guard, screen = self.guard(), FakeScreen(self.clock)
        self.clock.now += 4 * MINUTE
        self.home = True
        guard.filter(screen, -1)
        self.home = False
        self.clock.now += 4 * MINUTE
        guard.filter(screen, -1)
        self.assertEqual((screen.calls, self.cleared), ([], 0))

    def test_home_request_wakes_the_blank_screen_and_is_consumed(self):
        guard = self.guard()
        screen = FakeScreen(self.clock, [(1.0, -1), (1.0, -1)])
        self.clock.now += 5 * MINUTE
        original = screen.getch

        def getch():
            key = original()
            self.home = len(screen.script) == 0  # the Guide press arrives on the second wait
            return key

        screen.getch = getch
        self.assertEqual(guard.filter(screen, -1), -1)
        self.assertEqual(self.cleared, 1)
        self.assertFalse(self.home)
        self.assertFalse(guard.timer.blanked)

    def test_application_started_elsewhere_ends_the_blank(self):
        guard = self.guard()
        screen = FakeScreen(self.clock, [(10.0, -1)])
        self.clock.now += 5 * MINUTE
        self.running = False
        original = screen.getch

        def getch():
            self.running = True
            return original()

        screen.getch = getch
        self.assertEqual(guard.filter(screen, -1), -1)
        self.assertFalse(guard.timer.blanked)

    def test_resume_resets_everything_and_restores_the_screen(self):
        guard = self.guard()
        screen = FakeScreen(self.clock, [(1.0, -1)])
        self.clock.now += 5 * MINUTE
        original = screen.getch

        def getch():
            self.resume = True
            return original()

        screen.getch = getch
        self.assertEqual(guard.filter(screen, -1), -1)
        self.assertFalse(guard.timer.blanked)
        self.clock.now += 4 * MINUTE
        self.assertEqual(guard.filter(screen, -1), -1)
        self.assertFalse(guard.timer.blanked)  # the idle clock restarted at resume

    def test_the_first_real_key_after_a_resume_is_swallowed_and_the_next_one_is_not(self):
        # The resume marker is used up by a passive tick; the pad reconnects and the user presses A later.
        guard, screen = self.guard(), FakeScreen(self.clock)
        self.resume = True
        self.assertEqual(guard.filter(screen, -1), -1)
        self.clock.now += 4.0
        self.assertEqual(guard.filter(screen, 10), -1)  # the key that "wakes" the box does nothing
        self.assertEqual(guard.filter(screen, 10), 10)  # the one after it is a normal key again

    def test_the_swallow_lasts_about_ten_seconds(self):
        guard, screen = self.guard(), FakeScreen(self.clock)
        self.resume = True
        guard.filter(screen, -1)
        self.clock.now += power.RESUME_SWALLOW_SECONDS - 0.5
        self.assertEqual(guard.filter(screen, 10), -1)
        self.resume = True
        guard.filter(screen, -1)
        self.clock.now += power.RESUME_SWALLOW_SECONDS + 0.5
        self.assertEqual(guard.filter(screen, 10), 10)  # long after waking, a key press is meant
        self.assertGreaterEqual(power.RESUME_SWALLOW_SECONDS, 9.0)
        self.assertLessEqual(power.RESUME_SWALLOW_SECONDS, 11.0)

    def test_a_key_that_arrives_together_with_the_resume_is_the_one_swallowed(self):
        guard, screen = self.guard(), FakeScreen(self.clock)
        self.resume = True
        self.assertEqual(guard.filter(screen, 10), -1)
        self.clock.now += 2.0
        self.assertEqual(guard.filter(screen, 10), 10)  # no second key is held back

    def test_timeouts_and_resizes_do_not_use_up_the_swallow(self):
        guard, screen = self.guard(), FakeScreen(self.clock)
        self.resume = True
        guard.filter(screen, -1)
        for key in (-1, RESIZE, -1):
            self.clock.now += 1.0
            self.assertEqual(guard.filter(screen, key), -1)  # passive keys never reach the screens
        self.assertEqual(guard.filter(screen, 10), -1)
        self.assertEqual(guard.filter(screen, 10), 10)

    def test_a_resume_seen_while_the_screen_is_blank_also_swallows_the_next_key(self):
        guard = self.guard()
        screen = FakeScreen(self.clock, [(1.0, -1)])
        self.clock.now += 5 * MINUTE
        original = screen.getch

        def getch():
            self.resume = True
            return original()

        screen.getch = getch
        self.assertEqual(guard.filter(screen, -1), -1)  # blanked, then the resume marker appeared
        self.clock.now += 3.0
        self.assertEqual(guard.filter(screen, 10), -1)
        self.assertEqual(guard.filter(screen, 10), 10)

    def test_the_swallow_is_not_used_while_automatic_power_is_off(self):
        guard, screen = self.guard(enabled=False), FakeScreen(self.clock)
        self.resume = True
        guard.filter(screen, -1)
        self.assertEqual(guard.filter(screen, 10), 10)  # the QEMU smoke tests press keys right away

    def test_resume_is_consumed_even_when_automatic_power_is_off(self):
        guard = self.guard(enabled=False)
        self.resume = True
        self.assertEqual(guard.filter(FakeScreen(self.clock), 66), -1)
        self.assertFalse(self.resume)

    def test_disabled_guard_never_blanks_or_sleeps(self):
        guard, screen = self.guard(enabled=False), FakeScreen(self.clock)
        self.clock.now += 500 * MINUTE
        self.assertEqual(guard.filter(screen, -1), -1)
        self.assertEqual(guard.filter(screen, 66), 66)
        self.assertEqual((screen.calls, self.sleeps), ([], 0))

    def test_apply_restarts_timers_with_new_settings(self):
        guard, screen = self.guard(), FakeScreen(self.clock, [(1.0, 10)])
        self.clock.now += 4 * MINUTE
        guard.apply(power.Settings(blank=2, sleep=0))
        self.clock.now += 2 * MINUTE
        guard.filter(screen, -1)
        self.assertEqual(screen.calls, ["erase", "refresh"])


class SmokeAndMacTest(unittest.TestCase):
    def test_qemu_smoke_flag_disables_automatic_power(self):
        with tempfile.TemporaryDirectory() as directory:
            flag = pathlib.Path(directory) / "raw"
            self.assertFalse(power.smoke_test_active(flag))
            flag.write_text("apps")
            self.assertTrue(power.smoke_test_active(flag))

    def test_wired_mac_picks_the_physical_ethernet_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)

            def nic(name, address, kind="1", device=True, wireless=False):
                path = root / name
                path.mkdir()
                (path / "address").write_text(address + "\n")
                (path / "type").write_text(kind + "\n")
                if device:
                    (path / "device").mkdir()
                if wireless:
                    (path / "wireless").mkdir()

            nic("lo", "00:00:00:00:00:00", device=False)
            nic("tailscale0", "00:00:00:00:00:00", kind="65534", device=False)
            nic("docker0", "02:42:ac:11:00:01", device=False)
            nic("wlan0", "aa:aa:aa:aa:aa:aa", wireless=True)
            nic("enp2s0", "D8:BB:C1:01:02:03")
            self.assertEqual(power.wired_mac(root), "D8:BB:C1:01:02:03")
            self.assertIsNone(power.wired_mac(root / "nothing-here"))


def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(["x"], returncode, stdout, stderr)


class SuspendSupportTest(unittest.TestCase):
    def test_logind_yes_or_challenge_means_the_machine_can_suspend(self):
        # "challenge" only says the asking user would have to authenticate; the real
        # suspend runs as root, and logind answers "na" first when the hardware cannot.
        for answer in ("yes", "challenge"):
            self.assertTrue(power.suspend_supported(answer, None), answer)
            self.assertTrue(power.suspend_supported(answer, "disk"), answer)

    def test_logind_na_means_it_cannot_even_when_the_kernel_lists_mem(self):
        self.assertFalse(power.suspend_supported("na", "freeze mem disk"))

    def test_without_a_hardware_verdict_the_kernel_state_list_decides(self):
        # No polkit on the live image: a non-root CanSuspend call fails or says "no",
        # which describes the caller's rights, not the machine.
        for answer in (None, "no"):
            self.assertTrue(power.suspend_supported(answer, "freeze mem disk\n"), answer)
            self.assertTrue(power.suspend_supported(answer, "mem"), answer)
            self.assertTrue(power.suspend_supported(answer, "freeze"), answer)
            self.assertFalse(power.suspend_supported(answer, "disk"), answer)
            self.assertFalse(power.suspend_supported(answer, "standby disk"), answer)
            self.assertFalse(power.suspend_supported(answer, ""), answer)
            self.assertFalse(power.suspend_supported(answer, None), answer)

    def test_logind_answer_is_read_from_busctl_over_the_system_bus(self):
        run = mock.Mock(return_value=completed('s "yes"\n'))
        self.assertEqual(power.logind_can_suspend(run), "yes")
        command = run.call_args.args[0]
        self.assertEqual(command[0], "busctl")
        self.assertIn("--system", command)
        self.assertEqual(command[-1], "CanSuspend")
        self.assertIn("org.freedesktop.login1.Manager", command)
        self.assertEqual(power.logind_can_suspend(mock.Mock(return_value=completed('s "na"\n'))), "na")

    def test_unreachable_or_unreadable_logind_gives_no_answer(self):
        denied = completed("", 1, "Call failed: Access denied\n")
        for run in (
            mock.Mock(return_value=denied),
            mock.Mock(return_value=completed("garbage\n")),
            mock.Mock(return_value=completed('s "maybe"\n')),
            mock.Mock(side_effect=FileNotFoundError("busctl")),
            mock.Mock(side_effect=subprocess.TimeoutExpired("busctl", 3)),
        ):
            self.assertIsNone(power.logind_can_suspend(run))

    def test_can_suspend_asks_logind_then_the_kernel(self):
        with tempfile.TemporaryDirectory() as directory:
            state = pathlib.Path(directory) / "state"
            denied = mock.Mock(return_value=completed("", 1, "Call failed: Access denied\n"))
            self.assertFalse(power.can_suspend(denied, state))  # no state file at all
            state.write_text("freeze mem disk\n")
            self.assertTrue(power.can_suspend(denied, state))
            state.write_text("disk\n")
            self.assertFalse(power.can_suspend(denied, state))
            state.write_text("freeze mem disk\n")
            self.assertFalse(power.can_suspend(mock.Mock(return_value=completed('s "na"\n')), state))

    def test_unsupported_hardware_ignores_the_saved_sleep_timeout_but_not_blanking(self):
        saved = power.Settings(blank=10, sleep=60)
        self.assertEqual(power.effective_settings(saved, True), saved)
        effective = power.effective_settings(saved, False)
        self.assertEqual(effective, power.Settings(blank=10, sleep=0))
        self.assertEqual(saved, power.Settings(blank=10, sleep=60))  # the stored value is untouched
        timer = power.IdleTimer(effective, 0.0)
        self.assertIsNone(timer.poll(9 * MINUTE, lambda: False))
        self.assertEqual(timer.poll(10 * MINUTE, lambda: False), power.BLANK)
        self.assertIsNone(timer.poll(500 * MINUTE, lambda: False))  # never asks for sleep

    def test_nothing_that_can_wake_the_pc_turns_idle_sleep_off_at_runtime_only(self):
        saved = power.Settings(blank=10, sleep=60)
        effective = power.effective_settings(saved, True, wake_ok=False)
        self.assertEqual(effective, power.Settings(blank=10, sleep=0))
        self.assertEqual(saved, power.Settings(blank=10, sleep=60))  # the stored value is untouched
        self.assertEqual(power.effective_settings(saved, True, wake_ok=True), saved)
        self.assertEqual(power.effective_settings(saved, True), saved)
        self.assertEqual(power.effective_settings(saved, False, wake_ok=True).sleep, 0)
        timer = power.IdleTimer(effective, 0.0)
        self.assertEqual(timer.poll(10 * MINUTE, lambda: False), power.BLANK)
        self.assertIsNone(timer.poll(500 * MINUTE, lambda: False))  # never asks for sleep


ETHTOOL_MAGIC = """Settings for enp2s0:
\tSupported ports: [ TP ]
\tSupports Wake-on: pumbg
\tWake-on: d
\tLink detected: yes
"""
ETHTOOL_NO_MAGIC = "Settings for enp3s0:\n\tSupports Wake-on: pumb\n\tWake-on: d\n"
ETHTOOL_NO_WOL = "Settings for ens3:\n\tSupported ports: [ ]\n\tLink detected: yes\n"


class WakeOnLanTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.net = pathlib.Path(self.directory.name) / "net"
        self.state = pathlib.Path(self.directory.name) / "wol"
        self.net.mkdir()

    def nic(self, name, address, kind="1", device=True, wireless=False):
        path = self.net / name
        path.mkdir()
        (path / "address").write_text(address + "\n")
        (path / "type").write_text(kind + "\n")
        if device:
            (path / "device").mkdir()
        if wireless:
            (path / "wireless").mkdir()

    def test_magic_packet_support_is_read_from_the_supports_line_only(self):
        self.assertTrue(power.wol_magic_supported(ETHTOOL_MAGIC))
        self.assertTrue(power.wol_magic_supported("\tSupports Wake-on: umbg\n\tWake-on: d\n"))
        self.assertFalse(power.wol_magic_supported(ETHTOOL_NO_MAGIC))
        self.assertFalse(power.wol_magic_supported(ETHTOOL_NO_WOL))
        self.assertFalse(power.wol_magic_supported("\tSupports Wake-on: d\n"))
        self.assertFalse(power.wol_magic_supported("\tWake-on: g\n"))  # the current mode is not support
        self.assertFalse(power.wol_magic_supported(""))

    def test_probe_records_each_wired_adapter_by_mac(self):
        self.nic("lo", "00:00:00:00:00:00", device=False)
        self.nic("wlan0", "aa:aa:aa:aa:aa:aa", wireless=True)
        self.nic("enp2s0", "D8:BB:C1:01:02:03")
        self.nic("enp3s0", "d8:bb:c1:0a:0b:0c")
        self.nic("ens3", "52:54:00:12:34:56")
        outputs = {"enp2s0": ETHTOOL_MAGIC, "enp3s0": ETHTOOL_NO_MAGIC, "ens3": ETHTOOL_NO_WOL}
        run = mock.Mock(side_effect=lambda command, **_kw: completed(outputs[command[-1]]))
        power.probe_wake_on_lan(self.net, self.state, run)
        self.assertEqual(
            {path.name: path.read_text().strip() for path in self.state.iterdir()},
            {"d8-bb-c1-01-02-03": "magic", "d8-bb-c1-0a-0b-0c": "none", "52-54-00-12-34-56": "none"},
        )
        asked = [call.args[0] for call in run.call_args_list]
        self.assertTrue(all(command[0].endswith("ethtool") for command in asked))
        self.assertEqual(sorted(command[-1] for command in asked), ["enp2s0", "enp3s0", "ens3"])

    def test_probe_writes_nothing_when_ethtool_cannot_answer(self):
        self.nic("enp2s0", "d8:bb:c1:01:02:03")
        for run in (
            mock.Mock(side_effect=FileNotFoundError("ethtool")),
            mock.Mock(return_value=completed("", 75, "Cannot get device settings")),
        ):
            power.probe_wake_on_lan(self.net, self.state, run)
        self.assertFalse(self.state.exists() and any(self.state.iterdir()))

    def test_probe_replaces_an_older_answer_for_the_same_adapter(self):
        self.nic("enp2s0", "d8:bb:c1:01:02:03")
        power.probe_wake_on_lan(self.net, self.state, mock.Mock(return_value=completed(ETHTOOL_NO_MAGIC)))
        power.probe_wake_on_lan(self.net, self.state, mock.Mock(return_value=completed(ETHTOOL_MAGIC)))
        self.assertEqual((self.state / "d8-bb-c1-01-02-03").read_text().strip(), "magic")

    def test_status_shows_the_mac_only_when_a_wired_adapter_can_wake_on_magic_packet(self):
        self.assertEqual(power.wake_on_lan(self.net, self.state), ("none", None))
        self.nic("enp2s0", "d8:bb:c1:01:02:03")
        self.assertEqual(power.wake_on_lan(self.net, self.state), ("unknown", None))
        self.state.mkdir()
        (self.state / "d8-bb-c1-01-02-03").write_text("none\n")
        self.assertEqual(power.wake_on_lan(self.net, self.state), ("unsupported", None))
        (self.state / "d8-bb-c1-01-02-03").write_text("magic\n")
        self.assertEqual(power.wake_on_lan(self.net, self.state), ("supported", "D8:BB:C1:01:02:03"))

    def test_a_second_adapter_that_supports_it_is_found(self):
        self.nic("enp2s0", "d8:bb:c1:01:02:03")
        self.nic("enp3s0", "d8:bb:c1:0a:0b:0c")
        self.state.mkdir()
        (self.state / "d8-bb-c1-01-02-03").write_text("none\n")
        (self.state / "d8-bb-c1-0a-0b-0c").write_text("magic\n")
        self.assertEqual(power.wake_on_lan(self.net, self.state), ("supported", "D8:BB:C1:0A:0B:0C"))

    def test_an_answer_for_an_adapter_that_is_gone_is_ignored(self):
        self.nic("enp2s0", "d8:bb:c1:01:02:03")
        self.state.mkdir()
        (self.state / "aa-bb-cc-dd-ee-ff").write_text("magic\n")  # left behind by an unplugged USB adapter
        self.assertEqual(power.wake_on_lan(self.net, self.state), ("unknown", None))

    def test_the_probe_is_runnable_from_udev_as_a_script(self):
        with mock.patch.object(power, "probe_wake_on_lan") as probe:
            self.assertEqual(power.main(["wake-on-lan"]), 0)
        probe.assert_called_once_with()
        with contextlib.redirect_stderr(io.StringIO()) as usage:
            self.assertEqual(power.main(["unknown"]), 2)
            self.assertEqual(power.main([]), 2)
        self.assertIn("usage", usage.getvalue())


class WakeSourcesTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.usb = pathlib.Path(self.directory.name)

    def device(self, name, device_class="00", wakeup="enabled"):
        path = self.usb / name
        (path / "power").mkdir(parents=True)
        (path / "bDeviceClass").write_text(device_class + "\n")
        if wakeup is not None:
            (path / "power" / "wakeup").write_text(wakeup + "\n")

    def interface(self, name, interface_class, protocol="00"):
        path = self.usb / name
        path.mkdir()
        (path / "bInterfaceClass").write_text(interface_class + "\n")
        (path / "bInterfaceProtocol").write_text(protocol + "\n")

    def test_nothing_plugged_in_means_nothing_can_wake_the_pc(self):
        self.assertEqual(power.wake_sources(self.usb), [])
        self.assertEqual(power.wake_sources(self.usb / "missing"), [])

    def test_bluetooth_adapters_and_usb_keyboards_with_wakeup_enabled_are_listed(self):
        self.device("1-1", "e0")
        self.interface("1-1:1.0", "e0", "01")
        self.device("1-2")
        self.interface("1-2:1.0", "03", "01")
        self.device("usb1", "09")  # a hub
        self.assertEqual(power.wake_sources(self.usb), ["BLUETOOTH ADAPTER", "USB KEYBOARD"])

    def test_devices_that_cannot_wake_or_have_wakeup_off_are_not_listed(self):
        self.device("1-1", "e0", wakeup=None)
        self.device("1-2", wakeup="disabled")
        self.interface("1-2:1.0", "03", "01")
        self.device("1-3")
        self.interface("1-3:1.0", "03", "02")  # a mouse, not a keyboard
        self.assertEqual(power.wake_sources(self.usb), [])


if __name__ == "__main__":
    unittest.main()
