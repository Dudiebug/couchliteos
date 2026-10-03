import testenv  # noqa: F401  (first: scratch run and state directories)
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import couchliteos_session as session
import couchliteos_stream as stream
from test_quick import bare_tv
from test_session import MOONLIGHT, FakeSession

P720 = (1280, 720, 60000)
P1080 = (1920, 1080, 60000)
P1080_120 = (1920, 1080, 120000)
P4K = (3840, 2160, 60000)
CONF = "[General]\nwidth=2560\nheight=1440\nfps=60\nbitrate=30000\nvideocfg=0\n\n[hosts]\nsize=0\n"


def plan(preset, mode, decode="auto"):
    p = stream.preset_plan(preset, mode, decode)
    return p.width, p.height, p.fps, p.bitrate, p.hdr


class PresetTest(unittest.TestCase):
    """PERFORMANCE, BALANCED and QUALITY fitted to the display and the decoder."""

    def test_performance(self):
        self.assertEqual(plan("PERFORMANCE", P720), (1280, 720, 60, 10000, False))
        self.assertEqual(plan("PERFORMANCE", P1080), (1280, 720, 60, 10000, False))
        self.assertEqual(plan("PERFORMANCE", P1080_120), (1280, 720, 120, 14000, False))
        self.assertEqual(plan("PERFORMANCE", P4K), (1920, 1080, 60, 20000, False))
        self.assertEqual(plan("PERFORMANCE", (3840, 2160, 120000)), (1920, 1080, 120, 28000, False))
        # Software decoding: 720p even on a 4K TV, never above 60 fps.
        self.assertEqual(plan("PERFORMANCE", P4K, "software"), (1280, 720, 60, 10000, False))
        self.assertEqual(plan("PERFORMANCE", P1080_120, "software"), (1280, 720, 60, 10000, False))

    def test_balanced_is_what_optimize_picks(self):
        for mode in (P720, P1080, P1080_120, P4K):
            for decode in ("auto", "software"):
                with self.subTest(mode=mode, decode=decode):
                    self.assertEqual(stream.preset_plan("BALANCED", mode, decode),
                                     stream.plan_settings(*mode, stream.Network(), decode))
        self.assertEqual(plan("BALANCED", P4K), (3840, 2160, 60, 80000, None))
        self.assertEqual(plan("BALANCED", P4K, "software"), (1920, 1080, 60, 20000, None))

    def test_quality(self):
        self.assertEqual(plan("QUALITY", P720), (1280, 720, 60, 15000, True))
        self.assertEqual(plan("QUALITY", P1080), (1920, 1080, 60, 30000, True))
        self.assertEqual(plan("QUALITY", P1080_120), (1920, 1080, 60, 30000, True))
        self.assertEqual(plan("QUALITY", P4K), (3840, 2160, 60, 120000, True))
        # Software decoding: 1080p60 at most, its bitrate cap, no HDR.
        self.assertEqual(plan("QUALITY", P4K, "software"), (1920, 1080, 60, 20000, False))
        self.assertEqual(plan("QUALITY", P720, "software"), (1280, 720, 60, 15000, False))

    def test_no_preset_goes_beyond_the_display(self):
        for preset in stream.PRESETS:
            for mode in (P720, P1080, P1080_120, P4K, (1366, 768, 59940), (1680, 1050, 60000)):
                for decode in ("auto", "hardware", "software"):
                    with self.subTest(preset=preset, mode=mode, decode=decode):
                        p = stream.preset_plan(preset, mode, decode)
                        self.assertLessEqual(p.width, mode[0])
                        self.assertLessEqual(p.height, mode[1])
                        self.assertLessEqual(p.fps, max(24, round(mode[2] / 1000)))
                        self.assertEqual((p.width % 2, p.height % 2), (0, 0))
                        if decode == "software":
                            self.assertLessEqual(p.width * p.height, stream.SOFTWARE_MAX_PIXELS)
                            self.assertLessEqual(p.fps, stream.SOFTWARE_MAX_FPS)
                            self.assertLessEqual(p.bitrate, stream.SOFTWARE_MAX_BITRATE)
                            self.assertFalse(p.hdr)

    def test_a_known_link_caps_the_bitrate(self):
        self.assertEqual(stream.preset_plan("QUALITY", P4K, link_mbps=100).bitrate, 60000)
        self.assertTrue(stream.preset_plan("QUALITY", P4K, link_mbps=100).capped)

    def test_custom_is_fitted_too(self):
        custom = stream.GameSettings("CUSTOM", 3840, 2160, 120, 0, True)
        p = stream.game_plan(custom, P1080)
        self.assertEqual((p.width, p.height, p.fps, p.hdr), (1920, 1080, 60, True))
        self.assertEqual(p.bitrate, stream.default_bitrate(1920, 1080, 60))
        p = stream.game_plan(custom, P4K, "software")
        self.assertEqual((p.width, p.height, p.fps, p.bitrate, p.hdr), (1920, 1080, 60, 20000, False))
        own = stream.GameSettings("CUSTOM", 1280, 720, 30, 5000, False)
        p = stream.game_plan(own, P4K)
        self.assertEqual((p.width, p.height, p.fps, p.bitrate, p.hdr), (1280, 720, 30, 5000, False))
        # No display mode: CUSTOM is used as chosen, a preset waits for one.
        self.assertEqual(stream.game_plan(own, None).width, 1280)
        self.assertIsNone(stream.game_plan(stream.GameSettings("QUALITY"), None))

    def test_describe(self):
        self.assertEqual(stream.describe(stream.preset_plan("QUALITY", P4K)), "3840x2160 AT 60 FPS, 120 MBPS, HDR")
        self.assertEqual(stream.describe(stream.preset_plan("PERFORMANCE", P1080)), "1280x720 AT 60 FPS, 10 MBPS")


class GameSettingsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = pathlib.Path(directory.name)
        self.config = self.dir / "config.ini"

    def test_text_round_trips(self):
        for settings in (stream.GameSettings("PERFORMANCE"), stream.GameSettings("QUALITY"),
                         stream.GameSettings("CUSTOM", 2560, 1440, 120, 50000, True),
                         stream.GameSettings("CUSTOM", 1280, 720, 30, 0, False)):
            with self.subTest(settings=settings):
                self.assertEqual(stream.parse_game(settings.text()), settings)
        self.assertEqual(stream.GameSettings("CUSTOM", 2560, 1440, 120, 50000, True).text(),
                         "CUSTOM 2560x1440 120 50000 hdr")
        for bad in ("", "FAST", "CUSTOM", "CUSTOM 1920x1080 60 20000", "CUSTOM 1920x1080 999 0 sdr",
                    "CUSTOM 1920x1080 60 100 sdr", "CUSTOM 1920*1080 60 0 sdr", "CUSTOM 1920x1080 60 0 yes"):
            with self.subTest(bad=bad):
                self.assertIsNone(stream.parse_game(bad))

    def test_per_game_lookup_and_fallback(self):
        stream.save_game("UUID-1", "Cyberpunk 2077", stream.GameSettings("QUALITY"), self.config)
        stream.save_game("UUID-1", "Hades", stream.GameSettings("PERFORMANCE"), self.config)
        self.assertEqual(stream.game_settings("UUID-1", "Cyberpunk 2077", self.config), stream.GameSettings("QUALITY"))
        self.assertEqual(stream.game_settings("UUID-1", "Hades", self.config).preset, "PERFORMANCE")
        self.assertIsNone(stream.game_settings("UUID-2", "Hades", self.config), "per PC")
        self.assertIsNone(stream.game_settings("UUID-1", "hades", self.config), "names keep their case")
        self.assertIsNone(stream.game_settings("UUID-1", "Desktop", self.config))
        self.assertIsNone(stream.plan_for("UUID-1", "Desktop", mode=lambda: P1080, decode=lambda: "auto",
                                          config=self.config), "no settings of its own: the global ones")
        self.assertEqual(stream.plan_for("UUID-1", "Hades", mode=lambda: P4K, decode=lambda: "auto", config=self.config),
                         stream.preset_plan("PERFORMANCE", P4K))
        stream.save_game("UUID-1", "Hades", None, self.config)
        self.assertIsNone(stream.game_settings("UUID-1", "Hades", self.config))
        self.assertIsNotNone(stream.game_settings("UUID-1", "Cyberpunk 2077", self.config))

    def test_names_config_ini_cannot_hold_are_escaped_and_other_sections_kept(self):
        self.config.write_text("[streaming]\nautostart = true\n\n[moonlight]\ndecoder = auto\n")
        names = ["Halo: MCC", "A = B", "100% Orange", "[Section]", "#hash", "Ünïcode"]
        for index, name in enumerate(names):
            stream.save_game("PC:1", name, stream.GameSettings(stream.PRESETS[index % 3]), self.config)
        for index, name in enumerate(names):
            with self.subTest(name=name):
                self.assertEqual(stream.game_settings("PC:1", name, self.config).preset, stream.PRESETS[index % 3])
        text = self.config.read_text()
        self.assertIn("[streaming]\nautostart = true\n", text)
        self.assertIn("[moonlight]\ndecoder = auto\n", text)
        self.assertIn("PC%3A1/Halo%3A MCC = PERFORMANCE", text)
        self.assertTrue(stream.load_settings(self.config).autostart)
        for name in names:
            stream.save_game("PC:1", name, None, self.config)
        self.assertNotIn("stream-games", self.config.read_text(), "the last one gone removes the section")

    def test_saving_invalid_settings_is_refused(self):
        with self.assertRaises(ValueError):
            stream.save_game("PC", "Game", stream.GameSettings("CUSTOM", 1920, 1080, 500, 0, False), self.config)
        with self.assertRaises(ValueError):
            stream.save_game("PC", "Game", stream.GameSettings("FAST"), self.config)


