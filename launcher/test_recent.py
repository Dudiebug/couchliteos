import testenv  # noqa: F401  (first: scratch run and state directories)
import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import couchliteos_recent as recent
import couchliteos_stream as stream


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        self.now += 1
        return self.now


class RecentStoreTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = pathlib.Path(directory.name) / "lib" / "recent.json"
        self.clock = Clock()

    def record(self, host, app):
        recent.record(host, app, path=self.path, now=self.clock)

    def test_nothing_played_yet_reads_as_empty(self):
        self.assertEqual(recent.load(self.path), {})

    def test_the_latest_game_comes_first_and_a_replay_moves_it_up(self):
        self.record("UUID-1", "Desktop")
        self.record("UUID-2", "Steam")
        self.record("UUID-1", "Hades")
        self.assertEqual(list(recent.load(self.path)), [("UUID-1", "Hades"), ("UUID-2", "Steam"), ("UUID-1", "Desktop")])
        self.record("UUID-1", "Desktop")
        history = recent.load(self.path)
        self.assertEqual(list(history), [("UUID-1", "Desktop"), ("UUID-1", "Hades"), ("UUID-2", "Steam")])
        self.assertEqual(history[("UUID-1", "Desktop")], self.clock.now)

    def test_the_same_app_on_two_pcs_is_two_games(self):
        self.record("UUID-1", "Desktop")
        self.record("UUID-2", "Desktop")
        self.assertEqual(len(recent.load(self.path)), 2)

    def test_the_list_is_capped(self):
        for number in range(recent.LIMIT + 25):
            self.record("UUID-1", f"Game {number}")
        history = recent.load(self.path)
        self.assertEqual(len(history), recent.LIMIT)
        self.assertEqual(next(iter(history)), ("UUID-1", f"Game {recent.LIMIT + 24}"))
        self.assertNotIn(("UUID-1", "Game 0"), history)
        self.assertEqual(len(json.loads(self.path.read_text())["games"]), recent.LIMIT)

    def test_the_file_is_written_atomically_with_no_leftovers(self):
        self.record("UUID-1", "Desktop")
        with mock.patch.object(stream.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.record("UUID-1", "Hades")
        self.assertEqual(list(recent.load(self.path)), [("UUID-1", "Desktop")])  # the old file is intact
        self.assertEqual([item.name for item in self.path.parent.iterdir()], ["recent.json"])

    def test_unusable_names_are_refused(self):
        for host, app in (("", "Desktop"), ("UUID-1", ""), ("UUID-1", "a\nb"), ("UUID-1", "x" * 129), (None, "x")):
            with self.assertRaises(ValueError):
                self.record(host, app)
        self.assertFalse(self.path.exists())

    def test_a_damaged_file_reads_as_empty_and_is_replaced_on_the_next_game(self):
        self.path.parent.mkdir(parents=True)
        for text in ("{", "[]", '{"games": 3}', "\xff", ""):
            self.path.write_text(text, encoding="latin-1")
            self.assertEqual(recent.load(self.path), {}, text)
        self.record("UUID-1", "Desktop")
        self.assertEqual(list(recent.load(self.path)), [("UUID-1", "Desktop")])

    def test_bad_entries_are_skipped_and_the_rest_kept(self):
        self.path.parent.mkdir(parents=True)
        games = [
            {"host": "UUID-1", "app": "Old", "played": 5},
            "junk",
            {"host": "UUID-1", "app": "No time"},
            {"host": "UUID-1", "app": "Bool", "played": True},
            {"host": "UUID-1", "app": "Inf", "played": float("inf")},
            {"host": 7, "app": "Number host", "played": 9},
            {"host": "UUID-1", "app": "New", "played": 10},
            {"host": "UUID-1", "app": "New", "played": 1},  # a duplicate: the first one counts
        ]
        self.path.write_text(json.dumps({"version": 1, "games": games}))
        self.assertEqual(recent.load(self.path), {("UUID-1", "New"): 10.0, ("UUID-1", "Old"): 5.0})

    def test_an_oversized_file_or_a_symlink_is_not_read(self):
        self.path.parent.mkdir(parents=True)
        target = self.path.parent / "elsewhere.json"
        target.write_text(json.dumps({"games": [{"host": "U", "app": "A", "played": 1}]}))
        self.path.symlink_to(target)
        self.assertEqual(recent.load(self.path), {})
        self.path.unlink()
        self.path.write_text(" " * (recent.MAX_BYTES + 1))
        self.assertEqual(recent.load(self.path), {})

    def test_host_key_is_the_uuid_else_the_name(self):
        self.assertEqual(recent.host_key(stream.Host(name="PC", uuid="UUID-1")), "UUID-1")
        self.assertEqual(recent.host_key(stream.Host(name="PC")), "PC")


class OrderedTest(unittest.TestCase):
    GAMES = [("A", "Desktop"), ("A", "Steam"), ("B", "Desktop"), ("B", "Hades")]

    def test_played_games_come_first_newest_first_and_the_rest_keep_their_order(self):
        history = {("B", "Hades"): 30.0, ("A", "Steam"): 20.0, ("gone", "Old"): 10.0}
        self.assertEqual(
            recent.ordered(self.GAMES, history=history),
            [("B", "Hades"), ("A", "Steam"), ("A", "Desktop"), ("B", "Desktop")],
        )

    def test_nothing_played_keeps_the_given_order(self):
        self.assertEqual(recent.ordered(self.GAMES, history={}), self.GAMES)

    def test_a_key_function_orders_any_objects(self):
        games = [{"host": host, "app": app} for host, app in self.GAMES]
        result = recent.ordered(games, key=lambda game: (game["host"], game["app"]), history={("B", "Desktop"): 1.0})
        self.assertEqual(result[0], {"host": "B", "app": "Desktop"})

    def test_it_reads_the_file_when_no_history_is_given(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "recent.json"
            recent.record("B", "Desktop", path=path)
            self.assertEqual(recent.ordered(self.GAMES, path=path)[0], ("B", "Desktop"))


class LauncherRecordsStreamsTest(unittest.TestCase):
    """Launcher.start_stream records the game only once Moonlight has really started."""

    HOST = stream.Host(name="Gaming-PC", uuid="UUID-1", local="192.168.1.50", apps=("Desktop", "Hades"))

    @classmethod
    def setUpClass(cls):
        path = pathlib.Path(__file__).with_name("couchliteos-launcher.py")
        spec = importlib.util.spec_from_file_location("launcher_recent", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def start(self, started=True, record=None):
        module = self.module
        moonlight = module.apps.Application(id="moonlight", name="MOONLIGHT", kind="request", request="start-moonlight", status_id="moonlight")
        launcher = module.Launcher.__new__(module.Launcher)  # no screen or monitors: only start_stream runs
        launcher.app_by_id = mock.Mock(return_value=moonlight)
        launcher.launch_app = mock.Mock(return_value=started)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(module, "RUN", pathlib.Path(directory)), \
                mock.patch.object(module.recent, "record", record or mock.Mock()) as recorded:
            result = launcher.start_stream(self.HOST, "Hades")
        return result, recorded

    def test_a_started_stream_is_recorded_by_the_pc_uuid(self):
        result, recorded = self.start()
        self.assertTrue(result)
        recorded.assert_called_once_with("UUID-1", "Hades")

    def test_a_stream_that_did_not_start_is_not_recorded(self):
        result, recorded = self.start(started=False)
        self.assertFalse(result)
        recorded.assert_not_called()

    def test_a_failed_save_does_not_fail_the_stream(self):
        for error in (OSError("read-only"), ValueError("bad name")):
            result, _recorded = self.start(record=mock.Mock(side_effect=error))
            self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
