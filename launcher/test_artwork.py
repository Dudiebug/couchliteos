import testenv  # noqa: F401  (first: scratch run and state directories)
import importlib.util
import io
import json
import os
import pathlib
import shutil
import stat
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

import couchliteos_apps as apps
import couchliteos_artwork as artwork
import couchliteos_home as home
import couchliteos_stream as stream
import couchliteos_tvlayout as tvlayout

try:
    import gi

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
except (ImportError, ValueError):
    GdkPixbuf = None

JPEG = b"\xff\xd8\xff\xe0" + b"j" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 100
KEY = "abcdef0123456789abcdef0123456789"


class Response(io.BytesIO):
    def __init__(self, body: bytes, url: str = "", headers: dict | None = None) -> None:
        super().__init__(body)
        self.url = url
        self.headers = headers or {}

    def geturl(self) -> str:
        return self.url


class FakeWeb:
    """Answers by URL prefix; records every request. Nothing reaches the network."""

    def __init__(self, routes: dict | None = None) -> None:
        self.routes = routes or {}
        self.requests: list = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.requests.append(request)
        for prefix, answer in self.routes.items():
            if url.startswith(prefix):
                if isinstance(answer, BaseException):
                    raise answer
                if isinstance(answer, int):
                    raise urllib.error.HTTPError(url, answer, "error", {}, None)
                body = json.dumps(answer).encode() if isinstance(answer, (dict, list)) else answer
                return Response(body, url)
        raise urllib.error.HTTPError(url, 404, "not found", {}, None)

    def urls(self) -> list[str]:
        return [request.full_url for request in self.requests]


def steam(*items) -> dict:
    return {"total": len(items), "items": [{"type": "app", "id": appid, "name": name} for appid, name in items]}


STEAM_SEARCH = "https://store.steampowered.com/api/storesearch/"
STEAM_CDN = "https://cdn.akamai.steamstatic.com/steam/apps/"
GRID_SEARCH = "https://www.steamgriddb.com/api/v2/search/autocomplete/"
GRID_COVERS = "https://www.steamgriddb.com/api/v2/grids/game/"


class NormalizeTests(unittest.TestCase):
    TABLE = [
        ("The Witcher® 3: Wild Hunt", "the witcher 3 wild hunt"),
        ("DOOM Eternal (Steam)", "doom eternal"),
        ("Fallout 4 GOTY", "fallout 4"),
        ("Fallout 4: Game of the Year Edition", "fallout 4"),
        ("Divinity: Original Sin 2 - Definitive Edition", "divinity original sin 2"),
        ("Batman™: Arkham Knight", "batman arkham knight"),
        ("Assassin's Creed® Odyssey", "assassins creed odyssey"),
        ("Tom Clancy’s Rainbow Six® Siege", "tom clancys rainbow six siege"),
        ("Ori & the Will of the Wisps", "ori and the will of the wisps"),
        ("Cyberpunk 2077 [GOG]", "cyberpunk 2077"),
        ("Cyberpunk 2077 - GOG", "cyberpunk 2077"),
        ("Half-Life 2", "half life 2"),
        ("Pokémon Legends", "pokemon legends"),
        ("Grand Theft Auto V: Premium Edition", "grand theft auto v"),
        ("Hades II", "hades ii"),
        ("Steam Big Picture", "steam big picture"),
        ("  Portal   2  ", "portal 2"),
        ("Disco Elysium - The Final Cut", "disco elysium the final cut"),
    ]

    def test_table(self):
        for name, expected in self.TABLE:
            with self.subTest(name=name):
                self.assertEqual(artwork.normalize(name), expected)

    def test_match_acceptance(self):
        self.assertTrue(artwork.matches("The Witcher 3: Wild Hunt GOTY", "The Witcher® 3: Wild Hunt"))
        self.assertTrue(artwork.matches("DOOM Eternal (Steam)", "DOOM Eternal"))
        self.assertFalse(artwork.matches("Portal", "Portal 2"))
        self.assertFalse(artwork.matches("Desktop", "Desktop Goose"))
        self.assertFalse(artwork.matches("™", "™"))  # nothing left to compare

    def test_cache_key_is_per_host_and_app(self):
        self.assertNotEqual(artwork.cache_key("a", "Portal"), artwork.cache_key("b", "Portal"))
        self.assertNotEqual(artwork.cache_key("a", "Portal"), artwork.cache_key("a", "Portal 2"))
        self.assertEqual(artwork.cache_key("a", "Portal"), artwork.cache_key("a", "Portal"))


