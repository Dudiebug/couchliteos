"""Launcher behaviours fixed after field testing (live mode, network, saving)."""

import errno
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

    def test_read_only_system_image_and_ram_devices_do_not_count_as_persistence(self):
        for output in ("/dev/loop0 squashfs ro,relatime\n", "/dev/sr0 iso9660 ro\n", "/dev/zram0 ext4 rw\n"):
            with self.subTest(output=output):
                self.assertFalse(self.module.state_is_persistent(output))

    def test_persistence_file_on_a_loop_device_counts(self):
        # Real `findmnt` output for a bind mount from a loop-mounted ext4 image.
        self.assertTrue(self.module.state_is_persistent("/dev/loop0[/moonlightos-state] ext4 rw,relatime\n"))

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


class DisplayConfirmSaveTest(LauncherFixesTest):
    BEFORE = 'DP-1 "Dell Inc. DELL U2723QE ABC123"\n  Enabled: yes\n  Modes:\n    3840x2160 px, 60.000000 Hz (preferred, current)\n    1920x1080 px, 120.000000 Hz\n'
    AFTER = 'DP-1 "Dell Inc. DELL U2723QE ABC123"\n  Enabled: yes\n  Modes:\n    3840x2160 px, 60.000000 Hz (preferred)\n    1920x1080 px, 120.000000 Hz (current)\n'

    def finish(self, save_display, after=None):
        display = self.module.display
        old = display.parse_wlr_randr(self.BEFORE)[0]
        new = display.parse_wlr_randr(after or self.AFTER)[0]
        requested = display.Mode(1920, 1080, 120000)
        settings = self.module.Settings(Screen(), self.launcher())
        settings.output, settings.original_mode = old, old.current_mode
        settings.rollback = mock.Mock()
        with mock.patch.object(display, "valid_output_mode", return_value=(new, requested)), mock.patch.object(
            display, "query_outputs", return_value=[new]
        ), mock.patch.object(display, "save_display", side_effect=save_display) as save, mock.patch.object(
            display, "apply_mode"
        ) as apply, mock.patch.object(display, "log"):
            settings.finish_preview(old, old.current_mode, requested)
        return settings, save, apply

    def test_saved_mode_is_not_rolled_back(self):
        settings, save, apply = self.finish(lambda *_args: None)
        save.assert_called_once()
        settings.rollback.assert_not_called()
        apply.assert_not_called()

    def test_full_disk_keeps_the_mode_and_says_why_it_was_not_saved(self):
        def full(*_args):
            raise OSError(errno.ENOSPC, "No space left on device")

        settings, _save, apply = self.finish(full)
        settings.rollback.assert_not_called()
        apply.assert_not_called()
        self.assertEqual(settings.status, "MODE APPLIED BUT NOT SAVED: NO SPACE LEFT ON DEVICE")

    def test_read_only_disk_keeps_the_mode_and_says_why_it_was_not_saved(self):
        def read_only(*_args):
            raise OSError(errno.EROFS, "Read-only file system")

        settings, _save, _apply = self.finish(read_only)
        settings.rollback.assert_not_called()
        self.assertEqual(settings.status, "MODE APPLIED BUT NOT SAVED: READ-ONLY FILE SYSTEM")

    def test_display_without_identity_reports_why_nothing_was_saved(self):
        def anonymous(*_args):
            raise RuntimeError("display identity is unavailable; mode was not saved")

        settings, _save, _apply = self.finish(anonymous)
        settings.rollback.assert_not_called()
        self.assertTrue(settings.status.startswith("MODE APPLIED BUT NOT SAVED: DISPLAY IDENTITY"))

    def test_mode_the_compositor_did_not_keep_is_still_rolled_back(self):
        settings, save, _apply = self.finish(lambda *_args: None, after=self.BEFORE)
        settings.rollback.assert_called_once()
        save.assert_not_called()


