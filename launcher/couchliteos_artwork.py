"""Cover art for the GAMES row, for games the gaming PC sends none for.

Sources, first match wins (`Worker.art`):
1. the cover the user picked (CHANGE ARTWORK), or TITLE CARD;
2. Moonlight's own box-art cache (what Sunshine sent);
3. this module's cache of earlier lookups;
4. a lookup in the background: the Steam store search, then SteamGridDB with the user's key;
5. nothing: the tile draws its title card.

Only game names are sent, only over HTTPS, only to the hosts in ALLOWED_HOSTS (redirects
included). A download is at most MAX_IMAGE bytes, must start like a JPEG, PNG or WebP, and is
decoded and re-encoded as a JPEG (GdkPixbuf, imported only when used) before it is cached, so
the home screen never loads a downloaded file as it came. The SteamGridDB key is kept 0600
in KEY_FILE and is never logged, shown or put in an error message.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import pathlib
import queue
import re
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

DATA = pathlib.Path(os.environ.get("COUCHLITEOS_STATE_DIR", "/var/lib/couchliteos"))
CACHE_DIR = DATA / "home/.cache/couchliteos/artwork"  # ~/.cache of the couchliteos user; not in snapshots
KEY_FILE = DATA / "artwork.key"
SETTINGS_FILE = DATA / "artwork.json"
PICKS_FILE = DATA / "artwork-picks.json"
PICKED_DIR = DATA / "artwork-picked"
# Moonlight's BoxArtManager: QStandardPaths::CacheLocation + "/boxart/<host uuid>/<app id>.png"
# (moonlight-qt app/path.cpp and app/backend/boxartmanager.cpp).
MOONLIGHT_BOXART = DATA / "home/.cache/Moonlight Game Streaming Project/Moonlight/boxart"

CACHE_LIMIT = 200 * 1024 * 1024
FOUND_SECONDS = 30 * 86400
MISS_SECONDS = 7 * 86400
MAX_IMAGE = 5 * 1024 * 1024
MAX_JSON = 1024 * 1024
MAX_PIXELS = 4096 * 4096
TILE = (400, 600)  # the largest a cover is kept at (portrait, like Steam's 600x900)
TIMEOUT = 10.0
WORKERS = 4
MAX_FAILURES = 3
CHOICES = 12
TITLE_CARD = "title"
ALLOWED_HOSTS = frozenset({
    "store.steampowered.com",
    "cdn.akamai.steamstatic.com", "shared.akamai.steamstatic.com",
    "cdn.cloudflare.steamstatic.com", "shared.cloudflare.steamstatic.com",
    "shared.fastly.steamstatic.com", "shared.steamstatic.com", "steamcdn-a.akamaihd.net",
    "www.steamgriddb.com", "cdn2.steamgriddb.com", "cdn.steamgriddb.com",
})
STEAM_SEARCH = "https://store.steampowered.com/api/storesearch/?term={term}&l=english&cc=US"
STEAM_COVER = "https://cdn.akamai.steamstatic.com/steam/apps/{appid}/library_600x900.jpg"
GRID_SEARCH = "https://www.steamgriddb.com/api/v2/search/autocomplete/{term}"
GRID_COVERS = "https://www.steamgriddb.com/api/v2/grids/game/{game}?dimensions=600x900&types=static"
KEY_PATTERN = re.compile(r"[A-Za-z0-9]{16,128}")
PRIVACY = "LOOKUP SENDS ONLY GAME NAMES: TO STEAM, AND TO STEAMGRIDDB WITH A KEY"

# Suffixes that name an edition or a store, not the game. Removed from the end, longest first.
EDITIONS = (
    "game of the year edition", "game of the year", "goty edition", "goty",
    "definitive edition", "deluxe edition", "complete edition", "ultimate edition",
    "special edition", "enhanced edition", "gold edition", "premium edition",
    "standard edition", "anniversary edition", "directors cut", "director s cut",
)
PLATFORMS = ("steam", "epic games", "epic", "gog", "xbox", "microsoft store", "windows", "pc")


class ArtworkError(Exception):
    """A download or an image that is not used. The message never holds the key."""


class Offline(ArtworkError):
    """The network or the service did not answer: counts towards MAX_FAILURES."""


class BadKey(ArtworkError):
    """SteamGridDB refused the key."""


# ------------------------------------------------------------------ names

def normalize(name: str) -> str:
    """Lower case, no ™/®, accents, punctuation, edition or store suffixes; single spaces."""
    text = re.sub("[™®©℠]", " ", name)  # before NFKD, which spells ™ as "TM"
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", text)  # (Steam), [GOG], (2019)
    text = re.sub(r"['’`]", "", text)  # Assassin's -> assassins
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    changed = True
    while changed:
        changed = False
        for suffix in (*EDITIONS, *PLATFORMS):
            if text.endswith(" " + suffix):
                text = text[: -len(suffix) - 1].strip()
                changed = True
    return text


def matches(wanted: str, found: str) -> bool:
    """A search result is used only when its normalized name is the game's."""
    return bool(normalize(wanted)) and normalize(wanted) == normalize(found)


