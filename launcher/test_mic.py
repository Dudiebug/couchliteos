"""Microphone: listing, choice, level, mute, the MIC LIVE flag and TEST (PipeWire faked)."""

import os
import pathlib
import tempfile
import unittest
from unittest import mock

import couchliteos_audio as audio
import couchliteos_audiomenu as audiomenu
from test_audio_bluetooth import HEADSET, RECORDING, USB_MIC, Commands, bt_device, bt_sink, bt_source, graph

STATUS = """PipeWire 'pipewire-0' [1.4.2]
Audio
 ├─ Devices:
 │      47. Built-in Audio                      [alsa]
 ├─ Sinks:
 │  *   42. Built-in Audio Analog Stereo        [vol: 0.50]
 ├─ Sources:
 │      50. USB Mic                             [vol: 0.80]
 │  *   51. Built-in Audio Analog Stereo        [vol: 1.00 MUTED]
 ├─ Filters:
 └─ Streams:

Video
 ├─ Devices:
 ├─ Sinks:
 ├─ Sources:
 │  *   80. Integrated Camera (V4L2)
"""


class SourcesTest(unittest.TestCase):
    def test_audio_inputs_are_listed_with_the_default_marked(self):
        self.assertEqual(audio.parse_sources(STATUS), [
            audio.Sink(50, "USB Mic", False), audio.Sink(51, "Built-in Audio Analog Stereo", True),
        ])
        self.assertEqual(audio.parse_sinks(STATUS), [audio.Sink(42, "Built-in Audio Analog Stereo", True)])

    def test_query_sources_reads_wpctl_status(self):
        with mock.patch.object(audio.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=STATUS)) as run:
            self.assertEqual(len(audio.query_sources()), 2)
        self.assertEqual(run.call_args.args[0], ["wpctl", "status"])

    def test_a_bluetooth_headset_offers_its_mic_before_it_is_in_headset_mode(self):
        sources = audio.parse_sources(STATUS)
        found = graph(USB_MIC, bt_device(71, HEADSET, "Headset"), bt_sink(61, HEADSET, "Headset"))
        choices = audio.mic_choices(sources, found)
        self.assertEqual([c.label for c in choices], ["USB Mic", "Built-in Audio Analog Stereo", "Headset (HEADSET MIC)"])
        self.assertEqual(choices[2], audio.MicChoice("Headset (HEADSET MIC)", None, HEADSET, False))
        chosen = audio.mic_choices(sources, found, HEADSET)
        self.assertEqual([c.default for c in chosen], [False, False, True])

    def test_headphones_without_a_mic_are_not_offered(self):
        found = graph(bt_device(70, HEADSET, "Headphones", headset=False), bt_sink(60, HEADSET, "Headphones"))
        self.assertEqual(audio.mic_choices([], found), [])

    def test_a_headset_already_in_headset_mode_is_listed_once(self):
        sources = [audio.Sink(63, "Headset", True)]
        found = graph(bt_device(71, HEADSET, "Headset", profile="headset-head-unit"), bt_source(63, HEADSET, "Headset"))
        choices = audio.mic_choices(sources, found)
        self.assertEqual([(c.label, c.address) for c in choices], [("Headset", HEADSET)])


class ChoiceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = pathlib.Path(self.directory.name) / "config.ini"

    def tearDown(self):
        self.directory.cleanup()

    def test_choosing_an_input_makes_it_the_default_and_forgets_a_headset(self):
        commands = Commands()
        with mock.patch.object(audio.subprocess, "run", side_effect=commands):
            audio.choose_mic(audio.MicChoice("Headset (HEADSET MIC)", None, HEADSET), self.config)
            self.assertEqual(audio.chosen_mic(self.config), HEADSET)
            self.assertEqual(commands.calls, [], "the headset mic is switched on only while recording")
            audio.choose_mic(audio.MicChoice("USB Mic", 50), self.config)
        self.assertEqual(commands.calls, [["wpctl", "set-default", "50"]])
        self.assertEqual(audio.chosen_mic(self.config), "")
        self.assertIn("[audio]\nmic = default\n", self.config.read_text(encoding="utf-8"))

    def test_level_and_mute_act_on_the_default_source(self):
        def run(command, **_kwargs):
            return mock.Mock(returncode=0, stdout="Volume: 0.65 [MUTED]\n", stderr="")

        with mock.patch.object(audio.subprocess, "run", side_effect=run) as fake:
            self.assertEqual(audio.change_mic_level(5), audio.Volume(65, True))
            self.assertTrue(audio.toggle_mic_mute().muted)
        commands = [call.args[0] for call in fake.call_args_list]
        self.assertEqual(commands[0], ["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SOURCE@", "5%+"])
        self.assertIn(["wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "toggle"], commands)


class LiveTest(unittest.TestCase):
    def test_states(self):
        idle = graph(USB_MIC)
        recording = graph(USB_MIC, RECORDING)
        self.assertEqual(audio.mic_state(None, None), "none")
        self.assertEqual(audio.mic_state(graph(), audio.Volume(50, False)), "none")
        self.assertEqual(audio.mic_state(idle, audio.Volume(50, False)), "idle")
        self.assertEqual(audio.mic_state(recording, audio.Volume(50, False)), "live")
        self.assertEqual(audio.mic_state(recording, audio.Volume(50, True)), "muted")
        self.assertEqual(audio.mic_state(recording, None), "live", "unknown mute: a recording is shown")

    def test_footer_marker_follows_the_state(self):
        states = iter(["live", "muted"])
        monitor = audio.MicMonitor(lambda: next(states))
        self.assertEqual(monitor.line(), "")
        monitor.refresh()
        self.assertEqual(monitor.line(), "MIC LIVE")
        self.assertTrue(monitor.live())
        monitor.refresh()
        self.assertEqual(monitor.line(), "")
        monitor.reader = mock.Mock(side_effect=RuntimeError)
        monitor.refresh()
        self.assertEqual(monitor.state, "none")

    def test_mute_toggle_ends_the_live_flag(self):
        mute = {"on": False}

        def run(command, **_kwargs):
            if command[:2] == ["wpctl", "set-mute"]:
                mute["on"] = not mute["on"]
            return mock.Mock(returncode=0, stderr="",
                             stdout="Volume: 0.50" + (" [MUTED]" if mute["on"] else "") + "\n")

        with mock.patch.object(audio.subprocess, "run", side_effect=run), \
                mock.patch.object(audio, "query_graph", return_value=graph(USB_MIC, RECORDING)):
            self.assertTrue(audio.mic_live())
            audio.toggle_mic_mute()
            self.assertFalse(audio.mic_live())
            self.assertEqual(audio.query_mic_state(), "muted")


class FakeProcess:
    def __init__(self, command, **_kwargs):
        self.path = command[-1]
        pathlib.Path(self.path).write_bytes(b"RIFF" + b"\0" * 4000)
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


class MicTestTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.directory.cleanup()

    def leftovers(self):
        return os.listdir(self.directory.name)

    def test_records_plays_back_and_removes_the_recording(self):
        stages = []
        played = []

        def run(command, **_kwargs):
            played.append(command)
            self.assertTrue(os.path.exists(command[1]), "played before it is removed")
            return mock.Mock(returncode=0, stderr="")

        with mock.patch.object(audio.subprocess, "Popen", side_effect=FakeProcess) as popen, \
                mock.patch.object(audio.subprocess, "run", side_effect=run):
            audio.mic_test(progress=stages.append, directory=self.directory.name, sleep=lambda _s: None)
        self.assertEqual(popen.call_args.args[0][0], "pw-record")
        self.assertEqual(played[0][0], "pw-play")
        self.assertEqual(stages, ["recording", "playing"])
        self.assertEqual(self.leftovers(), [])

    def test_the_recording_is_removed_when_playback_fails(self):
        with mock.patch.object(audio.subprocess, "Popen", side_effect=FakeProcess), \
                mock.patch.object(audio.subprocess, "run", return_value=mock.Mock(returncode=1, stderr="no sink")):
            with self.assertRaisesRegex(RuntimeError, "no sink"):
                audio.mic_test(directory=self.directory.name, sleep=lambda _s: None)
        self.assertEqual(self.leftovers(), [])

    def test_the_recording_is_removed_when_recording_cannot_start(self):
        with mock.patch.object(audio.subprocess, "Popen", side_effect=FileNotFoundError("pw-record")):
            with self.assertRaises(FileNotFoundError):
                audio.mic_test(directory=self.directory.name, sleep=lambda _s: None)
        self.assertEqual(self.leftovers(), [])

    def test_silence_is_reported_and_removed(self):
        class Empty(FakeProcess):
            def __init__(self, command, **_kwargs):
                self.path = command[-1]

        with mock.patch.object(audio.subprocess, "Popen", side_effect=Empty), \
                mock.patch.object(audio.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "NOTHING WAS RECORDED"):
                audio.mic_test(directory=self.directory.name, sleep=lambda _s: None)
        run.assert_not_called()
        self.assertEqual(self.leftovers(), [])

    def test_the_recorder_is_stopped_even_when_interrupted(self):
        processes = []

        def popen(command, **kwargs):
            processes.append(FakeProcess(command, **kwargs))
            return processes[-1]

        def interrupted(_seconds):
            raise KeyboardInterrupt

        with mock.patch.object(audio.subprocess, "Popen", side_effect=popen):
            with self.assertRaises(KeyboardInterrupt):
                audio.mic_test(directory=self.directory.name, sleep=interrupted)
        self.assertTrue(processes[0].terminated)
        self.assertEqual(self.leftovers(), [])


class HelpTest(unittest.TestCase):
    def test_help_says_streams_have_no_mic_and_points_to_discord_and_chiaki(self):
        self.assertIn("MOONLIGHT STREAMS DO NOT CARRY THE MICROPHONE", audiomenu.MIC_HELP)
        self.assertIn("DISCORD IN THE BROWSER", audiomenu.MIC_HELP)
        self.assertIn("CHIAKI-NG", audiomenu.MIC_HELP)
        self.assertEqual(audiomenu.MIC_HELP, audiomenu.MIC_HELP.upper())


if __name__ == "__main__":
    unittest.main()
