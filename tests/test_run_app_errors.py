"""couchliteos-run-app refusals: unknown applications and missing executables.

test_run_app.py covers a Moonlight that starts; these are the paths that never start one.
"""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "couchliteos-run-app"


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bash"), "needs Linux and bash")
class RunAppRefusalTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = pathlib.Path(self.temporary.name)
        self.run_dir = self.base / "run"
        self.run_dir.mkdir()
        defaults = self.base / "defaults"
        defaults.write_text(f"COUCHLITEOS_LOG_DIR={self.base}/log\nCOUCHLITEOS_DATA_DIR={self.base}/data\n")
        self.environment = {
            **os.environ,
            "COUCHLITEOS_DEFAULTS": str(defaults),
            "COUCHLITEOS_RUN_DIR": str(self.run_dir),
            "COUCHLITEOS_APPS_DIR": str(self.base / "apps"),
            "COUCHLITEOS_LIBEXEC_DIR": str(ROOT / "launcher"),
            "COUCHLITEOS_HARDWARE_ENV": str(self.base / "no-hardware.env"),
        }

    def run_app(self, *arguments):
        return subprocess.run(
            ["bash", str(SCRIPT), *arguments], env=self.environment, capture_output=True, text=True, timeout=60
        )

    def test_unknown_application_is_a_usage_error(self):
        for arguments in (("steam",), ("",), ()):
            result = self.run_app(*arguments)
            self.assertEqual(result.returncode, 64, arguments)
            self.assertIn("Unknown application:", result.stderr)
        self.assertEqual(sorted(self.run_dir.iterdir()), [])

    def test_unknown_application_is_never_used_in_a_path(self):
        result = self.run_app("../../moonlight")
        self.assertEqual(result.returncode, 64)
        self.assertEqual(sorted(self.run_dir.iterdir()), [])

    def assert_missing(self, app, request_name):
        for name in (request_name, f"{app}-ready", f"close-{app}"):
            (self.run_dir / name).write_text("stale")
        result = self.run_app(app)
        self.assertEqual(result.returncode, 66, result.stderr)
        self.assertIn("Application executable is missing:", result.stderr)
        self.assertEqual(
            (self.run_dir / f"{app}-status").read_text(), "failed: application executable is missing\n"
        )
        # The one-shot start request and stale markers from a previous run are gone,
        # and the app was never marked active.
        self.assertEqual(sorted(item.name for item in self.run_dir.iterdir()), [f"{app}-status"])

    def test_missing_moonlight_payload(self):
        self.assert_missing("moonlight", "start-moonlight")

    def test_missing_chiaki_payload(self):
        self.assert_missing("chiaki-ng", "start-chiaki")

    def test_non_executable_payload_counts_as_missing(self):
        binary = self.base / "apps" / "moonlight" / "usr" / "bin" / "moonlight"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o644)
        self.assert_missing("moonlight", "start-moonlight")

    def test_log_and_data_directories_are_created_even_when_refused(self):
        self.run_app("moonlight")
        self.assertTrue((self.base / "log").is_dir())
        self.assertTrue((self.base / "data" / "home" / ".config").is_dir())
        self.assertTrue((self.base / "data" / "home" / ".cache").is_dir())


if __name__ == "__main__":
    unittest.main()