def cache_key(host_uuid: str, app: str) -> str:
    return hashlib.sha256(f"{host_uuid}\n{app}".encode()).hexdigest()[:32]


# ------------------------------------------------------------------ HTTP

def allowed_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and parts.hostname in ALLOWED_HOSTS and port in (None, 443)
            and not parts.username and not parts.password)


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_url(newurl):
            raise ArtworkError("redirect to a host that is not allowed")
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).hostname != urllib.parse.urlsplit(req.full_url).hostname:
            new.remove_header("Authorization")  # the key goes to the SteamGridDB API only
        return new


def default_opener() -> Callable[..., object]:
    return urllib.request.build_opener(_Redirects()).open


def fetch(url: str, opener: Callable[..., object], limit: int, timeout: float = TIMEOUT,
          headers: dict[str, str] | None = None) -> bytes:
    """GET `url` (HTTPS, allowed hosts only), at most `limit` bytes."""
    if not allowed_url(url):
        raise ArtworkError("host is not allowed")
    request = urllib.request.Request(url, headers={"User-Agent": "CouchLiteOS", **(headers or {})})
    try:
        with opener(request, timeout=timeout) as response:
            final = getattr(response, "geturl", lambda: url)()
            if final and not allowed_url(final):
                raise ArtworkError("redirect to a host that is not allowed")
            length = str((getattr(response, "headers", None) or {}).get("Content-Length") or "")
            if length.isdigit() and int(length) > limit:
                raise ArtworkError("download is too large")
            body = response.read(limit + 1)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403) and url.startswith("https://www.steamgriddb.com/api/"):
            raise BadKey("SteamGridDB refused the key") from None
        if error.code == 404:
            raise ArtworkError("not found") from None
        raise Offline(f"HTTP {error.code}") from None
    except ArtworkError:
        raise
    except (OSError, ValueError, AttributeError) as error:  # URLError, timeouts, resets
        raise Offline(type(error).__name__) from None
    if len(body) > limit:
        raise ArtworkError("download is too large")
    return body


def fetch_json(url: str, opener: Callable[..., object], timeout: float = TIMEOUT,
               headers: dict[str, str] | None = None) -> dict:
    try:
        data = json.loads(fetch(url, opener, MAX_JSON, timeout, headers).decode("utf-8"))
    except (UnicodeError, ValueError):
        raise Offline("answer is not JSON") from None  # the service changed or is broken
    if not isinstance(data, dict):
        raise Offline("answer is not an object")
    return data


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def steam_apps(name: str, opener, timeout: float = TIMEOUT) -> list[tuple[int, str]]:
    """(appid, name) of the Steam store's matches for `name`, best first (unofficial API)."""
    data = fetch_json(STEAM_SEARCH.format(term=urllib.parse.quote(name)), opener, timeout)
    found = []
    for item in data.get("items") or []:
        if isinstance(item, dict) and isinstance(item.get("id"), int) and isinstance(item.get("name"), str):
            if item.get("type", "app") == "app":
                found.append((item["id"], item["name"]))
    return found