class UrlTests(unittest.TestCase):
    def test_allow_list(self):
        self.assertTrue(artwork.allowed_url("https://cdn.akamai.steamstatic.com/steam/apps/1/library_600x900.jpg"))
        self.assertTrue(artwork.allowed_url("https://cdn2.steamgriddb.com/grid/x.png"))
        for url in ("http://cdn.akamai.steamstatic.com/a.jpg", "https://evil.example/a.jpg",
                    "https://cdn2.steamgriddb.com.evil.example/a.png", "https://user@cdn2.steamgriddb.com/a.png",
                    "https://cdn2.steamgriddb.com:8443/a.png", "file:///etc/passwd", "ftp://cdn2.steamgriddb.com/a"):
            with self.subTest(url=url):
                self.assertFalse(artwork.allowed_url(url))

    def test_fetch_refuses_other_hosts_without_a_request(self):
        web = FakeWeb()
        with self.assertRaises(artwork.ArtworkError):
            artwork.fetch("https://evil.example/a.jpg", web, 100)
        self.assertEqual(web.requests, [])

    def test_fetch_refuses_a_redirect_off_the_list(self):
        web = lambda request, timeout=None: Response(JPEG, "https://evil.example/x.jpg")  # noqa: E731
        with self.assertRaises(artwork.ArtworkError):
            artwork.fetch(STEAM_CDN + "1/library_600x900.jpg", web, artwork.MAX_IMAGE)

    def test_redirect_handler_refuses_other_hosts(self):
        handler = artwork._Redirects()
        with self.assertRaises(artwork.ArtworkError):
            handler.redirect_request(None, None, 302, "", {}, "http://cdn.akamai.steamstatic.com/a.jpg")
        request = urllib.request.Request(GRID_SEARCH + "x", headers={"Authorization": f"Bearer {KEY}"})
        moved = handler.redirect_request(request, None, 302, "", {}, "https://cdn2.steamgriddb.com/x.png")
        self.assertIsNone(moved.get_header("Authorization"))  # the key never follows a redirect off the API
        same = handler.redirect_request(request, None, 302, "", {}, GRID_SEARCH + "y")
        self.assertEqual(same.get_header("Authorization"), f"Bearer {KEY}")

    def test_oversize_download_is_rejected(self):
        big = JPEG + b"x" * artwork.MAX_IMAGE
        web = FakeWeb({STEAM_CDN: big})
        with self.assertRaises(artwork.ArtworkError):
            artwork.fetch(STEAM_CDN + "1/library_600x900.jpg", web, artwork.MAX_IMAGE)
        announced = lambda request, timeout=None: Response(JPEG, request.full_url, {"Content-Length": str(10**9)})  # noqa: E731
        with self.assertRaises(artwork.ArtworkError):
            artwork.fetch(STEAM_CDN + "1/library_600x900.jpg", announced, artwork.MAX_IMAGE)

    def test_non_images_are_rejected(self):
        self.assertEqual(artwork.image_kind(JPEG), "jpeg")
        self.assertEqual(artwork.image_kind(PNG), "png")
        self.assertEqual(artwork.image_kind(b"RIFF\0\0\0\0WEBPVP8 "), "webp")
        for data in (b"<html>", b"GIF89a....", b"", b"%PDF-1.4"):
            with self.assertRaises(artwork.ArtworkError):
                artwork.image_kind(data)
        with self.assertRaises(artwork.ArtworkError):
            artwork.gdk_reencode(b"<html>not an image</html>")  # refused before GTK is imported
        with self.assertRaises(artwork.ArtworkError):
            artwork.gdk_reencode(JPEG + b"x" * artwork.MAX_IMAGE)

    def test_offline_and_errors(self):
        url = STEAM_SEARCH + "?term=x"
        with self.assertRaises(artwork.Offline):
            artwork.fetch(url, FakeWeb({STEAM_SEARCH: urllib.error.URLError("no route")}), 100)
        with self.assertRaises(artwork.Offline):
            artwork.fetch(url, FakeWeb({STEAM_SEARCH: TimeoutError()}), 100)
        with self.assertRaises(artwork.Offline):
            artwork.fetch(url, FakeWeb({STEAM_SEARCH: 503}), 100)
        with self.assertRaises(artwork.Offline):
            artwork.fetch_json(url, FakeWeb({STEAM_SEARCH: b"<html>"}))


