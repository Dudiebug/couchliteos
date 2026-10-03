import dataclasses
import pathlib
import tempfile
import unittest
from unittest import mock

import couchliteos_apps as apps


SYSTEM = pathlib.Path(__file__).parents[1] / "config" / "apps.d"


class ApplicationsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temporary.name)
        self.user = root / "user"
        self.state = root / "state.ini"

    def tearDown(self):
        self.temporary.cleanup()

    def load(self):
        return apps.load_applications(SYSTEM, self.user, self.state)

    def test_system_apps_load_in_stable_default_order(self):
        result = self.load()
        visible = [app.id for app in result.applications if app.visible]
        self.assertEqual(
            visible,
            ["moonlight", "chiaki-ng", "firefox", "google-chrome", "terminal", "tailscale"],
        )
        self.assertEqual(result.errors, ())

    def test_user_app_and_state_overrides_load(self):
        custom = apps.Application(
            id="editor", name="EDITOR", kind="command", command="/bin/true",
            status_id="editor", order=70,
        )
        apps.write_user_application(custom, system_dir=SYSTEM, user_dir=self.user)
        loaded = self.load()
        changed = [
            dataclasses.replace(app, enabled=False, order=5) if app.id == "editor" else app
            for app in loaded.applications
        ]
        apps.write_state(changed, self.state)
        result = self.load()
        editor = next(app for app in result.applications if app.id == "editor")
        self.assertFalse(editor.enabled)
        self.assertEqual(editor.order, 5)

    def test_browser_manifests_name_their_binary_and_are_hidden_without_it(self):
        result = self.load()
        binaries = {app.id: app.binary for app in result.applications if app.binary}
        self.assertEqual(binaries, {"firefox": "/usr/bin/firefox-esr", "google-chrome": "/usr/bin/google-chrome-stable"})
        root = pathlib.Path(self.temporary.name) / "root"
        firefox = next(app for app in result.applications if app.id == "firefox")
        terminal = next(app for app in result.applications if app.id == "terminal")
        self.assertFalse(apps.installed(firefox, root))
        self.assertTrue(apps.installed(terminal, root), "no binary: always shown")
        (root / "usr/bin").mkdir(parents=True)
        (root / "usr/bin/firefox-esr").touch()
        self.assertTrue(apps.installed(firefox, root))

    def test_binary_must_be_absolute_and_survives_a_round_trip(self):
        app = apps.Application(id="web", name="WEB", kind="command", command="/bin/true", binary="/usr/bin/x",
                               status_id="web")
        apps.write_user_application(app, system_dir=SYSTEM, user_dir=self.user)
        self.assertEqual(next(item for item in self.load().applications if item.id == "web").binary, "/usr/bin/x")
        (self.user / "bad.ini").write_text(apps.serialize(dataclasses.replace(app, id="bad", status_id="bad"))
                                           .replace("/usr/bin/x", "usr/bin/x"))
        self.assertIn("bad.ini: binary must be an absolute path", self.load().errors)

    def test_duplicate_user_id_is_skipped(self):
        self.user.mkdir()
        (self.user / "duplicate.ini").write_text(
            apps.serialize(apps.Application(
                id="terminal", name="OTHER", kind="command", command="/bin/true",
                status_id="terminal",
            )), encoding="utf-8",
        )
        result = self.load()
        self.assertEqual([app.id for app in result.applications].count("terminal"), 1)
        self.assertTrue(any("duplicate" in error for error in result.errors))

    def test_invalid_manifests_are_isolated(self):
        self.user.mkdir()
        (self.user / "bad.ini").write_text("not ini", encoding="utf-8")
        result = self.load()
        self.assertIn("moonlight", {app.id for app in result.applications})
        self.assertEqual(len(result.errors), 1)

    def test_validation_rejects_invalid_id_relative_command_and_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "app.ini"
            for text, message in (
                ("[app]\nid = BAD\nname = Bad\nkind = command\ncommand = /bin/true\n", "id"),
                ("[app]\nid = bad\nname = Bad\nkind = command\ncommand = bin/true\n", "absolute"),
                ("[app]\nid = bad\nname = Bad\nkind = command\ncommand = /bin/true\n[environment]\nBAD-NAME=x\n", "environment"),
            ):
                path.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(apps.ManifestError, message):
                    apps.read_manifest(path)

    def test_user_manifests_cannot_request_services_or_shadow_fixed_controls(self):
        self.user.mkdir()
        for name, text in (
            ("request.ini", "[app]\nid = custom\nname = Custom\nkind = request\nrequest = start-moonlight\n"),
            ("reboot.ini", "[app]\nid = reboot\nname = Reboot\nkind = command\ncommand = /bin/true\n"),
        ):
            (self.user / name).write_text(text, encoding="utf-8")
        result = self.load()
        self.assertEqual(len(result.errors), 2)
        self.assertFalse({"custom", "reboot"} & {app.id for app in result.applications})

    def test_atomic_user_and_state_files_use_0640(self):
        app = apps.Application(
            id="custom", name="CUSTOM", kind="command", command="/bin/true",
            status_id="custom",
        )
        path = apps.write_user_application(app, system_dir=SYSTEM, user_dir=self.user)
        apps.write_state([app], self.state)
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o640)

    def test_validation_temp_file_is_not_a_loadable_manifest(self):
        app = apps.Application(
            id="custom", name="CUSTOM", kind="command", command="/bin/true",
            status_id="custom",
        )
        visible_to_loader = []
        real_read_manifest = apps.read_manifest

        def spy(path, **kwargs):
            if path.name.startswith(".validate-app."):
                visible_to_loader.append(sorted(item.name for item in self.user.glob("*.ini")))
            return real_read_manifest(path, **kwargs)

        with mock.patch.object(apps, "read_manifest", side_effect=spy):
            apps.write_user_application(app, system_dir=SYSTEM, user_dir=self.user)
        self.assertEqual(visible_to_loader, [[]])

    def test_leftover_validation_file_from_a_crash_is_not_loaded(self):
        app = apps.Application(
            id="ghost", name="GHOST", kind="command", command="/bin/true",
            status_id="ghost",
        )
        self.user.mkdir()
        (self.user / ".validate-app.abcd1234.ini").write_text(apps.serialize(app), encoding="utf-8")
        result = self.load()
        self.assertNotIn("ghost", {item.id for item in result.applications})
        self.assertEqual(result.errors, ())

    def test_web_url_validation(self):
        self.assertEqual(apps.validate_web_url("https://example.com/a?q=1"), "https://example.com/a?q=1")
        for value in (
            "file:///tmp/a", "javascript:alert(1)", "https://user:pass@example.com",
            "https://example.com\nnext", "https://example.com/$(id)", "https://example.com/`id`",
        ):
            with self.assertRaises(apps.ManifestError):
                apps.validate_web_url(value)

    def test_delete_user_app_and_reject_system_delete(self):
        app = apps.Application(
            id="custom", name="CUSTOM", kind="command", command="/bin/true",
            status_id="custom",
        )
        apps.write_user_application(app, system_dir=SYSTEM, user_dir=self.user)
        apps.delete_user_application("custom", system_dir=SYSTEM, user_dir=self.user)
        self.assertFalse((self.user / "custom.ini").exists())
        with self.assertRaisesRegex(apps.ManifestError, "system"):
            apps.delete_user_application("terminal", system_dir=SYSTEM, user_dir=self.user)


if __name__ == "__main__":
    unittest.main()
