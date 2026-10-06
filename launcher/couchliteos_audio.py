#!/usr/bin/python3
"""PipeWire output discovery and selection for the launcher."""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Collection

import couchliteos_settings as settings


@dataclasses.dataclass(frozen=True)
class Sink:
    id: int
    name: str
    default: bool = False


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
SINK_RE = re.compile(r"^[\s│├└─]*(\*)?\s*(\d+)\.\s+(.+?)(?:\s+\[[^]]*\])?\s*$")


def parse_sinks(output: str, section: str = "Sinks") -> list[Sink]:
    """The Audio section's sinks (or, with section="Sources", its inputs) from `wpctl status`."""
    sinks: list[Sink] = []
    in_sinks = False
    for raw in output.splitlines():
        line = ANSI_RE.sub("", raw)
        if re.search(rf"\b{section}:\s*$", line):
            in_sinks = True
            continue
        if in_sinks and re.match(r"^[\s│├└─]*[A-Za-z][A-Za-z ]+:\s*$", line):
            break
        if not in_sinks:
            continue
        match = SINK_RE.match(line)
        if match:
            name = re.sub(r"\s+\[[^]]*\]\s*$", "", match.group(3)).strip()
            sinks.append(Sink(int(match.group(2)), name, bool(match.group(1))))
    return sinks


def parse_sources(output: str) -> list[Sink]:
    return parse_sinks(output, "Sources")


def _status() -> str:
    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    result = subprocess.run(
        ["wpctl", "status"], text=True, capture_output=True, check=False,
        timeout=5, env=environment,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or "wpctl status failed").strip())
    return result.stdout


def query_sinks() -> list[Sink]:
    return parse_sinks(_status())


def query_sources() -> list[Sink]:
    """Microphones and other inputs, the default one marked as with sinks."""
    return parse_sources(_status())


def set_default(sink_id: int) -> None:
    result = subprocess.run(
        ["wpctl", "set-default", str(sink_id)], text=True, capture_output=True,
        check=False, timeout=5,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or "wpctl set-default failed").strip())


@dataclasses.dataclass(frozen=True)
class ProfileOutput:
    """An output a sound card offers only under another profile.

    A plain HDA card plays on its analog jack or on one HDMI/DisplayPort output
    at a time; the others have no sink until the card's profile is switched.
    """

    device: int
    profile: int
    name: str


def _output_part(profile_name: str) -> str | None:
    return next((part for part in profile_name.split("+") if part.startswith("output:")), None)


def _output_label(description: str) -> str:
    label = description.split(" + ")[0].strip()
    return re.sub(r"\s+Output$", "", label)


def parse_profile_outputs(dump: str) -> list[ProfileOutput]:
    """Outputs that switching an ALSA card's profile would add, from `pw-dump`.

    One entry per output the active profile does not already play on, using the
    highest-priority profile that has it. Profiles reported unavailable (an
    HDMI/DP jack with no TV) are left out.
    """
    outputs: list[ProfileOutput] = []
    for item in json.loads(dump or "[]"):
        if not isinstance(item, dict) or item.get("type") != "PipeWire:Interface:Device":
            continue
        info = item.get("info") or {}
        props = info.get("props") or {}
        if props.get("device.api") != "alsa" or props.get("media.class") != "Audio/Device":
            continue
        params = info.get("params") or {}
        active = (params.get("Profile") or [{}])[0].get("name", "")
        playing = _output_part(active)
        best: dict[str, dict] = {}
        for profile in params.get("EnumProfile") or []:
            output = _output_part(profile.get("name", ""))
            if not output or output == playing or profile.get("available") == "no":
                continue
            if output not in best or profile.get("priority", 0) > best[output].get("priority", 0):
                best[output] = profile
        device_name = props.get("device.description") or props.get("device.nick") or "Sound card"
        for profile in sorted(best.values(), key=lambda p: -p.get("priority", 0)):
            label = _output_label(profile.get("description") or profile["name"])
            outputs.append(ProfileOutput(int(item["id"]), int(profile["index"]), f"{device_name} {label}"))
    return outputs


