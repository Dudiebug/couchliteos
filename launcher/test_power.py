import pathlib
import tempfile
import unittest
from unittest import mock

import moonlightos_power as power

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


if __name__ == "__main__":
    unittest.main()
