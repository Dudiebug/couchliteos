import importlib.machinery
import importlib.util
import pathlib
import tempfile
import unittest
from unittest import mock

import couchliteos_cec as cec


def load_daemon():
    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "couchliteos-cec"
    loader = importlib.machinery.SourceFileLoader("couchliteos_cec_daemon", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


REAL_WATCH = cec.watch  # DaemonTest replaces cec.watch; a few tests run the real one
ADAPTER = cec.Adapter("/dev/cec0", "vivid", "vivid-000-vid-out0", frozenset(), "1.1.0.0", 0x10)


class DaemonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.daemon = load_daemon()

    def setUp(self):
        self.sleeps = []
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.marker = pathlib.Path(directory.name) / "cec-active-source"
        self.popen = mock.MagicMock()
        self.popen.return_value.stdout = iter(())
        patches = {
            "sleep": mock.patch.object(self.daemon.time, "sleep", side_effect=self.sleeps.append),
            "popen": mock.patch.object(self.daemon.subprocess, "Popen", self.popen),
            "edid": mock.patch.object(cec, "edid_path", return_value=None),
            "settings": mock.patch.object(cec, "load_settings", return_value=cec.Settings(True, False)),
            "watch": mock.patch.object(cec, "watch"),
            "tv_on": mock.patch.object(cec, "turn_tv_on", return_value=True),
            "marker": mock.patch.object(cec, "ACTIVE_SOURCE_MARKER", self.marker),
        }
        self.mocks = {}
        for name, patch in patches.items():
            self.mocks[name] = patch.start()
            self.addCleanup(patch.stop)

    def nodes(self, *rounds):
        patch = mock.patch.object(cec, "device_nodes", side_effect=list(rounds))
        self.addCleanup(patch.stop)
        return patch.start()

    def bring_up(self, *results):
        patch = mock.patch.object(cec, "bring_up", side_effect=list(results))
        self.addCleanup(patch.stop)
        return patch.start()

    def test_without_a_cec_device_it_exits_at_once_and_touches_nothing(self):
        self.nodes([])
        bring_up = self.bring_up()
        self.assertEqual(self.daemon.main(), 0)
        bring_up.assert_not_called()
        self.popen.assert_not_called()
        self.mocks["tv_on"].assert_not_called()

    def test_with_no_link_to_a_tv_it_waits_and_looks_again(self):
        self.nodes(["/dev/cec0"], [])
        self.bring_up(None)
        self.assertEqual(self.daemon.main(), 0)
        self.assertEqual(self.sleeps, [self.daemon.RETRY_SECONDS])
        self.mocks["tv_on"].assert_not_called()

    def test_the_wait_for_a_tv_link_backs_off_and_is_logged_once(self):
        self.nodes(*[["/dev/cec0"]] * 6, [])
        self.bring_up(*[None] * 6)
        with mock.patch.object(self.daemon, "log") as log:
            self.assertEqual(self.daemon.main(), 0)
        self.assertEqual(self.sleeps, [30, 60, 120, 240, 300, 300])
        waiting = [call for call in log.call_args_list if "waiting" in call.args[0]]
        self.assertEqual(len(waiting), 1)

    def test_the_wait_starts_over_and_is_logged_again_after_the_link_came_and_went(self):
        self.nodes(["/dev/cec0"], ["/dev/cec0"], ["/dev/cec0"], ["/dev/cec0"], [])
        self.bring_up(None, None, ADAPTER, None)
        with mock.patch.object(self.daemon, "log") as log:
            self.assertEqual(self.daemon.main(), 0)
        self.assertEqual(self.sleeps, [30, 60, self.daemon.RESTART_SECONDS, 30])
        messages = [call.args[0] for call in log.call_args_list]
        self.assertEqual(sum("waiting" in message for message in messages), 2)
        self.assertEqual(sum("link" in message and "waiting" not in message for message in messages), 1)

    def test_switches_the_tv_on_once_then_follows_the_bus_again_after_a_dropout(self):
        self.nodes(["/dev/cec0"], ["/dev/cec0"], [])
        self.bring_up(ADAPTER, ADAPTER)
        self.assertEqual(self.daemon.main(), 0)
        self.assertEqual(self.mocks["tv_on"].call_count, 1)
        self.assertEqual(self.mocks["tv_on"].call_args.args[:2], ("/dev/cec0", "1.1.0.0"))
        self.assertEqual(self.mocks["watch"].call_count, 2)
        self.assertEqual(
            self.popen.call_args.args[0], ["cec-ctl", "-d", "/dev/cec0", "--skip-info", "--monitor"]
        )

    def test_the_quiet_period_for_standby_starts_when_the_tv_is_on_not_when_the_daemon_starts(self):
        clock = {"now": 100.0}

        def slow_tv(*_args, **_kwargs):
            clock["now"] = 112.0  # the TV took twelve seconds to wake
            return True

        self.mocks["tv_on"].side_effect = slow_tv
        self.nodes(["/dev/cec0"], [])
        self.bring_up(ADAPTER)
        with mock.patch.object(self.daemon.time, "monotonic", side_effect=lambda: clock["now"]):
            self.daemon.main()
        self.assertEqual(self.mocks["watch"].call_args.args[-1], 112.0)

    def test_a_saved_sleep_setting_is_ignored_on_a_pc_that_cannot_suspend(self):
        self.mocks["settings"].return_value = cec.Settings(True, True)
        self.nodes(["/dev/cec0"], [])
        self.bring_up(ADAPTER)
        with mock.patch.object(cec, "suspend_supported", return_value=False):
            self.daemon.main()
            load = self.mocks["watch"].call_args.args[1]
            self.assertEqual(load(), cec.Settings(True, False))

    def test_leaves_the_tv_alone_when_the_setting_is_off(self):
        self.mocks["settings"].return_value = cec.Settings(False, True)
        self.nodes(["/dev/cec0"], [])
        self.bring_up(ADAPTER)
        self.daemon.main()
        self.mocks["tv_on"].assert_not_called()
        self.assertEqual(self.mocks["watch"].call_count, 1)

    def test_before_sleep_the_tv_is_sent_to_standby_when_it_is_showing_this_box(self):
        self.marker.touch()
        self.nodes(["/dev/cec0"])
        with mock.patch.object(cec, "standby_tv", return_value=True) as standby:
            self.assertEqual(self.daemon.main(["--standby"]), 0)
        standby.assert_called_once_with(["/dev/cec0"])
        self.popen.assert_not_called()
        self.assertEqual(self.sleeps, [])
        self.mocks["tv_on"].assert_not_called()

    def test_before_sleep_nothing_is_sent_when_the_setting_is_off(self):
        self.mocks["settings"].return_value = cec.Settings(True, False, False)
        self.marker.touch()
        self.nodes(["/dev/cec0"])
        with mock.patch.object(cec, "standby_tv") as standby:
            self.assertEqual(self.daemon.main(["--standby"]), 0)
        standby.assert_not_called()

    def test_before_sleep_the_tv_is_left_alone_when_it_may_be_showing_something_else(self):
        # no marker: another input is showing, or nothing is known (a daemon that just started, a TV that never said)
        self.nodes(["/dev/cec0"])
        with mock.patch.object(cec, "standby_tv") as standby, mock.patch.object(self.daemon, "log") as log:
            self.assertEqual(self.daemon.main(["--standby"]), 0)
        standby.assert_not_called()
        self.assertIn("left alone", log.call_args.args[0])

    def test_before_sleep_a_marker_alone_does_not_bring_back_a_setting_that_is_off(self):
        self.mocks["settings"].return_value = cec.Settings(True, False, False)
        self.marker.touch()
        self.nodes(["/dev/cec0"])
        with mock.patch.object(cec, "standby_tv") as standby, mock.patch.object(self.daemon, "log") as log:
            self.assertEqual(self.daemon.main(["--standby"]), 0)
        standby.assert_not_called()
        log.assert_not_called()

    def run_daemon_over(self, *monitor_lines):
        """The daemon with the real watch() reading these cec-ctl lines, TV switch-on mocked as answered."""
        self.mocks["watch"].side_effect = REAL_WATCH
        self.popen.return_value.stdout = iter(monitor_lines)
        self.nodes(["/dev/cec0"], [])
        self.bring_up(ADAPTER)
        self.daemon.main()

    def test_a_tv_switched_to_another_input_is_left_on_when_the_pc_sleeps(self):
        # the box turned the TV on and was the active source; later the cable box took over
        self.run_daemon_over(
            "Received from Playback Device 2 to all (8 to 15): ACTIVE_SOURCE (0x82):",
            "\tphys-addr: 2.0.0.0",
        )
        self.assertFalse(self.marker.exists())
        self.nodes(["/dev/cec0"])
        with mock.patch.object(cec, "standby_tv") as standby:
            self.daemon.main(["--standby"])
        standby.assert_not_called()

    def test_a_tv_still_showing_this_box_goes_to_standby_when_the_pc_sleeps(self):
        self.run_daemon_over(
            "Received from Playback Device 2 to all (8 to 15): ACTIVE_SOURCE (0x82):",
            "\tphys-addr: 2.0.0.0",
            "Received from TV to all (0 to 15): SET_STREAM_PATH (0x86):",
            "\tphys-addr: 1.1.0.0",
        )
        self.assertTrue(self.marker.exists())
        self.nodes(["/dev/cec0"])
        with mock.patch.object(cec, "standby_tv", return_value=True) as standby:
            self.daemon.main(["--standby"])
        standby.assert_called_once_with(["/dev/cec0"])

    def test_the_tv_is_known_to_show_this_box_once_the_daemon_made_it_the_active_source(self):
        self.nodes(["/dev/cec0"], [])
        self.bring_up(ADAPTER)
        self.daemon.main()
        self.assertTrue(self.marker.exists())

    def test_a_marker_left_by_an_earlier_run_is_cleared_when_the_daemon_starts(self):
        self.marker.touch()  # before a sleep, say; the TV may have been switched meanwhile
        self.mocks["settings"].return_value = cec.Settings(False, False)  # and this run does not switch it on
        self.nodes(["/dev/cec0"], [])
        self.bring_up(ADAPTER)
        self.daemon.main()
        self.assertFalse(self.marker.exists())

    def test_a_tv_that_does_not_answer_is_not_known_to_show_this_box(self):
        self.marker.touch()
        self.mocks["tv_on"].return_value = False
        self.nodes(["/dev/cec0"], [])
        self.bring_up(ADAPTER)
        self.daemon.main()
        self.assertFalse(self.marker.exists())

    def test_a_dropout_of_the_monitor_does_not_forget_what_the_tv_shows(self):
        self.nodes(["/dev/cec0"], ["/dev/cec0"], [])
        self.bring_up(ADAPTER, ADAPTER)
        self.daemon.main()
        self.assertEqual(self.mocks["watch"].call_count, 2)
        self.assertTrue(self.marker.exists())

    def test_the_watch_is_given_this_boxs_address_and_the_marker(self):
        self.nodes(["/dev/cec0"], [])
        self.bring_up(ADAPTER)
        self.daemon.main()
        self.assertEqual(self.mocks["watch"].call_args.kwargs, {"phys_addr": "1.1.0.0", "marker": self.marker})

    def test_a_marker_that_cannot_be_written_does_not_stop_the_daemon(self):
        gone = self.marker.parent / "gone" / "cec-active-source"
        self.nodes(["/dev/cec0"], [])
        self.bring_up(ADAPTER)
        with mock.patch.object(cec, "ACTIVE_SOURCE_MARKER", gone), mock.patch.object(self.daemon, "log") as log:
            self.assertEqual(self.daemon.main(), 0)
        self.assertEqual(self.mocks["watch"].call_count, 1)
        self.assertTrue(any("could not create" in call.args[0] for call in log.call_args_list))

    def test_before_sleep_nothing_is_sent_without_a_cec_device(self):
        self.nodes([])
        with mock.patch.object(cec, "standby_tv") as standby:
            self.assertEqual(self.daemon.main(["--standby"]), 0)
        standby.assert_not_called()

    def test_a_tv_that_does_not_answer_does_not_fail_the_sleep_hook(self):
        self.nodes(["/dev/cec0"])
        with mock.patch.object(cec, "standby_tv", return_value=False):
            self.assertEqual(self.daemon.main(["--standby"]), 0)

    def test_suspend_asks_systemd(self):
        with mock.patch.object(self.daemon.subprocess, "run") as run:
            self.daemon.suspend()
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], [self.daemon.SYSTEMCTL, "suspend"])

    def test_a_failing_systemctl_does_not_kill_the_daemon(self):
        with mock.patch.object(self.daemon.subprocess, "run", side_effect=OSError("no systemctl")):
            self.daemon.suspend()


if __name__ == "__main__":
    unittest.main()