def query_profile_outputs() -> list[ProfileOutput]:
    """Like parse_profile_outputs on the live graph; empty if PipeWire can't say."""
    try:
        result = subprocess.run(
            ["pw-dump", "--no-colors"], text=True, capture_output=True, check=False, timeout=5,
        )
        if result.returncode:
            return []
        return parse_profile_outputs(result.stdout)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return []


def switch_to(output: ProfileOutput, previous: Collection[int], wait: float = 3.0) -> Sink:
    """Switch the card to `output`'s profile and return the sink that appears.

    The choice is saved, so WirePlumber restores it after a restart and keeps
    it while the output is available (instead of following the TV).
    """
    result = subprocess.run(
        ["pw-cli", "set-param", str(output.device), "Profile",
         json.dumps({"index": output.profile, "save": True})],
        text=True, capture_output=True, check=False, timeout=5,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or "pw-cli set-param failed").strip())
    deadline = time.monotonic() + wait
    while True:
        new = [sink for sink in query_sinks() if sink.id not in previous]
        if new:
            return new[0]
        if time.monotonic() >= deadline:
            raise RuntimeError("the output did not appear")
        time.sleep(0.25)


DEFAULT_SINK = "@DEFAULT_AUDIO_SINK@"
VOLUME_STEP = 5  # percent
SANE_VOLUME = 0.5
VOLUME_RE = re.compile(r"Volume:\s*(\d+(?:\.\d+)?)(\s+\[MUTED\])?")


@dataclasses.dataclass(frozen=True)
class Volume:
    percent: int
    muted: bool


def parse_volume(output: str) -> Volume:
    """Parse `wpctl get-volume` output such as "Volume: 0.50 [MUTED]"."""
    match = VOLUME_RE.search(output)
    if not match:
        raise RuntimeError("wpctl reported no volume")
    return Volume(round(float(match.group(1)) * 100), bool(match.group(2)))


def _wpctl(*arguments: str) -> str:
    result = subprocess.run(
        ["wpctl", *arguments], text=True, capture_output=True, check=False, timeout=5,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or f"wpctl {arguments[0]} failed").strip())
    return result.stdout


def get_volume(target: str = DEFAULT_SINK) -> Volume:
    return parse_volume(_wpctl("get-volume", target))


def step_volume(step: int, target: str = DEFAULT_SINK) -> None:
    """Raise or lower the volume by `step` percent, never above 100%, without reading it back
    (one wpctl call: the volume keys step it while held, and nothing shows the level)."""
    _wpctl("set-volume", "-l", "1.0", target, f"{abs(step)}%{'+' if step > 0 else '-'}")


def change_volume(step: int, target: str = DEFAULT_SINK) -> Volume:
    """step_volume(), then the new level for the Guide menu and Settings to show."""
    step_volume(step, target)
    return get_volume(target)


def toggle_mute(target: str = DEFAULT_SINK) -> Volume:
    _wpctl("set-mute", target, "toggle")
    return get_volume(target)


def ensure_audible(sink_id: int) -> str:
    """Unmute a newly chosen output and lift it off 0%; return what was changed."""
    target = str(sink_id)
    volume = get_volume(target)
    changes = []
    if volume.muted:
        _wpctl("set-mute", target, "0")
        changes.append("UNMUTED")
    if volume.percent == 0:
        _wpctl("set-volume", target, str(SANE_VOLUME))
        changes.append(f"VOLUME SET TO {round(SANE_VOLUME * 100)}%")
    return ", ".join(changes)


# ---------------------------------------------------------------------------
# The PipeWire graph (`pw-dump`): Bluetooth outputs, microphones, recordings.
# ---------------------------------------------------------------------------

