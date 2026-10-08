#!/usr/bin/env python3
"""Make the TV interface's sounds and its background music, from nothing but this script.

    tools/make-sounds.py [OUT]       OUT defaults to overlay/usr/share/couchliteos

Writes OUT/sounds/<name>.wav (mono, 16 bit, 44.1 kHz) for every UI sound and
OUT/music/ambient.ogg: a seamless ambient loop (pads, a bell arpeggio and a soft bass in
D major, 32 bars at 68 bpm, about 113 s), encoded with ffmpeg (libvorbis). Everything is
synthesised here and deterministic (a fixed seed), so the files carry this project's licence and
running it again gives the same sound. Pure Python, no numpy: the music takes a minute or two.
"""

from __future__ import annotations

import array
import math
import pathlib
import random
import shutil
import subprocess
import sys
import tempfile
import wave

ROOT = pathlib.Path(__file__).resolve().parents[1]
RATE = 44100
TAU = 2 * math.pi


def hz(note: float) -> float:
    return 440.0 * 2 ** ((note - 69) / 12)


def write_wav(path: pathlib.Path, channels: list[array.array], peak: float) -> None:
    """Float channels (same length) to 16-bit PCM, scaled so the loudest sample is `peak`."""
    top = max(max(abs(x) for x in channel) for channel in channels) or 1.0
    gain = peak / top
    frames = array.array("h")
    for samples in zip(*channels):
        frames.extend(round(max(-1.0, min(1.0, x * gain)) * 32767) for x in samples)
    if sys.byteorder == "big":
        frames.byteswap()
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(len(channels))
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(frames.tobytes())


# ---------------------------------------------------------------------- UI sounds


def bell(freq: float, seconds: float, decay: float, ratio: float = 3.5, index: float = 1.2,
         attack: float = 0.004) -> array.array:
    """A soft FM bell: brightness falls away faster than the note."""
    count = round(seconds * RATE)
    out = array.array("d", bytes(8 * count))
    for i in range(count):
        t = i / RATE
        env = math.exp(-t / decay) * min(1.0, t / attack)
        mod = index * math.exp(-t / (decay * 0.4)) * math.sin(TAU * freq * ratio * t)
        out[i] = env * math.sin(TAU * freq * t + mod)
    return out


def glide(start: float, end: float, seconds: float, decay: float) -> array.array:
    """A sine sliding from `start` to `end` Hz: the category whoosh and the edge bump."""
    count = round(seconds * RATE)
    out = array.array("d", bytes(8 * count))
    phase = 0.0
    for i in range(count):
        t = i / RATE
        freq = start + (end - start) * min(1.0, t / seconds)
        phase += TAU * freq / RATE
        out[i] = math.exp(-t / decay) * min(1.0, t / 0.003) * (math.sin(phase) + 0.25 * math.sin(2 * phase))
    return out


def mix(length: float, *parts: tuple[float, array.array, float]) -> array.array:
    """(start s, samples, gain) parts mixed into `length` seconds."""
    out = array.array("d", bytes(8 * round(length * RATE)))
    for start, samples, gain in parts:
        offset = round(start * RATE)
        for i, x in enumerate(samples[:len(out) - offset]):
            out[offset + i] += x * gain
    return out


D6, FS6, A6, E6, A5, D5, B5 = 86, 90, 93, 88, 81, 74, 83


def ui_sounds() -> dict[str, tuple[array.array, float]]:
    """name -> (samples, peak). Quiet by design: pw-play plays them at half volume as well."""
    return {
        "move": (bell(hz(D6 + 12), 0.06, 0.018, ratio=2.0, index=0.4), 0.32),
        "category": (glide(520, 880, 0.12, 0.05), 0.30),
        "edge": (glide(190, 150, 0.12, 0.04), 0.40),
        "select": (mix(0.32, (0, bell(hz(B5), 0.3, 0.09), 1.0), (0.07, bell(hz(FS6), 0.25, 0.09), 0.9)), 0.50),
        "back": (mix(0.32, (0, bell(hz(FS6), 0.3, 0.08), 0.8), (0.07, bell(hz(B5), 0.25, 0.08), 0.9)), 0.42),
        "open": (mix(0.5, (0, bell(hz(D6), 0.45, 0.12), 0.8), (0.045, bell(hz(FS6), 0.4, 0.12), 0.8),
                     (0.09, bell(hz(A6), 0.4, 0.14), 0.9)), 0.45),
        "close": (mix(0.5, (0, bell(hz(A6), 0.45, 0.1), 0.7), (0.045, bell(hz(FS6), 0.4, 0.1), 0.8),
                      (0.09, bell(hz(D6), 0.4, 0.12), 0.9)), 0.40),
        "notify": (mix(0.9, (0, bell(hz(A5), 0.8, 0.25), 1.0), (0.15, bell(hz(E6), 0.75, 0.25), 0.9)), 0.50),
        "startup": (startup(), 0.60),
    }


