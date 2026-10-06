"""Bluetooth audio: the output list, switching, the watcher, delays and the headset mode.

PipeWire is faked: graphs are built as `pw-dump` JSON and commands are recorded.
"""

import json
import pathlib
import tempfile
import unittest
from unittest import mock

import couchliteos_audio as audio
import couchliteos_audiomenu as audiomenu

HEADPHONES = "AA:BB:CC:DD:EE:01"
HEADSET = "AA:BB:CC:DD:EE:02"


def node(node_id, name, media_class="Audio/Sink", description=None, **props):
    return {
        "id": node_id,
        "type": "PipeWire:Interface:Node",
        "info": {"props": {"node.name": name, "node.description": description or name,
                           "media.class": media_class, **props}},
    }


def bt_sink(node_id, address, description, codec="sbc", headset=False):
    suffix = "headset-head-unit" if headset else "1"
    return node(node_id, f"bluez_output.{address.replace(':', '_')}.{suffix}", description=description,
                **{"device.api": "bluez5", "api.bluez5.address": address, "api.bluez5.codec": codec})


def bt_source(node_id, address, description):
    return node(node_id, f"bluez_input.{address.replace(':', '_')}.0", "Audio/Source", description,
                **{"device.api": "bluez5", "api.bluez5.address": address, "api.bluez5.codec": "msbc"})


def bt_device(device_id, address, description, profile="a2dp-sink", headset=True):
    profiles = [{"index": 0, "name": "off", "priority": 0},
                {"index": 1, "name": "a2dp-sink", "priority": 40},
                {"index": 2, "name": "a2dp-sink-sbc", "priority": 38}]
    if headset:
        profiles.append({"index": 3, "name": "headset-head-unit", "priority": 30})
    return {
        "id": device_id,
        "type": "PipeWire:Interface:Device",
        "info": {
            "props": {"device.api": "bluez5", "media.class": "Audio/Device",
                      "api.bluez5.address": address, "device.description": description},
            "params": {
                "EnumProfile": profiles,
                "Profile": [{"index": 0, "name": profile}],
                "Route": [{"index": 0, "device": 0, "direction": "Output"},
                          {"index": 1, "device": 1, "direction": "Input"}],
            },
        },
    }


def metadata(sink="", source="", targets=()):
    entries = []
    if sink:
        entries.append({"subject": 0, "key": "default.audio.sink", "type": "Spa:String:JSON", "value": {"name": sink}})
    if source:
        entries.append({"subject": 0, "key": "default.audio.source", "type": "Spa:String:JSON",
                        "value": json.dumps({"name": source})})
    for subject, target in targets:
        entries.append({"subject": subject, "key": "target.object", "value": target})
    return {"id": 30, "type": "PipeWire:Interface:Metadata", "props": {"metadata.name": "default"}, "metadata": entries}


TV = node(40, "alsa_output.pci-0000_00_1f.3.hdmi-stereo", description="TV (HDMI)")
SPEAKER = node(41, "alsa_output.usb-speaker.analog-stereo", description="USB Speaker")
USB_MIC = node(50, "alsa_input.usb-mic.mono", "Audio/Source", "USB Mic")
RECORDING = node(90, "pw-record", "Stream/Input/Audio")
PLAYING = node(91, "moonlight", "Stream/Output/Audio")


def graph(*items, sink="", source="", targets=()):
    return audio.parse_graph(json.dumps([{"id": 0, "type": "PipeWire:Interface:Core"}, *items,
                                         metadata(sink, source, targets)]))


class Commands:
    """Records every command; wpctl and pw-* all succeed."""

    def __init__(self):
        self.calls = []

    def __call__(self, command, **_kwargs):
        self.calls.append(list(command))
        return mock.Mock(returncode=0, stdout="", stderr="")

    def of(self, *prefix):
        return [call for call in self.calls if call[:len(prefix)] == list(prefix)]