DEFAULT_SOURCE = "@DEFAULT_AUDIO_SOURCE@"
SECTION = "audio"  # config.ini: the mic choice and the Bluetooth delays
# The Bluetooth audio device used last; couchliteos-bluetoothd reconnects it at boot.
LAST_DEVICE = pathlib.Path("/var/lib/couchliteos/bluetooth-audio-last")
# One line the launcher shows for a few seconds ("AUDIO: WH-1000XM5").
NOTICE = pathlib.Path("/run/couchliteos/audio-notice")
NOTICE_SECONDS = 8.0
DELAY_STEP = 10  # milliseconds
DELAY_MAX = 500
MAC_RE = re.compile(r"(?:[0-9A-F]{2}[:_-]){5}[0-9A-F]{2}", re.I)
A2DP_PREFIX = "a2dp-sink"
HEADSET_PREFIXES = ("headset-head-unit", "handsfree-head-unit")
BLUETOOTH_MIC = "bluetooth:"  # [audio] mic = bluetooth:AA:BB:CC:DD:EE:FF
CODEC_LABELS = {
    "sbc": "SBC", "sbc_xq": "SBC XQ", "aac": "AAC", "aac_eld": "AAC-ELD", "aptx": "APTX",
    "aptx_hd": "APTX HD", "aptx_ll": "APTX LL", "aptx_ll_duplex": "APTX LL", "ldac": "LDAC",
    "lc3": "LC3", "lc3plus_h3": "LC3PLUS", "opus_05": "OPUS", "faststream": "FASTSTREAM",
    "msbc": "MSBC (HEADSET)", "cvsd": "CVSD (HEADSET)", "lc3_swb": "LC3-SWB (HEADSET)",
}


def normal_address(value: str) -> str:
    """AA:BB:CC:DD:EE:FF from any spelling (bluez_output.aa_bb_..., AA-BB-...); "" when none."""
    match = MAC_RE.search(value or "")
    return match.group(0).replace("_", ":").replace("-", ":").upper() if match else ""


@dataclasses.dataclass(frozen=True)
class Node:
    id: int
    name: str
    description: str
    media_class: str
    props: dict = dataclasses.field(default_factory=dict, compare=False, hash=False, repr=False)

    @property
    def bluetooth(self) -> str:
        """The device address of a Bluetooth node, else ""."""
        if self.props.get("device.api") != "bluez5" and not self.name.startswith("bluez_"):
            return ""
        return normal_address(str(self.props.get("api.bluez5.address") or self.name))

    @property
    def codec(self) -> str:
        raw = str(self.props.get("api.bluez5.codec") or "").lower()
        return CODEC_LABELS.get(raw, raw.upper())


@dataclasses.dataclass(frozen=True)
class Device:
    id: int
    address: str  # Bluetooth address; "" for other sound cards
    description: str
    profile: str  # the active profile's name
    profiles: tuple = ()  # (index, name, priority, available) from EnumProfile
    routes: tuple = ()  # (index, device, direction) from Route


@dataclasses.dataclass
class Graph:
    nodes: list = dataclasses.field(default_factory=list)
    devices: list = dataclasses.field(default_factory=list)
    default_sink: str = ""  # node.name
    default_source: str = ""
    targets: dict = dataclasses.field(default_factory=dict)  # stream id -> the output it is pinned to

    def of_class(self, media_class: str) -> list[Node]:
        return [node for node in self.nodes if node.media_class == media_class]

    def sinks(self) -> list[Node]:
        return self.of_class("Audio/Sink")

    def sources(self) -> list[Node]:
        return self.of_class("Audio/Source")

    def node(self, name: str) -> Node | None:
        return next((node for node in self.nodes if node.name == name), None)

    def bluetooth_sinks(self) -> dict[str, Node]:
        """Address -> output of each connected Bluetooth audio device."""
        return {node.bluetooth: node for node in self.sinks() if node.bluetooth}

    def bluetooth_devices(self) -> dict[str, Device]:
        return {device.address: device for device in self.devices if device.address}

    def recording(self) -> bool:
        """Something records from a microphone (not a program capturing what plays)."""
        return any(
            str(node.props.get("stream.capture.sink", "")).lower() not in ("true", "1")
            and str(node.props.get("stream.monitor", "")).lower() not in ("true", "1")
            for node in self.of_class("Stream/Input/Audio")
        )

    def codec(self, address: str) -> str:
        node = self.bluetooth_sinks().get(normal_address(address))
        return node.codec if node else ""


def _metadata_name(value: object) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    return str(value.get("name") or "") if isinstance(value, dict) else ""