class LookupTests(unittest.TestCase):
    def test_steam_first_result_must_match(self):
        web = FakeWeb({STEAM_SEARCH: steam((620, "Portal 2"), (400, "Portal")), STEAM_CDN: JPEG})
        self.assertIsNone(artwork.find_cover("Portal", web))
        self.assertFalse(any(url.startswith(STEAM_CDN) for url in web.urls()))
        web = FakeWeb({STEAM_SEARCH: steam((620, "Portal 2")), STEAM_CDN + "620/": JPEG})
        self.assertEqual(artwork.find_cover("Portal 2", web), JPEG)
        self.assertEqual(web.urls()[-1], STEAM_CDN + "620/library_600x900.jpg")

    def test_only_the_name_is_sent_to_steam(self):
        web = FakeWeb({STEAM_SEARCH: steam()})
        artwork.find_cover("Hades II", web)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(web.urls()[0]).query)
        self.assertEqual(query["term"], ["Hades II"])
        self.assertNotIn("Authorization", web.requests[0].headers)

    def test_steamgriddb_only_with_a_key_and_after_steam(self):
        routes = {
            STEAM_SEARCH: steam(),
            GRID_SEARCH: {"success": True, "data": [{"id": 7, "name": "RetroArch"}]},
            GRID_COVERS + "7": {"success": True, "data": [{"url": "https://cdn2.steamgriddb.com/grid/a.png"}]},
            "https://cdn2.steamgriddb.com/": PNG,
        }
        web = FakeWeb(routes)
        self.assertIsNone(artwork.find_cover("RetroArch", web))
        self.assertEqual(len(web.requests), 1)  # no key: Steam only
        web = FakeWeb(routes)
        self.assertEqual(artwork.find_cover("RetroArch", web, KEY), PNG)
        self.assertTrue(web.urls()[0].startswith(STEAM_SEARCH))
        self.assertEqual(web.requests[1].get_header("Authorization"), f"Bearer {KEY}")
        self.assertIsNone(web.requests[3].get_header("Authorization"))  # the CDN never gets the key

    def test_bad_key(self):
        web = FakeWeb({STEAM_SEARCH: steam(), GRID_SEARCH: 401})
        with self.assertRaises(artwork.BadKey) as caught:
            artwork.find_cover("RetroArch", web, KEY)
        self.assertNotIn(KEY, str(caught.exception))

    def test_grid_urls_off_the_list_are_dropped(self):
        web = FakeWeb({GRID_COVERS: {"data": [{"url": "https://evil.example/a.png"},
                                              {"url": "https://cdn2.steamgriddb.com/b.png"}]}})
        self.assertEqual(artwork.grid_covers(1, web, KEY), ["https://cdn2.steamgriddb.com/b.png"])

    def test_choices(self):
        web = FakeWeb({STEAM_SEARCH: steam(*((n, f"Game {n}") for n in range(20)))})
        urls = artwork.cover_choices("Game", web)
        self.assertEqual(len(urls), artwork.CHOICES)
        self.assertEqual(urls[0], STEAM_CDN + "0/library_600x900.jpg")
        web = FakeWeb({GRID_SEARCH: {"data": [{"id": 3, "name": "Game"}]},
                       GRID_COVERS + "3": {"data": [{"url": f"https://cdn2.steamgriddb.com/{n}.png"} for n in range(15)]}})
        self.assertEqual(len(artwork.cover_choices("Game", web, KEY)), artwork.CHOICES)
        self.assertFalse(any(url.startswith(STEAM_SEARCH) for url in web.urls()))


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.clock = Clock()

    def tearDown(self):
        shutil.rmtree(self.root, True)

    def test_found_expires_after_30_days(self):
        cache = artwork.Cache(self.root, clock=self.clock)
        path = cache.put_image("k", JPEG, "Portal", "PC")
        self.assertEqual(cache.lookup("k"), ("found", path))
        self.clock.now += artwork.FOUND_SECONDS - 10
        self.assertEqual(cache.lookup("k")[0], "found")
        self.clock.now += 20
        self.assertIsNone(cache.lookup("k"))

    def test_miss_is_remembered_for_7_days(self):
        cache = artwork.Cache(self.root, clock=self.clock)
        cache.put_miss("k", "Emulator", "PC")
        self.assertEqual(cache.lookup("k"), ("miss", None))
        self.assertEqual(cache.unmatched(), [("PC", "Emulator")])
        self.clock.now += artwork.MISS_SECONDS + 1
        self.assertIsNone(cache.lookup("k"))
        self.assertEqual(cache.unmatched(), [])

    def test_miss_from_before_a_key_change_is_tried_again(self):
        cache = artwork.Cache(self.root, clock=self.clock)
        cache.put_miss("k")
        self.assertIsNone(cache.lookup("k", not_before=self.clock.now + 1))

    def test_index_survives_a_restart(self):
        artwork.Cache(self.root, clock=self.clock).put_miss("k", "A", "PC")
        self.assertEqual(artwork.Cache(self.root, clock=self.clock).lookup("k"), ("miss", None))

    def test_size_limit_removes_least_recently_shown(self):
        cache = artwork.Cache(self.root, limit=250, clock=self.clock)
        for index, name in enumerate(("a", "b", "c")):
            self.clock.now += 10
            cache.put_image(name, b"x" * 100)
        self.assertFalse(cache.path("a").exists())
        self.assertIsNone(cache.lookup("a"))
        self.assertTrue(cache.path("b").exists() and cache.path("c").exists())
        os.utime(cache.path("b"), (self.clock.now + 100, self.clock.now + 100))  # b shown recently
        self.clock.now += 10
        cache.put_image("d", b"x" * 100)
        self.assertTrue(cache.path("b").exists())
        self.assertFalse(cache.path("c").exists())

    def test_missing_file_means_look_again(self):
        cache = artwork.Cache(self.root, clock=self.clock)
        cache.put_image("k", JPEG)
        cache.path("k").unlink()
        self.assertIsNone(cache.lookup("k"))

    def test_corrupt_index_is_ignored(self):
        (self.root / "index.json").write_text("{not json")
        cache = artwork.Cache(self.root, clock=self.clock)
        self.assertIsNone(cache.lookup("k"))
        (self.root / "index.json").write_text(json.dumps({"k": {"status": "found", "time": "x"}}))
        self.assertIsNone(artwork.Cache(self.root, clock=self.clock).lookup("k"))


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_key_is_private_and_validated(self):
        path = self.root / "artwork.key"
        artwork.save_key(f"  {KEY}\n", path)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(artwork.load_key(path), KEY)
        with self.assertRaises(ValueError) as caught:
            artwork.save_key("not a key!", path)
        self.assertNotIn("not a key!", str(caught.exception))
        self.assertEqual(artwork.load_key(path), KEY)
        artwork.save_key("", path)
        self.assertFalse(path.exists())
        self.assertEqual(artwork.load_key(path), "")

    def test_lookup_defaults_on(self):
        path = self.root / "artwork.json"
        self.assertTrue(artwork.lookup_enabled(path))
        artwork.save_lookup(False, path)
        self.assertFalse(artwork.lookup_enabled(path))
        artwork.save_lookup(True, path)
        self.assertTrue(artwork.lookup_enabled(path))


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.clock = Clock()
        self.cache = artwork.Cache(self.root / "cache", clock=self.clock)
        self.picks = artwork.Picks(self.root / "picks.json", self.root / "picked")
        self.moonlight_root = self.root / "boxart"
        self.on = True
        self.key = ""
        self.encoded: list[bytes] = []

    def reencode(self, data: bytes) -> bytes:
        artwork.image_kind(data)
        self.encoded.append(data)
        return b"\xff\xd8\xff" + b"re-encoded"

    def worker(self, web) -> artwork.Worker:
        return artwork.Worker(
            self.cache, self.picks, opener=web, reencode=self.reencode, enabled=lambda: self.on,
            key=lambda: self.key, key_time=lambda: 0.0,
            moonlight=lambda uuid, app_id: artwork.moonlight_art(uuid, app_id, self.moonlight_root),
        )

    def test_source_order(self):
        web = FakeWeb({STEAM_SEARCH: steam((1, "Portal")), STEAM_CDN: JPEG})
        worker = self.worker(web)
        worker.request = lambda *args: self.fail("no lookup expected")  # type: ignore[method-assign]
        key = artwork.cache_key("uuid", "Portal")
        # 3. the cache
        cached = self.cache.put_image(key, JPEG)
        self.assertEqual(worker.art("uuid", "Portal", 5), cached)
        # 2. Moonlight's box art beats the cache
        sent = self.moonlight_root / "uuid" / "5.png"
        sent.parent.mkdir(parents=True)
        sent.write_bytes(PNG)
        self.assertEqual(worker.art("uuid", "Portal", 5), sent)
        self.assertEqual(worker.art("uuid", "Portal", None), cached)  # no app id: no Moonlight file
        # 1. the user's pick beats both; TITLE CARD means no art at all
        self.picks.pick_image(key, b"\xff\xd8\xffpicked")
        self.assertEqual(worker.art("uuid", "Portal", 5), self.root / "picked" / f"{key}.jpg")
        self.picks.pick_title_card(key)
        self.assertIsNone(worker.art("uuid", "Portal", 5))
        self.assertEqual(web.requests, [])

    def test_lookup_is_queued_only_when_needed_and_on(self):
        worker = self.worker(FakeWeb())
        queued = []
        worker.request = lambda key, app, host="": queued.append(app)  # type: ignore[method-assign]
        self.assertIsNone(worker.art("uuid", "Portal"))
        self.assertEqual(queued, ["Portal"])
        self.on = False
        self.assertIsNone(worker.art("uuid", "Hades"))
        self.assertEqual(queued, ["Portal"])
        self.on = True
        self.cache.put_miss(artwork.cache_key("uuid", "Emu"))
        self.assertIsNone(worker.art("uuid", "Emu"))
        self.assertEqual(queued, ["Portal"])

    def test_found_is_re_encoded_and_cached(self):
        web = FakeWeb({STEAM_SEARCH: steam((1, "Portal")), STEAM_CDN: JPEG})
        worker = self.worker(web)
        key = artwork.cache_key("uuid", "Portal")
        self.assertEqual(worker.run_job(key, "Portal", "PC"), "found")
        self.assertEqual(self.encoded, [JPEG])
        self.assertEqual(self.cache.path(key).read_bytes(), b"\xff\xd8\xffre-encoded")
        self.assertTrue(worker.take_changed())
        self.assertFalse(worker.take_changed())
        self.assertEqual(worker.art("uuid", "Portal"), self.cache.path(key))

    def test_non_image_download_becomes_a_miss(self):
        web = FakeWeb({STEAM_SEARCH: steam((1, "Portal")), STEAM_CDN: b"<html>"})
        worker = self.worker(web)
        key = artwork.cache_key("uuid", "Portal")
        self.assertEqual(worker.run_job(key, "Portal"), "miss")
        self.assertFalse(self.cache.path(key).exists())

    def test_no_match_is_remembered(self):
        web = FakeWeb({STEAM_SEARCH: steam((2, "Something Else"))})
        worker = self.worker(web)
        key = artwork.cache_key("uuid", "Emu")
        self.assertEqual(worker.run_job(key, "Emu", "PC"), "miss")
        self.assertEqual(self.cache.lookup(key), ("miss", None))
        self.assertEqual(self.cache.unmatched(), [("PC", "Emu")])

    def test_offline_stops_after_three_failures_and_records_nothing(self):
        web = FakeWeb({STEAM_SEARCH: urllib.error.URLError("offline")})
        worker = self.worker(web)
        for index in range(3):
            self.assertEqual(worker.run_job(f"k{index}", f"Game {index}"), "failed")
        self.assertTrue(worker.stopped)
        self.assertEqual(worker.run_job("k9", "Game 9"), "skipped")
        self.assertFalse(worker.request("k9", "Game 9"))
        self.assertEqual(len(web.requests), 3)
        self.assertIsNone(self.cache.lookup("k0"))

    def test_a_success_resets_the_failure_count(self):
        routes = {STEAM_SEARCH: urllib.error.URLError("offline")}
        worker = self.worker(FakeWeb(routes))
        worker.run_job("a", "A")
        worker.run_job("b", "B")
        worker.opener = FakeWeb({STEAM_SEARCH: steam()})
        self.assertEqual(worker.run_job("c", "C"), "miss")
        worker.opener = FakeWeb(routes)
        worker.run_job("d", "D")
        self.assertFalse(worker.stopped)

    def test_bad_key_is_flagged_and_steam_still_used(self):
        self.key = KEY
        web = FakeWeb({STEAM_SEARCH: steam(), GRID_SEARCH: 401})
        worker = self.worker(web)
        self.assertEqual(worker.run_job("k", "Emu"), "miss")
        self.assertTrue(worker.key_rejected)
        self.assertFalse(worker.stopped)

    def test_off_means_no_requests(self):
        self.on = False
        web = FakeWeb()
        self.assertEqual(self.worker(web).run_job("k", "Portal"), "skipped")
        self.assertEqual(web.requests, [])

    def test_threads_run_queued_jobs(self):
        web = FakeWeb({STEAM_SEARCH: steam((1, "Portal")), STEAM_CDN: JPEG})
        worker = self.worker(web)
        self.assertIsNone(worker.art("uuid", "Portal"))
        for _ in range(200):
            if worker.take_changed():
                break
            time.sleep(0.01)
        self.assertIsNotNone(worker.art("uuid", "Portal"))

    def test_change_artwork_persists(self):
        urls = [f"https://cdn2.steamgriddb.com/{n}.png" for n in range(3)]
        self.key = KEY
        web = FakeWeb({GRID_SEARCH: {"data": [{"id": 3, "name": "Emu"}]},
                       GRID_COVERS + "3": {"data": [{"url": url} for url in urls]},
                       urls[0]: PNG, urls[1]: b"<html>", urls[2]: JPEG})
        worker = self.worker(web)
        choices = worker.choices("Emu", self.root / "choices")
        self.assertEqual([path.name for path in choices], ["00.jpg", "02.jpg"])
        worker.pick("uuid", "Emu", choices[1])
        again = artwork.Worker(self.cache, artwork.Picks(self.root / "picks.json", self.root / "picked"),
                               opener=FakeWeb(), reencode=self.reencode, enabled=lambda: False,
                               key=lambda: "", key_time=lambda: 0.0, moonlight=lambda *_: None)
        picked = again.art("uuid", "Emu")
        self.assertEqual(picked.read_bytes(), choices[1].read_bytes())
        self.assertIsNone(again.art("other-pc", "Emu"))  # per host
        worker.pick("uuid", "Emu", artwork.TITLE_CARD)
        self.assertIsNone(again.art("uuid", "Emu"))
        self.assertEqual(again.picks.get(artwork.cache_key("uuid", "Emu")), artwork.TITLE_CARD)
        self.cache.put_image(artwork.cache_key("uuid", "Emu"), JPEG)
        worker.pick("uuid", "Emu", None)  # RESET: no pick, and look it up again
        self.assertIsNone(again.picks.get(artwork.cache_key("uuid", "Emu")))
        self.assertIsNone(self.cache.lookup(artwork.cache_key("uuid", "Emu")))

    def test_moonlight_art_checks(self):
        root = self.moonlight_root
        self.assertIsNone(artwork.moonlight_art("uuid", 5, root))
        (root / "uuid").mkdir(parents=True)
        (root / "uuid" / "5.png").write_bytes(b"")
        self.assertIsNone(artwork.moonlight_art("uuid", 5, root))  # Moonlight's failed save
        (root / "uuid" / "5.png").write_bytes(PNG)
        self.assertEqual(artwork.moonlight_art("uuid", 5, root), root / "uuid" / "5.png")
        self.assertIsNone(artwork.moonlight_art("../uuid", 5, root))
        self.assertIsNone(artwork.moonlight_art("", 5, root))