def grid_games(name: str, opener, key: str, timeout: float = TIMEOUT) -> list[tuple[int, str]]:
    data = fetch_json(GRID_SEARCH.format(term=urllib.parse.quote(name, safe="")), opener, timeout, _bearer(key))
    return [(item["id"], item["name"]) for item in data.get("data") or []
            if isinstance(item, dict) and isinstance(item.get("id"), int) and isinstance(item.get("name"), str)]


def grid_covers(game: int, opener, key: str, timeout: float = TIMEOUT) -> list[str]:
    data = fetch_json(GRID_COVERS.format(game=game), opener, timeout, _bearer(key))
    return [item["url"] for item in data.get("data") or []
            if isinstance(item, dict) and isinstance(item.get("url"), str) and allowed_url(item["url"])]


def find_cover(name: str, opener, key: str = "", timeout: float = TIMEOUT) -> bytes | None:
    """The cover for `name`: Steam's when its first result matches, else SteamGridDB's (with a
    key). None when nothing matches. Raises Offline, or BadKey after Steam found nothing."""
    apps = steam_apps(name, opener, timeout)
    if apps and matches(name, apps[0][1]):
        try:
            return fetch(STEAM_COVER.format(appid=apps[0][0]), opener, MAX_IMAGE, timeout)
        except Offline:
            raise
        except ArtworkError:
            pass  # no portrait cover for that app: try SteamGridDB
    if not key:
        return None
    games = grid_games(name, opener, key, timeout)
    if not games or not matches(name, games[0][1]):
        return None
    for url in grid_covers(games[0][0], opener, key, timeout)[:3]:
        try:
            return fetch(url, opener, MAX_IMAGE, timeout)
        except Offline:
            raise
        except ArtworkError:
            continue
    return None


def cover_choices(name: str, opener, key: str = "", timeout: float = TIMEOUT) -> list[str]:
    """Up to CHOICES cover URLs for CHANGE ARTWORK: SteamGridDB's with a key, else the top Steam matches."""
    if key:
        games = grid_games(name, opener, key, timeout)
        return grid_covers(games[0][0], opener, key, timeout)[:CHOICES] if games else []
    return [STEAM_COVER.format(appid=appid) for appid, _name in steam_apps(name, opener, timeout)[:CHOICES]]


# ------------------------------------------------------------------ images

