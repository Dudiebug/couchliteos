"""Background music for the TV interface, as plain Python: when it plays, how loud, and the player.

An ambient loop plays on the home screen and in the menus, the way a games console's menu does.
It fades in after the first frame, ducks under the bigger UI sounds, and fades out (and pauses,
so it resumes where it left off) while anything holds it: an application or a stream in front,
the blank screen, sleep, a screen with its own test sound. Settings > APPEARANCE turns it on or
off ([appearance] music) and sets its volume ([appearance] music_volume, 10-100 %).

The loop is MUSIC_DIR/LOOP; a file the user puts in USER_DIR plays instead. The sound itself is
played by couchliteos-music (GStreamer, in its own process so a decoder crash never takes the TV
interface down), driven over its stdin by one line per change:

    play <path>                     load the track and loop it, silent and paused
    seek <seconds>                  start from there (wrapped at the track's end)
    level <0..1> <seconds>          ramp the volume there; at 0 it pauses, above 0 it plays
    quit

While nothing of the TV interface can be heard or seen (a game in front, the blank screen, sleep)
rest() ends the player process once it has faded out; the next one seeks to where it stopped, so
the music resumes there with its fade-in. A player that dies (seen when a level is sent, or by
check() on the TV's 1 s tick) is started again at most RESTARTS times in a row; one that plays for
HEALTHY seconds starts the count over. After that there is no music, never an error on screen:
the player's stderr and why the music stopped go to LOG (music.log, in the support file).
"""

from __future__ import annotations

import configparser
import math
import os
import pathlib
import subprocess
import sys
import threading
import time
from collections.abc import Callable

import couchliteos_theme as theme

MUSIC_DIR = pathlib.Path("/usr/share/couchliteos/music")
REPO_MUSIC = pathlib.Path(__file__).resolve().parents[1] / "overlay/usr/share/couchliteos/music"
LOOP = "ambient.ogg"
USER_DIR = pathlib.Path(os.environ.get("XDG_DATA_HOME") or pathlib.Path.home() / ".local/share") / "couchliteos/music"
USER_SUFFIXES = (".ogg", ".oga", ".opus", ".flac", ".mp3", ".wav")
PLAYER = pathlib.Path("/usr/libexec/couchliteos-music")
REPO_PLAYER = pathlib.Path(__file__).resolve().parent / "couchliteos-music.py"

VOLUMES = (10, 20, 30, 40, 50, 60, 70, 80, 90, 100)
DEFAULT_VOLUME = 30
FADE_IN = 2.5  # s, the first start and after an application closes
FADE_OUT = 0.8  # s, an application, the blank screen or sleep comes
DUCK = 0.4  # of the volume, under a UI sound
DUCK_IN = 0.06  # s
DUCK_HOLD = 0.35  # s at the ducked level before coming back
DUCK_OUT = 0.5  # s
DUCKED_SOUNDS = {"select", "back", "open", "close", "notify", "startup", "launch", "return", "error"}  # not move
RESTARTS = 3
HEALTHY = 60.0  # s of playing that forgive the earlier deaths
LOG = pathlib.Path(os.environ.get("COUCHLITEOS_LOG_DIR") or "/var/log/couchliteos") / "music.log"
LOG_MAX = 256 * 1024  # bytes; a longer log starts over

# Why the music is silent (Music.hold / Music.release).
APP, BLANK, SLEEP, SCREEN = "app", "blank", "sleep", "screen"