@unittest.skipIf(GdkPixbuf is None, "needs GdkPixbuf (python3-gi)")
class GdkReencodeTests(unittest.TestCase):
    @staticmethod
    def image(width: int, height: int, kind: str = "png", alpha: bool = False) -> bytes:
        pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, alpha, 8, width, height)
        pixbuf.fill(0x3366CC80)
        ok, data = pixbuf.save_to_bufferv(kind, [], [])
        assert ok
        return bytes(data)

    def decoded(self, data: bytes):
        loader = GdkPixbuf.PixbufLoader.new_with_type("jpeg")
        loader.write(data)
        loader.close()
        return loader.get_pixbuf()

    def test_png_and_jpeg_become_a_jpeg_that_fits_a_tile(self):
        for kind in ("png", "jpeg"):
            with self.subTest(kind=kind):
                out = artwork.gdk_reencode(self.image(600, 900, kind))
                self.assertEqual(artwork.image_kind(out), "jpeg")
                pixbuf = self.decoded(out)
                self.assertEqual((pixbuf.get_width(), pixbuf.get_height()), artwork.TILE)

    def test_small_image_is_not_enlarged_and_alpha_is_flattened(self):
        pixbuf = self.decoded(artwork.gdk_reencode(self.image(200, 300, alpha=True)))
        self.assertEqual((pixbuf.get_width(), pixbuf.get_height()), (200, 300))
        self.assertFalse(pixbuf.get_has_alpha())

    def test_broken_and_huge_images_are_rejected(self):
        good = self.image(64, 64)
        with self.assertRaises(artwork.ArtworkError):
            artwork.gdk_reencode(good[:40])  # truncated
        with self.assertRaises(artwork.ArtworkError):
            artwork.gdk_reencode(good[:16] + b"\xff" * 64)  # a PNG header, then garbage
        with self.assertRaises(artwork.ArtworkError):
            artwork.gdk_reencode(self.image(8, 8))  # too small to be a cover
        with self.assertRaises(artwork.ArtworkError):
            artwork.gdk_reencode(self.image(5000, 4000, "jpeg"))  # over MAX_PIXELS


