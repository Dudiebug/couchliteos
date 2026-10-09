import testenv  # noqa: F401  (first: scratch run and state directories)
import importlib.util
import pathlib
import tempfile
import threading
import unittest
from unittest import mock

import couchliteos_pcstatus as pcstatus
import couchliteos_stream as stream

MAC = bytes.fromhex("aabbccddeeff")


def host(name="Desktop-7Q2", mac=MAC, local="192.168.1.20"):
    return stream.Host(name=name, mac=mac, local=local)


def text(hosts, result, link=True, settings=stream.StreamSettings()):
    chosen = pcstatus.pick_host(hosts, settings)
    block = stream.wake_block(chosen) if isinstance(chosen, stream.Host) else ""
    return pcstatus.status_text(hosts, chosen, result, link, block)


class StatusTextTest(unittest.TestCase):
    def test_ready_when_probe_up(self):
        self.assertEqual(text([host()], "up"), "GAMING PC: DESKTOP-7Q2 READY")

    def test_asleep_says_moonlight_wakes_it_when_wol_possible(self):
        self.assertEqual(
            text([host()], "down"), "GAMING PC: DESKTOP-7Q2 ASLEEP - STARTING MOONLIGHT WAKES IT"
        )

    def test_not_found_when_wake_blocked(self):
        self.assertEqual(
            text([host(mac=b"")], "down"), "GAMING PC: DESKTOP-7Q2 NOT FOUND - IS IT TURNED ON?"
        )

    def test_awake_but_sunshine_down(self):
        self.assertEqual(
            text([host()], "awake"), "GAMING PC: DESKTOP-7Q2 IS ON BUT SUNSHINE IS NOT ANSWERING"
        )

    def test_no_host_paired_shows_nothing(self):
        # Many people use this box for PlayStation Remote Play or as a plain desktop.
        self.assertEqual(text([], None), "")
        self.assertFalse(hasattr(pcstatus, "NONE_PAIRED"))

    def test_no_link_shows_nothing(self):
        for hosts, result in (([host()], "up"), ([], None), ([host(), host("B")], None)):
            self.assertEqual(text(hosts, result, link=False), "")

    def test_several_hosts_without_default_shows_count(self):
        hosts = [host("ONE"), host("TWO", local="192.168.1.21"), host("THREE", local="192.168.1.22")]
        self.assertEqual(text(hosts, None), "GAMING PCS: 3 PAIRED")

    def test_several_hosts_with_a_saved_default_shows_that_pc(self):
        hosts = [host("ONE"), host("TWO", local="192.168.1.21")]
        self.assertEqual(
            text(hosts, "up", settings=stream.StreamSettings(host="two")), "GAMING PC: TWO READY"
        )

    def test_saved_default_from_another_system_is_not_guessed(self):
        hosts = [host("ONE"), host("TWO", local="192.168.1.21")]
        self.assertEqual(
            text(hosts, None, settings=stream.StreamSettings(host="gone")), "GAMING PCS: 2 PAIRED"
        )

    def test_unknown_probe_and_missing_probe_show_nothing(self):
        self.assertEqual(text([host(local="")], "unknown"), "")
        self.assertEqual(text([host()], None), "")

    def test_long_label_truncated(self):
        shown = text([host("a" * 60)], "up")
        self.assertEqual(shown, "GAMING PC: " + "A" * 24 + " READY")

    def test_label_drops_control_characters(self):
        shown = text([host("PC\x1b[2J\nONE")], "up")
        self.assertNotIn("\x1b", shown)
        self.assertNotIn("\n", shown)

    def test_every_text_fits_76_columns(self):
        names = ("a" * 80, "漢" * 40, "W" * 24, "x")
        for name in names:
            for result in ("up", "awake", "down"):
                for mac in (MAC, b""):
                    shown = text([host(name, mac=mac)], result)
                    self.assertTrue(shown)
                    cells = sum(2 if ord(c) > 0x2E80 else 1 for c in shown)
                    self.assertLessEqual(cells, 76, shown)
        self.assertLessEqual(len(text([host()] * 9999, None)), 76)


