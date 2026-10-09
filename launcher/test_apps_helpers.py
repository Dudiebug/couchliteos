"""couchliteos_apps: environment parsing, id generation, state overrides and summaries."""

import testenv  # noqa: F401  (first: scratch run and state directories)
import pathlib
import tempfile
import time
import unittest
from unittest import mock

import couchliteos_apps as apps


def request_manifest(app_id, name, order, *, enabled=True, visible=True):
    return (
        "[app]\n"
        f"id = {app_id}\nname = {name}\nkind = request\nrequest = start-{app_id}\n"
        f"enabled = {str(enabled).lower()}\nvisible = {str(visible).lower()}\norder = {order}\n"
    )


class ParseEnvironmentTest(unittest.TestCase):
    def test_empty_or_blank_is_no_environment(self):
        self.assertEqual(apps.parse_environment(""), {})
        self.assertEqual(apps.parse_environment("   "), {})

    def test_pairs_are_split_on_semicolons_and_the_first_equals(self):
        self.assertEqual(
            apps.parse_environment("A=1;_B2=x=y;EMPTY="),
            {"A": "1", "_B2": "x=y", "EMPTY": ""},
        )

    def test_later_duplicates_win(self):
        self.assertEqual(apps.parse_environment("A=1;A=2"), {"A": "2"})

    def test_item_without_equals_is_rejected(self):
        with self.assertRaisesRegex(apps.ManifestError, "KEY=value"):
            apps.parse_environment("A=1;B")

    def test_invalid_names_are_rejected(self):
        for text in ("1A=x", "A-B=x", "=x", "A B=x", "Ä=x"):
            with self.assertRaisesRegex(apps.ManifestError, "invalid environment name"):
                apps.parse_environment(text)

    def test_values_are_bounded_and_single_line(self):
        self.assertEqual(len(apps.parse_environment("A=" + "x" * apps.MAX_ENV_VALUE)["A"]), apps.MAX_ENV_VALUE)
        with self.assertRaisesRegex(apps.ManifestError, "too long"):
            apps.parse_environment("A=" + "x" * (apps.MAX_ENV_VALUE + 1))
        for value in ("a\nb", "a\rb", "a\0b"):
            with self.assertRaisesRegex(apps.ManifestError, "invalid character"):
                apps.parse_environment("A=" + value)


class ApplicationIdTest(unittest.TestCase):
    def test_name_is_slugged(self):
        self.assertEqual(apps.application_id("My Cool App!", set()), "my-cool-app")
        self.assertEqual(apps.application_id("  --Retro  Arch-- ", set()), "retro-arch")

    def test_unusable_names_fall_back(self):
        self.assertEqual(apps.application_id("", set()), "custom-app")
        self.assertEqual(apps.application_id("!!!", set()), "custom-app")

    def test_collisions_and_reserved_ids_get_numbered(self):
        self.assertEqual(apps.application_id("Settings", set()), "settings-2")
        self.assertEqual(apps.application_id("Game", {"game", "game-2"}), "game-3")

    def test_ids_always_fit_the_manifest_rules(self):
        long_name = "A" * 40
        first = apps.application_id(long_name, set())
        self.assertEqual(first, "a" * 32)
        second = apps.application_id(long_name, {first})
        self.assertEqual(second, "a" * 30 + "-2")
        names = ["x" * 31 + " y", "Ünïcode Gäme", "Game 2", "9lives", "-" * 50 + "z"]
        for name in names:
            candidate = apps.application_id(name, {"game-2"})
            self.assertRegex(candidate, apps.ID_RE, name)
            self.assertNotIn(candidate, apps.RESERVED_IDS)

    def test_many_collisions_stay_within_32_characters(self):
        existing = set()
        for _ in range(12):
            existing.add(apps.application_id("B" * 40, existing))
        self.assertEqual(len(existing), 12)
        self.assertTrue(all(len(item) <= 32 and apps.ID_RE.fullmatch(item) for item in existing))
        self.assertIn("b" * 29 + "-12", existing)


class StateOverrideTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = pathlib.Path(directory.name)
        self.system = root / "system"
        self.system.mkdir()
        self.user = root / "user"
        self.state = root / "state.ini"
        (self.system / "10-alpha.ini").write_text(request_manifest("alpha", "ALPHA", 10))
        (self.system / "20-beta.ini").write_text(request_manifest("beta", "BETA", 20))
        (self.system / "30-hidden.ini").write_text(request_manifest("hidden", "HIDDEN", 30, visible=False))

    def load(self):
        return apps.load_applications(self.system, self.user, self.state)

    def test_missing_user_directory_and_state_are_fine(self):
        result = self.load()
        self.assertEqual([app.id for app in result.applications], ["alpha", "beta", "hidden"])
        self.assertEqual(result.errors, ())

    def test_state_reorders_and_disables(self):
        self.state.write_text("[alpha]\norder = 50\n\n[beta]\nenabled = no\n")
        result = self.load()
        self.assertEqual([app.id for app in result.applications], ["beta", "hidden", "alpha"])
        self.assertFalse(next(app for app in result.applications if app.id == "beta").enabled)
        self.assertEqual(result.errors, ())

    def test_ties_sort_by_name_then_id(self):
        self.state.write_text("[alpha]\norder = 20\n")
        self.assertEqual([app.id for app in self.load().applications][:2], ["alpha", "beta"])

    def test_invalid_override_value_is_reported_and_the_manifest_value_kept(self):
        for text in ("[alpha]\nenabled = maybe\n", "[alpha]\norder = soon\n", "[alpha]\norder = 10001\n"):
            self.state.write_text(text)
            result = self.load()
            alpha = next(app for app in result.applications if app.id == "alpha")
            self.assertEqual((alpha.enabled, alpha.order), (True, 10), text)
            self.assertEqual(result.errors, (f"{self.state.name}: invalid override for alpha",), text)

    def test_unsupported_state_file_is_ignored_entirely(self):
        for text in ("[alpha]\ncommand = /bin/sh\n", "[Bad Id]\norder = 1\n", "not ini", "[a]\nx=1\n[a]\nx=2\n"):
            self.state.write_text(text)
            result = self.load()
            self.assertEqual([app.order for app in result.applications], [10, 20, 30], text)
            self.assertEqual(len(result.errors), 1, text)
            self.assertTrue(result.errors[0].startswith(self.state.name + ": "), text)

    def test_state_for_unknown_applications_is_ignored(self):
        self.state.write_text("[gone]\norder = 1\n")
        result = self.load()
        self.assertEqual(result.errors, ())
        self.assertEqual(len(result.applications), 3)

    def test_visible_applications_hides_invisible_and_disabled(self):
        self.state.write_text("[beta]\nenabled = false\n")
        result = apps.visible_applications(system_dir=self.system, user_dir=self.user, state_file=self.state)
        self.assertEqual([app.id for app in result.applications], ["alpha"])

    def test_visible_applications_keeps_the_errors(self):
        (self.system / "40-broken.ini").write_text("[app]\nid = Bad\n")
        result = apps.visible_applications(system_dir=self.system, user_dir=self.user, state_file=self.state)
        self.assertEqual([app.id for app in result.applications], ["alpha", "beta"])
        self.assertEqual(len(result.errors), 1)
        self.assertTrue(result.errors[0].startswith("40-broken.ini: "))


