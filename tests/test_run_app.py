import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "couchliteos-run-app"


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bash"), "needs Linux and bash")
class MoonlightStreamExitTest(unittest.TestCase):
    """couchliteos-run-app against a fake Moonlight: what the unit's Restart=on-failure sees."""

    def run_app(self, exit_code, request=None, browse=False, profile=False):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            run = base / "run"
            run.mkdir()
            tools = base / "bin"
            tools.mkdir()
            if profile:
                # A paired PC with a profile app: a plain start streams it.
                (base / "data").mkdir()
                (base / "data" / "config.ini").write_text("[host:gaming-pc]\napp = Desktop\n")
                address = tools / "couchliteos-host-address"
                address.write_text("#!/bin/sh\necho pc.lan\n")
                address.chmod(0o755)
            if browse:
                (run / "moonlight-browse.request").touch()
            binary = base / "apps" / "moonlight" / "usr" / "bin" / "moonlight"
            binary.parent.mkdir(parents=True)
            binary.write_text(f"#!/bin/sh\necho \"$@\" > '{base}/argv'\nexit {exit_code}\n")
            binary.chmod(0o755)
            defaults = base / "defaults"
            defaults.write_text(f"COUCHLITEOS_LOG_DIR={base}/log\nCOUCHLITEOS_DATA_DIR={base}/data\n")
            if request is not None:
                (run / "moonlight-stream.request").write_text(request)
            environment = {
                **os.environ,
                "COUCHLITEOS_DEFAULTS": str(defaults),
                "COUCHLITEOS_RUN_DIR": str(run),
                "COUCHLITEOS_APPS_DIR": str(base / "apps"),
                "COUCHLITEOS_LIBEXEC_DIR": str(ROOT / "launcher"),
                "COUCHLITEOS_HARDWARE_ENV": str(base / "no-hardware.env"),
                "PATH": f"{tools}{os.pathsep}{os.environ.get('PATH', '')}",
            }
            result = subprocess.run(
                ["bash", str(SCRIPT), "moonlight"], env=environment, capture_output=True, text=True, timeout=60
            )
            argv = (base / "argv").read_text().strip() if (base / "argv").exists() else None
            status = (run / "moonlight-status").read_text().strip()
            consumed = not (run / "moonlight-stream.request").exists()
            self.browse_left = (run / "moonlight-browse.request").exists()
        return result.returncode, status, argv, consumed

    def test_a_failed_auto_stream_does_not_make_systemd_restart_moonlight(self):
        # The request is one-shot. A restart would open the Moonlight GUI (or stream to the profile
        # host) instead of leaving the launcher's error screen, which reads the status below.
        code, status, argv, consumed = self.run_app(1, "pc.lan\nSteam Big Picture\n")
        self.assertEqual(argv, "stream pc.lan Steam Big Picture")
        self.assertTrue(consumed)
        self.assertEqual(status, "failed: exited before the application became ready (status 1)")
        self.assertEqual(code, 0)

    def test_a_successful_auto_stream_exits_normally(self):
        code, _status, argv, _consumed = self.run_app(0, "pc.lan\nDesktop\n")
        self.assertEqual((code, argv), (0, "stream pc.lan Desktop"))

    def test_an_ordinary_moonlight_crash_still_fails_the_unit_so_systemd_retries(self):
        code, status, argv, _consumed = self.run_app(1)
        self.assertEqual(argv, "")
        self.assertEqual(status, "failed: exited before the application became ready (status 1)")
        self.assertEqual(code, 1)

    def test_a_plain_start_streams_the_profile_app(self):
        _code, _status, argv, _consumed = self.run_app(0, profile=True)
        self.assertEqual(argv, "stream pc.lan Desktop")

    def test_moonlight_from_apps_opens_its_list_of_pcs_instead_of_the_profile_stream(self):
        _code, _status, argv, _consumed = self.run_app(0, browse=True, profile=True)
        self.assertEqual(argv, "")
        self.assertFalse(self.browse_left)

    def test_a_game_streams_even_after_a_left_over_browse_request(self):
        _code, _status, argv, _consumed = self.run_app(0, "pc.lan\nHades\n", browse=True)
        self.assertEqual(argv, "stream pc.lan Hades")

    def test_a_bad_request_is_ignored_and_the_crash_is_still_retried(self):
        code, _status, argv, consumed = self.run_app(1, "--evil\nDesktop\n")
        self.assertEqual(argv, "")
        self.assertTrue(consumed)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