class RdpDiskFullTest(LauncherFixesTest):
    MESSAGE = "COULD NOT SAVE: DISK FULL OR READ-ONLY"

    def remote(self):
        launcher = self.launcher()
        launcher.show_launch_failure = mock.Mock()
        return launcher, self.module.RemoteDesktopSettings(Screen(), launcher)

    @staticmethod
    def values(**changes):
        base = {
            "name": "Work", "host": "10.0.0.9", "port": "3389", "username": "alice", "domain": "",
            "resolution": "native", "fullscreen": True, "audio": True, "clipboard": True,
            "save_password": False,
        }
        return {**base, **changes}

    def save(self, remote, original, values, password, upsert):
        rdp = self.module.rdp
        with mock.patch.object(rdp, "load_connections", return_value=([], [])), mock.patch.object(
            self.module, "application_result", return_value=self.module.apps.LoadResult((), ())
        ), mock.patch.object(rdp, "upsert_connection", side_effect=upsert) as saved:
            return remote.save_connection(original, values, password), saved

    @staticmethod
    def full(*_args):
        raise OSError(errno.ENOSPC, "No space left on device")

    def saved_connection(self):
        return self.module.rdp.Connection(
            id="rdp-work", name="Work", host="10.0.0.9", username="alice", certificate="ab" * 32,
            save_password=True,
        )

    def test_full_disk_on_first_save_is_reported_not_raised(self):
        _launcher, remote = self.remote()
        result, _saved = self.save(remote, None, self.values(), None, self.full)
        self.assertFalse(result)
        self.assertEqual(remote.status, self.MESSAGE)

    def test_full_disk_when_clearing_a_password_after_a_server_change_is_reported(self):
        _launcher, remote = self.remote()
        changed = self.values(host="10.0.0.10", save_password=True)
        result, saved = self.save(remote, self.saved_connection(), changed, None, [None, OSError(errno.EROFS, "ro")])
        self.assertFalse(result)
        self.assertEqual(saved.call_count, 2)
        self.assertEqual(remote.status, self.MESSAGE)

    def test_full_disk_after_the_password_could_not_be_stored_still_removes_it(self):
        _launcher, remote = self.remote()
        remote.password_request = mock.Mock(return_value=(False, "helper said no"))
        result, _saved = self.save(
            remote, None, self.values(save_password=True), "Fake-Typed-Password", [None, OSError(errno.ENOSPC, "full")]
        )
        self.assertFalse(result)
        self.assertEqual(remote.status, self.MESSAGE)
        self.assertEqual([call.args[0] for call in remote.password_request.call_args_list], ["set", "delete"])

    def test_successful_save_is_unchanged(self):
        _launcher, remote = self.remote()
        result, _saved = self.save(remote, None, self.values(), None, [None])
        self.assertTrue(result)
        self.assertEqual(remote.status, "SAVED WORK")

    def test_full_disk_while_pinning_the_certificate_stops_the_launch_with_the_reason(self):
        connection = self.module.rdp.Connection(id="rdp-work-pc", name="Work", host="10.0.0.9", username="alice")
        launcher, remote = self.remote()
        remote.confirm_certificate = mock.Mock(return_value=True)
        remote.text_input = mock.Mock()
        app = self.module.apps.Application(
            id="rdp-work-pc", name="OFFICE", kind="rdp", connection="rdp-work-pc", status_id="rdp-work-pc",
        )
        rdp = self.module.rdp
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ), mock.patch.object(rdp, "get_connection", return_value=connection), mock.patch.object(
            rdp, "probe_certificate", return_value="cd" * 32
        ), mock.patch.object(rdp, "upsert_connection", side_effect=self.full), mock.patch.object(
            self.module, "rdp_log"
        ):
            self.assertFalse(remote.prepare_launch(app))
            self.assertFalse((pathlib.Path(directory) / "rdp-session.secret").exists())
        launcher.show_launch_failure.assert_called_once_with("OFFICE", self.MESSAGE)
        remote.text_input.assert_not_called()


class SettingsLaunchStatusTest(LauncherFixesTest):
    def test_unavailable_app_is_explained_inside_settings(self):
        for label, app_id in (
            ("TAILSCALE", "tailscale"),
            ("SYSTEM DIAGNOSTICS", "system-diagnostics"),
            ("NETWORK", "network-setup"),
        ):
            with self.subTest(label=label):
                launcher = self.launcher()
                settings = self.module.Settings(Screen(), launcher)
                settings.selected = self.module.SETTINGS_MENU.index(label)
                with mock.patch.object(launcher, "app_by_id", return_value=None):
                    self.assertTrue(settings.activate())
                self.assertEqual(settings.status, f"{app_id.upper()} IS NOT AVAILABLE: CHECK SETTINGS > APPLICATIONS")

    def test_started_app_result_is_shown_inside_settings(self):
        launcher = self.launcher()
        settings = self.module.Settings(Screen(), launcher)
        settings.selected = self.module.SETTINGS_MENU.index("TAILSCALE")

        def start(_app_id):
            launcher.status = "TAILSCALE STARTED"
            return True

        with mock.patch.object(launcher, "launch_by_id", side_effect=start):
            settings.activate()
        self.assertEqual(settings.status, "TAILSCALE STARTED")