def startup() -> array.array:
    """The start-up chime: a Dmaj9 swell with a rising sparkle."""
    seconds = 3.2
    count = round(seconds * RATE)
    out = array.array("d", bytes(8 * count))
    for note in (50, 57, 61, 64, 66):
        freq = hz(note)
        for i in range(count):
            t = i / RATE
            env = min(1.0, t / 0.9) * math.exp(-max(0.0, t - 0.9) / 0.9)
            out[i] += 0.16 * env * (math.sin(TAU * freq * t) + 0.3 * math.sin(TAU * 2.003 * freq * t))
    sparkle = [(0.25 + 0.12 * k, bell(hz(note), 1.6, 0.45), 0.35) for k, note in enumerate((74, 78, 81, 85, 88))]
    return mix(seconds, (0, out, 1.0), *sparkle)


# ---------------------------------------------------------------------- music

BPM = 68
BEAT = 60 / BPM
BAR = 4 * BEAT
CHORD_BARS = 2
PROGRESSION = (  # MIDI notes, lowest first: D major with a Lydian lift
    (50, 57, 61, 64, 66),  # Dmaj9
    (47, 54, 57, 62, 64),  # Bm11
    (43, 50, 54, 59, 61),  # Gmaj7#11
    (45, 52, 59, 61, 66),  # A6/9
    (52, 59, 62, 66, 67),  # Em9
    (42, 52, 57, 61, 64),  # F#m7
    (43, 50, 54, 57, 59),  # Gmaj9
    (45, 52, 57, 62, 64),  # Asus4
)
LOOP_BARS = 2 * len(PROGRESSION) * CHORD_BARS  # the progression twice: plain, then with more bells
TABLE_SIZE = 4096


def soft_saw() -> list[float]:
    """One cycle of a warm, band-limited saw (8 harmonics, rolled off)."""
    table = [sum((0.72 ** n) / n * math.sin(TAU * n * i / TABLE_SIZE) for n in range(1, 9))
             for i in range(TABLE_SIZE)]
    top = max(abs(x) for x in table)
    return [x / top for x in table]


def pad(chord: tuple[int, ...], seconds: float, release: float, table: list[float]) -> tuple[array.array, array.array]:
    """A chord held for `seconds`, then let go: two slightly detuned voices per note, one per side."""
    count = round((seconds + release) * RATE)
    left = array.array("d", bytes(8 * count))
    right = array.array("d", bytes(8 * count))
    envelope = [min(1.0, (i / RATE) / 1.4) * (1.0 if i / RATE <= seconds else
                                               math.exp(-(i / RATE - seconds) / (release / 4)))
                for i in range(count)]
    mask = TABLE_SIZE - 1
    for note in chord[1:]:
        gain = 0.11 if note < 60 else 0.08
        for side, cents in ((left, -5), (right, 5)):
            step = hz(note) * 2 ** (cents / 1200) * TABLE_SIZE / RATE
            other = hz(note) * 2 ** (-cents / 2400) * TABLE_SIZE / RATE
            phase = phase2 = (note * 997) % TABLE_SIZE  # a fixed, different start per note
            for i in range(count):
                side[i] += gain * envelope[i] * (table[int(phase) & mask] + 0.6 * table[int(phase2) & mask])
                phase += step
                phase2 += other
    return left, right


def bass(root: int, seconds: float, release: float) -> array.array:
    note = root - 12 if root >= 47 else root
    freq = hz(note)
    count = round((seconds + release) * RATE)
    out = array.array("d", bytes(8 * count))
    for i in range(count):
        t = i / RATE
        env = min(1.0, t / 0.5) * (1.0 if t <= seconds else math.exp(-(t - seconds) / (release / 4)))
        out[i] = 0.2 * env * (math.sin(TAU * freq * t) + 0.15 * math.sin(TAU * 2 * freq * t))
    return out