def parse_graph(dump: str) -> Graph:
    graph = Graph()
    for item in json.loads(dump or "[]"):
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        info = item.get("info") or {}
        props = info.get("props") or {}
        if kind == "PipeWire:Interface:Node" and props.get("media.class"):
            graph.nodes.append(Node(
                int(item["id"]), str(props.get("node.name") or ""),
                str(props.get("node.description") or props.get("node.nick") or props.get("node.name") or ""),
                str(props["media.class"]), props,
            ))
        elif kind == "PipeWire:Interface:Device" and props.get("media.class") == "Audio/Device":
            params = info.get("params") or {}
            bluez = props.get("device.api") == "bluez5"
            graph.devices.append(Device(
                int(item["id"]),
                normal_address(str(props.get("api.bluez5.address") or props.get("device.name") or "")) if bluez else "",
                str(props.get("device.description") or props.get("device.alias") or props.get("device.name") or ""),
                str(((params.get("Profile") or [{}])[0]).get("name") or ""),
                tuple(
                    (int(p.get("index", 0)), str(p.get("name") or ""), int(p.get("priority", 0)), p.get("available", "yes"))
                    for p in params.get("EnumProfile") or []
                ),
                tuple(
                    (int(r.get("index", 0)), int(r.get("device", 0)), str(r.get("direction") or ""))
                    for r in params.get("Route") or []
                ),
            ))
        elif kind == "PipeWire:Interface:Metadata" and (item.get("props") or {}).get("metadata.name") == "default":
            for entry in item.get("metadata") or []:
                key = entry.get("key")
                if key == "default.audio.sink":
                    graph.default_sink = _metadata_name(entry.get("value"))
                elif key == "default.audio.source":
                    graph.default_source = _metadata_name(entry.get("value"))
                elif key in ("target.object", "target.node") and entry.get("subject"):
                    graph.targets[int(entry["subject"])] = str(entry.get("value"))
    return graph


def query_graph() -> Graph | None:
    """The live graph, or None when PipeWire cannot say."""
    try:
        result = subprocess.run(
            ["pw-dump", "--no-colors"], text=True, capture_output=True, check=False, timeout=5,
        )
        if result.returncode:
            return None
        return parse_graph(result.stdout)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return None


def _pw(*command: str) -> None:
    result = subprocess.run(list(command), text=True, capture_output=True, check=False, timeout=5)
    if result.returncode:
        raise RuntimeError((result.stderr or f"{command[0]} failed").strip())


def choose_output(sink_id: int, graph: Graph | None = None) -> None:
    """Make sink_id the output now, a running stream included.

    Streams follow the default output unless a program pinned one to a sink; those
    pins are dropped so that a stream already playing moves too.
    """
    set_default(sink_id)
    graph = graph if graph is not None else query_graph()
    if graph is None:
        return
    playing = {node.id for node in graph.of_class("Stream/Output/Audio")}
    for stream in sorted(set(graph.targets) & playing):
        for key in ("target.object", "target.node"):
            try:
                _pw("pw-metadata", "-d", str(stream), key)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                pass


def set_profile(device_id: int, index: int) -> None:
    """Switch a card's profile for now; not saved (the headset switch is temporary)."""
    _pw("pw-cli", "set-param", str(device_id), "Profile", json.dumps({"index": index, "save": False}))


def best_profile(device: Device, prefixes: tuple[str, ...] | str) -> tuple[int, str] | None:
    candidates = [
        (priority, index, name) for index, name, priority, available in device.profiles
        if name.startswith(prefixes) and available != "no"
    ]
    if not candidates:
        return None
    _priority, index, name = max(candidates)
    return index, name


# --- per-device settings ([audio] in config.ini) ---

def _delay_key(address: str) -> str:
    return "bt_delay_" + normal_address(address).replace(":", "").lower()


def load_delay(address: str, path: pathlib.Path = settings.CONFIG) -> int:
    """The BLUETOOTH DELAY saved for this device, in milliseconds (0 when none)."""
    if not normal_address(address):
        return 0
    return settings.get_int(settings.read_section(SECTION, path), _delay_key(address), 0, 0, DELAY_MAX)


