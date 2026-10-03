import testenv  # noqa: F401  (first: scratch run and state directories)
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

import couchliteos_apps as apps
import couchliteos_home as home
import couchliteos_recent as recent
import couchliteos_stream as stream

PC = stream.Host(name="Gaming-PC", uuid="UUID-1", apps=("Desktop", "Steam Big Picture", "Hades"))
LAPTOP = stream.Host(name="laptop", uuid="UUID-2", apps=("Desktop",))


def app(app_id, kind="request", **fields):
    return apps.Application(id=app_id, name=app_id.upper(), kind=kind, **fields)


APPS = (
    app("moonlight", request="start-moonlight"),
    app("chiaki-ng", request="start-chiaki"),
    app("firefox", request="start-firefox"),
    app("terminal", kind="command", command="/bin/bash"),
    app("work-pc", kind="rdp", connection="work-pc"),
)


class Monitors:
    def __init__(self, line="", low=(), available=""):
        self._line, self._low, self._available = line, low, available

    def line(self):
        return self._line

    def low(self):
        return list(self._low)

    def available(self):
        return self._available


class HomeTestCase(unittest.TestCase):
    def model(self, hosts=(PC, LAPTOP), applications=APPS, history=None, missing=(), **kwargs):
        self.world = {"hosts": list(hosts), "apps": tuple(applications), "history": dict(history or {})}
        return home.HomeModel(
            hosts=lambda: list(self.world["hosts"]),
            applications=lambda: apps.LoadResult(self.world["apps"], ()),
            installed=lambda item: item.id not in missing,
            history=lambda: dict(self.world["history"]),
            **kwargs,
        )

    @staticmethod
    def labels(model, row):
        return [tile.label for tile in model.rows[row].tiles]


class RowsTest(HomeTestCase):
    def test_three_rows_top_to_bottom(self):
        model = self.model()
        self.assertEqual([row.name for row in model.rows], ["games", "apps", "system"])
        self.assertEqual([row.title for row in model.rows], ["GAMES", "APPS", "SYSTEM"])
        self.assertEqual(self.labels(model, 2), ["SETTINGS", "HOSTS", "POWER"])

    def test_games_are_every_pcs_apps_in_host_order_when_nothing_was_played(self):
        tiles = self.model().rows[0].tiles
        self.assertEqual(
            [(tile.label, tile.detail) for tile in tiles],
            [("DESKTOP", "GAMING-PC"), ("STEAM BIG PICTURE", "GAMING-PC"), ("HADES", "GAMING-PC"), ("DESKTOP", "LAPTOP")],
        )
        self.assertTrue(all(tile.played is None for tile in tiles))

    def test_games_are_ordered_most_recently_played_first(self):
        history = {("UUID-2", "Desktop"): 300.0, ("UUID-1", "Hades"): 200.0, ("UUID-9", "Gone"): 100.0}
        tiles = self.model(history=history).rows[0].tiles
        self.assertEqual(
            [(tile.label, tile.detail, tile.played) for tile in tiles],
            [
                ("DESKTOP", "LAPTOP", 300.0), ("HADES", "GAMING-PC", 200.0),
                ("DESKTOP", "GAMING-PC", None), ("STEAM BIG PICTURE", "GAMING-PC", None),
            ],
        )

    def test_a_pc_without_a_uuid_is_known_by_its_name(self):
        nameonly = stream.Host(name="Old-PC", apps=("Desktop",))
        tiles = self.model(hosts=(PC, nameonly), history={("Old-PC", "Desktop"): 5.0}).rows[0].tiles
        self.assertEqual((tiles[0].detail, tiles[0].key), ("OLD-PC", "game:Old-PC/Desktop"))

    def test_an_app_listed_twice_by_one_pc_is_one_tile(self):
        twice = stream.Host(name="PC", uuid="U", apps=("Desktop", "Desktop"))
        self.assertEqual(self.labels(self.model(hosts=(twice,)), 0), ["DESKTOP"])

    def test_with_no_pc_paired_the_games_row_offers_pairing(self):
        model = self.model(hosts=())
        self.assertEqual(self.labels(model, 0), [home.NO_GAMES])
        self.assertEqual(model.activate(), ("hosts",))

    def test_unreadable_hosts_or_history_still_give_a_home_screen(self):
        model = home.HomeModel(
            hosts=mock.Mock(side_effect=OSError("gone")),
            applications=lambda: apps.LoadResult(APPS, ()),
            installed=lambda _item: True,
            history=mock.Mock(side_effect=OSError("gone")),
        )
        self.assertEqual(self.labels(model, 0), [home.NO_GAMES])

    def test_apps_are_the_installed_ones_without_moonlight(self):
        model = self.model()
        self.assertEqual(self.labels(model, 1), ["CHIAKI-NG", "FIREFOX", "TERMINAL", "WORK-PC"])

    def test_an_app_that_is_not_installed_is_hidden(self):
        model = self.model(missing={"firefox", "chiaki-ng"})
        self.assertEqual(self.labels(model, 1), ["TERMINAL", "WORK-PC"])

    def test_disabled_or_invisible_apps_are_hidden(self):
        applications = APPS + (app("off", enabled=False), app("hidden", visible=False))
        self.assertNotIn("OFF", self.labels(self.model(applications=applications), 1))
        self.assertNotIn("HIDDEN", self.labels(self.model(applications=applications), 1))

    def test_load_errors_are_kept_for_the_front_end(self):
        model = home.HomeModel(
            hosts=lambda: [], applications=lambda: apps.LoadResult(APPS, ("bad.ini: invalid INI",)),
            installed=lambda _item: True, history=dict,
        )
        self.assertEqual(model.errors, ("bad.ini: invalid INI",))

    def test_an_installed_check_that_fails_hides_the_tile(self):
        model = home.HomeModel(
            hosts=lambda: [], applications=lambda: apps.LoadResult(APPS, ()),
            installed=mock.Mock(side_effect=OSError("io")), history=dict,
        )
        self.assertEqual(model.rows[1].tiles, ())


class InstalledTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name)

    def binary(self, relative, mode=0o755):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
        os.chmod(path, mode)

    def test_command_apps_need_their_program(self):
        chrome = app("google-chrome", kind="command", command="/usr/bin/google-chrome-stable")
        self.assertFalse(home.installed(chrome, self.root))
        self.binary("usr/bin/google-chrome-stable")
        self.assertTrue(home.installed(chrome, self.root))

    @unittest.skipIf(os.geteuid() == 0, "root may execute anything")
    def test_a_program_that_is_not_executable_does_not_count(self):
        self.binary("usr/bin/tool", mode=0o644)
        self.assertFalse(home.installed(app("tool", kind="command", command="/usr/bin/tool"), self.root))

    def test_request_apps_need_the_program_couchliteos_run_app_starts(self):
        firefox = app("firefox", request="start-firefox")
        chiaki = app("chiaki-ng", request="start-chiaki")
        self.assertFalse(home.installed(firefox, self.root))
        self.assertFalse(home.installed(chiaki, self.root))
        self.binary("usr/bin/firefox-esr")
        self.binary("opt/couchliteos/apps/chiaki-ng/usr/bin/chiaki")
        self.assertTrue(home.installed(firefox, self.root))
        self.assertTrue(home.installed(chiaki, self.root))

    def test_remote_desktop_and_unknown_requests_always_show(self):
        self.assertTrue(home.installed(app("work-pc", kind="rdp", connection="work-pc"), self.root))
        self.assertTrue(home.installed(app("other", request="start-other"), self.root))

    def test_the_table_matches_couchliteos_run_app(self):
        script = (pathlib.Path(__file__).resolve().parents[1] / "scripts/couchliteos-run-app").read_text()
        for request, binary in home.REQUEST_BINARIES.items():
            self.assertIn(f"request=$RUN_DIR/{request}", script)
            self.assertIn(binary.rsplit("/", 1)[1], script)


