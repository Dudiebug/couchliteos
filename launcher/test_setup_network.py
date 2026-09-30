"""Tests for the network step of the first-boot wizard (moonlightos_setup.py).

This file is separate from test_setup.py on purpose: it carries its own small
fakes so the network tests do not depend on the other wizard test classes.
"""

import pathlib
import re
import types
import unittest
from unittest import mock

import moonlightos_setup as setup

HOME_NET = setup.WifiNetwork("HomeNet", 80, "WPA2")
ETHERNET_DOWN = setup.NetworkDevice("enp2s0", "ethernet", "unavailable")
WIFI_OFF = setup.NetworkDevice("wlan0", "wifi", "disconnected")
def wifi_up():
    return setup.NetworkDevice("wlan0", "wifi", "connected", "HomeNet")


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
        return "\n".join(line for screen in self.screens for line in screen["lines"] + screen["choices"])

    def assert_fits_an_80_column_screen(self, test):
        for screen in self.screens:
            for line in screen["lines"]:
                test.assertLessEqual(len(line), 72, line)
            for choice in screen["choices"]:
                test.assertLessEqual(len(choice), 68, choice)


class FakeSystem:
    def __init__(self, **values):
        self.devices = [ETHERNET_DOWN, WIFI_OFF]
        self.networks = []
        self.connect_results = [(True, "")]
        self.connected = []
        self.results = [setup.Connectivity("192.168.1.1", True, True, True, False)]
        self.scans = 0
        self.__dict__.update(values)

    def network_devices(self):
        return self.devices

    def wifi_networks(self):
        self.scans += 1
        return self.networks

    def connect_wifi(self, network, password):
        self.connected.append((network, password))
        return self.connect_results.pop(0) if len(self.connect_results) > 1 else self.connect_results[0]

    def connectivity(self):
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


class NetworkStepTestCase(unittest.TestCase):
    def setUp(self):
        self.texts = []
        self.prompts = []

    def wizard(self, ui, system):
        def text(title, prompt, limit, *, masked=False):
            self.prompts.append((title, prompt, limit, masked))
            return self.texts.pop(0) if self.texts else None

        return setup.SetupWizard(ui, {"text": text}, system)

    def run_network(self, ui, system):
        return self.wizard(ui, system).step_network()


# --- finding 1: setup run again while Wi-Fi already works ---------------------