class ShortTextTest(unittest.TestCase):
    """The TV home screen's corner: the PC and one word."""

    def short(self, hosts, result, link=True, settings=stream.StreamSettings()):
        chosen = pcstatus.pick_host(hosts, settings)
        block = stream.wake_block(chosen) if isinstance(chosen, stream.Host) else ""
        return pcstatus.short_text(hosts, chosen, result, link, block)

    def test_the_pc_and_its_state(self):
        self.assertEqual(self.short([host()], "up"), "DESKTOP-7Q2: ONLINE")
        self.assertEqual(self.short([host()], "down"), "DESKTOP-7Q2: ASLEEP")
        self.assertEqual(self.short([host(mac=b"")], "down"), "DESKTOP-7Q2: NOT FOUND")
        self.assertEqual(self.short([host()], "awake"), "DESKTOP-7Q2: NO SUNSHINE")

    def test_nothing_without_a_pc_a_result_or_the_network(self):
        self.assertEqual(self.short([], None), "")
        self.assertEqual(self.short([host()], None), "")
        self.assertEqual(self.short([host()], "unknown"), "")
        self.assertEqual(self.short([host()], "up", link=False), "")

    def test_several_pcs_without_a_default_are_counted(self):
        self.assertEqual(self.short([host("ONE"), host("TWO", local="192.168.1.21")], None), "GAMING PCS: 2 PAIRED")