def image_kind(data: bytes) -> str:
    """'jpeg', 'png' or 'webp' from the first bytes; ArtworkError for anything else."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    raise ArtworkError("not a JPEG, PNG or WebP image")


def gdk_reencode(data: bytes, size: tuple[int, int] = TILE) -> bytes:
    """Decode with GdkPixbuf, shrink to fit `size`, and return a fresh JPEG. Raises ArtworkError."""
    if len(data) > MAX_IMAGE:
        raise ArtworkError("image is too large")
    kind = image_kind(data)
    import gi  # here, not at the top: the module and its tests work without GTK

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf, GLib

    too_big = []

    def prepared(loader, width, height) -> None:
        if width * height > MAX_PIXELS or width < 16 or height < 16:
            too_big.append(True)
            loader.set_size(1, 1)  # do not decode the full picture

    try:
        loader = GdkPixbuf.PixbufLoader.new_with_type(kind)
        loader.connect("size-prepared", prepared)
        loader.write(data)
        loader.close()
        pixbuf = loader.get_pixbuf()
    except GLib.Error:
        raise ArtworkError(f"not a readable {kind} image") from None
    if pixbuf is None or too_big:
        raise ArtworkError("image size is not usable")
    width, height = pixbuf.get_width(), pixbuf.get_height()
    scale = min(size[0] / width, size[1] / height, 1.0)
    target = (max(1, round(width * scale)), max(1, round(height * scale)))
    if pixbuf.get_has_alpha():  # JPEG has no transparency: flatten onto dark grey
        pixbuf = pixbuf.composite_color_simple(*target, GdkPixbuf.InterpType.BILINEAR, 255, 64, 0x202020, 0x202020)
    elif target != (width, height):
        pixbuf = pixbuf.scale_simple(*target, GdkPixbuf.InterpType.BILINEAR)
    try:
        ok, buffer = pixbuf.save_to_bufferv("jpeg", ["quality"], ["85"])
    except GLib.Error:
        raise ArtworkError("could not encode the image") from None
    if not ok:
        raise ArtworkError("could not encode the image")
    return bytes(buffer)


# ------------------------------------------------------------------ files

def _write(path: pathlib.Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)  # created 0600
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_json(path: pathlib.Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def lookup_enabled(path: pathlib.Path | None = None) -> bool:
    """Settings > APPEARANCE > ARTWORK > LOOKUP; ON unless switched off."""
    return _read_json(path or SETTINGS_FILE).get("lookup", True) is not False


def save_lookup(on: bool, path: pathlib.Path | None = None) -> None:
    _write(path or SETTINGS_FILE, json.dumps({"lookup": bool(on)}).encode(), 0o640)


def load_key(path: pathlib.Path | None = None) -> str:
    try:
        key = (path or KEY_FILE).read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return ""
    return key if KEY_PATTERN.fullmatch(key) else ""


def save_key(key: str, path: pathlib.Path | None = None) -> None:
    """Store the key 0600 (empty removes it). Raises ValueError for text that is not a key."""
    path = path or KEY_FILE
    key = key.strip()
    if not key:
        path.unlink(missing_ok=True)
        return
    if not KEY_PATTERN.fullmatch(key):
        raise ValueError("THAT IS NOT A STEAMGRIDDB API KEY")
    _write(path, key.encode("ascii"), 0o600)


def key_changed_at(path: pathlib.Path | None = None) -> float:
    """When the key was last set or removed: no-match results from before it are tried again."""
    path = path or KEY_FILE
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


class Cache:
    """Re-encoded covers and no-match results, keyed by host uuid + app name.

    `index.json` holds {key: {status, time, name, host}}; a cover is `<key>.jpg`, its
    modification time is when it was last shown, and the least recently shown go first once
    the directory is over `limit` bytes."""

    def __init__(self, root: pathlib.Path | None = None, limit: int = CACHE_LIMIT,
                 clock: Callable[[], float] = time.time) -> None:
        self.root = root or CACHE_DIR
        self.limit = limit
        self.clock = clock
        self.lock = threading.RLock()
        self.index = _read_json(self.root / "index.json")

    def path(self, key: str) -> pathlib.Path:
        return self.root / f"{key}.jpg"

    def lookup(self, key: str, not_before: float = 0.0) -> tuple[str, pathlib.Path | None] | None:
        """("found", file), ("miss", None) while still fresh, else None (look it up)."""
        with self.lock:
            entry = self.index.get(key)
            if not isinstance(entry, dict) or not isinstance(entry.get("time"), (int, float)):
                return None
            age = self.clock() - entry["time"]
            if entry.get("status") == "found" and 0 <= age <= FOUND_SECONDS:
                path = self.path(key)
                try:
                    if self.clock() - path.stat().st_mtime > 3600:  # remember the showing, hourly
                        os.utime(path, (self.clock(), self.clock()))
                except OSError:
                    return None
                return "found", path
            if entry.get("status") == "miss" and 0 <= age <= MISS_SECONDS and entry["time"] >= not_before:
                return "miss", None
            return None

    def put_image(self, key: str, data: bytes, name: str = "", host: str = "") -> pathlib.Path:
        with self.lock:
            _write(self.path(key), data)
            now = self.clock()
            os.utime(self.path(key), (now, now))
            self.index[key] = {"status": "found", "time": now, "name": name, "host": host}
            self.evict()
            self._save()
            return self.path(key)

    def put_miss(self, key: str, name: str = "", host: str = "") -> None:
        with self.lock:
            self.path(key).unlink(missing_ok=True)
            self.index[key] = {"status": "miss", "time": self.clock(), "name": name, "host": host}
            self._save()

    def forget(self, key: str) -> None:
        with self.lock:
            self.path(key).unlink(missing_ok=True)
            if self.index.pop(key, None) is not None:
                self._save()

    def evict(self) -> None:
        with self.lock:
            files = []
            for path in self.root.glob("*.jpg"):
                try:
                    info = path.stat()
                except OSError:
                    continue
                files.append((info.st_mtime, info.st_size, path))
            total = sum(size for _time, size, _path in files)
            for _time, size, path in sorted(files):
                if total <= self.limit:
                    break
                path.unlink(missing_ok=True)
                self.index.pop(path.stem, None)
                total -= size

    def unmatched(self, not_before: float = 0.0) -> list[tuple[str, str]]:
        """(host, name) of the games still remembered as no match, for Settings."""
        with self.lock:
            keys = [key for key in self.index if self.lookup(key, not_before) == ("miss", None)]
            return sorted((self.index[key].get("host", ""), self.index[key].get("name", "")) for key in keys)

    def _save(self) -> None:
        _write(self.root / "index.json", json.dumps(self.index, sort_keys=True).encode())


class Picks:
    """CHANGE ARTWORK choices per host and app: a picked cover (kept in PICKED_DIR) or TITLE CARD."""

    def __init__(self, path: pathlib.Path | None = None, folder: pathlib.Path | None = None) -> None:
        self.path = path or PICKS_FILE
        self.folder = folder or PICKED_DIR
        self.lock = threading.Lock()

    def get(self, key: str) -> str | pathlib.Path | None:
        choice = _read_json(self.path).get(key)
        if choice == TITLE_CARD:
            return TITLE_CARD
        if choice == "image" and (self.folder / f"{key}.jpg").is_file():
            return self.folder / f"{key}.jpg"
        return None

    def _set(self, key: str, value: str | None) -> None:
        with self.lock:
            data = _read_json(self.path)
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
            _write(self.path, json.dumps(data, sort_keys=True).encode(), 0o640)

    def pick_image(self, key: str, data: bytes) -> None:
        """`data` is an already re-encoded cover."""
        _write(self.folder / f"{key}.jpg", data)
        self._set(key, "image")

    def pick_title_card(self, key: str) -> None:
        self._set(key, TITLE_CARD)
        (self.folder / f"{key}.jpg").unlink(missing_ok=True)

    def reset(self, key: str) -> None:
        self._set(key, None)
        (self.folder / f"{key}.jpg").unlink(missing_ok=True)


def moonlight_art(host_uuid: str, app_id: int | None, root: pathlib.Path | None = None) -> pathlib.Path | None:
    """The box art Moonlight cached from the gaming PC, when there is a usable file."""
    if not host_uuid or app_id is None or "/" in host_uuid or host_uuid.startswith("."):
        return None
    path = (root or MOONLIGHT_BOXART) / host_uuid / f"{int(app_id)}.png"
    try:
        size = path.stat().st_size
    except OSError:
        return None
    return path if 0 < size <= MAX_IMAGE else None


# ------------------------------------------------------------------ the background worker

class Worker:
    """Looks covers up off the UI thread: WORKERS at once, TIMEOUT each, and after MAX_FAILURES
    failed requests in a row it stops until the next start. `art()` never touches the network."""

    def __init__(
        self,
        cache: Cache | None = None,
        picks: Picks | None = None,
        *,
        opener: Callable[..., object] | None = None,
        reencode: Callable[[bytes], bytes] = gdk_reencode,
        enabled: Callable[[], bool] = lookup_enabled,
        key: Callable[[], str] = load_key,
        key_time: Callable[[], float] = key_changed_at,
        moonlight: Callable[[str, int | None], pathlib.Path | None] = moonlight_art,
        threads: int = WORKERS,
        timeout: float = TIMEOUT,
    ) -> None:
        self.cache = cache or Cache()
        self.picks = picks or Picks()
        self.opener = opener or default_opener()
        self.reencode = reencode
        self.enabled = enabled
        self.key = key
        self.key_time = key_time
        self.moonlight = moonlight
        self.threads = threads
        self.timeout = timeout
        self.jobs: queue.Queue = queue.Queue()
        self.pending: set[str] = set()
        self.failures = 0
        self.stopped = False
        self.key_rejected = False
        self.lock = threading.Lock()
        self._changed = threading.Event()
        self._started = False

    def art(self, host_uuid: str, app: str, app_id: int | None = None, host_label: str = "") -> pathlib.Path | None:
        """The cover to draw now, or None for the title card; queues a lookup when one is due."""
        key = cache_key(host_uuid, app)
        pick = self.picks.get(key)
        if pick == TITLE_CARD:
            return None
        if isinstance(pick, pathlib.Path):
            return pick
        sent = self.moonlight(host_uuid, app_id)
        if sent is not None:
            return sent
        cached = self.cache.lookup(key, self.key_time())
        if cached is not None:
            return cached[1]
        if self.enabled():
            self.request(key, app, host_label)
        return None

    def request(self, key: str, app: str, host_label: str = "") -> bool:
        with self.lock:
            if self.stopped or key in self.pending:
                return False
            self.pending.add(key)
            if not self._started:
                self._started = True
                for _ in range(self.threads):
                    threading.Thread(target=self._loop, name="artwork", daemon=True).start()
        self.jobs.put((key, app, host_label))
        return True

    def _loop(self) -> None:
        while True:
            key, app, host_label = self.jobs.get()
            try:
                self.run_job(key, app, host_label)
            finally:
                with self.lock:
                    self.pending.discard(key)

    def run_job(self, key: str, app: str, host_label: str = "") -> str:
        """One lookup: "found", "miss", "failed" or "skipped". Never raises."""
        if self.stopped or not self.enabled():
            return "skipped"
        try:
            data = find_cover(app, self.opener, self.key(), self.timeout)
        except BadKey:
            self.key_rejected = True
            data = None  # Steam found nothing and the key does not work: a miss until the key changes
        except Offline:
            with self.lock:
                self.failures += 1
                if self.failures >= MAX_FAILURES:
                    self.stopped = True
            return "failed"
        except Exception:  # noqa: BLE001 - a lookup must never reach the home screen
            data = None
        with self.lock:
            self.failures = 0
        if data is not None:
            try:
                self.cache.put_image(key, self.reencode(data), app, host_label)
                self._changed.set()
                return "found"
            except Exception:  # noqa: BLE001 - not an image, or GdkPixbuf refused it
                pass
        try:
            self.cache.put_miss(key, app, host_label)
        except OSError:
            pass
        return "miss"

    def take_changed(self) -> bool:
        """True once after a new cover arrived (the home screen redraws)."""
        if self._changed.is_set():
            self._changed.clear()
            return True
        return False

    def choices(self, app: str, folder: pathlib.Path | None = None) -> list[pathlib.Path]:
        """Download and re-encode up to CHOICES covers for CHANGE ARTWORK into `folder`."""
        folder = folder or self.cache.root / "choices"
        for old in folder.glob("*.jpg"):
            old.unlink(missing_ok=True)
        try:
            urls = cover_choices(app, self.opener, self.key(), self.timeout)
        except BadKey:
            self.key_rejected = True
            urls = []
        except ArtworkError:
            return []

        def one(item: tuple[int, str]) -> pathlib.Path | None:
            index, url = item
            try:
                data = self.reencode(fetch(url, self.opener, MAX_IMAGE, self.timeout))
            except Exception:  # noqa: BLE001 - a missing or broken cover is left out
                return None
            path = folder / f"{index:02d}.jpg"
            _write(path, data)
            return path

        with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
            return [path for path in pool.map(one, enumerate(urls[:CHOICES])) if path is not None]

    def pick(self, host_uuid: str, app: str, choice: pathlib.Path | str | None) -> None:
        """Keep the user's choice: a file from choices(), TITLE_CARD, or None for RESET."""
        key = cache_key(host_uuid, app)
        if choice is None:
            self.picks.reset(key)
            self.cache.forget(key)  # look it up again
        elif choice == TITLE_CARD:
            self.picks.pick_title_card(key)
        else:
            self.picks.pick_image(key, pathlib.Path(choice).read_bytes())
        self._changed.set()