class AlreadyOnWifiTest(NetworkStepTestCase):
    def test_a_connected_wifi_device_counts_as_connected(self):
        self.assertTrue(setup.wifi_connected([ETHERNET_DOWN, wifi_up()]))
        self.assertFalse(setup.wifi_connected([ETHERNET_DOWN, WIFI_OFF]))
        self.assertFalse(setup.wifi_connected([setup.NetworkDevice("p2p-dev-wlan0", "wifi-p2p", "connected")]))
        self.assertFalse(setup.wifi_connected([]))

    def test_the_connection_name_comes_from_the_fourth_nmcli_column(self):
        devices = setup.parse_devices("enp2s0:ethernet:unavailable:--\nwlan0:wifi:connected:Home\\:Net 5G\n")
        self.assertEqual(setup.wifi_connection_name(devices), "Home:Net 5G")
        self.assertEqual(setup.wifi_connection_name(setup.parse_devices("wlan0:wifi:connected\n")), "")

    def test_a_cable_still_wins_over_wifi(self):
        cable = setup.NetworkDevice("enp2s0", "ethernet", "connected")
        self.assertEqual(setup.network_mode([cable, wifi_up()]), "wired")

    def test_keeping_the_working_wifi_never_scans_or_asks_for_a_password(self):
        system = FakeSystem(devices=[ETHERNET_DOWN, wifi_up()], networks=[HOME_NET])
        ui = FakeUI("KEEP THIS NETWORK", "CONTINUE")
        self.assertEqual(self.run_network(ui, system), "done")
        self.assertEqual(system.scans, 0)
        self.assertEqual(system.connected, [])
        self.assertEqual(self.prompts, [])
        self.assertIn("HomeNet", ui.text())
        ui.assert_fits_an_80_column_screen(self)

    def test_the_keep_screen_names_the_buttons(self):
        ui = FakeUI("KEEP THIS NETWORK", "CONTINUE")
        self.run_network(ui, FakeSystem(devices=[ETHERNET_DOWN, wifi_up()]))
        first = ui.screens[0]
        self.assertEqual(first["title"], "NETWORK")
        self.assertEqual(first["choices"][0], "KEEP THIS NETWORK")
        self.assertIn("A", " ".join(first["lines"]).split())
        self.assertIn("B", " ".join(first["lines"]).split())

    def test_choosing_another_network_shows_the_list_with_the_current_one_marked(self):
        current = setup.WifiNetwork("HomeNet", 80, "WPA2", in_use=True)
        other = setup.WifiNetwork("Neighbor", 40, "WPA2")
        system = FakeSystem(devices=[ETHERNET_DOWN, wifi_up()], networks=[current, other])
        ui = FakeUI("CHOOSE ANOTHER NETWORK", "SKIP")
        self.assertEqual(self.run_network(ui, system), "skipped")
        listing = ui.screens[-1]["choices"]
        self.assertIn("(CONNECTED)", next(row for row in listing if "HomeNet" in row))
        self.assertNotIn("(CONNECTED)", next(row for row in listing if "Neighbor" in row))
        ui.assert_fits_an_80_column_screen(self)

    def test_scanning_again_does_not_bring_the_keep_screen_back(self):
        system = FakeSystem(devices=[ETHERNET_DOWN, wifi_up()], networks=[HOME_NET])
        ui = FakeUI("CHOOSE ANOTHER NETWORK", "SCAN AGAIN", "SKIP")
        self.assertEqual(self.run_network(ui, system), "skipped")
        self.assertEqual(system.scans, 2)
        self.assertEqual(sum(1 for screen in ui.screens if "KEEP THIS NETWORK" in screen["choices"]), 1)

    def test_escape_on_the_keep_screen_skips_the_step(self):
        self.assertEqual(self.run_network(FakeUI(None), FakeSystem(devices=[ETHERNET_DOWN, wifi_up()])), "skipped")

    def test_a_working_wifi_with_no_internet_is_reported_like_any_other_network(self):
        system = FakeSystem(
            devices=[ETHERNET_DOWN, wifi_up()], results=[setup.Connectivity("", False, False, False, False)]
        )
        ui = FakeUI("KEEP THIS NETWORK", "CONTINUE ANYWAY")
        self.assertEqual(self.run_network(ui, system), "failed")


# --- the NetworkManager D-Bus boundary, faked ----------------------------------

class FakeDBusException(Exception):
    pass


class FakeNetworkManager:
    """One NetworkManager with saved profiles and a scripted activation.

    `states` is what the ActiveConnection reports on each poll; the string
    "gone" raises DBusException, like an ActiveConnection NetworkManager has
    already removed after a failed activation."""

    def __init__(self, saved=(), states=(2,)):
        self.profiles = {f"/profile/{index}": {"connection": {"id": name, "type": kind}}
                         for index, (name, kind) in enumerate(saved)}
        self.states = list(states)
        self.events = []
        self.added = None

    # the subset of the `dbus` module the wizard uses
    def module(self):
        return types.SimpleNamespace(
            SystemBus=lambda: self,
            Interface=lambda proxy, _name: proxy,
            Dictionary=lambda value, signature=None: value,
            ByteArray=bytes,
            DBusException=FakeDBusException,
        )

    def get_object(self, _service, path):
        return FakeProxy(self, path)


class FakeProxy:
    def __init__(self, nm, path):
        self.nm, self.path = nm, path

    def GetDeviceByIpIface(self, name):
        return "/device/" + name

    def ListConnections(self):
        return list(self.nm.profiles)

    def GetSettings(self):
        if self.path not in self.nm.profiles:
            raise FakeDBusException("no such profile")
        return self.nm.profiles[self.path]

    def Delete(self):
        if self.path not in self.nm.profiles:
            raise FakeDBusException("no such profile")
        del self.nm.profiles[self.path]
        self.nm.events.append(("delete", self.path))

    def AddAndActivateConnection(self, settings, _device, _specific):
        self.nm.added = settings
        self.nm.profiles["/profile/new"] = {"connection": dict(settings["connection"])}
        self.nm.events.append(("add", "/profile/new"))
        return "/profile/new", "/active/1"

    def Get(self, _interface, _name):
        state = self.nm.states.pop(0) if len(self.nm.states) > 1 else self.nm.states[0]
        if state == "gone":
            raise FakeDBusException("no such object")
        return state