class SummaryTest(unittest.TestCase):
    def test_summary_lists_state_without_names_or_commands(self):
        present = pathlib.Path(__file__).resolve()
        result = apps.LoadResult(
            (
                apps.Application("alpha", "SECRET NAME", "request", request="start-alpha", order=10),
                apps.Application("tool", "TOOL", "command", command=str(present), arguments="--token=hunter2", enabled=False),
                apps.Application("gone", "GONE", "command", command=str(present.with_name("does-not-exist"))),
            ),
            ("bad.ini: invalid", "other.ini: invalid"),
        )
        summary = apps.sanitized_summary(result)
        self.assertEqual(
            summary.splitlines(),
            [
                "alpha: enabled=yes order=10 kind=request command_exists=n/a",
                "tool: enabled=no order=60 kind=command command_exists=yes",
                "gone: enabled=yes order=60 kind=command command_exists=no",
                "invalid manifests skipped: 2",
            ],
        )
        self.assertNotIn("SECRET", summary)
        self.assertNotIn("hunter2", summary)

    def test_empty_summary(self):
        self.assertEqual(apps.sanitized_summary(apps.LoadResult((), ())), "")


class CachedApplicationsTest(unittest.TestCase):
    """The home screen asks every second and on every key: parsing only after a change."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = pathlib.Path(directory.name)
        self.system = root / "system"
        self.system.mkdir()
        self.user = root / "user"
        self.state = root / "state.ini"
        (self.system / "10-alpha.ini").write_text(request_manifest("alpha", "ALPHA", 10))
        self.addCleanup(apps._LOADED.clear)
        self.now = time.time() + 60  # every change has settled

    def load(self):
        return apps.cached_applications(self.system, self.user, self.state, clock=lambda: self.now)

    def ids(self):
        return [app.id for app in self.load().applications]

    def test_nothing_changed_is_not_parsed_again(self):
        first = self.load()
        with mock.patch.object(apps, "load_applications") as load:
            self.assertIs(self.load(), first)
            self.assertIs(self.load(), first)
        load.assert_not_called()

    def test_a_new_manifest_a_user_app_or_a_state_change_is_read(self):
        self.assertEqual(self.ids(), ["alpha"])
        (self.system / "20-beta.ini").write_text(request_manifest("beta", "BETA", 20))
        self.assertEqual(self.ids(), ["alpha", "beta"])
        before = self.load()
        self.user.mkdir()
        self.assertIsNot(self.load(), before)
        before = self.load()
        (self.user / "gamma.ini").write_text("[app]\nid = gamma\n")  # read again (and refused)
        self.assertIsNot(self.load(), before)
        self.assertIs(self.load(), self.load())
        self.state.write_text("[alpha]\norder = 90\n")
        self.assertEqual(self.ids(), ["beta", "alpha"])
        self.state.unlink()
        self.assertEqual(self.ids(), ["alpha", "beta"])

    def test_a_replaced_manifest_is_read(self):
        self.assertEqual(self.load().applications[0].name, "ALPHA")
        temporary = self.system / ".10-alpha.ini.tmp"
        temporary.write_text(request_manifest("alpha", "RENAMED", 10))
        temporary.replace(self.system / "10-alpha.ini")  # as atomic_write does
        self.assertEqual(self.load().applications[0].name, "RENAMED")

    def test_a_change_that_has_not_settled_is_parsed_every_time(self):
        self.now = time.time()  # the files were written just now
        self.load()
        with mock.patch.object(apps, "load_applications", wraps=apps.load_applications) as load:
            self.load()
            self.load()
        self.assertEqual(load.call_count, 2)

    def test_directories_are_cached_separately(self):
        other = self.system.parent / "other"
        other.mkdir()
        self.assertEqual(self.ids(), ["alpha"])
        result = apps.cached_applications(other, self.user, self.state, clock=lambda: self.now)
        self.assertEqual(result.applications, ())


class EnvironmentSpacingTest(unittest.TestCase):
    def test_spaces_after_separators_and_a_trailing_separator_are_accepted(self):
        self.assertEqual(apps.parse_environment("A=1; B=2;"), {"A": "1", "B": "2"})
        self.assertEqual(apps.parse_environment(" ; A=1 ;B=2"), {"A": "1 ", "B": "2"})

    def test_a_missing_value_is_still_refused(self):
        with self.assertRaises(apps.ManifestError):
            apps.parse_environment("A=1; B")


if __name__ == "__main__":
    unittest.main()
