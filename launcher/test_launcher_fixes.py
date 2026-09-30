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


if __name__ == "__main__":
    unittest.main()