class GraphTest(unittest.TestCase):
    def test_bluetooth_outputs_are_found_by_address_with_their_codec(self):
        found = graph(TV, bt_sink(60, HEADPHONES, "WH-1000XM5", "ldac"), bt_device(70, HEADPHONES, "WH-1000XM5"),
                      sink=TV["info"]["props"]["node.name"])
        self.assertEqual(list(found.bluetooth_sinks()), [HEADPHONES])
        self.assertEqual(found.codec(HEADPHONES), "LDAC")
        self.assertEqual(found.codec(HEADPHONES.lower().replace(":", "_")), "LDAC")
        self.assertEqual(found.default_sink, "alsa_output.pci-0000_00_1f.3.hdmi-stereo")
        self.assertEqual(found.bluetooth_devices()[HEADPHONES].routes[0], (0, 0, "Output"))
        self.assertEqual(found.sinks()[0].bluetooth, "")

    def test_default_source_metadata_may_be_a_json_string(self):
        self.assertEqual(graph(USB_MIC, source="alsa_input.usb-mic.mono").default_source, "alsa_input.usb-mic.mono")

    def test_capturing_what_plays_is_not_recording(self):
        monitor = node(92, "obs", "Stream/Input/Audio", **{"stream.capture.sink": "true"})
        self.assertFalse(graph(monitor).recording())
        self.assertTrue(graph(monitor, RECORDING).recording())

    def test_output_rows_mark_bluetooth_and_its_codec(self):
        found = graph(TV, bt_sink(60, HEADPHONES, "WH-1000XM5", "aac"))
        self.assertEqual(audiomenu.output_label(audio.Sink(60, "WH-1000XM5", True), found),
                         "*  WH-1000XM5  (BLUETOOTH · AAC)")
        self.assertEqual(audiomenu.output_label(audio.Sink(40, "TV (HDMI)"), found), "   TV (HDMI)")
        self.assertEqual(audiomenu.output_label(audio.Sink(40, "TV (HDMI)"), None), "   TV (HDMI)")

    def test_query_graph_is_none_when_pipewire_cannot_answer(self):
        for outcome in (FileNotFoundError("pw-dump"), mock.Mock(returncode=1, stdout="")):
            with self.subTest(outcome=outcome), mock.patch.object(
                audio.subprocess, "run", side_effect=outcome if isinstance(outcome, Exception) else None,
                return_value=outcome,
            ):
                self.assertIsNone(audio.query_graph())


class ChooseOutputTest(unittest.TestCase):
    def test_choosing_an_output_during_a_stream_moves_the_stream(self):
        commands = Commands()
        playing = graph(TV, bt_sink(60, HEADPHONES, "WH-1000XM5"), PLAYING,
                        sink=TV["info"]["props"]["node.name"], targets=[(91, "alsa_output.pci-0000_00_1f.3.hdmi-stereo")])
        with mock.patch.object(audio.subprocess, "run", side_effect=commands):
            audio.choose_output(60, playing)
        self.assertEqual(commands.calls[0], ["wpctl", "set-default", "60"])
        self.assertIn(["pw-metadata", "-d", "91", "target.object"], commands.calls)

    def test_streams_that_follow_the_default_are_left_alone(self):
        commands = Commands()
        with mock.patch.object(audio.subprocess, "run", side_effect=commands):
            audio.choose_output(60, graph(TV, PLAYING))
        self.assertEqual(commands.calls, [["wpctl", "set-default", "60"]])

    def test_a_refused_switch_is_raised(self):
        with mock.patch.object(audio.subprocess, "run", return_value=mock.Mock(returncode=1, stderr="nope")):
            with self.assertRaisesRegex(RuntimeError, "nope"):
                audio.choose_output(60, graph(TV))


class DelayTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = pathlib.Path(self.directory.name) / "config.ini"

    def tearDown(self):
        self.directory.cleanup()

    def test_each_device_keeps_its_own_delay(self):
        self.config.write_text("[power]\nblank_minutes = 5\n", encoding="utf-8")
        self.assertEqual(audio.load_delay(HEADPHONES, self.config), 0)
        self.assertEqual(audio.save_delay(HEADPHONES, 180, self.config), 180)
        self.assertEqual(audio.save_delay(HEADSET.lower(), 2000, self.config), audio.DELAY_MAX)
        self.assertEqual(audio.load_delay(HEADPHONES, self.config), 180)
        self.assertEqual(audio.load_delay(HEADSET, self.config), audio.DELAY_MAX)
        self.assertEqual(audio.save_delay(HEADPHONES, -30, self.config), 0)
        text = self.config.read_text(encoding="utf-8")
        self.assertIn("[power]\nblank_minutes = 5\n", text)
        self.assertIn("bt_delay_aabbccddee02 = 500", text)

    def test_a_delay_needs_an_address(self):
        with self.assertRaises(ValueError):
            audio.save_delay("not a device", 100, self.config)
        self.assertEqual(audio.load_delay("", self.config), 0)

    def test_delay_is_set_as_the_output_route_latency_offset(self):
        commands = Commands()
        with mock.patch.object(audio.subprocess, "run", side_effect=commands):
            self.assertTrue(audio.apply_delay(graph(bt_device(70, HEADPHONES, "WH")), HEADPHONES, 150))
            self.assertFalse(audio.apply_delay(graph(TV), HEADPHONES, 150))
        self.assertEqual(len(commands.calls), 1)
        command = commands.calls[0]
        self.assertEqual(command[:4], ["pw-cli", "set-param", "70", "Route"])
        self.assertEqual(json.loads(command[4]), {"index": 0, "device": 0,
                                                  "props": {"latencyOffsetNsec": 150_000_000}, "save": False})


class WatcherCase(unittest.TestCase):
    TV_NAME = TV["info"]["props"]["node.name"]
    SPEAKER_NAME = SPEAKER["info"]["props"]["node.name"]

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.directory.name)
        self.config = root / "config.ini"
        self.last = root / "bluetooth-audio-last"
        self.graphs = []
        self.notices = []
        self.commands = Commands()
        patcher = mock.patch.object(audio.subprocess, "run", side_effect=self.commands)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.watcher = audio.Watcher(lambda: self.graphs.pop(0), config=self.config, last_device=self.last,
                                     notice=self.notices.append)

    def tearDown(self):
        self.directory.cleanup()

    def look(self, *items, sink="", source=""):
        self.graphs.append(graph(*items, sink=sink, source=source))
        self.watcher.poll()

    def defaults(self):
        return [call[2] for call in self.commands.of("wpctl", "set-default")]


class WatcherTest(WatcherCase):
    def test_connect_moves_the_sound_and_disconnect_brings_it_back(self):
        self.look(TV, SPEAKER, sink=self.SPEAKER_NAME)  # the user chose the USB speaker
        self.assertEqual(self.defaults(), [], "nothing moves on start")
        self.look(TV, SPEAKER, bt_device(70, HEADPHONES, "WH-1000XM5"), bt_sink(60, HEADPHONES, "WH-1000XM5"),
                  sink=self.SPEAKER_NAME)
        self.assertEqual(self.defaults(), ["60"])
        self.assertEqual(self.notices, ["AUDIO: WH-1000XM5"])
        self.assertEqual(self.last.read_text().strip(), HEADPHONES)
        # Gone: WirePlumber fell back to the TV, but the sound returns to the speaker in use before.
        self.look(TV, SPEAKER, sink=self.TV_NAME)
        self.assertEqual(self.defaults(), ["60", "41"])
        self.assertEqual(self.notices[-1], "AUDIO BACK ON USB SPEAKER")

    def test_fallback_order_is_latest_first_skipping_outputs_that_are_gone(self):
        headphones = [bt_device(70, HEADPHONES, "WH-1000XM5"), bt_sink(60, HEADPHONES, "WH-1000XM5")]
        headset = [bt_device(71, HEADSET, "Headset", headset=True), bt_sink(61, HEADSET, "Headset")]
        self.look(TV, SPEAKER, sink=self.TV_NAME)
        self.look(TV, SPEAKER, *headphones, sink=self.TV_NAME)  # TV -> headphones
        headphones_name = headphones[1]["info"]["props"]["node.name"]
        self.look(TV, SPEAKER, *headphones, *headset, sink=headphones_name)  # headphones -> headset
        self.assertEqual(self.defaults(), ["60", "61"])
        self.look(TV, SPEAKER, *headphones, sink=self.TV_NAME)  # headset off: back to the headphones
        self.assertEqual(self.defaults()[-1], "60")
        self.look(TV, SPEAKER, sink=self.SPEAKER_NAME)  # headphones off: back to the TV, the one before
        self.assertEqual(self.defaults()[-1], "40")
        self.assertEqual(self.notices[-1], "AUDIO BACK ON TV (HDMI)")

    def test_disconnect_leaves_the_sound_alone_when_the_user_moved_it(self):
        headphones = [bt_device(70, HEADPHONES, "WH-1000XM5"), bt_sink(60, HEADPHONES, "WH-1000XM5")]
        self.look(TV, SPEAKER, sink=self.SPEAKER_NAME)
        self.look(TV, SPEAKER, *headphones, sink=self.SPEAKER_NAME)
        self.look(TV, SPEAKER, *headphones, sink=self.TV_NAME)  # the user picked the TV
        self.look(TV, SPEAKER, sink=self.TV_NAME)
        self.assertEqual(self.defaults(), ["60"])

    def test_a_device_whose_output_comes_later_is_switched_to_then(self):
        self.look(TV, sink=self.TV_NAME)
        self.look(TV, bt_device(70, HEADPHONES, "WH"), sink=self.TV_NAME)
        self.assertEqual(self.defaults(), [])
        self.look(TV, bt_device(70, HEADPHONES, "WH"), bt_sink(60, HEADPHONES, "WH"), sink=self.TV_NAME)
        self.assertEqual(self.defaults(), ["60"])

    def test_the_saved_delay_is_applied_on_connect(self):
        audio.save_delay(HEADPHONES, 200, self.config)
        self.look(TV, sink=self.TV_NAME)
        self.look(TV, bt_device(70, HEADPHONES, "WH"), bt_sink(60, HEADPHONES, "WH"), sink=self.TV_NAME)
        routes = self.commands.of("pw-cli", "set-param", "70", "Route")
        self.assertEqual(len(routes), 1)
        self.assertEqual(json.loads(routes[0][4])["props"]["latencyOffsetNsec"], 200_000_000)

    def test_pipewire_not_answering_changes_nothing(self):
        self.graphs.append(None)
        self.watcher.poll()
        self.assertIsNone(self.watcher.seen)