def add(into: array.array, samples: array.array, offset: int, gain: float = 1.0) -> None:
    """Add `samples` at `offset`, wrapping past the end to the start: the loop has no seam."""
    length = len(into)
    for i, x in enumerate(samples):
        into[(offset + i) % length] += x * gain


def ping_pong(left: array.array, right: array.array, delay: float, feedback: float, wet: float) -> None:
    """A stereo echo, run twice around the loop so its tail is already there at the start."""
    length = len(left)
    taps = round(delay * RATE)
    echo_l = array.array("d", bytes(8 * length))
    echo_r = array.array("d", bytes(8 * length))
    for _ in range(2):
        for i in range(length):
            j = (i - taps) % length
            echo_l[i] = right[j] * 0.5 + echo_r[j] * feedback
            echo_r[i] = left[j] * 0.5 + echo_l[j] * feedback
    for i in range(length):
        left[i] += wet * echo_l[i]
        right[i] += wet * echo_r[i]


def music() -> tuple[array.array, array.array]:
    rng = random.Random(1987)
    length = round(LOOP_BARS * BAR * RATE)
    left = array.array("d", bytes(8 * length))
    right = array.array("d", bytes(8 * length))
    bells_l = array.array("d", bytes(8 * length))
    bells_r = array.array("d", bytes(8 * length))
    table = soft_saw()
    hold = CHORD_BARS * BAR
    pads = {chord: pad(chord, hold, 3.0, table) for chord in PROGRESSION}
    basses = {chord: bass(chord[0], hold, 1.2) for chord in PROGRESSION}
    bells: dict[int, array.array] = {}
    plain = (0, 3, 6, 8, 11, 14)  # eighth notes of the two bars that ring, first time round
    busy = (0, 2, 4, 7, 8, 10, 12, 15)  # and the second time
    for slot in range(LOOP_BARS // CHORD_BARS):
        chord = PROGRESSION[slot % len(PROGRESSION)]
        start = round(slot * hold * RATE)
        add(left, pads[chord][0], start)
        add(right, pads[chord][1], start)
        add(left, basses[chord], start)
        add(right, basses[chord], start)
        second = slot >= len(PROGRESSION)
        tones = [note + 12 for note in chord[1:]]
        if second:
            tones = tones[::-1]
        for k, eighth in enumerate(busy if second else plain):
            note = tones[k % len(tones)] + (12 if second and k % 4 == 3 else 0)
            if note not in bells:
                bells[note] = bell(hz(note), 2.2, 0.55, ratio=3.5, index=0.9, attack=0.006)
            velocity = 0.07 + 0.04 * rng.random()
            pan = 0.25 if k % 2 else 0.75
            at = start + round(eighth * BEAT / 2 * RATE)
            add(bells_l, bells[note], at, velocity * (1 - pan))
            add(bells_r, bells[note], at, velocity * pan)
    ping_pong(bells_l, bells_r, 0.75 * BEAT, 0.42, 0.55)
    for i in range(length):
        left[i] = math.tanh(left[i] + bells_l[i])
        right[i] = math.tanh(right[i] + bells_r[i])
    return left, right


def main(argv: list[str]) -> int:
    out = pathlib.Path(argv[1]) if len(argv) > 1 else ROOT / "overlay/usr/share/couchliteos"
    for name, (samples, peak) in ui_sounds().items():
        write_wav(out / "sounds" / f"{name}.wav", [samples], peak)
        print(f"sounds/{name}.wav")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        print("make-sounds: ffmpeg (with libvorbis) is needed for the music", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory() as directory:
        loop = pathlib.Path(directory) / "ambient.wav"
        write_wav(loop, list(music()), 0.7)
        target = out / "music" / "ambient.ogg"
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([ffmpeg, "-loglevel", "error", "-y", "-i", str(loop), "-map_metadata", "-1",
                        "-fflags", "+bitexact", "-flags:a", "+bitexact", "-c:a", "libvorbis", "-q:a", "4",
                        str(target)], check=True)
    print("music/ambient.ogg")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
