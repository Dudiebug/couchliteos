import testenv  # noqa: F401  (first: scratch run and state directories)
import importlib.util
import pathlib
import tempfile
import unittest

import couchliteos_music as music


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class Player:
    def __init__(self, log, alive=True):
        self.log, self.alive = log, alive
        self.lines = []
        log.append(self)

    def send(self, line):
        if not self.alive:
            return False
        self.lines.append(line)
        return True

    def close(self, wait=True):
        self.alive = False
        self.waited = wait

    def exit_code(self):
        return None if self.alive else 1


class MusicTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.players = []
        self.on, self.volume = True, 30
        self.track = pathlib.Path("/usr/share/couchliteos/music/ambient.ogg")
        self.logged = []

    def music(self, **kwargs):
        options = dict(
            enabled=lambda: self.on, volume=lambda: self.volume, track=lambda: self.track,
            player=lambda: Player(self.players), clock=self.clock, log=self.logged.append,
        )
        options.update(kwargs)
        return music.Music(**options)

    @property
    def lines(self):
        return [line for player in self.players for line in player.lines]


class MusicTest(MusicTestCase):
    def test_nothing_plays_before_the_first_frame(self):
        player = self.music()
        self.assertEqual(self.players, [])
        self.assertFalse(player.playing)

    def test_the_first_frame_loads_the_track_and_fades_in(self):
        player = self.music()
        player.start()
        self.assertEqual(self.lines, [f"play {self.track}", "level 0.3 2.5"])
        self.assertTrue(player.playing)

    def test_an_application_fades_it_out_and_closing_it_fades_back_in(self):
        player = self.music()
        player.start()
        player.hold(music.APP)
        player.hold(music.APP)
        self.assertEqual(self.lines[-1], "level 0 0.8")
        self.assertFalse(player.playing)
        player.release(music.APP)
        self.assertEqual(self.lines[-1], "level 0.3 2.5")
        self.assertEqual(len(self.players), 1)  # one player: it resumes where it paused

    def test_every_hold_must_go_before_it_plays_again(self):
        player = self.music()
        player.start()
        player.hold(music.APP)
        player.hold(music.BLANK)
        player.release(music.APP)
        self.assertFalse(player.playing)
        player.release(music.BLANK)
        self.assertTrue(player.playing)

    def test_held_from_the_start_it_never_starts_a_player(self):
        player = self.music()
        player.hold(music.APP)
        player.start()
        self.assertEqual(self.players, [])

    def test_bigger_sounds_duck_it_and_tick_brings_it_back(self):
        player = self.music()
        player.start()
        self.assertIsNone(player.sound("move"))
        self.assertEqual(player.sound("select"), music.DUCK_HOLD)
        self.assertEqual(self.lines[-1], "level 0.12 0.06")
        self.clock.now += music.DUCK_HOLD
        player.tick()
        self.assertEqual(self.lines[-1], "level 0.3 0.5")

    def test_a_silent_player_is_not_ducked(self):
        player = self.music()
        self.assertIsNone(player.sound("select"))

    def test_settings_turn_it_off_and_change_the_volume(self):
        player = self.music()
        player.start()
        self.volume = 60
        player.settings()
        self.assertEqual(self.lines[-1], "level 0.6 0.3")
        self.on = False
        player.settings()
        self.assertEqual(self.lines[-1], "level 0 0.8")

    def test_off_in_settings_never_starts_a_player(self):
        self.on = False
        player = self.music()
        player.start()
        self.assertEqual(self.players, [])

    def test_no_track_means_silence(self):
        self.track = None
        player = self.music()
        player.start()
        self.assertEqual(self.players, [])
        self.assertTrue(player.dead)

    def test_a_dead_player_is_started_again_with_a_fade(self):
        player = self.music()
        player.start()
        self.players[0].alive = False
        player.hold(music.APP)
        player.release(music.APP)
        self.assertEqual(len(self.players), 2)
        self.assertEqual(self.players[1].lines, [f"play {self.track}", "level 0.3 2.5"])

    def test_a_player_that_keeps_dying_is_given_up(self):
        dead = lambda: Player(self.players, alive=False)  # noqa: E731
        player = self.music(player=dead)
        player.start()
        for _ in range(music.RESTARTS + 2):
            player.hold(music.APP)
            player.release(music.APP)
        self.assertTrue(player.dead)
        self.assertLessEqual(len(self.players), music.RESTARTS + 1)

    def test_the_tick_finds_a_dead_player_and_starts_it_again(self):
        player = self.music()
        player.start()
        player.check()  # alive: nothing to do
        self.assertEqual(len(self.players), 1)
        self.players[0].alive = False  # GStreamer stopped it, no level sent since
        player.check()
        self.assertEqual(len(self.players), 2)
        self.assertEqual(self.players[1].lines, [f"play {self.track}", "level 0.3 2.5"])
        self.assertEqual(player.restarts, 1)
        self.assertEqual(self.logged, ["music player stopped (exit 1)"])

    def test_a_dead_player_found_while_held_waits_for_the_release(self):
        player = self.music()
        player.start()
        player.hold(music.APP)
        self.players[0].alive = False
        player.check()
        self.assertIsNone(player.player)
        self.assertEqual(len(self.players), 1)
        player.release(music.APP)
        self.assertEqual(len(self.players), 2)
        self.assertTrue(player.playing)

    def test_the_tick_without_a_player_does_nothing(self):
        player = self.music()
        player.check()
        self.assertEqual(self.players, [])

    def test_healthy_play_starts_the_restart_count_over(self):
        player = self.music()
        player.start()
        for _ in range(music.RESTARTS):
            self.players[-1].alive = False
            player.check()
        self.assertEqual(player.restarts, music.RESTARTS)
        self.clock.now += music.HEALTHY - 1
        player.check()
        self.assertEqual(player.restarts, music.RESTARTS)  # not yet
        self.clock.now += 1
        player.check()
        self.assertEqual(player.restarts, 0)
        self.players[-1].alive = False
        player.check()
        self.assertFalse(player.dead)  # one more death is not the last one
        self.assertTrue(player.playing)

    def test_a_paused_player_is_not_healthy_play(self):
        player = self.music()
        player.start()
        self.players[-1].alive = False
        player.check()
        player.hold(music.APP)
        self.clock.now += music.HEALTHY * 2
        player.check()
        self.assertEqual(player.restarts, 1)

    def test_giving_up_is_logged_once(self):
        player = self.music()
        player.start()
        for _ in range(music.RESTARTS + 3):
            if player.player is not None:
                player.player.alive = False
            player.check()
            player.hold(music.APP)
            player.release(music.APP)
        self.assertTrue(player.dead)
        self.assertEqual(len(self.players), music.RESTARTS + 1)
        given_up = [line for line in self.logged if line.startswith("no music:")]
        self.assertEqual(given_up, [f"no music: the music player stopped {music.RESTARTS + 1} times"])

    def test_no_track_is_logged_once(self):
        self.track = None
        player = self.music()
        player.start()
        player.settings()
        self.assertEqual(self.logged, ["no music: no track"])

    def test_rest_ends_the_player_once_it_has_faded_out(self):
        player = self.music()
        player.start()
        self.assertIsNone(player.rest())  # it plays: nothing to do
        player.hold(music.APP)
        self.assertAlmostEqual(player.rest(), music.FADE_OUT)
        self.assertTrue(self.players[0].alive)
        self.clock.now += music.FADE_OUT
        self.assertIsNone(player.rest())
        self.assertFalse(self.players[0].alive)
        self.assertFalse(self.players[0].waited)  # reaped on a thread: the UI never waits
        self.assertIsNone(player.player)
        self.assertIsNone(player.rest())

    def test_the_next_player_seeks_to_where_the_last_one_stopped(self):
        player = self.music()
        player.start()
        self.clock.now += 30
        player.hold(music.APP)
        self.clock.now += 5
        player.rest()
        player.release(music.APP)
        self.assertEqual(len(self.players), 2)
        self.assertEqual(self.players[1].lines, [f"play {self.track}", "seek 30.80", "level 0.3 2.5"])
        self.assertTrue(player.playing)

    def test_the_count_goes_on_through_ducks_and_a_fade_cut_short(self):
        player = self.music()
        player.start()
        self.clock.now += 10
        player.sound("select")
        self.clock.now += 1
        player.tick()
        player.hold(music.BLANK)
        self.clock.now += 0.3  # back before the fade-out ended: it never paused
        player.release(music.BLANK)
        self.clock.now += 2
        self.assertAlmostEqual(player.position(), 13.3)
        player.hold(music.BLANK)
        self.clock.now += 100  # paused after FADE_OUT
        self.assertAlmostEqual(player.position(), 13.3 + music.FADE_OUT)
        player.release(music.BLANK)
        self.clock.now += 1
        self.assertAlmostEqual(player.position(), 14.3 + music.FADE_OUT)

    def test_rest_without_a_player_or_while_it_plays_does_nothing(self):
        player = self.music()
        self.assertIsNone(player.rest())
        player.start()
        player.sound("launch")
        self.assertIsNone(player.rest())
        self.assertTrue(self.players[0].alive)

    def test_the_launch_return_and_error_sounds_duck_it(self):
        self.assertLessEqual({"launch", "return", "error"}, music.DUCKED_SOUNDS)

    def test_a_player_that_cannot_start_is_silence(self):
        def broken():
            raise OSError("no python3")
        player = self.music(player=broken)
        player.start()
        self.assertTrue(player.dead)
        self.assertFalse(player.playing)


class LogTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = pathlib.Path(self.temporary.name) / "logs" / "music.log"

    def test_lines_are_appended(self):
        music.log("one", self.path)
        music.log("two\nthree", self.path)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith(" one"))
        self.assertTrue(lines[1].endswith(" two?three"))

    def test_a_long_log_starts_over(self):
        self.path.parent.mkdir()
        self.path.write_text("x" * (music.LOG_MAX + 1), encoding="utf-8")
        music.log("fresh", self.path)
        self.assertTrue(self.path.read_text(encoding="utf-8").endswith(" fresh\n"))
        self.assertLess(self.path.stat().st_size, 100)

    def test_an_unwritable_log_is_no_error(self):
        self.path.parent.mkdir()
        self.path.mkdir()  # a directory where the file should be
        music.log("lost", self.path)


def _player_script():
    spec = importlib.util.spec_from_file_location("couchliteos_music_player", music.REPO_PLAYER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SinkTest(unittest.TestCase):
    """couchliteos-music's choice of audio output, without GStreamer."""

    def setUp(self):
        self.player = _player_script()

    def test_pipewire_first(self):
        made = []

        def make(name):
            made.append(name)
            return f"<{name}>"
        self.assertEqual(self.player.make_sink(make), ("pipewiresink", "<pipewiresink>"))
        self.assertEqual(made, ["pipewiresink"])

    def test_the_next_one_that_exists(self):
        have = {"alsasink"}
        self.assertEqual(self.player.make_sink(lambda name: name if name in have else None), ("alsasink", "alsasink"))

    def test_a_broken_plugin_is_skipped(self):
        def make(name):
            if name == "pipewiresink":
                raise RuntimeError("broken")
            return name if name == "autoaudiosink" else None
        self.assertEqual(self.player.make_sink(make), ("autoaudiosink", "autoaudiosink"))

    def test_none_at_all(self):
        self.assertEqual(self.player.make_sink(lambda name: None), (None, None))

    def test_the_stream_is_named_when_the_sink_can_take_it(self):
        class Sink:
            def __init__(self, has):
                self.has, self.set = has, {}

            def find_property(self, name):
                return object() if self.has else None

            def set_property(self, name, value):
                self.set[name] = value
        sink = Sink(True)
        self.assertTrue(self.player.name_stream(sink, lambda text: ("parsed", text)))
        self.assertEqual(sink.set["stream-properties"], ("parsed", self.player.STREAM_PROPERTIES))
        self.assertIn('media.name="CouchLiteOS music"', self.player.STREAM_PROPERTIES)
        self.assertIn("media.role=Music", self.player.STREAM_PROPERTIES)
        older = Sink(False)
        self.assertFalse(self.player.name_stream(older, lambda text: text))
        self.assertEqual(older.set, {})

        def broken(_text):
            raise ValueError("bad structure")
        self.assertFalse(self.player.name_stream(Sink(True), broken))


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = pathlib.Path(self.temporary.name) / "config.ini"

    def test_defaults_without_a_file(self):
        self.assertTrue(music.music_enabled(self.config))
        self.assertEqual(music.music_volume(self.config), music.DEFAULT_VOLUME)

    def test_saved_values_are_read_back_and_other_keys_kept(self):
        self.config.write_text("[appearance]\ntheme = midnight\nsounds = off\n", encoding="utf-8")
        music.save_music(False, 70, self.config)
        self.assertFalse(music.music_enabled(self.config))
        self.assertEqual(music.music_volume(self.config), 70)
        text = self.config.read_text(encoding="utf-8")
        self.assertIn("theme = midnight", text)
        self.assertIn("sounds = off", text)

    def test_odd_volumes_snap_to_a_step(self):
        for written, read in (("34", 30), ("55%", 50), ("1000", 100), ("0", 10), ("loud", music.DEFAULT_VOLUME)):
            self.config.write_text(f"[appearance]\nmusic_volume = {written}\n", encoding="utf-8")
            self.assertEqual(music.music_volume(self.config), read, written)


class TrackTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = pathlib.Path(self.temporary.name)
        self.user = root / "user"
        self.builtin = root / "ambient.ogg"
        self.builtin.write_bytes(b"OggS")

    def test_the_built_in_loop_without_a_users_own(self):
        self.assertEqual(music.track(self.user, self.builtin), self.builtin)

    def test_the_users_own_track_wins_first_by_name(self):
        self.user.mkdir()
        for name in ("b.mp3", "a.flac", "notes.txt", ".hidden.ogg"):
            (self.user / name).write_bytes(b"x")
        self.assertEqual(music.track(self.user, self.builtin), self.user / "a.flac")

    def test_nothing_at_all_is_none(self):
        self.assertIsNone(music.track(self.user, self.builtin.with_name("gone.ogg")))


if __name__ == "__main__":
    unittest.main()
