"""tools/apps-manifest.sh: apps.json from build/applications.lock, read back by the app updater."""

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "apps-manifest.sh"
sys.path.insert(0, str(ROOT / "launcher"))
import couchliteos_appupdate as appupdate  # noqa: E402

sys.path.pop(0)


@unittest.skipUnless(shutil.which("bash") and shutil.which("sha256sum"), "needs bash and coreutils")
class AppsManifestTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)
        self.downloads = self.tmp / "downloads"
        self.downloads.mkdir()
        self.lock = self.tmp / "applications.lock"
        # The repository's own lock, with stand-in downloads.
        self.lock.write_bytes((ROOT / "build" / "applications.lock").read_bytes())
        self.data = {}
        for line in self.lock.read_text().splitlines():
            if line and not line.startswith("#"):
                name, _version, _url, filename = line.split("|")
                self.data[name] = f"{name} appimage".encode()
                (self.downloads / filename).write_bytes(self.data[name])

    def tool(self, *args):
        environment = {**os.environ, "COUCHLITEOS_APPS_LOCK": str(self.lock),
                       "COUCHLITEOS_DOWNLOADS": str(self.downloads)}
        return subprocess.run(["bash", str(TOOL), *args], capture_output=True, text=True, env=environment)

    def test_the_manifest_lists_every_locked_app_with_its_checksum(self):
        result = self.tool("--min-os", "0.3.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        entries = appupdate.parse_manifest(result.stdout)
        self.assertEqual(set(entries), set(appupdate.APPS))
        self.assertEqual(appupdate.image_versions(self.lock),
                         {name: entry.version for name, entry in entries.items()})
        for name, entry in entries.items():
            self.assertEqual(entry.sha256, hashlib.sha256(self.data[name]).hexdigest())
            self.assertEqual(entry.size, len(self.data[name]))
            self.assertEqual(entry.min_os, "0.3.0")
        self.assertEqual(data["schema"], 1)

    def test_min_os_defaults_to_this_version_and_output_can_be_a_file(self):
        output = self.tmp / "apps.json"
        self.assertEqual(self.tool("--output", str(output)).returncode, 0)
        version = (ROOT / "VERSION").read_text().strip()
        self.assertEqual({entry.min_os for entry in appupdate.parse_manifest(output.read_text()).values()}, {version})

    def test_an_app_not_downloaded_from_github_is_refused(self):
        self.lock.write_bytes(b"moonlight|6.2.0|https://example.com/Moonlight.AppImage|moonlight.AppImage\n")
        result = self.tool()
        self.assertEqual(result.returncode, 65)
        self.assertIn("not downloaded from GitHub", result.stderr)

    def test_a_missing_download_is_refused(self):
        for path in self.downloads.iterdir():
            path.unlink()
        result = self.tool()
        self.assertEqual(result.returncode, 66)
        self.assertIn("make fetch-apps", result.stderr)


if __name__ == "__main__":
    unittest.main()
