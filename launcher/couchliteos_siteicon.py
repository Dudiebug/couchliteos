"""The site's own icon for a web application on the XMB (Netflix, YouTube...), as plain Python.

`find_icon(url)` reads the site's front page for its web app manifest and <link rel=icon>
pictures, largest first, then tries /apple-touch-icon.png and /favicon.ico. Only the site is
asked, only over HTTPS to a public name (never an address, a local name, or a name that
resolves to a private address; redirects included), and nothing but the GETs is sent. The
picture is re-encoded like covers (couchliteos_artwork) and cached per site; with ARTWORK >
LOOKUP off, or offline, the item keeps its bundled icon.
"""

from __future__ import annotations

import html.parser
import ipaddress
import json
import pathlib
import socket
import urllib.parse
from collections.abc import Callable

import couchliteos_artwork as artwork

SIZE = (256, 256)
SMALLEST = 32  # a 16 px favicon blown up to an XMB icon looks worse than the bundled one
MAX_PAGE = 512 * 1024
MAX_MANIFEST = 256 * 1024
TRIES = 4
CACHE_LIMIT = 20 * 1024 * 1024
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localhost", ".arpa", ".localdomain", ".test",
                  ".invalid", ".example", ".corp", ".intranet")
RELS = {"apple-touch-icon": 180, "apple-touch-icon-precomposed": 180, "icon": 32, "shortcut icon": 32}


def _resolve(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]


def public_url(url: str, resolve: Callable[[str], list[str]] = _resolve) -> bool:
    """HTTPS to a public DNS name on port 443, which resolves only to global addresses."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").rstrip(".")
    if parts.scheme != "https" or port not in (None, 443) or parts.username or parts.password:
        return False
    if "." not in host or host == "localhost" or host.endswith(LOCAL_SUFFIXES):
        return False
    try:
        ipaddress.ip_address(host)
        return False  # an address, not a name
    except ValueError:
        pass
    try:
        addresses = resolve(host)
    except (OSError, UnicodeError):
        return False
    try:
        return bool(addresses) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses)
    except ValueError:
        return False


def site_of(url: str) -> str:
    """The site's front page for a web application's address ("" when it is not a web address)."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    return f"https://{parts.hostname.rstrip('.').lower()}/"


def _size(sizes: str, default: int) -> int:
    best = 0
    for item in (sizes or "").lower().split():
        width, _, height = item.partition("x")
        if width.isdigit() and height.isdigit():
            best = max(best, min(int(width), int(height)))
    return best or default


def _usable(href: str, kind: str = "") -> bool:
    path = urllib.parse.urlsplit(href).path.lower()
    return bool(href) and not href.startswith("data:") and "svg" not in kind.lower() and not path.endswith(".svg")


class _Links(html.parser.HTMLParser):
    """<link rel=icon|apple-touch-icon|manifest> of a page, until </head> or <body>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.icons: list[tuple[int, str]] = []
        self.manifest = ""
        self.done = False

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if tag == "body":
            self.done = True
            return
        if tag != "link":
            return
        values = {name: value or "" for name, value in attrs}
        rel = " ".join(values.get("rel", "").lower().split())
        href = values.get("href", "").strip()
        if rel == "manifest" and href and not self.manifest:
            self.manifest = href
        elif rel in RELS and _usable(href, values.get("type", "")):
            self.icons.append((_size(values.get("sizes", ""), RELS[rel]), href))

    def handle_endtag(self, tag):
        if tag == "head":
            self.done = True


def page_icons(page: str, base: str) -> tuple[list[tuple[int, str]], str]:
    """([(size, absolute url)], manifest url) from a page's HTML."""
    parser = _Links()
    try:
        parser.feed(page)
    except (AssertionError, ValueError):
        pass
    icons = [(size, urllib.parse.urljoin(base, href)) for size, href in parser.icons]
    return icons, urllib.parse.urljoin(base, parser.manifest) if parser.manifest else ""


