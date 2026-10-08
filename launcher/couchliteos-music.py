#!/usr/bin/python3
"""The TV interface's background music player (couchliteos_music drives it over stdin).

    play <path>              load the track and loop it, silent and paused
    level <0..1> <seconds>   ramp the volume there; at 0 it pauses (and resumes from there)
    quit

GStreamer playbin, looped gaplessly with about-to-finish. Exits 3 without GStreamer, 0 on quit
or end of input (the TV interface went away), and never plays louder than 1.0.
"""

from __future__ import annotations

import pathlib
import sys
import threading

INIT_FAILED = 3
STEP = 1 / 30  # s between volume steps of a ramp


def main() -> int:
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import GLib, Gst
    except (ImportError, ValueError):
        return INIT_FAILED
    Gst.init(None)
    player = Gst.ElementFactory.make("playbin", "music")
    if player is None:
        return INIT_FAILED
    player.set_property("volume", 0.0)
    flags = player.get_property("flags")
    player.set_property("flags", int(flags) & ~0x1 & ~0x4)  # no video, no subtitles
    loop = GLib.MainLoop()
    state = {"uri": "", "level": 0.0, "ramp": None}

    def again(_element):  # gapless loop: queue the same track before this one ends
        if state["uri"]:
            player.set_property("uri", state["uri"])

    player.connect("about-to-finish", again)
    bus = player.get_bus()
    bus.add_signal_watch()

    def on_message(_bus, message):
        if message.type == Gst.MessageType.ERROR:
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