def _appearance(path: pathlib.Path | None) -> configparser.SectionProxy | dict:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string((theme.CONFIG if path is None else path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, configparser.Error):
        return {}
    return parser[theme.SECTION] if parser.has_section(theme.SECTION) else {}


def music_enabled(path: pathlib.Path | None = None) -> bool:
    """[appearance] music; anything but "off" (or no file) is on."""
    return str(_appearance(path).get("music", "on")).strip().lower() != "off"


def music_volume(path: pathlib.Path | None = None) -> int:
    """[appearance] music_volume in %, the nearest of VOLUMES; DEFAULT_VOLUME when unset or unreadable."""
    try:
        value = int(str(_appearance(path).get("music_volume", DEFAULT_VOLUME)).strip().rstrip("%"))
    except ValueError:
        return DEFAULT_VOLUME
    return min(VOLUMES, key=lambda volume: abs(volume - value))


def save_music(on: bool, volume: int, path: pathlib.Path | None = None) -> None:
    """Settings > APPEARANCE > MUSIC and MUSIC VOLUME. Raises OSError."""
    theme.save_values({"music": "on" if on else "off", "music_volume": str(min(VOLUMES, key=lambda v: abs(v - volume)))}, path)


def _log_file(path: pathlib.Path):
    """`path` opened to append (started over past LOG_MAX), or None when it cannot be."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            over = path.stat().st_size > LOG_MAX
        except OSError:
            over = False
        return path.open("w" if over else "a", encoding="utf-8", errors="replace")
    except OSError:
        return None


def log(message: str, path: pathlib.Path | None = None) -> None:
    """One dated line in LOG; never raises."""
    stream = _log_file(LOG if path is None else path)
    if stream is None:
        return
    printable = "".join(char if char.isprintable() else "?" for char in message)
    try:
        with stream:
            stream.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {printable}\n")
    except OSError:
        pass


def track(user_dir: pathlib.Path | None = None, builtin: pathlib.Path | None = None) -> pathlib.Path | None:
    """The user's own track (the first by name in USER_DIR), else the built-in loop, else None."""
    user_dir = USER_DIR if user_dir is None else user_dir
    try:
        own = sorted(path for path in user_dir.iterdir()
                     if path.suffix.lower() in USER_SUFFIXES and path.is_file() and not path.name.startswith("."))
    except OSError:
        own = []
    if own:
        return own[0]
    if builtin is None:
        builtin = (MUSIC_DIR if MUSIC_DIR.is_dir() else REPO_MUSIC) / LOOP
    return builtin if builtin.is_file() else None


class PlayerProcess:
    """couchliteos-music in its own process; send() returns False once it is gone. Its stderr
    (why GStreamer stopped) is appended to `log_path` (LOG)."""

    def __init__(self, command: list[str] | None = None, log_path: pathlib.Path | None = None) -> None:
        if command is None:
            player = PLAYER if PLAYER.exists() else REPO_PLAYER
            command = [str(player)] if os.access(player, os.X_OK) else [sys.executable, str(player)]
        errors = _log_file(LOG if log_path is None else log_path)
        try:
            self.process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL if errors is None else errors,
                text=True, bufsize=1, close_fds=True,
            )
        finally:
            if errors is not None:
                errors.close()  # the player has its own copy

    def exit_code(self) -> int | None:
        """None while it runs."""
        return self.process.poll()

    def send(self, line: str) -> bool:
        if self.process.poll() is not None or self.process.stdin is None:
            return False
        try:
            self.process.stdin.write(line + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            return False
        return True

    def close(self, wait: bool = True) -> None:
        """Ask it to quit; `wait=False` reaps it on a thread instead (the UI must not wait)."""
        self.send("quit")
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
        except OSError:
            pass
        if wait:
            self.reap()
        else:
            threading.Thread(target=self.reap, name="music-reap", daemon=True).start()

    def reap(self) -> None:
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()


class Music:
    """When the music plays and how loud. Call start() after the first frame, hold()/release()
    as things come and go, sound() when a UI sound plays, settings() after APPEARANCE changes, and
    tick() when the time duck() or sound() returned has passed."""

    def __init__(
        self, *, enabled: Callable[[], bool] = music_enabled, volume: Callable[[], int] = music_volume,
        track: Callable[[], pathlib.Path | None] = track, player: Callable[[], object] = PlayerProcess,
        clock: Callable[[], float] = time.monotonic, log: Callable[[str], None] = log,
    ) -> None:
        self.enabled_source = enabled
        self.volume_source = volume
        self.track_source = track
        self.player_factory = player
        self.clock = clock
        self.log = log
        self.player: object | None = None
        self.track: pathlib.Path | None = None
        self.started = False
        self.holds: set[str] = set()
        self.enabled = False
        self.volume = DEFAULT_VOLUME
        self.ducked_until = float("-inf")
        self.level: float = 0.0  # the last level sent
        self.restarts = 0  # deaths since it last played HEALTHY s
        self.dead = False  # no player, no track, or it kept dying: silence
        self.played = 0.0  # s of the track played before playing_since: where a new player seeks to
        self.playing_since: float | None = None  # it has played since then (None: paused)
        self.silent_at = -math.inf  # a fade to 0 ends then (and it pauses); inf while it plays
        self.settings()

    # ------------------------------------------------------------------ what it should be

    def target(self) -> float:
        if not self.started or not self.enabled or self.holds or self.dead:
            return 0.0
        level = self.volume / 100
        if self.clock() < self.ducked_until:
            level *= DUCK
        return round(level, 3)

    @property
    def playing(self) -> bool:
        return self.level > 0

    # ------------------------------------------------------------------ events

    def start(self) -> None:
        """The first frame is on screen: fade in."""
        self.started = True
        self._update(FADE_IN)

    def hold(self, reason: str) -> None:
        if reason not in self.holds:
            self.holds.add(reason)
            self._update(FADE_OUT)

    def release(self, reason: str) -> None:
        if reason in self.holds:
            self.holds.discard(reason)
            self._update(FADE_IN)

    def settings(self) -> None:
        """Read MUSIC and MUSIC VOLUME again (Settings > APPEARANCE changed)."""
        try:
            self.enabled = bool(self.enabled_source())
            self.volume = int(self.volume_source())
        except Exception:  # noqa: BLE001 - unreadable settings: the defaults
            self.enabled, self.volume = True, DEFAULT_VOLUME
        self._update(FADE_OUT if not self.enabled else 0.3)

    def sound(self, name: str) -> float | None:
        """A UI sound starts: duck under the bigger ones. Returns the seconds after which to call tick()."""
        if name not in DUCKED_SOUNDS or not self.playing:
            return None
        self.ducked_until = self.clock() + DUCK_HOLD
        self._update(DUCK_IN)
        return DUCK_HOLD

    def tick(self) -> None:
        """After a duck: come back up."""
        self._update(DUCK_OUT)

    def check(self) -> None:
        """Once a second (the TV's tick): start a player that died again, by the same rules as one
        found dead when a level is sent, and forgive the deaths before HEALTHY s of playing."""
        if self.player is None or self.dead:
            return
        try:
            code = self.player.exit_code()
        except Exception:  # noqa: BLE001 - cannot tell: leave it
            return
        if code is None:
            if (self.restarts and self.playing and self.playing_since is not None
                    and self.clock() - self.playing_since >= HEALTHY):
                self.restarts = 0
            return
        self._say(f"music player stopped (exit {code})")
        self._died(self.target())

    def rest(self) -> float | None:
        """Nothing to hear (an application in front, the blank screen, sleep): once it is silent,
        end the player process; the music comes back where it stopped, in a new one. Returns the
        seconds until the fade-out ends when it has not yet, else None."""
        if self.player is None or self.target() > 0:
            return None
        left = self.silent_at - self.clock()
        if left > 0:
            return left
        self._drop(wait=False)
        return None

    def position(self) -> float:
        """Seconds of the track played so far (the player wraps them at its end)."""
        if self.playing_since is None:
            return self.played
        return self.played + max(0.0, min(self.clock(), self.silent_at) - self.playing_since)

    def close(self) -> None:
        self._drop(wait=True)

    def _drop(self, wait: bool) -> None:
        if self.player is not None:
            self.played, self.playing_since = self.position(), None
            try:
                self.player.close(wait=wait)
            except Exception:  # noqa: BLE001
                pass
        self.player = None
        self.level = 0.0

    # ------------------------------------------------------------------ the player

    def _update(self, seconds: float) -> None:
        level = self.target()
        if level == self.level:
            return
        if self.player is None:
            if level == 0 or not self._spawn():
                self.level = 0.0
                return
        if self.player.send(f"level {level:g} {seconds:g}"):
            self._sent(level, seconds)
            return
        self._died(level)

    def _died(self, level: float) -> None:
        """It died: start it again (from silence, fading in) a few times, then give up."""
        self.close()
        self.restarts += 1
        if self.restarts > RESTARTS:
            self._give_up(f"the music player stopped {self.restarts} times")
        elif level > 0 and self._spawn() and self.player.send(f"level {level:g} {FADE_IN:g}"):
            self._sent(level, FADE_IN)

    def _give_up(self, why: str) -> None:
        """Silence for good, said once in the log."""
        if not self.dead:
            self.dead = True
            self._say(f"no music: {why}")

    def _say(self, message: str) -> None:
        try:
            self.log(message)
        except Exception:  # noqa: BLE001 - the log never stops the music
            pass

    def _sent(self, level: float, seconds: float) -> None:
        """The player took `level`: keep count of where the track is. It plays while the level is
        above 0, and on through a fade to 0 until that ends (then it pauses)."""
        now = self.clock()
        if level > 0:
            if self.playing_since is not None and now >= self.silent_at:  # it had paused
                self.played, self.playing_since = self.position(), None
            if self.playing_since is None:
                self.playing_since = now
            self.silent_at = math.inf
        elif self.level > 0:
            self.silent_at = now + seconds
        self.level = level

    def _spawn(self) -> bool:
        """Start the player on the track; False (and silence for good) when there is none."""
        if self.dead:
            return False
        if self.track is None:
            try:
                self.track = self.track_source()
            except Exception:  # noqa: BLE001
                self.track = None
        if self.track is None:
            self._give_up("no track")
            return False
        try:
            self.player = self.player_factory()
        except Exception as error:  # noqa: BLE001 - no player script, no python3: silence
            self.player = None
            self._give_up(f"the music player cannot start: {error}")
            return False
        if not self.player.send(f"play {self.track}") or (
                self.played >= 0.05 and not self.player.send(f"seek {self.played:.2f}")):
            self.close()
            self.restarts += 1
            if self.restarts > RESTARTS:
                self._give_up(f"the music player stopped {self.restarts} times")
            return False
        return True