class HostAppIdTests(unittest.TestCase):
    def test_app_ids_are_kept(self):
        text = (
            "[hosts]\n1\\hostname=PC\n1\\uuid=U\n1\\apps\\size=3\n"
            "1\\apps\\1\\name=Desktop\n1\\apps\\1\\id=881448767\n"
            "1\\apps\\2\\name=Steam Big Picture\n1\\apps\\2\\id=1093255277\n"
            "1\\apps\\3\\name=Broken\n1\\apps\\3\\id=x\nsize=1\n"
        )
        host = stream.parse_hosts(text)[0]
        self.assertEqual(host.apps, ("Desktop", "Steam Big Picture", "Broken"))
        self.assertEqual(host.app_id("Steam Big Picture"), 1093255277)
        self.assertIsNone(host.app_id("Broken"))
        self.assertEqual(host, stream.Host(name=host.name, uuid=host.uuid, apps=host.apps))


class ArtworkSettingsScreenTests(unittest.TestCase):
    """Settings > APPEARANCE > ARTWORK in the classic (curses) launcher."""

    def run_screen(self, keys, typed=None):
        from test_controls import FakeScreen, load_launcher

        root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        launcher = load_launcher()
        screen = FakeScreen(keys)
        menu = launcher.Settings(screen, mock.Mock())
        frames = []
        original_draw = menu.draw

        def draw(title, rows, selected, *args, **kwargs):
            frames.append((title, list(rows), menu.status))
            return original_draw(title, rows, selected, *args, **kwargs)

        with mock.patch.object(launcher, "read_key", side_effect=lambda window, **_kw: window.getch()), \
                mock.patch.object(artwork, "SETTINGS_FILE", root / "artwork.json"), \
                mock.patch.object(artwork, "KEY_FILE", root / "artwork.key"), \
                mock.patch.object(artwork, "CACHE_DIR", root / "cache"), \
                mock.patch.object(menu, "draw", side_effect=draw), \
                mock.patch.object(launcher.ApplicationsSettings, "text_input", return_value=typed) as text_input:
            menu.run_artwork()
        return root, frames, text_input

    def test_rows_and_the_privacy_sentence(self):
        _root, frames, _text_input = self.run_screen([27])
        title, rows, status = frames[0]
        self.assertEqual(title, "ARTWORK")
        self.assertEqual(rows, ["LOOKUP  ON", "STEAMGRIDDB KEY  NOT SET", "UNMATCHED GAMES  0", "BACK"])
        self.assertIn("ONLY GAME NAMES", status)
        self.assertLessEqual(len(status), 76)

    def test_lookup_toggles_and_the_key_is_saved_but_never_shown(self):
        root, frames, text_input = self.run_screen([10, 258, 10, 27], typed=KEY)  # 258 = KEY_DOWN
        self.assertFalse(artwork.lookup_enabled(root / "artwork.json"))
        self.assertEqual(artwork.load_key(root / "artwork.key"), KEY)
        self.assertEqual(stat.S_IMODE((root / "artwork.key").stat().st_mode), 0o600)
        self.assertTrue(text_input.call_args.kwargs["masked"])
        self.assertEqual(frames[-1][1][:2], ["LOOKUP  OFF", "STEAMGRIDDB KEY  SET"])
        self.assertFalse(any(KEY in " ".join(rows) + status for _title, rows, status in frames))

    def test_a_wrong_key_is_refused(self):
        root, frames, _text_input = self.run_screen([258, 10, 27], typed="hello")
        self.assertFalse((root / "artwork.key").exists())
        self.assertEqual(frames[-1][2], "THAT IS NOT A STEAMGRIDDB API KEY")


