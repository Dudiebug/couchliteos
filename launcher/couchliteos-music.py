#!/usr/bin/python3
"""The TV interface's background music player (couchliteos_music drives it over stdin).

    play <path>              load the track and loop it, silent and paused
    seek <seconds>           go there (wrapped at the track's end): a new player resumes the old one
    level <0..1> <seconds>   ramp the volume there; at 0 it pauses (and resumes from there)
    quit

GStreamer playbin, looped gaplessly with about-to-finish, played through the first audio output
of SINKS (the ISO has pipewiresink; playbin's own choice needs autoaudiosink, which it has not).
Exits 3 without GStreamer, 4 without an audio output, 1 when GStreamer reports an error, 0 on quit
or end of input (the TV interface went away), and never plays louder than 1.0. Why it stopped goes
to stderr (couchliteos_music appends that to music.log).
"""

from __future__ import annotations

import pathlib
import sys
import threading
from collections.abc import Callable

PLAY_FAILED = 1
INIT_FAILED = 3
NO_SINK = 4
STEP = 1 / 30  # s between volume steps of a ramp
SINKS = ("pipewiresink", "autoaudiosink", "alsasink")  # the first that exists plays it
STREAM_PROPERTIES = 'props, media.name="CouchLiteOS music", application.name="CouchLiteOS music", media.role=Music'


def make_sink(make: Callable[[str], object]) -> tuple[str, object] | tuple[None, None]:
    """The first of SINKS that `make` (Gst.ElementFactory.make with a name) can make: (name, element),
    else (None, None)."""
    for name in SINKS:
        try:
            element = make(name)
        except Exception:  # noqa: BLE001 - a broken plugin is a missing one
            element = None
        if element is not None:
            return name, element
    return None, None


def name_stream(sink: object, structure: Callable[[str], object]) -> bool:
    """Name the PipeWire stream (the mixer shows "CouchLiteOS music", role Music) when the sink can
    take stream-properties; `structure` is Gst.Structure.new_from_string. False when it could not."""
    try:
        if sink.find_property("stream-properties") is None:
            return False
        properties = structure(STREAM_PROPERTIES)
        if properties is None:
            return False
        sink.set_property("stream-properties", properties)
    except Exception:  # noqa: BLE001 - an unnamed stream still plays
        return False
    return True


def main() -> int:
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import GLib, Gst
    except (ImportError, ValueError) as error:
        print(f"couchliteos-music: no GStreamer for Python: {error}", file=sys.stderr)
        return INIT_FAILED
    Gst.init(None)
    player = Gst.ElementFactory.make("playbin", "music")
    if player is None:
        print("couchliteos-music: GStreamer has no playbin (gstreamer1.0-plugins-base)", file=sys.stderr)
        return INIT_FAILED
    sink_name, sink = make_sink(lambda name: Gst.ElementFactory.make(name, "music-out"))
    if sink is None:
        print(f"couchliteos-music: no audio output: GStreamer has none of {', '.join(SINKS)}", file=sys.stderr)
        return NO_SINK
    if sink_name == "pipewiresink":
        name_stream(sink, lambda text: Gst.Structure.new_from_string(text))
    player.set_property("audio-sink", sink)
    player.set_property("volume", 0.0)
    flags = player.get_property("flags")
    player.set_property("flags", int(flags) & ~0x1 & ~0x4)  # no video, no subtitles
    loop = GLib.MainLoop()
    state = {"uri": "", "level": 0.0, "ramp": None, "failed": False}

    def again(_element):  # gapless loop: queue the same track before this one ends
        if state["uri"]:
            player.set_property("uri", state["uri"])

    player.connect("about-to-finish", again)
    bus = player.get_bus()
    bus.add_signal_watch()

    def on_message(_bus, message):
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            print(f"couchliteos-music: {sink_name}: {error.message} ({debug or 'no details'})", file=sys.stderr, flush=True)
            state["failed"] = True
            loop.quit()
        elif message.type == Gst.MessageType.EOS and state["uri"]:  # a track that cannot loop gaplessly
            player.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, 0)

    bus.connect("message", on_message)

    def ramp_to(level: float, seconds: float) -> None:
        if state["ramp"] is not None:
            GLib.source_remove(state["ramp"])
            state["ramp"] = None
        start = float(player.get_property("volume"))
        if level > 0 and state["uri"]:
            player.set_state(Gst.State.PLAYING)
        steps = max(1, round(seconds / STEP))
        done = {"step": 0}

        def step() -> bool:
            done["step"] += 1
            player.set_property("volume", start + (level - start) * done["step"] / steps)
            if done["step"] < steps:
                return True
            state["ramp"] = None
            if level <= 0:
                player.set_state(Gst.State.PAUSED)
            return False

        if seconds <= 0:
            done["step"] = steps - 1
            step()
        else:
            state["ramp"] = GLib.timeout_add(round(STEP * 1000), step)

    def command(line: str) -> bool:
        word, _, rest = line.strip().partition(" ")
        if word == "quit":
            loop.quit()
        elif word == "play" and rest:
            path = pathlib.Path(rest)
            if path.is_file():
                state["uri"] = path.resolve().as_uri()
                player.set_state(Gst.State.NULL)
                player.set_property("volume", 0.0)
                player.set_property("uri", state["uri"])
                player.set_state(Gst.State.PAUSED)
        elif word == "seek" and state["uri"]:
            try:
                offset = max(0.0, float(rest))
            except ValueError:
                return False
            player.get_state(2 * Gst.SECOND)  # the track has to be loaded (paused) before it can seek
            known, duration = player.query_duration(Gst.Format.TIME)
            if known and duration > 0:
                player.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                                   round(offset * Gst.SECOND) % duration)
        elif word == "level":
            try:
                level, seconds = (float(part) for part in rest.split())
            except ValueError:
                return False
            ramp_to(min(1.0, max(0.0, level)), min(10.0, max(0.0, seconds)))
        return False

    def read_input() -> None:
        for line in sys.stdin:
            GLib.idle_add(command, line)
        GLib.idle_add(lambda: loop.quit())

    threading.Thread(target=read_input, daemon=True).start()
    loop.run()
    player.set_state(Gst.State.NULL)
    return PLAY_FAILED if state["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
