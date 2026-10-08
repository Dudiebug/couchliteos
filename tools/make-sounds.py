#!/usr/bin/env python3
"""Make the TV interface's sounds and its background music, from nothing but this script.

    tools/make-sounds.py [OUT]       OUT defaults to overlay/usr/share/couchliteos

Writes OUT/sounds/<name>.wav (mono, 16 bit, 22.05 kHz) for every UI sound and
OUT/music/ambient.ogg: a seamless ambient loop (pads, a bell arpeggio and a soft bass in
D major, 32 bars at 68 bpm, about 113 s, no seam), encoded with ffmpeg (libvorbis). Everything is
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


def halve(samples: array.array) -> array.array:
    """RATE to RATE / 2 (the UI sounds: small files, nothing up there worth keeping)."""
    return array.array("d", ((samples[i] + samples[i + 1]) / 2 for i in range(0, len(samples) - 1, 2)))


def write_wav(path: pathlib.Path, channels: list[array.array], peak: float, rate: int = RATE) -> None:
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
        out.setframerate(rate)
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
NOISE = random.Random(3)  # the same "air" every run


def air(seconds: float, low: float, high: float, decay: float, attack: float = 0.002,
        q: float = 4.0, wobble: float = 0.0, rate: float = 7.0) -> array.array:
    """Breathy noise through a band-pass sweeping from `low` to `high` Hz; `wobble` swings the
    band up and down `rate` times a second, the watery swish."""
    count = round(seconds * RATE)
    out = array.array("d", bytes(8 * count))
    x1 = x2 = y1 = y2 = 0.0
    for i in range(count):
        t = i / RATE
        centre = low * (high / low) ** min(1.0, t / seconds) * (1 + wobble * math.sin(TAU * rate * t))
        w = TAU * min(centre, RATE * 0.45) / RATE
        alpha = math.sin(w) / (2 * q)
        a0 = 1 + alpha
        x = NOISE.uniform(-1.0, 1.0)
        y = (alpha * x - alpha * x2 - (-2 * math.cos(w)) * y1 - (1 - alpha) * y2) / a0
        x2, x1, y2, y1 = x1, x, y1, y
        out[i] = y * min(1.0, t / attack) * math.exp(-t / decay)
    return out


def bubble(start: float, end: float, seconds: float, decay: float, glide: float = 0.03,
           vibrato: float = 0.012, attack: float = 0.004) -> array.array:
    """A water drop: a round tone gliding from `start` to `end` Hz (up for a drop, down for a
    sinking one), a chorus voice a few cents away and a little vibrato, so it shimmers."""
    count = round(seconds * RATE)
    out = array.array("d", bytes(8 * count))
    phase = phase2 = 0.0
    for i in range(count):
        t = i / RATE
        freq = end + (start - end) * math.exp(-t / glide)
        freq *= 1 + vibrato * math.sin(TAU * 9 * t) * min(1.0, t / 0.04)
        phase += TAU * freq / RATE
        phase2 += TAU * freq * 1.0087 / RATE  # +15 cents: the chorus
        env = min(1.0, t / attack) * math.exp(-t / decay)
        out[i] = env * (math.sin(phase) + 0.55 * math.sin(phase2) + 0.08 * math.sin(2 * phase))
    return out


def drop(start: float, end: float, seconds: float, decay: float, attack: float = 0.003) -> array.array:
    """A dull falling thud (the edge bump)."""
    count = round(seconds * RATE)
    out = array.array("d", bytes(8 * count))
    phase = 0.0
    for i in range(count):
        t = i / RATE
        freq = end + (start - end) * math.exp(-t / 0.018)
        phase += TAU * freq / RATE
        out[i] = min(1.0, t / attack) * math.exp(-t / decay) * (math.sin(phase) + 0.12 * math.sin(2 * phase))
    return out


def thock(pitch: float = 340.0, seconds: float = 0.08, decay: float = 0.018) -> array.array:
    """A deep, muted click, like a well-damped key bottoming out: a woody body that drops a little
    in pitch, a second mode above it that dies fast, and a soft felt transient: under every key sound."""
    count = round(seconds * RATE)
    out = array.array("d", bytes(8 * count))
    felt = air(seconds, 1800, 1200, 0.0025, attack=0.0005, q=0.8)
    phase = phase2 = 0.0
    for i in range(count):
        t = i / RATE
        freq = pitch * (1 + 0.25 * math.exp(-t / 0.006))
        phase += TAU * freq / RATE
        phase2 += TAU * freq * 2.31 / RATE
        attack = min(1.0, t / 0.0008)
        out[i] = attack * (math.exp(-t / decay) * math.sin(phase)
                           + 0.35 * math.exp(-t / (decay * 0.4)) * math.sin(phase2)) + 0.5 * felt[i]
    return out


def echo(samples: array.array, taps=((0.031, 0.32), (0.047, 0.22), (0.071, 0.14), (0.113, 0.08))) -> array.array:
    """A short, wet room on a sound (inside its own length): the ripple after the drop."""
    out = array.array("d", samples)
    for delay, gain in taps:
        offset = round(delay * RATE)
        for i in range(offset, len(out)):
            out[i] += samples[i - offset] * gain
    return out


def ui_sounds() -> dict[str, tuple[array.array, float]]:
    """name -> (samples, peak). In the spirit of a games console's menu: soft watery drops that
    never tire, rippling swishes between categories and pages, a dull bump at an edge. Quiet by
    design: pw-play plays them at half volume as well."""
    return {
        "move": (echo(mix(0.16, (0, thock(), 1.0), (0.004, bubble(1250, 1900, 0.15, 0.02, glide=0.012), 0.22)),
                      taps=((0.031, 0.18), (0.047, 0.12), (0.071, 0.07))), 0.34),
        "category": (echo(mix(0.26, (0, thock(290, 0.1, 0.022), 1.0),
                              (0.008, air(0.25, 900, 3600, 0.07, attack=0.02, q=5.0, wobble=0.25, rate=11), 0.55),
                              (0.01, bubble(900, 1400, 0.2, 0.035, glide=0.02), 0.25))), 0.34),
        "edge": (mix(0.14, (0, drop(170, 115, 0.14, 0.04), 1.0), (0, bubble(420, 300, 0.1, 0.025), 0.2)), 0.45),
        "select": (echo(mix(0.28, (0, thock(380), 0.9), (0.01, bubble(900, 1350, 0.2, 0.05, glide=0.02), 0.7),
                            (0.06, bubble(1350, 2000, 0.2, 0.055, glide=0.02), 0.8),
                            (0, air(0.12, 2000, 6000, 0.03, q=3.0, wobble=0.2), 0.2))), 0.50),
        "back": (echo(mix(0.28, (0, thock(280), 0.9), (0.01, bubble(1500, 1000, 0.2, 0.05, glide=0.025), 0.6),
                          (0.06, bubble(1000, 680, 0.2, 0.055, glide=0.025), 0.9),
                          (0, air(0.12, 4000, 1400, 0.03, q=3.0, wobble=0.2), 0.2))), 0.42),
        "open": (echo(mix(0.55, (0, air(0.55, 500, 5000, 0.16, attack=0.08, q=4.0, wobble=0.3, rate=8), 1.0),
                          (0.05, bubble(800, 1600, 0.4, 0.09, glide=0.05), 0.35))), 0.38),
        "close": (echo(mix(0.55, (0, air(0.55, 5000, 500, 0.14, attack=0.03, q=4.0, wobble=0.3, rate=8), 1.0),
                           (0.0, bubble(1400, 700, 0.4, 0.08, glide=0.05), 0.3))), 0.34),
        "notify": (echo(mix(0.9, (0, bubble(hz(A5) * 0.94, hz(A5), 0.8, 0.22, glide=0.02), 1.0),
                            (0.12, bubble(hz(E6) * 0.94, hz(E6), 0.75, 0.22, glide=0.02), 0.8))), 0.45),
        "startup": (startup(), 0.65),
        # A game or an application starts: a swell of air that rises and opens on a bright fifth.
        "launch": (echo(mix(1.0, (0, air(0.9, 400, 6500, 0.28, attack=0.3, q=3.5, wobble=0.25, rate=6), 1.0),
                            (0.32, bell(hz(74), 0.65, 0.22, ratio=2.0, index=0.6), 0.45),
                            (0.36, bell(hz(81), 0.6, 0.2, ratio=2.0, index=0.5), 0.35))), 0.45),
        # The TV is back in front: the same air falling, landing on a low, warm note.
        "return": (echo(mix(0.9, (0, air(0.6, 6000, 500, 0.18, attack=0.04, q=3.5, wobble=0.25, rate=6), 1.0),
                            (0.22, bell(hz(62), 0.65, 0.25, ratio=2.0, index=0.4), 0.5))), 0.4),
        # A start failed: two low drops, the second lower.
        "error": (mix(0.45, (0, bubble(520, 430, 0.18, 0.05, glide=0.02), 1.0),
                      (0.15, bubble(400, 300, 0.25, 0.07, glide=0.03), 1.0)), 0.42),
        # Back from sleep (not the start-up swell): two soft bells rising a fifth over a breath.
        "wake": (echo(mix(1.5, (0, air(1.3, 1500, 5000, 0.45, attack=0.5, q=5.0, wobble=0.2, rate=4), 0.5),
                          (0.15, bell(hz(69), 1.2, 0.45, ratio=2.0, index=0.3, attack=0.03), 0.8),
                          (0.45, bell(hz(76), 1.0, 0.45, ratio=2.0, index=0.3, attack=0.03), 0.7))), 0.42),
    }


def startup() -> array.array:
    """The start-up swell: a wide, string-like D major chord that rises out of nothing and opens
    up (more harmonics as it grows), a bright shimmer on top, then lets go. Original; only the
    gesture is the console's."""
    seconds = 3.4
    count = round(seconds * RATE)
    out = array.array("d", bytes(8 * count))
    peak_at = 1.7
    for note in (38, 50, 57, 62, 66, 69, 74):
        for cents in (-7, 0, 7):
            freq = hz(note) * 2 ** (cents / 1200)
            gain = (0.05 if note < 45 else 0.035) / (1 + 0.2 * abs(cents) / 7)
            for i in range(count):
                t = i / RATE
                rise = min(1.0, t / peak_at)
                env = rise * rise * (1.0 if t <= peak_at else math.exp(-(t - peak_at) / 0.55))
                bright = 0.15 + 0.85 * rise
                phase = TAU * freq * t
                out[i] += gain * env * (math.sin(phase) + bright * 0.5 * math.sin(2 * phase)
                                        + bright * bright * 0.3 * math.sin(3 * phase)
                                        + bright ** 3 * 0.18 * math.sin(4 * phase))
    shimmer = air(seconds, 4000, 9000, 1.2, attack=1.3, q=6.0)
    return mix(seconds, (0, out, 1.0), (0, shimmer, 0.25),
               (peak_at - 0.05, bell(hz(D6 + 12), 1.5, 0.5, ratio=2.0, index=0.4), 0.12))


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
LOOP_BARS = 2 * len(PROGRESSION) * CHORD_BARS  # the progression twice
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
    # The loop must not have a seam you can hear: no sections. Every pattern repeats with a period
    # (4 chords) that divides the loop (16 chords), so the jump from the last chord back to the
    # first is exactly like the jump between any two chords inside it.
    patterns = ((0, 6, 11), (0, 4, 7, 10, 14), (0, 6, 11), (0, 3, 8, 12))  # eighth notes that ring
    for slot in range(LOOP_BARS // CHORD_BARS):
        chord = PROGRESSION[slot % len(PROGRESSION)]
        start = round(slot * hold * RATE)
        add(left, pads[chord][0], start)
        add(right, pads[chord][1], start)
        add(left, basses[chord], start)
        add(right, basses[chord], start)
        tones = [note + 12 for note in chord[1:]]
        if slot % 2:
            tones = tones[::-1]
        for k, eighth in enumerate(patterns[slot % len(patterns)]):
            note = tones[k % len(tones)] + (12 if slot % 4 == 1 and k == 3 else 0)
            if note not in bells:
                bells[note] = bell(hz(note), 2.2, 0.55, ratio=3.5, index=0.9, attack=0.006)
            velocity = 0.045 + 0.03 * rng.random()
            pan = 0.25 if k % 2 else 0.75
            at = start + round(eighth * BEAT / 2 * RATE)
            add(bells_l, bells[note], at, velocity * (1 - pan))
            add(bells_r, bells[note], at, velocity * pan)
    ping_pong(bells_l, bells_r, 0.75 * BEAT, 0.5, 0.7)
    for i in range(length):
        left[i] = math.tanh(left[i] + bells_l[i])
        right[i] = math.tanh(right[i] + bells_r[i])
    return left, right


def main(argv: list[str]) -> int:
    out = pathlib.Path(argv[1]) if len(argv) > 1 else ROOT / "overlay/usr/share/couchliteos"
    for name, (samples, peak) in ui_sounds().items():
        write_wav(out / "sounds" / f"{name}.wav", [halve(samples)], peak, RATE // 2)
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
