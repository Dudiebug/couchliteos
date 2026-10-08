import testenv  # noqa: F401  (first: scratch run and state directories)
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

    def close(self):
        self.alive = False


class MusicTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.players = []
        self.on, self.volume = True, 30
        self.track = pathlib.Path("/usr/share/couchliteos/music/ambient.ogg")

    def music(self, **kwargs):
        options = dict(
            enabled=lambda: self.on, volume=lambda: self.volume, track=lambda: self.track,
            player=lambda: Player(self.players), clock=self.clock,
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

    def test_a_player_that_cannot_start_is_silence(self):
        def broken():
            raise OSError("no python3")
        player = self.music(player=broken)
        player.start()
        self.assertTrue(player.dead)
        self.assertFalse(player.playing)


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