def save_delay(address: str, milliseconds: int, path: pathlib.Path = settings.CONFIG) -> int:
    if not normal_address(address):
        raise ValueError("not a Bluetooth address")
    value = max(0, min(DELAY_MAX, int(milliseconds) // DELAY_STEP * DELAY_STEP))
    settings.update_section(SECTION, {_delay_key(address): value}, path)
    return value


def apply_delay(graph: Graph, address: str, milliseconds: int) -> bool:
    """Set the latency offset of a Bluetooth device's output; False when it has none."""
    device = graph.bluetooth_devices().get(normal_address(address))
    routes = [route for route in device.routes if route[2] == "Output"] if device else []
    for index, route_device, _direction in routes:
        _pw("pw-cli", "set-param", str(device.id), "Route", json.dumps({
            "index": index, "device": route_device,
            "props": {"latencyOffsetNsec": int(milliseconds) * 1_000_000}, "save": False,
        }))
    return bool(routes)


def chosen_mic(path: pathlib.Path = settings.CONFIG) -> str:
    """The address of the Bluetooth headset chosen as microphone, else ""."""
    value = settings.read_section(SECTION, path).get("mic", "")
    return normal_address(value) if value.startswith(BLUETOOTH_MIC) else ""


@dataclasses.dataclass(frozen=True)
class MicChoice:
    label: str
    source_id: int | None  # None: a Bluetooth headset whose mic exists only in its headset mode
    address: str = ""
    default: bool = False


def mic_choices(sources: list[Sink], graph: Graph | None, chosen: str = "") -> list[MicChoice]:
    """The inputs to pick from, plus each Bluetooth headset with a mic it is not using yet."""
    choices = [MicChoice(source.name, source.id, default=source.default) for source in sources]
    if graph is not None:
        by_id = {node.id: node for node in graph.sources()}
        choices = [
            dataclasses.replace(choice, address=by_id[choice.source_id].bluetooth)
            if choice.source_id in by_id else choice
            for choice in choices
        ]
        present = {choice.address for choice in choices if choice.address}
        for address, device in sorted(graph.bluetooth_devices().items()):
            if address not in present and best_profile(device, HEADSET_PREFIXES):
                choices.append(MicChoice(f"{device.description} (HEADSET MIC)", None, address))
    if chosen:
        choices = [dataclasses.replace(choice, default=choice.address == chosen) for choice in choices]
    return choices


def choose_mic(choice: MicChoice, path: pathlib.Path = settings.CONFIG) -> None:
    """Record from this input. A Bluetooth headset's mic is remembered and switched on
    only while something records (its headset mode lowers the sound quality)."""
    if choice.source_id is not None:
        set_default(choice.source_id)
    settings.update_section(SECTION, {"mic": f"{BLUETOOTH_MIC}{choice.address}" if choice.address else "default"}, path)


def get_mic_level() -> Volume:
    return get_volume(DEFAULT_SOURCE)


def change_mic_level(step: int) -> Volume:
    return change_volume(step, DEFAULT_SOURCE)


def toggle_mic_mute() -> Volume:
    return toggle_mute(DEFAULT_SOURCE)


def mic_state(graph: Graph | None, volume: Volume | None) -> str:
    """"none" (no input), "muted", "idle" (nothing records) or "live" (it hears the room now)."""
    if graph is None or not graph.sources() and not graph.recording():
        return "none"
    if volume is not None and volume.muted:
        return "muted"
    return "live" if graph.recording() else "idle"


def query_mic_state() -> str:
    graph = query_graph()
    try:
        volume = get_mic_level()
    except (OSError, RuntimeError, subprocess.SubprocessError):
        volume = None  # unknown mute state: a recording still counts as live
    return mic_state(graph, volume)


def mic_live() -> bool:
    """For the top bar: True while an unmuted mic records. A mic is never live without the icon."""
    return query_mic_state() == "live"


MIC_LIVE_TEXT = "MIC LIVE"


class MicMonitor:
    """The mic state, refreshed off the UI thread, for the footer marker."""

    def __init__(self, reader: Callable[[], str] = query_mic_state, interval: float = 3.0) -> None:
        self.reader = reader
        self.interval = interval
        self.state = "none"
        self._stop = threading.Event()

    def refresh(self) -> None:
        try:
            self.state = self.reader()
        except Exception:  # a failed read must never reach the launcher
            self.state = "none"

    def live(self) -> bool:
        return self.state == "live"

    def line(self) -> str:
        return MIC_LIVE_TEXT if self.live() else ""

    def start(self) -> None:
        def loop() -> None:
            while True:
                self.refresh()
                if self._stop.wait(self.interval):
                    return

        threading.Thread(target=loop, name="mic-state", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


MIC_TEST_SECONDS = 3


def mic_test(
    seconds: float = MIC_TEST_SECONDS,
    progress: Callable[[str], None] = lambda _stage: None,
    *,
    directory: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Record from the default input for a few seconds, then play it back.

    The recording is a temporary file, removed afterwards whatever happens.
    """
    descriptor, path = tempfile.mkstemp(prefix="couchliteos-mic-test-", suffix=".wav", dir=directory)
    os.close(descriptor)
    try:
        progress("recording")
        process = subprocess.Popen(
            ["pw-record", path], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            sleep(seconds)
        finally:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if os.path.getsize(path) <= 44:  # a WAV header and nothing else
            raise RuntimeError("NOTHING WAS RECORDED")
        progress("playing")
        result = subprocess.run(
            ["pw-play", path], text=True, capture_output=True, check=False, timeout=seconds + 10,
        )
        if result.returncode:
            raise RuntimeError((result.stderr or "pw-play failed").strip())
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


# --- notices for the launcher ---

def write_notice(text: str, path: pathlib.Path = NOTICE) -> None:
    try:
        settings.atomic_write(path, (text.strip() + "\n").encode("utf-8"), 0o644)
    except OSError:
        pass


def read_notice(path: pathlib.Path = NOTICE, max_age: float = NOTICE_SECONDS, now: Callable[[], float] = time.time) -> str:
    """The last audio change ("AUDIO: WH-1000XM5"), while it is recent."""
    try:
        if now() - path.stat().st_mtime > max_age:
            return ""
        return path.read_text(encoding="utf-8", errors="replace").strip()[:96]
    except OSError:
        return ""


def _write_last_device(address: str, path: pathlib.Path) -> None:
    try:
        if path.read_text(encoding="utf-8").strip() == address:
            return
    except OSError:
        pass
    try:
        settings.atomic_write(path, f"{address}\n".encode("ascii"), 0o644)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# The watcher, run by couchliteos-audio beside PipeWire and WirePlumber.
# ---------------------------------------------------------------------------

class Watcher:
    """Moves the sound to a Bluetooth audio device when it connects and back when it
    goes, applies its saved delay, and keeps a headset in its headset (mic) mode only
    while something records from it.

    It reads the whole graph (`pw-dump`) every few seconds. Devices are keyed by
    their Bluetooth address, so the output node that is replaced by a profile switch
    is not taken for a disconnect.
    """

    def __init__(
        self,
        query: Callable[[], Graph | None] = query_graph,
        *,
        config: pathlib.Path = settings.CONFIG,
        last_device: pathlib.Path = LAST_DEVICE,
        notice: Callable[[str], None] = write_notice,
    ) -> None:
        self.query = query
        self.config = config
        self.last_device = last_device
        self.notice = notice
        self.seen: set[str] | None = None  # Bluetooth audio devices at the last look
        self.pending: set[str] = set()  # connected, but its output is not there yet
        self.previous: list[str] = []  # outputs used before a Bluetooth device, latest last
        self.routed = ""  # the Bluetooth device the sound plays on (our choice or the user's)
        self.follow = ""  # the sound goes back to this device once its new output appears
        self.headset: set[str] = set()  # devices we switched to headset mode

    def _choose(self, node: Node, graph: Graph) -> bool:
        try:
            choose_output(node.id, graph)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            print(f"audio: could not select {node.name}: {error}", file=sys.stderr)
            return False
        graph.default_sink = node.name
        return True

    def _apply_delay(self, graph: Graph, address: str) -> None:
        try:
            delay = load_delay(address, self.config)
            if delay:
                apply_delay(graph, address, delay)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            print(f"audio: delay not applied to {address}: {error}", file=sys.stderr)

    def _connected(self, graph: Graph, address: str) -> None:
        node = graph.bluetooth_sinks()[address]
        current = graph.default_sink
        if current and current != node.name:
            if current in self.previous:
                self.previous.remove(current)
            self.previous.append(current)
            del self.previous[:-8]
        if self._choose(node, graph):
            self.routed = address
            self.notice(f"AUDIO: {node.description.upper()}")
        self._apply_delay(graph, address)
        _write_last_device(address, self.last_device)

    def _disconnected(self, graph: Graph, address: str) -> None:
        self.headset.discard(address)
        if self.follow == address:
            self.follow = ""
        if self.routed != address:
            return  # the sound was elsewhere: leave it there
        self.routed = ""
        sinks = {node.name: node for node in graph.sinks()}
        for name in reversed(self.previous):
            node = sinks.get(name)
            if node is not None and self._choose(node, graph):
                self.previous.remove(name)
                self.routed = node.bluetooth
                self.notice(f"AUDIO BACK ON {node.description.upper()}")
                return
        fallback = sinks.get(graph.default_sink)
        if fallback is not None:  # WirePlumber picked the next best output (the TV first)
            self.notice(f"AUDIO BACK ON {fallback.description.upper()}")

    def _headset_mode(self, graph: Graph) -> None:
        """A Bluetooth headset chosen as the mic is in its headset mode while something
        records, and back in high quality playback otherwise."""
        wanted = chosen_mic(self.config) if graph.recording() else ""
        for address, device in sorted(graph.bluetooth_devices().items()):
            in_headset = device.profile.startswith(HEADSET_PREFIXES)
            target = None
            if address == wanted and not in_headset:
                target = best_profile(device, HEADSET_PREFIXES)
            elif address != wanted and in_headset and address in self.headset:
                target = best_profile(device, A2DP_PREFIX)
            if target is None:
                continue
            try:
                set_profile(device.id, target[0])
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                print(f"audio: profile {target[1]} not set on {address}: {error}", file=sys.stderr)
                continue
            if address == wanted:
                self.headset.add(address)
            else:
                self.headset.discard(address)
            if self.routed == address:
                self.follow = address

    def _follow(self, graph: Graph) -> None:
        """After a profile switch the device has a new output (and in headset mode an input)."""
        if not self.follow:
            return
        node = graph.bluetooth_sinks().get(self.follow)
        if node is None:
            return
        if graph.default_sink != node.name:
            self._choose(node, graph)
        if self.follow in self.headset:
            source = next((n for n in graph.sources() if n.bluetooth == self.follow), None)
            if source is None:
                return  # its mic is not there yet: look again next time
            if graph.default_source != source.name:
                try:
                    set_default(source.id)
                except (OSError, RuntimeError, subprocess.SubprocessError):
                    pass
        self._apply_delay(graph, self.follow)
        self.follow = ""

    def poll(self) -> None:
        graph = self.query()
        if graph is None:
            return
        sinks = graph.bluetooth_sinks()
        devices = set(graph.bluetooth_devices()) | set(sinks)
        default = graph.node(graph.default_sink)
        if self.seen is None:  # started (or restarted): take things as they are
            self.seen = devices
            self.routed = default.bluetooth if default else ""
            for address in sinks:
                self._apply_delay(graph, address)
            return
        gone = sorted(self.seen - devices)
        for address in gone:  # judged by where the sound was at the last look
            self.pending.discard(address)
            self._disconnected(graph, address)
        if not gone and default is not None and not self.follow:
            self.routed = default.bluetooth  # the user may have picked another output
        self.pending |= devices - self.seen
        for address in sorted(self.pending & set(sinks)):
            self.pending.discard(address)
            self._connected(graph, address)
        self._headset_mode(graph)
        self._follow(graph)
        self.seen = devices

    def run(self, interval: float = 2.0) -> None:
        while True:
            try:
                self.poll()
            except Exception as error:  # keep watching; PipeWire may be restarting
                print(f"audio: watcher: {error}", file=sys.stderr)
            time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["watch"]:
        Watcher().run()
        return 0
    print("usage: couchliteos_audio.py watch", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
