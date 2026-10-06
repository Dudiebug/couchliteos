import pathlib
import tempfile
import unittest
from unittest import mock

import couchliteos_app_runner as runner
import couchliteos_apps as apps


SYSTEM = pathlib.Path(__file__).parents[1] / "config" / "apps.d"


class FlatpakManifestTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.dir = pathlib.Path(self.temporary.name)

    def manifest(self, text):
        path = self.dir / "app.ini"
        path.write_text(text, encoding="utf-8")
        return apps.read_manifest(path)

    def test_flatpak_manifest_loads_as_a_command_the_runner_can_start(self):
        app = self.manifest("[app]\nid = kodi\nname = KODI\nkind = flatpak\nflatpak = tv.kodi.Kodi\n")
        self.assertEqual((app.kind, app.command, app.arguments, app.flatpak),
                         ("command", "/usr/bin/flatpak", "run tv.kodi.Kodi", "tv.kodi.Kodi"))
        with mock.patch.object(runner, "FOOT", "/usr/bin/foot"):
            self.assertEqual(runner.command_vector(app), ["/usr/bin/flatpak", "run", "tv.kodi.Kodi"])

    def test_serialize_round_trips_as_flatpak(self):
        app = apps.flatpak_application("tv.kodi.Kodi", "KODI", set())
        text = apps.serialize(app)
        self.assertIn("kind = flatpak", text)
        self.assertIn("flatpak = tv.kodi.Kodi", text)
        written = apps.write_user_application(app, system_dir=SYSTEM, user_dir=self.dir / "user")
        loaded = apps.read_manifest(written)
        self.assertEqual((loaded.id, loaded.kind, loaded.arguments, loaded.flatpak),
                         ("kodi", "command", "run tv.kodi.Kodi", "tv.kodi.Kodi"))

    def test_bad_flatpak_manifests_are_refused(self):
        for body in ("flatpak = kodi\n", "flatpak = tv.kodi.Kodi; rm -rf /\n", "flatpak = --user.a.b\n",
                     "flatpak = tv.kodi.Kodi\ncommand = /bin/sh\n", "flatpak = tv.kodi.Kodi\narguments = x\n", ""):
            with self.subTest(body=body), self.assertRaises(apps.ManifestError):
                self.manifest(f"[app]\nid = kodi\nname = KODI\nkind = flatpak\n{body}")
        with self.assertRaises(apps.ManifestError):
            self.manifest("[app]\nid = x\nname = X\nkind = command\ncommand = /bin/true\nflatpak = a.b.c\n")

    def test_flathub_remote_command_works_offline(self):
        command = apps.flathub_remote_command()
        self.assertEqual(command[:5], ["/usr/bin/flatpak", "remote-add", "--system", "--if-not-exists", "flathub"])
        repo = pathlib.Path(__file__).parents[1] / "overlay/usr/share/couchliteos/flathub.flatpakrepo"
        text = repo.read_text(encoding="ascii")
        self.assertIn("Url=https://dl.flathub.org/repo/", text)
        self.assertIn("GPGKey=", text)


if __name__ == "__main__":
    unittest.main()