class FocusTest(HomeTestCase):
    def test_it_starts_on_the_first_game(self):
        model = self.model()
        self.assertEqual(model.focus, (0, 0))
        self.assertEqual(model.focused().label, "DESKTOP")

    def test_left_and_right_stop_at_the_ends(self):
        model = self.model()
        self.assertFalse(model.move(-1, 0))
        self.assertEqual(model.focus, (0, 0))
        for _ in range(3):
            self.assertTrue(model.move(1, 0))
        self.assertEqual(model.focus, (0, 3))
        self.assertFalse(model.move(1, 0))
        self.assertEqual(model.focus, (0, 3))

    def test_up_and_down_stop_at_the_first_and_last_row(self):
        model = self.model()
        self.assertFalse(model.move(0, -1))
        self.assertTrue(model.move(0, 1))
        self.assertTrue(model.move(0, 1))
        self.assertEqual(model.focus[0], 2)
        self.assertFalse(model.move(0, 1))
        self.assertEqual(model.focused().label, "SETTINGS")

    def test_large_steps_move_one_tile(self):
        model = self.model()
        model.move(5, 0)
        self.assertEqual(model.focus, (0, 1))
        model.move(0, 9)
        self.assertEqual(model.focus[0], 1)

    def test_each_row_remembers_its_column(self):
        model = self.model()
        model.move(1, 0)
        model.move(1, 0)  # HADES
        model.move(0, 1)  # APPS starts at its own first tile
        self.assertEqual(model.focus, (1, 0))
        model.move(1, 0)
        model.move(0, -1)
        self.assertEqual(model.focused().label, "HADES")
        model.move(0, 1)
        self.assertEqual(model.focused().label, "FIREFOX")

    def test_a_column_past_the_end_of_a_shorter_row_is_clamped(self):
        model = self.model()
        model.move(0, 1)
        for _ in range(3):
            model.move(1, 0)
        self.assertEqual(model.focused().label, "WORK-PC")
        model.move(0, 1)  # SYSTEM has 3 tiles and its own column
        self.assertEqual(model.focus, (2, 0))

    def test_an_empty_apps_row_is_skipped(self):
        model = self.model(applications=(APPS[0],))  # only Moonlight, which is never an app tile
        self.assertEqual(model.rows[1].tiles, ())
        self.assertTrue(model.move(0, 1))
        self.assertEqual(model.focus[0], 2)
        self.assertTrue(model.move(0, -1))
        self.assertEqual(model.focus[0], 0)

    def test_back_returns_to_the_first_game(self):
        model = self.model()
        model.move(0, 1)
        model.move(1, 0)
        self.assertTrue(model.back())
        self.assertEqual(model.focus, (0, 0))
        self.assertFalse(model.back())

    def test_focus_key_and_focus_game(self):
        model = self.model()
        self.assertTrue(model.focus_key("system:power"))
        self.assertEqual(model.activate(), ("power",))
        self.assertTrue(model.focus_game("UUID-2", "Desktop"))
        self.assertEqual(model.focused().detail, "LAPTOP")
        self.assertFalse(model.focus_key("app:nothing"))
        self.assertEqual(model.focused().detail, "LAPTOP")


class ActionsTest(HomeTestCase):
    def test_a_game_streams_that_app_from_its_pc(self):
        model = self.model()
        model.move(1, 0)
        self.assertEqual(model.activate(), ("stream", PC, "Steam Big Picture"))

    def test_an_app_starts_by_id(self):
        model = self.model()
        model.move(0, 1)
        self.assertEqual(model.activate(), ("app", "chiaki-ng"))

    def test_the_system_row(self):
        model = self.model()
        model.move(0, 1)
        model.move(0, 1)
        actions = []
        for _ in range(3):
            actions.append(model.activate())
            model.move(1, 0)
        self.assertEqual(actions, [("settings",), ("hosts",), ("power",)])


