"""couchliteos-run-app with downloaded app versions: which copy starts, and the failed-start count."""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "couchliteos-run-app"
LOCK = "moonlight|6.1.0|https://github.com/x|moonlight.AppImage\nchiaki-ng|1.10.0|https://github.com/y|chiaki-ng.AppImage\n"


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bash"), "needs Linux and bash")
class RunAppUpdatesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = pathlib.Path(self._tmp.name)
        self.run_dir = self.base / "run"
        self.run_dir.mkdir()
        self.data = self.base / "data"
        (self.base / "lock").write_text(LOCK)
        (self.base / "defaults").write_text(f"COUCHLITEOS_LOG_DIR={self.base}/log\nCOUCHLITEOS_DATA_DIR={self.data}\n"
                                           f"COUCHLITEOS_UPDATES_DIR={self.data}/apps\n")
        self.program("chiaki-ng", self.base / "apps" / "chiaki-ng", "image")
        self.program("moonlight", self.base / "apps" / "moonlight", "image")

    def program(self, app, appdir, label, exit_code=1, seconds=0):
        binary = appdir / "usr" / "bin" / ("chiaki" if app == "chiaki-ng" else "moonlight")
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text(f"#!/bin/sh\necho {label} >> '{self.base}/started'\nsleep {seconds}\nexit {exit_code}\n")
        binary.chmod(0o755)

    def download(self, app, version, current=True, **kwargs):
        app_dir = self.data / "apps" / app
        self.program(app, app_dir / version, version, **kwargs)
        if current:
            link = app_dir / "current"
            if link.is_symlink():
                link.unlink()
            link.symlink_to(version)

    def run_app(self, app="chiaki-ng", request=None):
        if request is not None:
            (self.run_dir / "moonlight-stream.request").write_text(request)
        environment = {
            **os.environ,
            "COUCHLITEOS_DEFAULTS": str(self.base / "defaults"),
            "COUCHLITEOS_RUN_DIR": str(self.run_dir),
            "COUCHLITEOS_APPS_DIR": str(self.base / "apps"),
            "COUCHLITEOS_LIBEXEC_DIR": str(ROOT / "launcher"),
            "COUCHLITEOS_HARDWARE_ENV": str(self.base / "no-hardware.env"),
            "COUCHLITEOS_IMAGE_LOCK": str(self.base / "lock"),
        }
        result = subprocess.run(["bash", str(SCRIPT), app], env=environment, capture_output=True, text=True,
                                timeout=60)
        started = (self.base / "started").read_text().split()
        (self.base / "started").unlink()
        return result.returncode, started[-1]

    def health(self, app="chiaki-ng"):
        path = self.data / "app-health" / app
        return path.read_text().strip() if path.exists() else None

    def test_the_image_copy_runs_when_nothing_is_downloaded(self):
        self.assertEqual(self.run_app()[1], "image")
        self.assertIsNone(self.health(), "the image copy is never counted")

    def test_a_newer_download_is_preferred(self):
        self.download("chiaki-ng", "1.11.0")
        self.assertEqual(self.run_app()[1], "1.11.0")

    def test_the_image_wins_when_it_is_the_same_or_newer(self):
        for version in ("1.10.0", "1.9.2"):
            with self.subTest(version=version):
                self.download("chiaki-ng", version)
                self.assertEqual(self.run_app()[1], "image")

    def test_a_version_compare_is_numeric_not_textual(self):
        self.download("chiaki-ng", "1.9.10")
        self.assertEqual(self.run_app()[1], "image", "1.9.10 is older than 1.10.0")

    def test_a_download_without_its_program_is_ignored(self):
        app_dir = self.data / "apps" / "chiaki-ng"
        (app_dir / "1.11.0").mkdir(parents=True)
        (app_dir / "current").symlink_to("1.11.0")
        self.assertEqual(self.run_app()[1], "image")

    def test_two_failed_starts_in_a_row_ask_for_a_rollback(self):
        self.download("chiaki-ng", "1.11.0", exit_code=1)
        self.run_app()
        self.assertEqual(self.health(), "1.11.0 1")
        self.assertFalse((self.run_dir / "app-rollback-check").exists())
        self.run_app()
        self.assertEqual(self.health(), "1.11.0 2")
        self.assertTrue((self.run_dir / "app-rollback-check").exists())

    def test_a_count_for_an_older_version_starts_again(self):
        (self.data / "app-health").mkdir(parents=True)
        (self.data / "app-health" / "chiaki-ng").write_text("1.10.5 1\n")
        self.download("chiaki-ng", "1.11.0", exit_code=1)
        self.run_app()
        self.assertEqual(self.health(), "1.11.0 1")

    def test_a_start_that_reaches_ready_resets_the_count(self):
        (self.data / "app-health").mkdir(parents=True)
        (self.data / "app-health" / "chiaki-ng").write_text("1.11.0 1\n")
        self.download("chiaki-ng", "1.11.0", exit_code=0, seconds=6)
        self.run_app()
        self.assertEqual(self.health(), "1.11.0 0")

    def test_a_stream_that_cannot_reach_its_pc_is_not_the_apps_fault(self):
        self.download("moonlight", "6.2.0", exit_code=1)
        code, started = self.run_app("moonlight", "pc.lan\nDesktop\n")
        self.assertEqual((code, started), (0, "6.2.0"))
        self.assertIsNone(self.health("moonlight"))

    def test_a_crashing_stream_is_counted(self):
        self.download("moonlight", "6.2.0", exit_code=139)
        self.run_app("moonlight", "pc.lan\nDesktop\n")
        self.assertEqual(self.health("moonlight"), "6.2.0 1")


if __name__ == "__main__":
    unittest.main()
