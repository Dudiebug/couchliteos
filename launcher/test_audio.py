import unittest
from unittest import mock

import couchliteos_audio as audio


STATUS = """PipeWire 'pipewire-0' [1.2.7]
 └─ Clients:
Audio
 ├─ Devices:
 ├─ Sinks:
 │  *   42. Built-in Audio Analog Stereo  [vol: 0.50]
 │      57. WH-1000XM5                    [vol: 1.00]
 ├─ Sources:
"""


class AudioTest(unittest.TestCase):
    def test_parse_sinks_preserves_friendly_names_and_default(self):
        self.assertEqual(
            audio.parse_sinks(STATUS),
            [
                audio.Sink(42, "Built-in Audio Analog Stereo", True),
                audio.Sink(57, "WH-1000XM5", False),
            ],
        )

    def test_set_default_uses_wpctl_without_shell(self):
        with mock.patch.object(audio.subprocess, "run") as run:
            run.return_value.returncode = 0
            audio.set_default(57)
        self.assertEqual(run.call_args.args[0], ["wpctl", "set-default", "57"])


class VolumeTest(unittest.TestCase):
    def wpctl(self, outputs=None, returncode=0, stderr=""):
        """Patch subprocess.run; `outputs` maps a wpctl verb to its stdout."""
        outputs = outputs or {}

        def run(command, **_kwargs):
            return mock.Mock(returncode=returncode, stdout=outputs.get(command[1], ""), stderr=stderr)

        return mock.patch.object(audio.subprocess, "run", side_effect=run)

    @staticmethod
    def commands(run):
        return [call.args[0] for call in run.call_args_list]

    def test_parse_volume_reads_level_and_mute_flag(self):
        self.assertEqual(audio.parse_volume("Volume: 0.50\n"), audio.Volume(50, False))
        self.assertEqual(audio.parse_volume("Volume: 0.35 [MUTED]\n"), audio.Volume(35, True))
        self.assertEqual(audio.parse_volume("Volume: 1.00"), audio.Volume(100, False))
        with self.assertRaises(RuntimeError):
            audio.parse_volume("no volume here")

    def test_get_volume_asks_the_default_sink(self):
        with self.wpctl({"get-volume": "Volume: 0.20\n"}) as run:
            self.assertEqual(audio.get_volume(), audio.Volume(20, False))
        self.assertEqual(self.commands(run), [["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"]])

    def test_volume_steps_are_limited_to_full_scale(self):
        with self.wpctl({"get-volume": "Volume: 0.55\n"}) as run:
            self.assertEqual(audio.change_volume(5), audio.Volume(55, False))
            audio.change_volume(-5)
        self.assertEqual(
            self.commands(run),
            [
                ["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "5%+"],
                ["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
                ["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "5%-"],
                ["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
            ],
        )

    def test_toggle_mute_reports_the_new_state(self):
        with self.wpctl({"get-volume": "Volume: 0.50 [MUTED]\n"}) as run:
            self.assertTrue(audio.toggle_mute().muted)
        self.assertEqual(self.commands(run)[0], ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"])

    def test_wpctl_failure_is_raised_with_its_message(self):
        with self.wpctl(returncode=1, stderr="no default sink\n"):
            with self.assertRaisesRegex(RuntimeError, "no default sink"):
                audio.change_volume(5)

    def test_chosen_sink_is_unmuted_and_raised_from_zero(self):
        with self.wpctl({"get-volume": "Volume: 0.00 [MUTED]\n"}) as run:
            self.assertEqual(audio.ensure_audible(7), "UNMUTED, VOLUME SET TO 50%")
        self.assertEqual(
            self.commands(run),
            [
                ["wpctl", "get-volume", "7"],
                ["wpctl", "set-mute", "7", "0"],
                ["wpctl", "set-volume", "7", "0.5"],
            ],
        )

    def test_chosen_sink_that_is_muted_keeps_its_volume(self):
        with self.wpctl({"get-volume": "Volume: 0.30 [MUTED]\n"}) as run:
            self.assertEqual(audio.ensure_audible(7), "UNMUTED")
        self.assertEqual(self.commands(run)[1:], [["wpctl", "set-mute", "7", "0"]])

    def test_chosen_sink_that_is_already_audible_is_left_alone(self):
        with self.wpctl({"get-volume": "Volume: 0.30\n"}) as run:
            self.assertEqual(audio.ensure_audible(7), "")
        self.assertEqual(self.commands(run), [["wpctl", "get-volume", "7"]])


if __name__ == "__main__":
    unittest.main()
