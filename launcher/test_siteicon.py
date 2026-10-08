import testenv  # noqa: F401  (first: scratch run and state directories)
import json
import pathlib
import shutil
import tempfile
import unittest

import couchliteos_artwork as artwork
import couchliteos_siteicon as siteicon
from test_artwork import PNG, Clock, FakeWeb

ICO = b"\x00\x00\x01\x00" + b"i" * 50
SITE = "https://www.netflix.com/"
PAGE = b"""<!doctype html><html><head>
<link rel="icon" href="/favicon-32.png" sizes="32x32">
<link rel="apple-touch-icon" href="https://assets.nflxext.com/touch.png">
<link rel="icon" type="image/svg+xml" href="/logo.svg">
<link rel="manifest" href="/manifest.json">
</head><body><link rel="icon" href="/late.png" sizes="999x999"></body></html>"""
MANIFEST = {"icons": [
    {"src": "/icons/512.png", "sizes": "512x512", "type": "image/png"},
    {"src": "/icons/mono.png", "sizes": "1024x1024", "purpose": "monochrome"},
    {"src": "/icons/mask.png", "sizes": "512x512", "purpose": "maskable"},
    {"src": "/icons/any.svg", "sizes": "any", "type": "image/svg+xml"},
]}


def everywhere(_url):
    return True


class PublicUrlTest(unittest.TestCase):
    def test_only_https_names_that_resolve_to_public_addresses(self):
        public = lambda host: ["54.155.178.5", "2a05:d018:76c:b685::1"]  # noqa: E731
        self.assertTrue(siteicon.public_url("https://www.netflix.com/browse", public))
        for url in ("http://www.netflix.com/", "https://www.netflix.com:8443/", "https://user:pw@netflix.com/",
                    "https://192.168.1.1/", "https://[::1]/", "https://router/", "https://nas.local/",
                    "https://box.lan/", "https://localhost/", "ftp://netflix.com/", "https://x.home.arpa/"):
            self.assertFalse(siteicon.public_url(url, public), url)

    def test_a_name_that_resolves_to_a_private_address_is_refused(self):
        for addresses in (["10.0.0.5"], ["54.1.1.1", "127.0.0.1"], ["fe80::1%eth0"], [], ["nonsense"]):
            self.assertFalse(siteicon.public_url("https://evil.example.org/", lambda host, a=addresses: a), addresses)

        def broken(host):
            raise OSError("no such name")
        self.assertFalse(siteicon.public_url("https://nowhere.example.org/", broken))


class PageTest(unittest.TestCase):
    def test_site_of(self):
        self.assertEqual(siteicon.site_of("https://www.Netflix.com/browse?x=1"), "https://www.netflix.com/")
        self.assertEqual(siteicon.site_of("http://youtube.com/tv"), "https://youtube.com/")
        self.assertEqual(siteicon.site_of("file:///etc/passwd"), "")
        self.assertEqual(siteicon.site_of("not a url"), "")

    def test_the_heads_links_without_svg_or_what_comes_after(self):
        icons, manifest = siteicon.page_icons(PAGE.decode(), SITE)
        self.assertEqual(sorted(icons), [(32, SITE + "favicon-32.png"), (180, "https://assets.nflxext.com/touch.png")])
        self.assertEqual(manifest, SITE + "manifest.json")
        self.assertEqual(siteicon.page_icons("<link rel=icon href=", SITE), ([], ""))

    def test_manifest_icons_for_any_use(self):
        icons = siteicon.manifest_icons(json.dumps(MANIFEST).encode(), SITE + "manifest.json")
        self.assertEqual(icons, [(512, SITE + "icons/512.png"), (511, SITE + "icons/mask.png")])
        self.assertEqual(siteicon.manifest_icons(b"not json", SITE), [])
        self.assertEqual(siteicon.manifest_icons(b"[1, 2]", SITE), [])

    def test_candidates_best_first(self):
        web = FakeWeb({SITE + "manifest.json": MANIFEST, SITE: PAGE})
        self.assertEqual(siteicon.candidates(SITE, web, everywhere), [
            SITE + "icons/512.png", SITE + "icons/mask.png", "https://assets.nflxext.com/touch.png",
            SITE + "apple-touch-icon.png", SITE + "favicon-32.png", SITE + "favicon.ico",
        ])

    def test_a_site_without_a_page_still_gets_the_usual_places(self):
        self.assertEqual(siteicon.candidates(SITE, FakeWeb(), everywhere),
                         [SITE + "apple-touch-icon.png", SITE + "favicon.ico"])

    def test_an_offline_site(self):
        with self.assertRaises(artwork.Offline):
            siteicon.candidates(SITE, FakeWeb({SITE: OSError("down")}), everywhere)


