import json
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


def card(active, profiles, device_id=47, api="alsa", description="Built-in Audio"):
    """A pw-dump Device entry; profiles are (index, name, description, priority, available)."""
    enum = [
        {"index": i, "name": n, "description": d, "priority": p, "available": a}
        for i, n, d, p, a in profiles
    ]
    return {
        "id": device_id,
        "type": "PipeWire:Interface:Device",
        "info": {
            "props": {"device.api": api, "media.class": "Audio/Device", "device.description": description},
            "params": {"EnumProfile": enum, "Profile": [{"index": 0, "name": active}]},
        },
    }


# The Dell OptiPlex from the v0.2.1 report: ALC3234 analog plus three HDMI/DP
# PCMs on one HDA card, a TV ("Beyond TV") on the first. Names, descriptions and
# priorities as PipeWire's ACP reports them for a card without UCM.
OPTIPLEX = [
    (0, "off", "Off", 0, "yes"),
    (1, "output:analog-stereo+input:analog-stereo", "Analog Stereo Output + Analog Stereo Input", 6565, "yes"),
    (2, "output:analog-stereo", "Analog Stereo Output", 6500, "yes"),
    (3, "output:hdmi-stereo+input:analog-stereo", "Digital Stereo (HDMI) Output + Analog Stereo Input", 5965, "yes"),
    (4, "output:hdmi-stereo", "Digital Stereo (HDMI) Output", 5900, "yes"),
    (5, "output:hdmi-stereo-extra1+input:analog-stereo", "Digital Stereo (HDMI 2) Output + Analog Stereo Input", 5765, "no"),
    (6, "output:hdmi-stereo-extra2", "Digital Stereo (HDMI 3) Output", 5700, "no"),
    (7, "input:analog-stereo", "Analog Stereo Input", 65, "yes"),
    (8, "pro-audio", "Pro Audio", 1, "yes"),
]


class ProfileOutputTest(unittest.TestCase):
    def outputs(self, *devices):
        return audio.parse_profile_outputs(json.dumps([{"id": 1, "type": "PipeWire:Interface:Core"}, *devices]))

    def test_tv_on_hdmi_is_offered_while_the_card_plays_analog(self):
        outputs = self.outputs(card("output:analog-stereo+input:analog-stereo", OPTIPLEX))
        self.assertEqual(outputs, [audio.ProfileOutput(47, 3, "Built-in Audio Digital Stereo (HDMI)")])

    def test_speaker_is_offered_while_the_card_plays_hdmi(self):
        outputs = self.outputs(card("output:hdmi-stereo+input:analog-stereo", OPTIPLEX))
        self.assertEqual(outputs, [audio.ProfileOutput(47, 1, "Built-in Audio Analog Stereo")])

    def test_hdmi_ports_without_a_tv_are_not_offered(self):
        names = [o.name for o in self.outputs(card("output:hdmi-stereo", OPTIPLEX))]
        self.assertNotIn("Built-in Audio Digital Stereo (HDMI 2)", names)
        self.assertNotIn("Built-in Audio Digital Stereo (HDMI 3)", names)

    def test_only_alsa_cards_count(self):
        bluetooth = card("a2dp-sink", [(0, "a2dp-sink", "High Fidelity Playback", 40, "yes"),
                                       (1, "headset-head-unit", "Headset Head Unit", 30, "yes")], api="bluez5")
        self.assertEqual(self.outputs(bluetooth), [])

    def test_a_card_using_ucm_has_nothing_to_switch(self):
        ucm = card("HiFi", [(0, "off", "Off", 0, "yes"), (1, "HiFi", "Play HiFi quality Music", 8000, "yes")])
        self.assertEqual(self.outputs(ucm), [])

    def test_pw_dump_failure_means_no_extra_outputs(self):
        for outcome in (FileNotFoundError("pw-dump"), mock.Mock(returncode=1, stdout=""),
                        mock.Mock(returncode=0, stdout="not json")):
            with self.subTest(outcome=outcome), mock.patch.object(
                audio.subprocess, "run", side_effect=[outcome] if isinstance(outcome, Exception) else None,
                return_value=outcome,
            ):
                self.assertEqual(audio.query_profile_outputs(), [])

    def test_switching_saves_the_profile_and_returns_the_new_sink(self):
        output = audio.ProfileOutput(47, 3, "Built-in Audio Digital Stereo (HDMI)")
        hdmi = audio.Sink(60, "Built-in Audio Digital Stereo (HDMI)")
        with mock.patch.object(audio.subprocess, "run", return_value=mock.Mock(returncode=0)) as run,                 mock.patch.object(audio, "query_sinks", side_effect=[[audio.Sink(42, "Old")], [hdmi]]),                 mock.patch.object(audio.time, "sleep"):
            self.assertEqual(audio.switch_to(output, {42}), hdmi)
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["pw-cli", "set-param", "47", "Profile"])
        self.assertEqual(json.loads(command[4]), {"index": 3, "save": True})

    def test_switching_fails_when_no_new_sink_appears(self):
        output = audio.ProfileOutput(47, 3, "HDMI")
        with mock.patch.object(audio.subprocess, "run", return_value=mock.Mock(returncode=0)),                 mock.patch.object(audio, "query_sinks", return_value=[audio.Sink(42, "Old")]),                 mock.patch.object(audio.time, "sleep"),                 mock.patch.object(audio.time, "monotonic", side_effect=[0, 1, 5]):
            with self.assertRaisesRegex(RuntimeError, "did not appear"):
                audio.switch_to(output, {42})


if __name__ == "__main__":
    unittest.main()
