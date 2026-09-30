"""Settings > NETWORK: join Wi-Fi with a controller (wizard's network step), or go advanced."""

import unittest
from unittest import mock

import moonlightos_netmenu as netmenu
import moonlightos_setup as setup
from test_launcher import Screen
from test_launcher_fixes import LauncherFixesTest

ONLINE = "192.168.1.40  ONLINE"
OFFLINE = "OFFLINE - SETTINGS > NETWORK"
HINT = "WI-FI PASSWORD CHANGED? CHOOSE YOUR NETWORK AGAIN, TYPE THE NEW ONE."
PASSWORD = "correct horse battery"


class FakeUI:
    """Scripted answers: an int picks that row, a string picks the row containing it, None is Esc."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.screens = []

    def menu(self, title, lines, choices, *, big=None, selected=0):
        self.screens.append({"title": title, "lines": list(lines), "choices": list(choices)})
        if not self.answers:
            raise AssertionError(f"no scripted answer for screen {title!r} {choices!r}")
        answer = self.answers.pop(0)
        if isinstance(answer, str):
            return next(index for index, choice in enumerate(choices) if answer in choice)
        return answer

    def status(self, title, lines, *, big=None):
        self.screens.append({"title": title, "lines": list(lines), "choices": []})

    def text(self):
        return "\n".join(line for screen in self.screens for line in [screen["title"]] + screen["lines"] + screen["choices"])


class FakeSystem:
    def __init__(self, devices=None, networks=None, connect=(True, ""), broken=False):
        self.devices = devices if devices is not None else [
            setup.NetworkDevice("enp2s0", "ethernet", "unavailable"),
            setup.NetworkDevice("wlan0", "wifi", "disconnected"),
        ]
        self.networks = networks if networks is not None else [setup.WifiNetwork("HOME", 80, "WPA2")]
        self.connect = connect
        self.broken = broken
        self.connected = []

    def network_devices(self):
        return self.devices

    def wifi_interface(self):
        return setup.wifi_device(self.devices)

    def wifi_networks(self):
        if self.broken:
            raise OSError("nmcli exploded")
        return self.networks

    def connect_wifi(self, network, password):
        self.connected.append((network.ssid, password))
        return self.connect

    def connectivity(self):
        return setup.Connectivity("192.168.1.1", True, True, True, False)


class Recorder:
    """join and advanced stand-ins that count their calls."""

    def __init__(self, outcome=setup.DONE):
        self.joined = 0
        self.advanced_calls = 0
        self.outcome = outcome

    def join(self):
        self.joined += 1
        return self.outcome

    def advanced(self):
        self.advanced_calls += 1


class RowsTest(unittest.TestCase):
    def test_rows_are_join_advanced_back(self):
        self.assertEqual(
            netmenu.choices(True), ["JOIN A WI-FI NETWORK", "ADVANCED NETWORK SETTINGS (NEEDS A KEYBOARD)", "BACK"]
        )

    def test_no_adapter_row_says_so(self):
        self.assertEqual(
            netmenu.choices(False),
            ["JOIN A WI-FI NETWORK  (NO WI-FI ADAPTER FOUND)", "ADVANCED NETWORK SETTINGS (NEEDS A KEYBOARD)", "BACK"],
        )

    def test_header_shows_the_network_status(self):
        self.assertEqual(netmenu.header(ONLINE, True)[0], "NOW: 192.168.1.40  ONLINE")

    def test_offline_adds_password_hint(self):
        lines = netmenu.header(OFFLINE, True)
        self.assertEqual(lines[0], "NOW: OFFLINE")  # already in SETTINGS > NETWORK, so no pointer back to it
        self.assertIn(HINT, lines)

    def test_online_has_no_password_hint(self):
        self.assertNotIn(HINT, netmenu.header(ONLINE, True))
        self.assertEqual(len(netmenu.header(ONLINE, True)), 1)


class ResultTest(unittest.TestCase):
    def test_join_done_status_connected(self):
        self.assertEqual(netmenu.result_status(setup.DONE), "NETWORK: CONNECTED")

    def test_join_failed_status_suggests_cable(self):
        self.assertEqual(
            netmenu.result_status(setup.FAILED), "NETWORK: NOT CONNECTED. TRY AGAIN, OR USE A NETWORK CABLE."
        )

    def test_join_skipped_status_empty(self):
        self.assertEqual(netmenu.result_status(setup.SKIPPED), "")


class RunTest(unittest.TestCase):
    def run_menu(self, *answers, summary=ONLINE, wifi=True, outcome=setup.DONE):
        ui, recorder = FakeUI(*answers), Recorder(outcome)
        status = netmenu.run(ui, summary, wifi, recorder.join, recorder.advanced)
        return ui, recorder, status

    def test_join_runs_the_join_step_and_reports_its_outcome(self):
        for outcome, expected in (
            (setup.DONE, "NETWORK: CONNECTED"),
            (setup.FAILED, "NETWORK: NOT CONNECTED. TRY AGAIN, OR USE A NETWORK CABLE."),
            (setup.SKIPPED, ""),
        ):
            with self.subTest(outcome=outcome):
                ui, recorder, status = self.run_menu("JOIN", outcome=outcome)
                self.assertEqual((recorder.joined, recorder.advanced_calls, status), (1, 0, expected))

    def test_advanced_launches_network_setup(self):
        ui, recorder, status = self.run_menu("ADVANCED")
        self.assertEqual((recorder.joined, recorder.advanced_calls), (0, 1))
        self.assertEqual(status, "")  # the launch reports its own result in Settings

    def test_b_returns_without_action(self):
        for answer in (None, "BACK"):
            with self.subTest(answer=answer):
                ui, recorder, status = self.run_menu(answer)
                self.assertEqual((recorder.joined, recorder.advanced_calls, status), (0, 0, ""))

    def test_no_adapter_row_says_so_and_does_not_join(self):
        ui, recorder, status = self.run_menu(0, "OK", "BACK", wifi=False)
        self.assertEqual(recorder.joined, 0)
        self.assertEqual(status, "")
        notice = ui.screens[1]
        self.assertEqual(notice["lines"], ["NO WI-FI ADAPTER ON THIS PC. PLUG IN A NETWORK CABLE."])
        self.assertEqual(notice["choices"], ["OK"])
        self.assertEqual(len(ui.screens), 3)  # the menu comes back afterwards

    def test_advanced_still_works_without_an_adapter(self):
        ui, recorder, status = self.run_menu("ADVANCED", wifi=False)
        self.assertEqual((recorder.joined, recorder.advanced_calls), (0, 1))

    def test_menu_shows_the_header_and_rows(self):
        ui, _recorder, _status = self.run_menu(None, summary=OFFLINE)
        screen = ui.screens[0]
        self.assertEqual(screen["title"], "NETWORK")
        self.assertEqual(screen["lines"], netmenu.header(OFFLINE, True))
        self.assertEqual(screen["choices"], netmenu.choices(True))


class OpenTest(unittest.TestCase):
    """open() is the glue: the real wizard step driven through an injected UI and System."""

    def open_menu(self, *answers, system=None, typed=PASSWORD, advanced=None, summary=OFFLINE):
        ui = FakeUI(*answers)
        system = system or FakeSystem()
        asked = []

        def text_input(title, prompt, limit, *, masked=False):
            asked.append((title, prompt, limit, masked))
            return typed

        status = netmenu.open(
            None, text_input, advanced or mock.Mock(), lambda: summary, system=system, ui=ui
        )
        return ui, system, asked, status

    def test_join_runs_wizard_network_step(self):
        ui, system, asked, status = self.open_menu("JOIN", "HOME", "CONTINUE")
        self.assertEqual(status, "NETWORK: CONNECTED")
        self.assertEqual(system.connected, [("HOME", PASSWORD)])
        self.assertEqual(asked, [("WI-FI PASSWORD", "PASSWORD FOR HOME", 64, True)])  # typed masked
        self.assertIn("HOME  80%", ui.text())  # the wizard's own SSID list with signal
        self.assertIn("SCAN AGAIN", ui.text())

    def test_the_password_never_reaches_a_screen_or_the_status(self):
        ui, _system, _asked, status = self.open_menu("JOIN", "HOME", "CONTINUE")
        self.assertIn("CONNECTED TO HOME", ui.text())  # the whole flow ran
        self.assertNotIn(PASSWORD, ui.text())
        self.assertNotIn(PASSWORD, status)

    def test_backing_out_of_the_network_list_gives_no_status(self):
        _ui, system, _asked, status = self.open_menu("JOIN", None)
        self.assertEqual((status, system.connected), ("", []))

    def test_a_join_that_does_not_come_online_suggests_a_cable(self):
        system = FakeSystem()
        system.connectivity = lambda: setup.Connectivity("", False, False, False, False)
        _ui, _system, _asked, status = self.open_menu("JOIN", "HOME", "CONTINUE ANYWAY", system=system)
        self.assertEqual(status, "NETWORK: NOT CONNECTED. TRY AGAIN, OR USE A NETWORK CABLE.")

    def test_advanced_calls_the_launch_callback_only(self):
        advanced = mock.Mock()
        _ui, system, asked, status = self.open_menu("ADVANCED", advanced=advanced)
        advanced.assert_called_once_with()
        self.assertEqual((status, system.connected, asked), ("", [], []))

    def test_no_adapter_is_detected_at_run_time_from_the_system(self):
        system = FakeSystem(devices=[setup.NetworkDevice("enp2s0", "ethernet", "unavailable")])
        ui, _system, _asked, status = self.open_menu(0, "OK", "BACK", system=system)
        self.assertEqual(status, "")
        self.assertIn("JOIN A WI-FI NETWORK  (NO WI-FI ADAPTER FOUND)", ui.screens[0]["choices"])
        self.assertIn("NO WI-FI ADAPTER ON THIS PC. PLUG IN A NETWORK CABLE.", ui.text())

    def test_join_exception_returns_status_not_raise(self):
        _ui, _system, _asked, status = self.open_menu("JOIN", system=FakeSystem(broken=True))
        self.assertEqual(status, "COULD NOT OPEN WI-FI SETUP. TRY AGAIN.")

    def test_a_broken_summary_or_advanced_callback_returns_status_not_raise(self):
        ui = FakeUI("ADVANCED")
        status = netmenu.open(None, None, mock.Mock(side_effect=RuntimeError), lambda: ONLINE, system=FakeSystem(), ui=ui)
        self.assertEqual(status, "COULD NOT OPEN WI-FI SETUP. TRY AGAIN.")
        status = netmenu.open(None, None, mock.Mock(), mock.Mock(side_effect=OSError), system=FakeSystem(), ui=FakeUI())
        self.assertEqual(status, "COULD NOT OPEN WI-FI SETUP. TRY AGAIN.")

    def test_without_injection_it_uses_the_curses_ui_and_the_real_system(self):
        screen, ui, system = object(), FakeUI(None), FakeSystem()
        with mock.patch.object(netmenu.setup, "CursesUI", return_value=ui) as curses_ui, mock.patch.object(
            netmenu.setup, "System", return_value=system
        ):
            self.assertEqual(netmenu.open(screen, None, mock.Mock(), lambda: ONLINE), "")
        curses_ui.assert_called_once_with(screen)
        self.assertEqual(len(ui.screens), 1)

    def test_every_line_fits_76_columns(self):
        lines = [netmenu.TITLE, HINT, netmenu.NO_ADAPTER_NOTICE]
        for summary in (ONLINE, OFFLINE, "255.255.255.255  ONLINE"):
            for wifi in (True, False):
                lines += netmenu.header(summary, wifi) + netmenu.choices(wifi)
        lines += [netmenu.result_status(outcome) for outcome in setup.OUTCOMES]
        lines.append(netmenu.FAILED_TO_OPEN)
        ui = FakeUI(0, "OK", None)
        netmenu.run(ui, OFFLINE, False, lambda: setup.DONE, lambda: None)
        lines += ui.text().splitlines()
        self.assertGreater(len(lines), 20)
        for line in lines:
            with self.subTest(line=line):
                self.assertLessEqual(len(line), 76)


class SettingsWiringTest(LauncherFixesTest):
    def settings(self):
        launcher = self.launcher()
        settings = self.module.Settings(Screen(), launcher)
        settings.selected = self.module.SETTINGS_MENU.index("NETWORK")
        return launcher, settings

    def test_the_network_row_opens_the_controller_menu(self):
        launcher, settings = self.settings()
        with mock.patch.object(self.module.netmenu, "open", return_value="NETWORK: CONNECTED") as open_menu:
            self.assertTrue(settings.activate())
        screen, text_input, advanced, summary = open_menu.call_args.args
        self.assertIs(screen, settings.screen)
        self.assertEqual(text_input, launcher.wizard_text)  # the on-screen keyboard path the wizard uses
        self.assertIs(summary, self.module.network_summary)
        self.assertEqual(settings.status, "NETWORK: CONNECTED")

    def test_backing_out_leaves_the_status_alone(self):
        _launcher, settings = self.settings()
        settings.status = "CURRENT: BEFORE"
        with mock.patch.object(self.module.netmenu, "open", return_value=""):
            settings.activate()
        self.assertEqual(settings.status, "CURRENT: BEFORE")

    def test_advanced_is_the_old_network_setup_launch(self):
        launcher, settings = self.settings()
        with mock.patch.object(self.module.netmenu, "open", return_value="") as open_menu:
            settings.activate()
        advanced = open_menu.call_args.args[2]
        with mock.patch.object(launcher, "launch_by_id", return_value=True) as launch:
            advanced()
        launch.assert_called_once_with("network-setup")

    def test_advanced_failure_is_explained_inside_settings(self):
        launcher, settings = self.settings()
        with mock.patch.object(self.module.netmenu, "open", return_value="") as open_menu:
            settings.activate()
        with mock.patch.object(launcher, "app_by_id", return_value=None):
            open_menu.call_args.args[2]()
        self.assertEqual(settings.status, "NETWORK-SETUP IS NOT AVAILABLE: CHECK SETTINGS > APPLICATIONS")


if __name__ == "__main__":
    unittest.main()