class HeadsetModeTest(WatcherCase):
    """The headset (HFP) mode only while something records from the chosen headset."""

    def profiles(self):
        return [json.loads(call[4])["index"] for call in self.commands.of("pw-cli", "set-param", "71", "Profile")]

    def items(self, profile="a2dp-sink", recording=False, with_source=False):
        headset = profile.startswith("headset")
        items = [TV, USB_MIC, bt_device(71, HEADSET, "Headset", profile=profile),
                 bt_sink(62 if headset else 61, HEADSET, "Headset", "msbc" if headset else "sbc", headset=headset)]
        if with_source:
            items.append(bt_source(63, HEADSET, "Headset"))
        if recording:
            items.append(RECORDING)
        return items

    def test_headset_mode_only_while_recording_from_the_chosen_headset(self):
        audio.settings.update_section("audio", {"mic": f"bluetooth:{HEADSET}"}, self.config)
        sink_name = f"bluez_output.{HEADSET.replace(':', '_')}.1"
        self.look(*self.items(), sink=sink_name)
        self.look(*self.items(), sink=sink_name)
        self.assertEqual(self.profiles(), [], "nothing records: stays in high quality")
        self.look(*self.items(recording=True), sink=sink_name)
        self.assertEqual(self.profiles(), [3], "recording: headset mode")
        # The new output and the headset's mic become the defaults once they appear.
        self.look(*self.items("headset-head-unit", recording=True, with_source=True), sink=self.TV_NAME)
        self.assertEqual(self.defaults()[-2:], ["62", "63"])
        self.look(*self.items("headset-head-unit", with_source=True), sink=f"bluez_output.{HEADSET.replace(':', '_')}.headset-head-unit")
        self.assertEqual(self.profiles(), [3, 1], "recording stopped: back to the best A2DP profile")

    def test_recording_from_another_mic_keeps_high_quality(self):
        audio.settings.update_section("audio", {"mic": "default"}, self.config)
        self.look(*self.items(), sink=self.TV_NAME)
        self.look(*self.items(recording=True), sink=self.TV_NAME)
        self.assertEqual(self.profiles(), [])

    def test_a_headset_mode_the_user_chose_is_not_undone(self):
        self.look(*self.items("headset-head-unit"), sink=self.TV_NAME)
        self.look(*self.items("headset-head-unit"), sink=self.TV_NAME)
        self.assertEqual(self.profiles(), [])


if __name__ == "__main__":
    unittest.main()