def activate(nm, network=HOME_NET, password="correct horse battery"):
    settings = setup.wifi_settings(network, password, "11111111-2222-3333-4444-555555555555")
    with mock.patch.dict("sys.modules", {"dbus": nm.module()}), mock.patch.object(setup.time, "sleep"):
        return setup.System._activate(settings, "wlan0")


class ActivateKeepsTheWorkingProfileTest(unittest.TestCase):
    OLD = [("HomeNet", "802-11-wireless"), ("HomeNet", "802-3-ethernet"), ("Cafe", "802-11-wireless")]

    def test_the_old_profile_survives_until_the_new_one_is_active(self):
        nm = FakeNetworkManager(saved=self.OLD, states=(1, 1, 2))
        self.assertEqual(activate(nm), (True, ""))
        added = nm.events.index(("add", "/profile/new"))
        deleted = [event for event in nm.events if event[0] == "delete"]
        self.assertEqual(deleted, [("delete", "/profile/0")])
        self.assertGreater(nm.events.index(deleted[0]), added)
        self.assertEqual(sorted(nm.profiles), ["/profile/1", "/profile/2", "/profile/new"])

    def test_a_failed_join_keeps_the_old_profile_and_drops_only_the_new_one(self):
        nm = FakeNetworkManager(saved=self.OLD, states=(1, 4))
        ok, message = activate(nm)
        self.assertFalse(ok)
        self.assertIn("CHECK THE PASSWORD", message)
        self.assertEqual(nm.events, [("add", "/profile/new"), ("delete", "/profile/new")])
        self.assertIn("/profile/0", nm.profiles)
        self.assertNotIn("/profile/new", nm.profiles)

    def test_a_join_that_never_finishes_drops_only_the_new_profile(self):
        nm = FakeNetworkManager(saved=self.OLD, states=(1,))
        ticks = iter(range(0, 1000, 10))
        with mock.patch.object(setup.time, "monotonic", side_effect=lambda: next(ticks)):
            ok, _message = activate(nm)
        self.assertFalse(ok)
        self.assertEqual(nm.events, [("add", "/profile/new"), ("delete", "/profile/new")])
        self.assertIn("/profile/0", nm.profiles)

    def test_a_first_join_with_nothing_saved_just_works(self):
        nm = FakeNetworkManager(states=(2,))
        self.assertEqual(activate(nm), (True, ""))
        self.assertEqual(nm.events, [("add", "/profile/new")])

    def test_the_password_only_goes_into_the_dbus_settings(self):
        nm = FakeNetworkManager(states=(2,))
        activate(nm, password="correct horse battery")
        self.assertEqual(nm.added["802-11-wireless-security"]["psk"], "correct horse battery")


# --- finding 10: the polkit rule ----------------------------------------------

RULE = pathlib.Path(__file__).resolve().parent.parent / "overlay/etc/polkit-1/rules.d/50-moonlightos-network.rules"


class PolkitRuleTest(unittest.TestCase):
    """The wizard joins Wi-Fi as the unprivileged launcher user, which NetworkManager
    only allows through polkit. Its Wi-Fi join uses exactly these actions:
    wifi.scan (nmcli rescan), settings.modify.system (add and delete profiles) and
    network-control (activate the new profile). Nothing else may be granted."""

    WANTED = {
        "org.freedesktop.NetworkManager.network-control",
        "org.freedesktop.NetworkManager.settings.modify.system",
        "org.freedesktop.NetworkManager.wifi.scan",
    }

    def code(self):
        lines = RULE.read_text(encoding="utf-8").splitlines()
        return "\n".join(line for line in lines if not line.lstrip().startswith("//"))

    def test_only_the_actions_the_wifi_join_uses_are_named(self):
        named = set(re.findall(r'"(org\.freedesktop\.NetworkManager\.[A-Za-z0-9.-]+)"', self.code()))
        self.assertEqual(named, self.WANTED)

    def test_no_prefix_match_grants_every_networkmanager_action(self):
        code = self.code()
        self.assertNotRegex(code, r'indexOf\(\s*"org\.')
        self.assertNotIn("global-dns", code)
        self.assertNotIn("wifi.share", code)
        self.assertNotIn("enable-disable", code)

    def test_only_the_launcher_user_inside_the_launcher_service_is_granted(self):
        code = self.code()
        self.assertIn('subject.user == "moonlightos"', code)
        # The launcher runs as a plain systemd service: it has no logind session, so
        # polkit reports subject.local and subject.active as false for it and a rule
        # that required them would refuse the wizard. The service's cgroup is what
        # tells it apart from the browser and other apps running as the same user.
        self.assertIn("/moonlightos-launcher.service", code)
        self.assertIn("/proc/", code)
        self.assertEqual(code.count("polkit.Result."), 1)
        self.assertIn("polkit.Result.YES", code)