class AudioVolumeTest(LauncherFixesTest):
    KEY_DOWN, KEY_LEFT, KEY_RIGHT, ENTER, ESC = 258, 260, 261, 10, 27

    def run_audio(self, keys, sinks=True, **audio_patches):
        """Open the audio screen, feed keys, return [(rows, status)] per draw and the audio mocks."""
        audio = self.module.audio
        launcher = self.launcher()
        launcher.screen = Screen(keys)  # as in the real launcher, one screen serves Settings and its error dialogs
        settings = self.module.Settings(launcher.screen, launcher)
        frames = []
        settings.draw = lambda _title, rows, _selected=None: frames.append((list(rows), settings.status))
        mocks = {
            "query_sinks": mock.Mock(return_value=[audio.Sink(7, "HDMI OUTPUT", True)] if sinks else []),
            "get_volume": mock.Mock(return_value=audio.Volume(50, False)),
            "change_volume": mock.Mock(return_value=audio.Volume(55, False)),
            "toggle_mute": mock.Mock(return_value=audio.Volume(50, True)),
            "set_default": mock.Mock(),
            "ensure_audible": mock.Mock(return_value=""),
        }
        mocks.update(audio_patches)
        patches = [mock.patch.object(audio, name, value) for name, value in mocks.items()]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        settings.run_audio()
        return frames, mocks

    def test_rows_show_outputs_then_volume_mute_and_back(self):
        frames, _mocks = self.run_audio([self.ESC])
        self.assertEqual(
            frames[0][0],
            ["*  HDMI OUTPUT", "VOLUME  [##########..........]  50%", "MUTE  OFF", "BACK"],
        )

    def test_right_raises_and_left_lowers_the_volume_of_the_default_output(self):
        frames, mocks = self.run_audio([self.KEY_DOWN, self.KEY_RIGHT, self.KEY_LEFT, self.ESC])
        self.assertEqual([call.args for call in mocks["change_volume"].call_args_list], [(5,), (-5,)])
        self.assertEqual(frames[2][1], "VOLUME 55%")

    def test_enter_on_the_mute_row_toggles_mute(self):
        frames, mocks = self.run_audio([self.KEY_DOWN, self.KEY_DOWN, self.ENTER, self.ESC])
        mocks["toggle_mute"].assert_called_once_with()
        self.assertEqual(frames[-1][1], "MUTED")

    def test_back_is_the_last_row_and_leaves(self):
        self.run_audio([self.KEY_DOWN, self.KEY_DOWN, self.KEY_DOWN, self.ENTER])

    def test_left_right_on_an_output_row_does_not_change_anything(self):
        _frames, mocks = self.run_audio([self.KEY_RIGHT, self.KEY_LEFT, self.ESC])
        mocks["set_default"].assert_not_called()
        mocks["change_volume"].assert_not_called()

    def test_choosing_an_output_makes_sure_it_is_audible_and_says_so(self):
        frames, mocks = self.run_audio(
            [self.ENTER, self.ESC], ensure_audible=mock.Mock(return_value="UNMUTED, VOLUME SET TO 50%")
        )
        mocks["set_default"].assert_called_once_with(7)
        mocks["ensure_audible"].assert_called_once_with(7)
        self.assertEqual(frames[-1][1], "DEFAULT OUTPUT: HDMI OUTPUT (UNMUTED, VOLUME SET TO 50%)")

    def test_volume_check_failure_does_not_undo_the_output_change(self):
        def broken(_sink_id):
            raise RuntimeError("wpctl died")

        frames, _mocks = self.run_audio([self.ENTER, self.ESC], ensure_audible=broken)
        self.assertEqual(frames[-1][1], "DEFAULT OUTPUT: HDMI OUTPUT (VOLUME NOT CHECKED)")

    def test_volume_failure_is_shown(self):
        def refuse(_step):
            raise RuntimeError("no default sink")

        frames, _mocks = self.run_audio([self.KEY_DOWN, self.KEY_RIGHT, self.ESC], change_volume=refuse)
        self.assertEqual(frames[-1][1], "VOLUME NOT CHANGED: no default sink")

    def test_unreadable_volume_still_lists_the_outputs(self):
        def unreadable():
            raise RuntimeError("wpctl died")

        frames, _mocks = self.run_audio([self.ESC], get_volume=unreadable)
        self.assertEqual(frames[0][0], ["*  HDMI OUTPUT", "VOLUME  UNAVAILABLE", "MUTE  UNAVAILABLE", "BACK"])

    def test_no_outputs_means_no_volume_rows(self):
        # The first ESC closes the "no sound output found" explanation (feat/easy), the second leaves AUDIO.
        frames, _mocks = self.run_audio([self.ESC, self.ESC], sinks=False)
        self.assertEqual(frames[0][0], ["BACK"])


if __name__ == "__main__":
    unittest.main()