def manifest_icons(data: bytes, base: str) -> list[tuple[int, str]]:
    """[(size, absolute url)] of a web app manifest's icons for any use (no monochrome ones)."""
    try:
        manifest = json.loads(data.decode("utf-8-sig"))
    except (UnicodeError, ValueError):
        return []
    found = []
    for icon in (manifest.get("icons") if isinstance(manifest, dict) else None) or []:
        if not isinstance(icon, dict) or not isinstance(icon.get("src"), str):
            continue
        purpose = str(icon.get("purpose", "any")).lower().split()
        if "monochrome" in purpose and "any" not in purpose:
            continue
        if _usable(icon["src"], str(icon.get("type", ""))):
            size = _size(str(icon.get("sizes", "")), 0)
            found.append((size - (1 if "any" not in purpose else 0), urllib.parse.urljoin(base, icon["src"])))
    return found


def candidates(site: str, opener, check: Callable[[str], bool], timeout: float = artwork.TIMEOUT) -> list[str]:
    """Icon addresses for `site`, best first: the manifest's and the page's by size, then the
    usual /apple-touch-icon.png and /favicon.ico. Raises Offline when the site does not answer."""
    found: list[tuple[int, str]] = []
    try:
        page = artwork.fetch(site, opener, MAX_PAGE, timeout, {"Accept": "text/html"}, check)
    except artwork.Offline:
        raise
    except artwork.ArtworkError:
        page = b""
    icons, manifest = page_icons(page.decode("utf-8", "replace"), site)
    found += icons
    if manifest:
        try:
            found += manifest_icons(artwork.fetch(manifest, opener, MAX_MANIFEST, timeout, None, check), manifest)
        except artwork.ArtworkError:
            pass
    found += [(180, urllib.parse.urljoin(site, "/apple-touch-icon.png")), (32, urllib.parse.urljoin(site, "/favicon.ico"))]
    ordered: list[str] = []
    for _size_, url in sorted(found, key=lambda item: -item[0]):
        if url not in ordered:
            ordered.append(url)
    return ordered


def find_icon(url: str, opener, reencode: Callable[[bytes], bytes], check: Callable[[str], bool] = public_url,
              timeout: float = artwork.TIMEOUT) -> bytes | None:
    """The re-encoded icon of the site `url` belongs to, or None. Raises Offline."""
    site = site_of(url)
    if not site or not check(site):
        return None
    for address in candidates(site, opener, check, timeout)[:TRIES]:
        try:
            return reencode(artwork.fetch(address, opener, artwork.MAX_IMAGE, timeout, {"Accept": "image/*"}, check))
        except artwork.Offline:
            raise
        except Exception:  # noqa: BLE001 - missing, not an image, too small: the next one
            continue
    return None


def _reencode(data: bytes) -> bytes:
    return artwork.gdk_reencode(data, SIZE, alpha=True, smallest=SMALLEST)


class SiteIcons(artwork.Worker):
    """Site icons off the UI thread, cached per site (CACHE_DIR/site) like covers."""

    def __init__(self, cache: artwork.Cache | None = None, *, opener: Callable[..., object] | None = None,
                 reencode: Callable[[bytes], bytes] = _reencode, check: Callable[[str], bool] = public_url,
                 enabled: Callable[[], bool] = artwork.lookup_enabled, threads: int = 2,
                 timeout: float = artwork.TIMEOUT) -> None:
        cache = cache or artwork.Cache(artwork.CACHE_DIR / "site", CACHE_LIMIT, suffix=".png")
        super().__init__(cache, artwork.Picks(pathlib.Path("/nonexistent/picks.json"), pathlib.Path("/nonexistent")),
                         opener=opener or artwork.default_opener(check), reencode=reencode, enabled=enabled,
                         key=lambda: "", key_time=lambda: 0.0, moonlight=lambda _uuid, _app_id: None,
                         threads=threads, timeout=timeout)
        self.check = check

    def icon(self, url: str) -> pathlib.Path | None:
        """The site's icon to draw now, or None (the bundled icon); queues a lookup when due."""
        site = site_of(url)
        if not site:
            return None
        key = artwork.cache_key("site", site)
        cached = self.cache.lookup(key)
        if cached is not None:
            return cached[1]
        if self.enabled():
            self.request(key, site, "")
        return None

    def find(self, app: str):
        return find_icon(app, self.opener, self.reencode, self.check, self.timeout)

    def store(self, key: str, app: str, host_label: str, data: bytes | None) -> str:
        if data is not None:  # already re-encoded by find_icon
            self.cache.put_image(key, data, app, host_label)
            self._changed.set()
            return "found"
        try:
            self.cache.put_miss(key, app, host_label)
        except OSError:
            pass
        return "miss"