# --- finding 5: a router that does not answer ping ------------------------------

class SilentRouterTest(NetworkStepTestCase):
    def check(self, *, gateway="192.168.1.1", ping=False, dns=True, http="ok"):
        calls = []
        result = setup.check_connectivity(
            gateway=lambda: gateway,
            ping=lambda host: calls.append("ping") or ping,
            resolves=lambda: calls.append("dns") or dns,
            fetch=lambda: calls.append("fetch") or http,
        )
        return result, calls

    def test_the_lookup_and_the_download_run_even_when_the_router_does_not_answer(self):
        _result, calls = self.check()
        self.assertEqual(calls, ["ping", "dns", "fetch"])

    def test_the_lookup_and_the_download_run_even_without_an_ipv4_route(self):
        _result, calls = self.check(gateway="")
        self.assertEqual(calls, ["dns", "fetch"])

    def test_a_silent_router_with_working_internet_is_online(self):
        result, _calls = self.check()
        self.assertFalse(result.lan)
        self.assertTrue(result.internet)
        self.assertTrue(result.usable)
        self.assertEqual(result.headline, "ONLINE")

    def test_working_internet_without_an_ipv4_route_is_online(self):
        result, _calls = self.check(gateway="")
        self.assertTrue(result.usable)
        self.assertEqual(result.headline, "ONLINE")

    def test_a_login_page_behind_a_silent_router_is_still_reported_as_one(self):
        result, _calls = self.check(http="portal")
        self.assertTrue(result.usable)
        self.assertEqual(result.headline, "LOGIN PAGE BLOCKS THE INTERNET")

    def test_a_silent_router_and_nothing_else_working_is_a_failure(self):
        result, _calls = self.check(dns=False, http="")
        self.assertFalse(result.usable)
        self.assertEqual(result.headline, "ROUTER DOES NOT ANSWER")

    def test_no_route_and_nothing_working_is_no_local_network(self):
        result, _calls = self.check(gateway="", dns=False, http="")
        self.assertFalse(result.usable)
        self.assertEqual(result.headline, "NO LOCAL NETWORK")

    def test_a_missed_ping_is_a_note_not_a_failed_check(self):
        lines = self.check()[0].lines()
        self.assertEqual(len(lines), 3)
        self.assertNotIn("FAILED", lines[0])
        self.assertIn("NO PING ANSWER", lines[0])
        self.assertIn("192.168.1.1", lines[0])
        self.assertTrue(lines[1].endswith("OK") and lines[2].endswith("OK"))
        self.assertLessEqual(max(len(line) for line in lines), 72)

    def test_the_screen_lets_the_user_continue_when_only_the_ping_failed(self):
        system = FakeSystem(
            devices=[setup.NetworkDevice("enp2s0", "ethernet", "connected")],
            results=[setup.Connectivity("192.168.1.1", False, True, True, False)],
        )
        ui = FakeUI("CONTINUE")
        self.assertEqual(self.run_network(ui, system), "done")
        self.assertEqual(ui.screens[-1]["choices"], ["CONTINUE"])
        self.assertIn("ONLINE", ui.text())
        self.assertNotIn("TRY AGAIN", ui.text())

    def test_the_real_backend_does_not_skip_the_lookup_when_ping_fails(self):
        system = setup.System()
        seen = []

        def run(args, **_kwargs):
            seen.append(args[0])
            if args[0] == "ip":
                return mock.Mock(stdout="default via 192.168.1.1 dev wlan0\n", returncode=0)
            return mock.Mock(stdout="", returncode=1 if args[0] == "ping" else 0)

        body = mock.MagicMock()
        body.__enter__.return_value.read.return_value = b"NetworkManager is online\n"
        with mock.patch.object(setup.subprocess, "run", side_effect=run), \
                mock.patch.object(setup.urllib.request, "urlopen", return_value=body):
            result = system.connectivity()
        self.assertEqual(seen, ["ip", "ping", "getent"])
        self.assertTrue(result.usable and result.internet and not result.lan)


if __name__ == "__main__":
    unittest.main()