class TvArtworkTests(unittest.TestCase):
    """The TV interface's CHANGE ARTWORK keys and tile lookup, with the drawing left out."""

    HOST = stream.Host(name="Gaming-PC", uuid="U1", apps=("Portal", "Emu"), app_ids=(("Portal", 7),))

    def tv(self):
        path = pathlib.Path(__file__).with_name("couchliteos-tv.py")
        spec = importlib.util.spec_from_file_location("couchliteos_tv_art", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tv = object.__new__(module.Tv)
        tv.model = home.HomeModel(hosts=lambda: [self.HOST], applications=lambda: apps.LoadResult((), ()),
                                  installed=lambda _app: True, history=dict)
        tv.status = ""
        tv.mode = "home"
        for name in ("render_home", "render_bar", "render_art", "show", "change_artwork"):
            setattr(tv, name, mock.Mock())
        tv._art_worker = mock.Mock()
        return module, tv

    def test_f9_is_the_y_hold(self):
        self.assertEqual(tvlayout.action("F9"), "hold-y")

    def test_hold_y_opens_change_artwork(self):
        _module, tv = self.tv()
        tv.home_key("hold-y")
        tv.change_artwork.assert_called_once()

    def test_tile_asks_for_art_by_uuid_app_and_id(self):
        _module, tv = self.tv()
        tv._art_worker.art.return_value = pathlib.Path("/x.jpg")
        self.assertEqual(tv.cover(tv.model.focused()), pathlib.Path("/x.jpg"))
        tv._art_worker.art.assert_called_once_with("U1", "Portal", 7, "GAMING-PC")
        self.assertIsNone(tv.cover(home.Tile("system:settings", "SETTINGS", ("settings",))))

    def test_choosing_keeps_the_pick_and_goes_home(self):
        _module, tv = self.tv()
        covers = [pathlib.Path(f"/c/{n:02d}.jpg") for n in range(8)]
        tv.art_choice = (self.HOST, "Portal", "PORTAL", [*covers, None, artwork.TITLE_CARD])
        tv.art_index = 0
        tv.mode = "art"
        tv.art_key("right")
        tv.art_key("down")
        self.assertEqual(tv.art_index, 7)
        tv.art_key("down")
        self.assertEqual(tv.art_index, 9)  # clamped to the last choice: TITLE CARD
        tv.art_key("activate")
        tv._art_worker.pick.assert_called_once_with("U1", "Portal", artwork.TITLE_CARD)
        self.assertEqual(tv.status, "TITLE CARD CHOSEN")
        tv.show.assert_called_with("home")

    def test_back_leaves_without_a_pick(self):
        _module, tv = self.tv()
        tv.art_choice = (self.HOST, "Portal", "PORTAL", [None, artwork.TITLE_CARD])
        tv.art_index = 0
        tv.art_key("back")
        tv._art_worker.pick.assert_not_called()
        tv.show.assert_called_with("home")


if __name__ == "__main__":
    unittest.main()