class MonitorTest(unittest.TestCase):
    def monitor(self, hosts=None, result="up", link=True, active=False, settings=None, **extra):
        self.probe = mock.Mock(return_value=result)
        monitor = pcstatus.Monitor(
            load=lambda: [host()] if hosts is None else hosts,
            settings=settings or (lambda: stream.StreamSettings()),
            probe=self.probe,
            link=lambda: link,
            app_active=lambda: active,
            **extra,
        )
        return monitor

    def test_monitor_line_empty_before_first_probe(self):
        monitor = self.monitor()
        self.assertEqual(monitor.line(), "")
        self.probe.assert_not_called()  # line() is what draw() calls: it only reads a string

    def test_refresh_then_line(self):
        monitor = self.monitor()
        monitor.refresh()
        self.assertEqual(monitor.line(), "GAMING PC: DESKTOP-7Q2 READY")
        self.probe.assert_called_once()

    def test_refresh_keeps_the_short_form_too(self):
        monitor = self.monitor(result="down")
        self.assertEqual(monitor.short(), "")
        monitor.refresh()
        self.assertEqual(monitor.short(), "DESKTOP-7Q2: ASLEEP")
        self.probe.side_effect = OSError("boom")
        monitor.refresh()
        self.assertEqual((monitor.line(), monitor.short()), ("", ""))

    def test_no_probe_while_it_rests(self):
        monitor = self.monitor(interval=0.01)
        monitor.awake.clear()  # the TV interface rests behind a game
        monitor.start()
        self.addCleanup(monitor.stop)
        self.addCleanup(monitor.awake.set)
        threading.Event().wait(0.1)
        self.probe.assert_not_called()
        monitor.awake.set()
        for _ in range(100):
            if self.probe.called:
                break
            threading.Event().wait(0.02)
        self.probe.assert_called()

    def test_monitor_swallows_probe_and_load_errors(self):
        monitor = self.monitor()
        monitor.refresh()
        self.assertTrue(monitor.line())
        self.probe.side_effect = OSError("boom")
        monitor.refresh()
        self.assertEqual(monitor.line(), "")
        monitor.refresh()  # and it keeps working afterwards
        broken = pcstatus.Monitor(
            load=mock.Mock(side_effect=ValueError("bad conf")), settings=mock.Mock(), probe=mock.Mock(),
            link=lambda: True, app_active=lambda: False,
        )
        broken.refresh()
        self.assertEqual(broken.line(), "")
        for failing in ("settings", "link", "app_active"):
            kwargs = dict(
                load=lambda: [host()], settings=lambda: stream.StreamSettings(), probe=lambda _h: "up",
                link=lambda: True, app_active=lambda: False,
            )
            kwargs[failing] = mock.Mock(side_effect=RuntimeError("x"))
            monitor = pcstatus.Monitor(**kwargs)
            monitor.refresh()
            self.assertEqual(monitor.line(), "")

    def test_monitor_does_not_probe_while_app_active(self):
        active = [False]
        probe = mock.Mock(return_value="up")
        monitor = pcstatus.Monitor(
            load=lambda: [host()], settings=lambda: stream.StreamSettings(), probe=probe,
            link=lambda: True, app_active=lambda: active[0],
        )
        monitor.refresh()
        self.assertEqual(monitor.line(), "GAMING PC: DESKTOP-7Q2 READY")
        active[0] = True
        probe.reset_mock()
        monitor.refresh()
        probe.assert_not_called()
        self.assertEqual(monitor.line(), "GAMING PC: DESKTOP-7Q2 READY")  # keeps the last text

    def test_result_dropped_when_an_app_starts_during_the_probe(self):
        active = [False]

        def slow_probe(_host):
            active[0] = True  # a stream was started while we were probing
            return "down"

        monitor = pcstatus.Monitor(
            load=lambda: [host()], settings=lambda: stream.StreamSettings(), probe=slow_probe,
            link=lambda: True, app_active=lambda: active[0],
        )
        monitor.refresh()
        self.assertEqual(monitor.line(), "")

    def test_no_link_means_no_probe_and_no_line(self):
        monitor = self.monitor(link=False)
        monitor.refresh()
        self.probe.assert_not_called()
        self.assertEqual(monitor.line(), "")

    def test_hosts_are_reread_every_cycle(self):
        hosts = [[host("FIRST")], [host("SECOND", local="192.168.1.30")]]
        monitor = pcstatus.Monitor(
            load=lambda: hosts.pop(0), settings=lambda: stream.StreamSettings(), probe=lambda _h: "up",
            link=lambda: True, app_active=lambda: False,
        )
        monitor.refresh()
        self.assertIn("FIRST", monitor.line())
        monitor.refresh()
        self.assertIn("SECOND", monitor.line())

    def test_no_probe_for_none_paired_or_several(self):
        for hosts, line in (([], ""), ([host("ONE"), host("TWO", local="192.168.1.21")], "GAMING PCS: 2 PAIRED")):
            monitor = self.monitor(hosts=hosts)
            monitor.refresh()
            self.probe.assert_not_called()
            self.assertEqual(monitor.line(), line)

    def test_a_real_moonlight_conf_is_recognised(self):
        conf = (
            "[General]\nwidth=1920\n\n[hosts]\n1\\hostname=Desktop-7Q2\n1\\localaddress=192.168.1.20\n"
            "1\\mac=aa:bb:cc:dd:ee:ff\n1\\uuid=U1\nsize=1\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "Moonlight.conf"
            path.write_text(conf)
            monitor = pcstatus.Monitor(
                load=lambda: stream.load_hosts(path), settings=lambda: stream.StreamSettings(),
                probe=lambda _h: "up", link=lambda: True, app_active=lambda: False,
            )
            monitor.refresh()
        self.assertEqual(monitor.line(), "GAMING PC: DESKTOP-7Q2 READY")

    def test_probe_runs_off_the_calling_thread_and_stops(self):
        seen = {}
        done = threading.Event()

        def probe(_host):
            seen["thread"] = threading.current_thread()
            done.set()
            return "up"

        monitor = pcstatus.Monitor(
            load=lambda: [host()], settings=lambda: stream.StreamSettings(), probe=probe,
            link=lambda: True, app_active=lambda: False, interval=0.05,
        )
        monitor.start()
        self.addCleanup(monitor.stop)
        self.assertTrue(done.wait(5))
        self.assertIsNot(seen["thread"], threading.current_thread())
        self.assertTrue(seen["thread"].daemon)

    def test_default_app_active_sees_the_run_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = pathlib.Path(tmp)
            self.assertFalse(pcstatus.app_active(run))
            (run / "app-active").write_text("moonlight\n")
            self.assertTrue(pcstatus.app_active(run))
            (run / "app-active").unlink()
            (run / "moonlight-ready").touch()
            self.assertTrue(pcstatus.app_active(run))


class LauncherHookTest(unittest.TestCase):
    """The main screen shows the line right under the title and never probes itself."""

    @classmethod
    def setUpClass(cls):
        path = pathlib.Path(__file__).with_name("couchliteos-launcher.py")
        spec = importlib.util.spec_from_file_location("launcher_pcstatus", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_draw_puts_the_line_under_the_title(self):
        drawn = []

        class Screen:
            def getmaxyx(self):
                return 30, 100

            def erase(self):
                pass

            def border(self, *_a):
                pass

            def addstr(self, row, column, content, *_a):
                drawn.append((row, content))

            def addnstr(self, *_a):
                pass

            def refresh(self):
                pass

        with mock.patch.object(self.module.power, "can_suspend", return_value=True), mock.patch.object(
            self.module, "network_summary", return_value="OFFLINE"
        ):
            launcher = self.module.Launcher(Screen())
        launcher.pcstatus = mock.Mock()
        launcher.pcstatus.line.return_value = "GAMING PC: X READY"
        launcher.draw()
        title_row = max(2, 30 // 8)
        self.assertIn((title_row, "COUCHLITEOS"), drawn)
        self.assertIn((title_row + 1, "GAMING PC: X READY"), drawn)
        launcher.pcstatus.probe.assert_not_called()

    def test_launcher_starts_the_monitor_with_the_other_monitors(self):
        source = pathlib.Path(__file__).with_name("couchliteos-launcher.py").read_text()
        self.assertIn("self.pcstatus = pcstatus.Monitor()", source)
        self.assertIn("self.pcstatus.start()", source)


if __name__ == "__main__":
    unittest.main()
