"""Launcher behaviours fixed after field testing (live mode, network, saving)."""

import importlib.util
import pathlib
import tempfile
import unittest
from unittest import mock

from test_launcher import Screen


class LauncherFixesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = pathlib.Path(__file__).with_name("moonlightos-launcher.py")
        spec = importlib.util.spec_from_file_location("launcher_fixes", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def launcher(self):
        with mock.patch.object(self.module, "network_summary", return_value="OFFLINE"):
            return self.module.Launcher(Screen())


class LiveModeWarningTest(LauncherFixesTest):
    OVERLAY = (
        "overlay overlay rw,noatime,lowerdir=/run/live/rootfs/filesystem.squashfs/,"
        "upperdir=/run/live/overlay/rw,workdir=/run/live/overlay/work\n"
    )
    BIND_ON_USB = "/dev/sdb3[/moonlightos-state] ext4 rw,relatime\n"
    ENCRYPTED = "/dev/mapper/sdb3_crypt[/moonlightos-state] ext4 rw,relatime\n"
    UNION_ON_USB = (
        "overlay overlay rw,lowerdir=/run/live/rootfs/filesystem.squashfs/,"
        "upperdir=/run/live/persistence/sdb3/rw,workdir=/run/live/persistence/sdb3/.work\n"
    )

    def test_plain_live_overlay_is_not_persistent(self):
        self.assertFalse(self.module.state_is_persistent(self.OVERLAY))
        self.assertFalse(self.module.state_is_persistent("tmpfs tmpfs rw,nosuid\n"))
        self.assertFalse(self.module.state_is_persistent(""))

    def test_state_on_a_real_partition_or_union_upper_is_persistent(self):
        for output in (self.BIND_ON_USB, self.ENCRYPTED, self.UNION_ON_USB):
            with self.subTest(output=output):
                self.assertTrue(self.module.state_is_persistent(output))

    def test_squashfs_loop_device_does_not_count_as_persistence(self):
        self.assertFalse(self.module.state_is_persistent("/dev/loop0 squashfs ro,relatime\n"))

    def test_live_boot_without_persistence_warns(self):
        with tempfile.TemporaryDirectory() as directory:
            completed = mock.Mock(returncode=0, stdout=self.OVERLAY)
            with mock.patch.object(self.module.subprocess, "run", return_value=completed) as run:
                warning = self.module.live_mode_warning(pathlib.Path(directory))
        self.assertEqual(warning, "LIVE MODE: SETTINGS WILL NOT BE SAVED")
        self.assertEqual(run.call_args.args[0][0], "findmnt")
        self.assertIn("/var/lib/moonlightos", run.call_args.args[0])

    def test_live_boot_with_persistence_does_not_warn(self):
        with tempfile.TemporaryDirectory() as directory:
            completed = mock.Mock(returncode=0, stdout=self.BIND_ON_USB)
            with mock.patch.object(self.module.subprocess, "run", return_value=completed):
                self.assertEqual(self.module.live_mode_warning(pathlib.Path(directory)), "")

    def test_installed_system_never_warns_and_never_runs_findmnt(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = pathlib.Path(directory) / "live"
            with mock.patch.object(self.module.subprocess, "run") as run:
                self.assertEqual(self.module.live_mode_warning(missing), "")
        run.assert_not_called()

    def test_live_boot_where_findmnt_fails_still_warns(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(self.module.subprocess, "run", side_effect=OSError("no findmnt")):
                self.assertEqual(
                    self.module.live_mode_warning(pathlib.Path(directory)),
                    self.module.LIVE_WARNING,
                )

    def test_banner_stays_on_screen_after_the_status_line_changes(self):
        with mock.patch.object(self.module, "live_mode_warning", return_value=self.module.LIVE_WARNING):
            launcher = self.launcher()
        texts = []
        launcher.screen.addstr = lambda _row, _col, text, *_rest: texts.append(text)
        launcher.status = "192.168.1.5  ONLINE"
        launcher.draw()
        self.assertIn(self.module.LIVE_WARNING, texts)
        self.assertIn("192.168.1.5  ONLINE", texts)

    def test_no_banner_when_not_live(self):
        with mock.patch.object(self.module, "live_mode_warning", return_value=""):
            launcher = self.launcher()
        texts = []
        launcher.screen.addstr = lambda _row, _col, text, *_rest: texts.append(text)
        launcher.draw()
        self.assertFalse([text for text in texts if "LIVE MODE" in text])


class DisplayRestoreStartupTest(LauncherFixesTest):
    def run_startup(self, restore_result):
        launcher = self.launcher()
        launcher.prepare_session = mock.Mock()
        launcher.setup_wizard = mock.Mock()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ), mock.patch.object(
            self.module.display, "restore_saved_mode", return_value=restore_result
        ), mock.patch.object(self.module.curses, "curs_set"), mock.patch.object(
            self.module.curses, "use_default_colors"
        ):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                launcher.run()
        return launcher

    def test_skipped_restore_tells_the_user_where_to_choose_the_mode_again(self):
        launcher = self.run_startup(None)
        self.assertEqual(
            launcher.status,
            "SAVED DISPLAY MODE SKIPPED — CHOOSE IT AGAIN IN SETTINGS > DISPLAY",
        )

    def test_message_is_not_replaced_by_the_network_status_straight_away(self):
        launcher = self.run_startup(None)
        self.assertGreater(launcher.last_status_update, self.module.time.monotonic())

    def test_normal_restore_leaves_the_status_alone(self):
        launcher = self.run_startup(True)
        self.assertEqual(launcher.status, "OFFLINE")

    def test_first_key_press_confirms_the_restore_but_a_timeout_does_not(self):
        screen = Screen([-1, ord("x")])
        with mock.patch.object(self.module.display, "confirm_restore") as confirm:
            self.assertEqual(self.module.read_key(screen), -1)
            confirm.assert_not_called()
            self.assertEqual(self.module.read_key(screen), ord("x"))
            confirm.assert_called_once_with()


class NetworkStatusTest(LauncherFixesTest):
    ONLY_TAILSCALE = (
        "lo               UNKNOWN        127.0.0.1/8\n"
        "tailscale0       UNKNOWN        100.101.102.103/32\n"
    )
    TWO_NICS = (
        "lo               UNKNOWN        127.0.0.1/8\n"
        "docker0          UP             172.17.0.1/16\n"
        "enp2s0           UP             192.168.1.5/24\n"
        "wlan0            UP             10.0.0.9/24\n"
    )

    def fake_ip(self, addresses, routes=""):
        def run(command, **_kwargs):
            text = routes if "route" in command else addresses
            return mock.Mock(returncode=0, stdout=text)

        return mock.patch.object(self.module.subprocess, "run", side_effect=run)

    def test_tailscale_alone_is_not_a_lan_address(self):
        self.assertEqual(self.module.get_ipv4(self.ONLY_TAILSCALE), "NO IPV4")

    def test_virtual_and_link_local_addresses_are_skipped(self):
        sample = (
            "docker0 UP 172.17.0.1/16\nvirbr0 UP 192.168.122.1/24\n"
            "br-1a2b3c UP 172.18.0.1/16\nveth12 UP 10.9.9.9/24\n"
            "enp2s0 UP 169.254.3.4/16\nwlan0 UP 192.168.0.7/24\n"
        )
        self.assertEqual(self.module.get_ipv4(sample), "192.168.0.7")

    def test_default_route_interface_is_preferred(self):
        self.assertEqual(self.module.get_ipv4(self.TWO_NICS), "192.168.1.5")
        self.assertEqual(self.module.get_ipv4(self.TWO_NICS, ["wlan0"]), "10.0.0.9")

    def test_default_route_parser_reads_the_dev_field(self):
        routes = (
            "default via 192.168.1.1 dev enp2s0 proto dhcp src 192.168.1.5 metric 100\n"
            "default via 10.0.0.1 dev wlan0 proto dhcp metric 600\n"
        )
        self.assertEqual(self.module.default_route_interfaces(routes), ["enp2s0", "wlan0"])
        self.assertEqual(self.module.default_route_interfaces(""), [])

    def test_only_tailscale_up_reads_offline_and_points_at_network_settings(self):
        with self.fake_ip(self.ONLY_TAILSCALE):
            self.assertEqual(self.module.network_summary(), "OFFLINE - SETTINGS > NETWORK")

    def test_lan_with_default_route_reads_online(self):
        routes = "default via 10.0.0.1 dev wlan0 proto dhcp metric 600\n"
        with self.fake_ip(self.TWO_NICS, routes):
            self.assertEqual(self.module.network_summary(), "10.0.0.9  ONLINE")

    def test_missing_ip_command_reads_offline(self):
        with mock.patch.object(self.module.subprocess, "run", side_effect=OSError("no ip")):
            self.assertEqual(self.module.network_summary(), "OFFLINE - SETTINGS > NETWORK")

    def test_settings_network_opens_the_network_setup_app(self):
        self.assertIn("NETWORK", self.module.SETTINGS_MENU)
        launcher = self.launcher()
        settings = self.module.Settings(Screen(), launcher)
        settings.selected = self.module.SETTINGS_MENU.index("NETWORK")
        with mock.patch.object(launcher, "launch_by_id", return_value=True) as launch:
            self.assertTrue(settings.activate())
        launch.assert_called_once_with("network-setup")


if __name__ == "__main__":
    unittest.main()