class FindIconTest(unittest.TestCase):
    def setUp(self):
        self.encoded = []

    def reencode(self, data):
        if data == b"tiny":
            raise artwork.ArtworkError("too small")
        artwork.image_kind(data)
        self.encoded.append(data)
        return b"\x89PNG\r\n\x1a\nsite"

    def test_the_first_one_that_is_a_usable_picture(self):
        web = FakeWeb({SITE + "icons/512.png": b"tiny", SITE + "manifest.json": MANIFEST,
                       SITE + "apple-touch-icon.png": PNG, SITE: PAGE})
        found = siteicon.find_icon("https://www.netflix.com/browse", web, self.reencode, everywhere)
        self.assertEqual(found, b"\x89PNG\r\n\x1a\nsite")
        self.assertEqual(self.encoded, [PNG])

    def test_a_favicon_ico_will_do(self):
        web = FakeWeb({SITE + "favicon.ico": ICO})
        self.assertIsNotNone(siteicon.find_icon(SITE, web, self.reencode, everywhere))
        self.assertEqual(artwork.image_kind(ICO), "ico")

    def test_nothing_is_asked_of_a_refused_site(self):
        web = FakeWeb({SITE: PAGE})
        self.assertIsNone(siteicon.find_icon(SITE, web, self.reencode, lambda url: False))
        self.assertEqual(web.urls(), [])

    def test_icons_on_refused_hosts_are_skipped(self):
        web = FakeWeb({SITE: PAGE, "https://assets.nflxext.com/touch.png": PNG})
        found = siteicon.find_icon(SITE, web, self.reencode, lambda url: "nflxext" not in url)
        self.assertIsNone(found)
        self.assertFalse(any("nflxext" in url for url in web.urls()))


class SiteIconsTest(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.cache = artwork.Cache(self.root / "site", clock=Clock(), suffix=".png")
        self.on = True

    def icons(self, web):
        return siteicon.SiteIcons(self.cache, opener=web, reencode=lambda data: b"\x89PNG\r\n\x1a\nok",
                                  check=everywhere, enabled=lambda: self.on)

    def test_looked_up_once_per_site_then_cached(self):
        icons = self.icons(FakeWeb({SITE + "apple-touch-icon.png": PNG}))
        queued = []
        icons.request = lambda key, app, label="": queued.append((key, app)) or True  # type: ignore[method-assign]
        self.assertIsNone(icons.icon("https://www.netflix.com/browse"))
        self.assertEqual(queued, [(artwork.cache_key("site", SITE), SITE)])
        self.assertEqual(icons.run_job(*queued[0]), "found")
        self.assertEqual(icons.icon("https://www.netflix.com/title/1").read_bytes(), b"\x89PNG\r\n\x1a\nok")
        self.assertEqual(len(queued), 1)

    def test_a_miss_is_remembered_and_lookup_off_asks_nothing(self):
        icons = self.icons(FakeWeb())
        key = artwork.cache_key("site", SITE)
        self.assertEqual(icons.run_job(key, SITE), "miss")
        self.assertEqual(self.cache.lookup(key), ("miss", None))
        self.on = False
        icons.request = lambda *args: self.fail("no lookup with LOOKUP off")  # type: ignore[method-assign]
        self.assertIsNone(icons.icon("https://www.youtube.com/tv"))
        self.assertIsNone(icons.icon("not a url"))


if __name__ == "__main__":
    unittest.main()