class PrepareStreamTest(unittest.TestCase):
    """Moonlight.conf before each start: the game's plan, and the global settings back for the next."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = pathlib.Path(directory.name)
        self.config = self.dir / "config.ini"
        self.conf = self.dir / "Moonlight.conf"
        self.saved = self.dir / "stream-global.json"
        self.run = self.dir / "run"
        self.run.mkdir()
        self.conf.write_text(CONF)
        self.modes = []

    def prepare(self, game, mode=P4K, decode="auto"):
        def current():
            self.modes.append(mode)
            return mode
        return stream.prepare_stream(game, mode=current, decode=lambda: decode, config=self.config, conf=self.conf,
                                     saved=self.saved, run_dir=self.run)

    def values(self):
        return stream.general_values(self.conf.read_text())

    def test_the_conf_written_for_each_preset(self):
        expected = {
            "PERFORMANCE": {"width": "1920", "height": "1080", "fps": "60", "bitrate": "20000", "hdr": "false"},
            "BALANCED": {"width": "3840", "height": "2160", "fps": "60", "bitrate": "80000"},
            "QUALITY": {"width": "3840", "height": "2160", "fps": "60", "bitrate": "120000", "hdr": "true",
                        "unlockbitrate": "true"},
        }
        for preset, values in expected.items():
            with self.subTest(preset=preset):
                stream.save_game("UUID-1", "Game", stream.GameSettings(preset), self.config)
                self.assertEqual(self.prepare(("UUID-1", "Game")), preset)
                current = self.values()
                self.assertEqual({key: current.get(key) for key in values}, values)
                self.assertEqual(current["videocfg"], "0", "other keys untouched")
                self.assertIn("[hosts]\nsize=0\n", self.conf.read_text())
                self.assertEqual(stream.active_preset(self.saved), preset)
        custom = stream.GameSettings("CUSTOM", 1280, 720, 120, 15000, False)
        stream.save_game("UUID-1", "Game", custom, self.config)
        self.assertEqual(self.prepare(("UUID-1", "Game"), mode=P1080_120), "CUSTOM")
        self.assertEqual({key: self.values()[key] for key in ("width", "height", "fps", "bitrate", "hdr")},
                         {"width": "1280", "height": "720", "fps": "120", "bitrate": "15000", "hdr": "false"})

    def test_the_global_settings_come_back_for_the_next_game(self):
        stream.save_game("UUID-1", "Fast Game", stream.GameSettings("QUALITY"), self.config)
        before = self.conf.read_text()
        self.prepare(("UUID-1", "Fast Game"))
        self.assertNotEqual(self.conf.read_text(), before)
        self.assertEqual(self.prepare(("UUID-1", "Other Game")), "")
        self.assertEqual(self.conf.read_text(), before, "byte for byte: added keys removed, changed ones back")
        self.assertFalse(self.saved.exists())
        self.assertEqual(stream.active_preset(self.saved), "")
        self.assertEqual(self.modes, [P4K], "the display is asked only for a game with settings of its own")

    def test_moonlight_opened_from_its_tile_gets_the_global_settings_too(self):
        stream.save_game("UUID-1", "Game", stream.GameSettings("PERFORMANCE"), self.config)
        before = self.conf.read_text()
        self.prepare(("UUID-1", "Game"))
        self.assertEqual(self.prepare(None), "")
        self.assertEqual(self.conf.read_text(), before)

    def test_one_game_after_another_never_mixes(self):
        stream.save_game("UUID-1", "A", stream.GameSettings("QUALITY"), self.config)
        stream.save_game("UUID-1", "B", stream.GameSettings("PERFORMANCE"), self.config)
        before = self.conf.read_text()
        self.prepare(("UUID-1", "A"))
        self.prepare(("UUID-1", "B"))
        self.assertNotIn("unlockbitrate", self.values(), "QUALITY's unlock went with it")
        self.assertEqual(self.values()["width"], "1920")
        self.prepare(("UUID-1", "C"))
        self.assertEqual(self.conf.read_text(), before)

    def test_a_value_changed_since_is_kept(self):
        # SMOOTHER STREAM (or Moonlight's own settings) changed the bitrate after the game's plan.
        stream.save_game("UUID-1", "Game", stream.GameSettings("QUALITY"), self.config)
        self.prepare(("UUID-1", "Game"))
        self.conf.write_text(stream.rewrite_conf(self.conf.read_text(), {"bitrate": "9000"}))
        self.prepare(None)
        values = self.values()
        self.assertEqual((values["width"], values["height"], values["bitrate"]), ("2560", "1440", "9000"))
        self.assertNotIn("hdr", values)

    def test_nothing_changes_while_moonlight_runs(self):
        stream.save_game("UUID-1", "Game", stream.GameSettings("QUALITY"), self.config)
        self.prepare(("UUID-1", "Game"))
        written = self.conf.read_text()
        (self.run / "moonlight-ready").touch()
        with self.assertRaises(stream.StreamError):
            self.prepare(None)
        self.assertEqual(self.conf.read_text(), written)
        self.assertTrue(self.saved.exists(), "kept for the next start")

    def test_a_games_plan_is_refused_while_moonlight_runs(self):
        # No record to put back (restore_global has nothing to do): apply_game_plan's own check.
        stream.save_game("UUID-1", "Game", stream.GameSettings("QUALITY"), self.config)
        before = self.conf.read_text()
        (self.run / "app-active").write_text("moonlight\n")
        with self.assertRaises(stream.StreamError):
            self.prepare(("UUID-1", "Game"))
        with self.assertRaises(stream.StreamError):
            stream.apply_game_plan(stream.preset_plan("QUALITY", P4K, "auto"), "QUALITY", self.conf, self.saved, self.run)
        self.assertEqual(self.conf.read_text(), before)
        self.assertFalse(self.saved.exists())

    def test_restore_global_is_refused_while_moonlight_runs(self):
        stream.save_game("UUID-1", "Game", stream.GameSettings("QUALITY"), self.config)
        self.prepare(("UUID-1", "Game"))
        written, record = self.conf.read_text(), self.saved.read_text()
        (self.run / "app-active").write_text("moonlight\n")
        with self.assertRaises(stream.StreamError):
            stream.restore_global(self.conf, self.saved, self.run)
        self.assertEqual((self.conf.read_text(), self.saved.read_text()), (written, record))
        (self.run / "app-active").write_text("chiaki\n")  # another app: Moonlight is not running
        self.assertTrue(stream.restore_global(self.conf, self.saved, self.run))
        self.assertEqual(self.conf.read_text(), CONF)

    def test_restore_when_moonlight_conf_is_gone(self):
        # Moonlight's settings were reset (the file removed) after the game's plan: nothing to put
        # back, no file made up, and the record is dropped.
        stream.save_game("UUID-1", "Game", stream.GameSettings("QUALITY"), self.config)
        self.prepare(("UUID-1", "Game"))
        self.conf.unlink()
        self.assertFalse(stream.restore_global(self.conf, self.saved, self.run))
        self.assertFalse(self.conf.exists())
        self.assertFalse(self.saved.exists())
        self.assertEqual(stream.active_preset(self.saved), "")

    def test_a_damaged_record_is_dropped_and_never_written_into_the_conf(self):
        before = self.conf.read_text()
        for text in ("not json", "[]", json.dumps({"saved": {"width": "1\nx=1"}, "written": {"width": "1920"}}),
                     json.dumps({"saved": {"evil": "1"}, "written": {"evil": "2"}})):
            with self.subTest(text=text):
                self.saved.write_text(text)
                self.assertEqual(self.prepare(None), "")
                self.assertEqual(self.conf.read_text(), before)
                self.assertFalse(self.saved.exists())

    def test_no_conf_yet(self):
        self.conf.unlink()
        stream.save_game("UUID-1", "Game", stream.GameSettings("PERFORMANCE"), self.config)
        self.prepare(("UUID-1", "Game"))
        self.assertEqual(self.values()["width"], "1920")
        self.prepare(None)
        self.assertEqual(self.conf.read_text(), "[General]\n")

    def test_rewrite_conf_removes_keys_given_none(self):
        text = "[General]\r\nwidth=1\r\nhdr=true\r\n[hosts]\r\nhdr=keep\r\n"
        self.assertEqual(stream.rewrite_conf(text, {"hdr": None, "fps": "60"}),
                         "[General]\r\nwidth=1\r\nfps=60\r\n[hosts]\r\nhdr=keep\r\n")
        self.assertEqual(stream.rewrite_conf("", {"hdr": None}), "")


class SettingsFormTest(unittest.TestCase):
    def test_presets_and_custom(self):
        form = stream.SettingsForm(None)
        self.assertEqual(form.rows(), [("preset", "PRESET", "DEFAULT"), ("save", "SAVE", "")])
        self.assertIsNone(form.settings())
        form.adjust(-1)
        self.assertEqual(form.preset, "DEFAULT", "nothing wraps")
        form.adjust(1)
        self.assertEqual(form.settings(), stream.GameSettings("PERFORMANCE"))
        for _ in range(5):
            form.adjust(1)
        self.assertEqual(form.preset, "CUSTOM")
        self.assertEqual([row[0] for row in form.rows()], ["preset", "resolution", "fps", "bitrate", "hdr", "save"])
        form.move(1)
        form.adjust(1)
        form.move(1)
        form.adjust(1)
        form.move(1)
        form.adjust(1)
        form.adjust(1)
        form.move(1)
        form.adjust(1)
        form.move(5)
        self.assertEqual(form.rows()[form.index][0], "save")
        self.assertEqual(form.settings(), stream.GameSettings("CUSTOM", 2560, 1440, 90, 10000, True))
        self.assertIn(("bitrate", "BITRATE", "10 MBPS"), form.rows())
        self.assertEqual(stream.parse_game(form.settings().text()), form.settings())

    def test_it_starts_from_the_saved_settings(self):
        custom = stream.GameSettings("CUSTOM", 1280, 720, 120, 0, False)
        form = stream.SettingsForm(custom)
        self.assertEqual(form.rows()[1:5], [("resolution", "RESOLUTION", "1280x720"), ("fps", "FRAME RATE", "120 FPS"),
                                            ("bitrate", "BITRATE", "AUTOMATIC"), ("hdr", "HDR", "OFF")])
        form.adjust(-1)
        self.assertEqual(form.settings(), stream.GameSettings("QUALITY"))
        form.adjust(1)
        self.assertEqual(form.settings(), custom, "the custom values stay while another preset is shown")


class SessionStreamSettingsTest(unittest.TestCase):
    HOST = stream.Host(name="Gaming-PC", uuid="UUID-1", local="192.168.1.50", apps=("Desktop",))

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.run = pathlib.Path(directory.name)

    def session(self):
        def ready(s):
            (self.run / "moonlight-ready").touch()
        return FakeSession(self.run, on_wait=ready)

    def test_a_stream_applies_the_games_settings_before_moonlight_starts(self):
        s = self.session()
        calls = []

        def prepare(game, **kwargs):
            calls.append((game, kwargs["run_dir"], (self.run / "start-moonlight").exists()))
            return "QUALITY"

        with mock.patch.object(session.stream, "prepare_stream", side_effect=prepare), \
                mock.patch.object(session.recent, "record"):
            self.assertTrue(s.start_stream(self.HOST, "Desktop"))
        self.assertEqual(calls, [(("UUID-1", "Desktop"), self.run, False)])
        self.assertIsNone(s.pending_game)

    def test_moonlight_from_its_tile_gets_the_global_settings(self):
        s = self.session()
        with mock.patch.object(session.stream, "prepare_stream", return_value="") as prepare:
            self.assertTrue(s.launch_app(MOONLIGHT))
        self.assertIsNone(prepare.call_args.args[0])

    def test_a_failure_never_stops_the_stream(self):
        s = self.session()
        for error in (OSError("disk"), stream.StreamError("RUNNING"), RuntimeError("wlr-randr failed")):
            with self.subTest(error=error):
                (self.run / "moonlight-ready").unlink(missing_ok=True)
                with mock.patch.object(session.stream, "prepare_stream", side_effect=error), \
                        mock.patch.object(session.display, "log") as log, mock.patch.object(session.recent, "record"):
                    self.assertTrue(s.start_stream(self.HOST, "Desktop"))
                log.assert_called_once()

    def test_the_display_mode_is_read_from_wlr_randr(self):
        s = self.session()
        output = mock.Mock()
        output.current_mode = mock.Mock(width=3840, height=2160, refresh_mhz=60000)

        def prepare(game, *, mode, run_dir):
            self.assertEqual(mode(), (3840, 2160, 60000))
            return ""

        with mock.patch.object(session.stream, "prepare_stream", side_effect=prepare), \
                mock.patch.object(session.display, "query_outputs", return_value=[output]), \
                mock.patch.object(session.display, "active_output", return_value=output):
            s.apply_stream_settings(("UUID-1", "Desktop"))


class TvStreamRowsTest(unittest.TestCase):
    """The quick menu's stream rows and the STREAM SETTINGS screen's keys, without GTK."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.run = pathlib.Path(directory.name)

    def tv(self):
        tv = bare_tv()
        type(tv).run_dir = property(lambda _self: self.run)
        self.addCleanup(delattr, type(tv), "run_dir")
        tv.app_by_id = mock.Mock(return_value=MOONLIGHT)
        tv.close_quick = mock.Mock()
        return tv

    def test_rows_only_while_moonlight_runs(self):
        tv = self.tv()
        self.assertEqual(tv.quick_stream_items(), [])
        (self.run / "moonlight-ready").touch()
        with mock.patch.object(stream, "active_preset", return_value="QUALITY"):
            rows = tv.quick_stream_items()
        self.assertEqual([(row.label, row.value, row.selectable) for row in rows],
                         [("STREAM PRESET", "QUALITY", False), ("SHOW STATS", "OFF", True)])
        with mock.patch.object(stream, "active_preset", return_value=""):
            self.assertEqual(tv.quick_stream_items()[0].value, "DEFAULT")

    def test_show_stats_goes_back_to_moonlight_and_asks_gamepad_nav(self):
        tv = self.tv()
        (self.run / "moonlight-ready").touch()
        tv.focus_app.return_value = True
        tv.quick_extra_action(tv.quick_stream_items()[1].action)
        tv.close_quick.assert_called_once_with(resume=False)
        tv.focus_app.assert_called_once_with(MOONLIGHT)
        self.assertTrue((self.run / "moonlight-stats.request").exists())
        self.assertEqual(tv.quick_stream_items()[1].value, "ON")
        (self.run / "moonlight-ready").unlink()
        tv.quick_stream_items()
        (self.run / "moonlight-ready").touch()
        self.assertEqual(tv.quick_stream_items()[1].value, "OFF", "a new stream starts without statistics")

    def test_show_stats_without_a_moonlight_window_says_so(self):
        tv = self.tv()
        tv.focus_app.return_value = False
        tv.quick_extra_action(("stats",))
        self.assertFalse((self.run / "moonlight-stats.request").exists())
        self.assertIn("MOONLIGHT HAS NO WINDOW", tv.status)

    def test_stream_settings_keys_save_for_the_game(self):
        tv = self.tv()
        tv.render_stream_settings = mock.Mock()
        form = stream.SettingsForm(None)
        tv.stream_form = ("UUID-1", "Hades", "HADES", P1080, "auto", form)
        tv.stream_settings_key("right")
        tv.stream_settings_key("right")
        tv.stream_settings_key("down")
        with mock.patch.object(stream, "save_game") as save:
            tv.stream_settings_key("activate")
        save.assert_called_once_with("UUID-1", "Hades", stream.GameSettings("BALANCED"))
        self.assertEqual(tv.status, "STREAM SETTINGS SAVED: BALANCED")
        tv.show.assert_called_with("home")

    def test_back_leaves_without_saving(self):
        tv = self.tv()
        tv.stream_form = ("UUID-1", "Hades", "HADES", P1080, "auto", stream.SettingsForm(None))
        with mock.patch.object(stream, "save_game") as save:
            tv.stream_settings_key("back")
        save.assert_not_called()
        tv.show.assert_called_with("home")


if __name__ == "__main__":
    unittest.main()