class ReturnToLastGameTest(HomeTestCase):
    def test_a_boot_lands_on_the_last_game_played(self):
        model = self.model(history={("UUID-1", "Hades"): 50.0})
        self.assertEqual(model.focus, (0, 0))
        self.assertEqual(model.activate(), ("stream", PC, "Hades"))
        self.assertTrue(model.focus_last_played())

    def test_focus_last_played_says_when_nothing_was_played(self):
        model = self.model()
        self.assertFalse(model.focus_last_played())
        self.assertEqual(model.focus, (0, 0))

    def test_after_a_stream_the_played_game_is_focused_and_first(self):
        model = self.model()
        model.move(1, 0)
        model.move(1, 0)  # HADES, third
        self.assertEqual(model.activate(), ("stream", PC, "Hades"))
        self.world["history"] = {("UUID-1", "Hades"): 99.0}  # what Launcher.start_stream records
        model.reload()
        self.assertEqual(model.focus, (0, 0))
        self.assertEqual(model.focused().label, "HADES")
        self.assertEqual(model.focused().played, 99.0)

    def test_a_stream_started_elsewhere_is_focused_by_focus_last_played(self):
        model = self.model()
        model.move(0, 1)
        model.move(0, 1)  # in SYSTEM; the auto-stream (not the home screen) starts a stream
        self.world["history"] = {("UUID-2", "Desktop"): 10.0}
        model.reload()
        self.assertEqual(model.focused().label, "SETTINGS")  # a reload alone leaves the focus alone
        self.assertTrue(model.focus_last_played())
        self.assertEqual((model.focused().label, model.focused().detail), ("DESKTOP", "LAPTOP"))

    def test_with_the_real_store(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "recent.json"
            model = home.HomeModel(
                hosts=lambda: [PC, LAPTOP], applications=lambda: apps.LoadResult(APPS, ()),
                installed=lambda _item: True, history=lambda: recent.load(path),
            )
            recent.record(recent.host_key(LAPTOP), "Desktop", path=path)
            model.reload()
            self.assertTrue(model.focus_last_played())
            self.assertEqual(model.activate(), ("stream", LAPTOP, "Desktop"))


class ReloadTest(HomeTestCase):
    def test_the_focused_tile_stays_focused_when_tiles_move(self):
        model = self.model()
        model.move(0, 1)
        model.move(1, 0)  # FIREFOX
        self.world["apps"] = (app("new", kind="rdp", connection="new"),) + APPS
        model.reload()
        self.assertEqual(model.focused().label, "FIREFOX")
        self.assertEqual(model.focus, (1, 2))

    def test_a_removed_tile_leaves_the_focus_in_its_row(self):
        model = self.model()
        model.move(0, 1)
        for _ in range(3):
            model.move(1, 0)  # WORK-PC, the last app
        self.world["apps"] = APPS[:4]
        model.reload()
        self.assertEqual(model.focused().label, "TERMINAL")

    def test_an_emptied_row_moves_the_focus_to_the_nearest_row(self):
        model = self.model()
        model.move(0, 1)
        self.world["apps"] = ()
        model.reload()
        self.assertEqual(model.focus[0], 0)  # GAMES is as near as SYSTEM, and above it

    def test_unpairing_every_pc_leaves_the_pairing_tile(self):
        model = self.model()
        model.move(1, 0)
        self.world["hosts"] = []
        model.reload()
        self.assertEqual(model.focus, (0, 0))
        self.assertEqual(model.activate(), ("hosts",))


class StatusTest(HomeTestCase):
    NOON = time.struct_time((2026, 10, 3, 12, 5, 0, 5, 276, 0))

    def test_the_top_bar_comes_from_the_monitors(self):
        monitors = Monitors(line="CONTROLLER 80%", low=["pad"], available="0.3.1")
        model = self.model(controllers=monitors, updates=monitors, network=lambda: "ONLINE", clock=lambda: self.NOON)
        self.assertEqual(
            model.status(),
            home.Status(clock="12:05", network="ONLINE", battery="CONTROLLER 80%", battery_low=True, update="0.3.1"),
        )

    def test_without_monitors_the_fields_are_empty(self):
        status = self.model(network=lambda: "OFFLINE", clock=lambda: self.NOON).status()
        self.assertEqual((status.battery, status.battery_low, status.update), ("", False, ""))
        self.assertEqual(status.network, "OFFLINE")

    def test_a_failing_source_blanks_only_its_field(self):
        broken = mock.Mock()
        broken.line.side_effect = RuntimeError("boom")
        broken.low.side_effect = RuntimeError("boom")
        broken.available.side_effect = RuntimeError("boom")
        model = self.model(
            controllers=broken, updates=broken, network=mock.Mock(side_effect=OSError("x")), clock=lambda: self.NOON,
        )
        self.assertEqual(model.status(), home.Status("12:05", "", "", False, ""))

    def test_the_default_network_source_reads_the_link(self):
        model = self.model(clock=lambda: self.NOON)
        with mock.patch.object(home.stream, "link_up", return_value=True):
            self.assertEqual(model.status().network, "ONLINE")
        with mock.patch.object(home.stream, "link_up", return_value=False):
            self.assertEqual(model.status().network, "OFFLINE")


if __name__ == "__main__":
    unittest.main()
