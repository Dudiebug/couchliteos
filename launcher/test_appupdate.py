"""App updates between releases: manifest, download, the `current` swap, rollbacks, pruning."""

import contextlib
import hashlib
import io
import json
import os
import pathlib
import struct
import subprocess
import tempfile
import unittest
import urllib.error
from unittest import mock

import couchliteos_appupdate as appupdate

URL = "https://github.com/Dudiebug/couchliteos/releases/latest/download/apps.json"
MOONLIGHT_URL = "https://github.com/moonlight-stream/moonlight-qt/releases/download/v{0}/Moonlight-{0}-x86_64.AppImage"
LOCK = (
    "# name|version|url|installed filename\n"
    "moonlight|6.1.0|https://github.com/moonlight-stream/moonlight-qt/releases/download/v6.1.0/Moonlight-6.1.0-x86_64.AppImage|moonlight.AppImage\n"
    "chiaki-ng|1.10.0|https://github.com/streetpea/chiaki-ng/releases/download/v1.10.0/chiaki-ng.AppImage_x86_64|chiaki-ng.AppImage\n"
)


def _symlinks_work() -> bool:
    with tempfile.TemporaryDirectory() as directory:
        try:
            os.symlink("target", pathlib.Path(directory) / "link")
        except (OSError, NotImplementedError):
            return False
    return True


SYMLINKS = _symlinks_work()


def fake_symlink(target, path):
    """Where symlinks need privileges (Windows without developer mode): a file naming its target."""
    pathlib.Path(path).write_text("link:" + str(target), encoding="utf-8")


def fake_readlink(path):
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise OSError(str(error)) from error
    if not text.startswith("link:"):
        raise OSError("not a link")
    return text[5:]


def appimage(payload: bytes = b"payload") -> bytes:
    """A minimal 64-bit little-endian ELF runtime whose section headers end where a squashfs starts."""
    header = bytearray(64)
    header[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<Q", header, 0x28, 64)       # e_shoff
    struct.pack_into("<HH", header, 0x3A, 64, 2)   # e_shentsize, e_shnum
    return bytes(header) + bytes(128) + b"hsqs" + payload


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Response(io.BytesIO):
    def __init__(self, data: bytes, status: int = 200) -> None:
        super().__init__(data)
        self.status = status


class Opener:
    """Serves {url: bytes}; honours Range like GitHub's CDN; records every request."""

    def __init__(self, files):
        self.files = files
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        url = request.full_url
        if url not in self.files:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        data = self.files[url]
        start = request.get_header("Range")
        if start:
            offset = int(start.removeprefix("bytes=").rstrip("-"))
            return Response(data[offset:], 206)
        return Response(data)

    def urls(self):
        return [request.full_url for request in self.requests]


class Runner:
    """Records argv; `unsquashfs` makes the app's program in the destination."""

    def __init__(self, unsquash_ok=True):
        self.calls = []
        self.unsquash_ok = unsquash_ok

    def __call__(self, argv, **_kwargs):
        argv = [str(part) for part in argv]
        self.calls.append(argv)
        if argv[0] == "unsquashfs" and self.unsquash_ok:
            destination = pathlib.Path(argv[argv.index("-dest") + 1])
            for program in appupdate.APPS.values():
                path = destination / program
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("#!/bin/sh\n")
                path.chmod(0o755)
            return subprocess.CompletedProcess(argv, 0, stdout="")
        return subprocess.CompletedProcess(argv, 0 if self.unsquash_ok else 1, stdout="")


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)
        if not SYMLINKS:
            for name, fake in (("symlink", fake_symlink), ("readlink", fake_readlink)):
                patcher = mock.patch.object(appupdate, name, fake)
                patcher.start()
                self.addCleanup(patcher.stop)
        self.root = self.tmp / "root"
        lock = self.root / appupdate.IMAGE_LOCK_REL
        lock.parent.mkdir(parents=True)
        lock.write_text(LOCK)
        version = self.root / appupdate.VERSION_REL
        version.parent.mkdir(parents=True)
        version.write_text("0.3.0\n")
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()
        self.files = {}
        self.opener = Opener(self.files)
        self.runner = Runner()
        self.busy = ""
        self.tested = []
        self.test_ok = True
        self.flatpak_calls = 0
        self.flatpak_result = {"result": "ok", "message": ""}
        self.log = []
        self.env = appupdate.Env(
            runner=self.runner, opener=self.opener, root=self.root, apps_dir=self.tmp / "apps",
            health_dir=self.tmp / "health", cache_dir=self.tmp / "cache", run_dir=self.run_dir,
            state=self.tmp / "app-update.json", config=self.tmp / "config.ini", manifest_urls=(URL,),
            clock=lambda: 1000.0, owner_uid=None, busy=lambda _run: self.busy,
            test_run=self._test_run, flatpak=self._flatpak, log=self.log.append,
        )

    def _test_run(self, _env, app_dir, binary, args):
        self.tested.append((app_dir.name, binary.name, args))
        return self.test_ok

    def _flatpak(self, _env):
        self.flatpak_calls += 1
        return self.flatpak_result

    def publish(self, *apps, raw=None):
        """Publish a manifest; each app is (name, version) or a dict of overrides."""
        entries = []
        for app in apps:
            item = app if isinstance(app, dict) else {"name": app[0], "version": app[1]}
            name, version = item["name"], item["version"]
            data = appimage(f"{name}-{version}".encode())
            url = item.get("url", MOONLIGHT_URL.format(version) if name == "moonlight"
                           else f"https://github.com/streetpea/chiaki-ng/releases/download/v{version}/chiaki-ng.AppImage_x86_64")
            self.files[url] = data
            entries.append({"name": name, "version": version, "url": url, "sha256": sha(data),
                            "min_os": "0.3.0", "size": len(data), **item})
        self.files[URL] = (raw if raw is not None else json.dumps({"schema": 1, "apps": entries})).encode()

    def app_dir(self, name="moonlight"):
        return self.env.apps_dir / name

    def links(self, name="moonlight"):
        return (appupdate.link_version(self.app_dir(name) / "current"),
                appupdate.link_version(self.app_dir(name) / "previous"))

    def versions(self, name="moonlight"):
        return sorted(p.name for p in self.app_dir(name).iterdir() if p.is_dir() and not p.is_symlink())

    def state(self):
        return json.loads(self.env.state.read_text())

    def row(self, name="moonlight"):
        return next(row for row in self.state()["apps"] if row["name"] == name)

    def run_(self, **kwargs):
        return appupdate.run(self.env, **kwargs)


# ---------------------------------------------------------------- manifest


class ManifestTest(unittest.TestCase):
    def entry(self, **overrides):
        return {"name": "moonlight", "version": "6.2.0", "url": MOONLIGHT_URL.format("6.2.0"),
                "sha256": "a" * 64, "min_os": "0.3.0", **overrides}

    def parse(self, *apps, schema=1):
        return appupdate.parse_manifest(json.dumps({"schema": schema, "apps": list(apps)}))

    def test_a_valid_entry_is_read(self):
        entries = self.parse(self.entry(size=10, check_args=["--version"]))
        self.assertEqual(entries["moonlight"], appupdate.Entry(
            "moonlight", "6.2.0", MOONLIGHT_URL.format("6.2.0"), "a" * 64, "0.3.0", 10, ("--version",)))

    def test_only_github_https_addresses_are_accepted(self):
        for url in ("http://github.com/x/y.AppImage", "https://example.com/y.AppImage",
                    "https://github.com.evil.example/y", "https://objects.githubusercontent.com/y", 7):
            with self.subTest(url=url):
                self.assertEqual(self.parse(self.entry(url=url)), {})
        with self.assertRaises(appupdate.AppUpdateError):
            appupdate.check_url("https://raw.githubusercontent.com/x/apps.json")
        appupdate.check_url(URL)

    def test_unknown_apps_and_bad_fields_are_left_out(self):
        self.assertEqual(self.parse(self.entry(name="steam")), {})
        for field, value in (("version", "latest"), ("version", "../6"), ("sha256", "abc"), ("min_os", "soon"),
                             ("size", 0), ("size", True), ("check_args", ["; rm -rf /"]), ("check_args", "x")):
            with self.subTest(field=field, value=value):
                self.assertEqual(self.parse(self.entry(**{field: value})), {})

    def test_an_uppercase_checksum_is_normalised(self):
        self.assertEqual(self.parse(self.entry(sha256="A" * 64))["moonlight"].sha256, "a" * 64)

    def test_a_damaged_or_foreign_manifest_is_refused(self):
        for text in ("not json", "[]", json.dumps({"schema": 2, "apps": []}), json.dumps({"schema": 1})):
            with self.subTest(text=text), self.assertRaises(appupdate.AppUpdateError):
                appupdate.parse_manifest(text)

    def test_image_versions_come_from_the_installed_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "applications.lock"
            path.write_text(LOCK + "unknown|1.0|https://github.com/x|x\n")
            self.assertEqual(appupdate.image_versions(path), {"moonlight": "6.1.0", "chiaki-ng": "1.10.0"})
            self.assertEqual(appupdate.image_versions(pathlib.Path(directory) / "missing"), {})

    def test_the_repository_lock_parses(self):
        lock = pathlib.Path(__file__).resolve().parents[1] / "build" / "applications.lock"
        self.assertEqual(set(appupdate.image_versions(lock)), set(appupdate.APPS))


class FetchTest(Case):
    def test_the_second_address_is_asked_when_the_first_fails(self):
        self.env.manifest_urls = ("https://github.com/Dudiebug/missing/apps.json", URL)
        self.publish(("moonlight", "6.2.0"))
        self.assertIn("moonlight", appupdate.fetch_manifest(self.env))

    def test_a_manifest_address_off_github_is_refused_without_asking(self):
        self.env.manifest_urls = ("https://example.com/apps.json",)
        with self.assertRaises(appupdate.AppUpdateError):
            appupdate.fetch_manifest(self.env)
        self.assertEqual(self.opener.requests, [])

    def test_no_answer_is_a_network_error(self):
        with self.assertRaises(appupdate.AppUpdateError) as caught:
            appupdate.fetch_manifest(self.env)
        self.assertEqual(caught.exception.message, appupdate.MSG_NETWORK)

    def test_an_oversized_manifest_is_refused(self):
        self.files[URL] = b" " * (appupdate.MANIFEST_MAX + 1)
        with self.assertRaises(appupdate.AppUpdateError):
            appupdate.fetch_manifest(self.env)


# ---------------------------------------------------------------- install


class InstallTest(Case):
    def test_a_newer_version_is_downloaded_unpacked_tested_and_made_current(self):
        self.publish(("moonlight", "6.2.0"), ("chiaki-ng", "1.10.0"))
        self.assertEqual(self.run_(manual=True), 0)
        self.assertEqual(self.links(), ("6.2.0", ""))
        self.assertTrue((self.app_dir() / "6.2.0" / "usr/bin/moonlight").is_file())
        self.assertEqual(self.tested, [("6.2.0", "moonlight", ())])
        unsquash = [call for call in self.runner.calls if call[0] == "unsquashfs"]
        self.assertEqual(unsquash[0][unsquash[0].index("-offset") + 1], str(64 + 64 * 2))
        self.assertFalse(self.app_dir("chiaki-ng").exists(), "the image already ships chiaki-ng 1.10.0")
        self.assertEqual(self.row()["active"], "6.2.0")
        self.assertEqual(self.row()["state"], "uptodate")
        self.assertEqual(self.state()["result"], "ok")
        self.assertEqual(self.state()["message"], appupdate.MSG_INSTALLED)
        self.assertEqual(self.state()["checked_at"], 1000)
        self.assertEqual(list(self.env.cache_dir.iterdir()), [], "the AppImage is removed once unpacked")
        self.assertEqual(self.flatpak_calls, 1)

    def test_a_checksum_mismatch_is_refused_and_nothing_changes(self):
        self.publish({"name": "moonlight", "version": "6.2.0", "sha256": "0" * 64})
        self.assertEqual(self.run_(manual=True), 1)
        self.assertEqual(self.links(), ("", ""))
        self.assertEqual(self.row()["state"], "failed")
        self.assertIn("CHECKSUM", self.row()["message"])
        self.assertEqual(self.state()["result"], "failed")
        self.assertFalse([call for call in self.runner.calls if call[0] == "unsquashfs"])
        self.assertEqual(list(self.env.cache_dir.iterdir()), [], "the damaged download is deleted")

    def test_a_version_for_a_newer_couchliteos_is_skipped(self):
        self.publish({"name": "moonlight", "version": "6.2.0", "min_os": "0.4.0"})
        self.assertEqual(self.run_(manual=True), 0)
        self.assertEqual(self.links(), ("", ""))
        self.assertEqual(self.row()["state"], "needs_os")
        self.assertEqual(self.row()["message"], "NEEDS COUCHLITEOS 0.4.0")
        self.assertEqual(self.opener.urls(), [URL])

    def test_the_same_or_an_older_version_than_the_image_is_not_installed(self):
        self.publish(("moonlight", "6.1.0"), ("chiaki-ng", "1.9.0"))
        self.assertEqual(self.run_(manual=True), 0)
        self.assertEqual(self.opener.urls(), [URL])
        self.assertEqual([row["state"] for row in self.state()["apps"]], ["uptodate", "uptodate"])

    def test_a_version_that_fails_its_test_run_is_not_made_current(self):
        self.publish(("moonlight", "6.2.0"))
        self.test_ok = False
        self.assertEqual(self.run_(manual=True), 1)
        self.assertEqual(self.links(), ("", ""))
        self.assertEqual(self.versions(), [])
        self.assertEqual(self.row()["message"], "THE NEW APP VERSION DID NOT START")

    def test_a_download_that_is_not_an_appimage_is_refused(self):
        self.publish(("moonlight", "6.2.0"))
        url = MOONLIGHT_URL.format("6.2.0")
        self.files[url] = b"<html>not an appimage</html>"
        self.files[URL] = json.dumps({"schema": 1, "apps": [{"name": "moonlight", "version": "6.2.0", "url": url,
                                                             "sha256": sha(self.files[url])}]}).encode()
        self.assertEqual(self.run_(manual=True), 1)
        self.assertEqual(self.row()["message"], "THE DOWNLOAD IS NOT AN APPIMAGE")

    def test_a_partial_download_is_resumed(self):
        self.publish(("moonlight", "6.2.0"))
        data = self.files[MOONLIGHT_URL.format("6.2.0")]
        self.env.cache_dir.mkdir()
        (self.env.cache_dir / "moonlight-6.2.0.AppImage.part").write_bytes(data[:100])
        (self.env.cache_dir / "moonlight-6.0.0.AppImage.part").write_bytes(b"old")
        self.assertEqual(self.run_(manual=True), 0)
        download = [request for request in self.opener.requests if request.full_url != URL]
        self.assertEqual(download[0].get_header("Range"), "bytes=100-")
        self.assertEqual(self.links(), ("6.2.0", ""))
        self.assertEqual(list(self.env.cache_dir.iterdir()), [])

    def test_exactly_one_previous_version_is_kept(self):
        for version in ("6.2.0", "6.3.0", "6.4.0"):
            self.publish(("moonlight", version))
            self.assertEqual(self.run_(manual=True), 0)
        self.assertEqual(self.links(), ("6.4.0", "6.3.0"))
        self.assertEqual(self.versions(), ["6.3.0", "6.4.0"])
        self.assertEqual(self.row()["previous"], "6.3.0")

    def test_a_new_version_starts_with_a_clean_failure_count(self):
        self.env.health_dir.mkdir()
        (self.env.health_dir / "moonlight").write_text("6.2.0 1\n")
        self.publish(("moonlight", "6.2.0"))
        self.run_(manual=True)
        self.assertFalse((self.env.health_dir / "moonlight").exists())

    def test_half_made_folders_are_cleared(self):
        self.app_dir().mkdir(parents=True)
        (self.app_dir() / ".6.2.0.new").mkdir()
        (self.app_dir() / "5.0.0").mkdir()
        self.publish(("moonlight", "6.2.0"))
        self.run_(manual=True)
        self.assertEqual(sorted(p.name for p in self.app_dir().iterdir()), ["6.2.0", "current"])

    def test_flatpak_failure_fails_the_run(self):
        self.publish(("moonlight", "6.1.0"))
        self.flatpak_result = {"result": "failed", "message": "FLATPAK UPDATE FAILED"}
        self.assertEqual(self.run_(manual=True), 1)
        self.assertEqual(self.state()["message"], "FLATPAK UPDATE FAILED")

    def test_the_default_flatpak_step_is_noninteractive(self):
        self.env.flatpak = None
        with mock.patch.object(appupdate.shutil, "which", return_value="/usr/bin/flatpak"):
            self.assertEqual(appupdate.flatpak_update(self.env), {"result": "ok", "message": ""})
        self.assertEqual(self.runner.calls[-1], ["flatpak", "update", "--noninteractive", "--system", "-y"])
        with mock.patch.object(appupdate.shutil, "which", return_value=None):
            self.assertEqual(appupdate.flatpak_update(self.env)["result"], "none")

    def test_check_reports_without_installing(self):
        self.publish(("moonlight", "6.2.0"))
        self.assertEqual(self.run_(command="check"), 0)
        self.assertEqual(self.links(), ("", ""))
        self.assertEqual((self.row()["state"], self.row()["available"]), ("available", "6.2.0"))
        self.assertEqual(self.state()["message"], appupdate.MSG_AVAILABLE)
        self.assertEqual(self.flatpak_calls, 0)

    def test_an_unsafe_app_folder_is_refused(self):
        self.env.owner_uid = 54321
        self.publish(("moonlight", "6.2.0"))
        self.assertEqual(self.run_(manual=True), 1)
        self.assertEqual(self.state()["message"], appupdate.MSG_UNSAFE)
        self.assertEqual(self.opener.requests, [])


class WhenTest(Case):
    def test_a_timer_run_waits_while_the_box_is_in_use(self):
        self.publish(("moonlight", "6.2.0"))
        self.busy = "app"
        self.assertEqual(self.run_(), 0)
        self.assertEqual(self.state()["result"], "busy")
        self.assertEqual(self.opener.requests, [])
        self.assertEqual(self.flatpak_calls, 0)

    def test_an_owner_request_also_waits_for_a_stream(self):
        self.publish(("moonlight", "6.2.0"))
        (self.run_dir / appupdate.INSTALL_REQUEST).touch()
        self.busy = "inhibited"
        self.run_()
        self.assertEqual(self.state()["result"], "busy")
        self.assertFalse((self.run_dir / appupdate.INSTALL_REQUEST).exists())

    def test_a_stream_that_starts_mid_run_stops_before_the_next_app(self):
        self.publish(("moonlight", "6.2.0"), ("chiaki-ng", "1.11.0"))
        calls = iter(["", "", "app"])
        self.env.busy = lambda _run: next(calls)
        self.run_()
        self.assertEqual(self.links(), ("6.2.0", ""))
        self.assertEqual(self.links("chiaki-ng"), ("", ""))
        self.assertEqual(self.state()["result"], "busy")

    def test_timer_runs_obey_auto_apps_and_owner_requests_do_not(self):
        self.env.config.write_text("[update]\nauto_apps = false\n")
        self.publish(("moonlight", "6.2.0"))
        self.run_()
        self.assertEqual(self.state()["result"], "disabled")
        self.assertEqual(self.links(), ("", ""))
        (self.run_dir / appupdate.INSTALL_REQUEST).touch()
        self.run_()
        self.assertEqual(self.links(), ("6.2.0", ""))

    def test_a_skipped_run_keeps_what_the_last_check_found(self):
        self.publish(("moonlight", "6.2.0"))
        self.run_(command="check")
        self.busy = "app"
        self.run_()
        self.assertEqual((self.row()["state"], self.row()["available"]), ("available", "6.2.0"))


# ---------------------------------------------------------------- rollback and pruning


class RollbackTest(Case):
    def install(self, *versions):
        for version in versions:
            self.publish(("moonlight", version))
            self.assertEqual(self.run_(manual=True), 0)

    def test_manual_rollback_returns_to_the_previous_version_and_skips_the_bad_one(self):
        self.install("6.2.0", "6.3.0")
        self.assertEqual(self.run_(command="rollback", target="moonlight"), 0)
        self.assertEqual(self.links(), ("6.2.0", ""))
        self.assertEqual(self.versions(), ["6.2.0"])
        self.assertEqual(appupdate.read_skip(self.app_dir()), "6.3.0")
        rolled = self.state()["rolled_back"][-1]
        self.assertEqual((rolled["from"], rolled["to"], rolled["automatic"]), ("6.3.0", "6.2.0", False))
        self.publish(("moonlight", "6.3.0"))
        self.run_(manual=True)
        self.assertEqual(self.links(), ("6.2.0", ""), "a version rolled back from is not installed again")
        self.assertEqual(self.row()["state"], "skipped")
        self.install("6.4.0")
        self.assertEqual(self.links(), ("6.4.0", "6.2.0"))

    def test_rollback_without_a_previous_version_returns_to_the_image_copy(self):
        self.install("6.2.0")
        self.run_(command="rollback", target="moonlight")
        self.assertEqual(self.links(), ("", ""))
        self.assertEqual(self.row()["active"], "6.1.0")
        self.assertEqual(self.state()["rolled_back"][-1]["to"], "")

    def test_rollback_with_nothing_downloaded_fails_plainly(self):
        self.assertEqual(self.run_(command="rollback", target="moonlight"), 1)
        self.assertEqual(self.state()["message"], "NOTHING TO ROLL BACK")

    def test_the_launcher_asks_for_a_rollback_with_a_request_file(self):
        self.install("6.2.0", "6.3.0")
        (self.run_dir / appupdate.ROLLBACK_REQUEST).write_text("moonlight\n")
        fetched = len(self.opener.requests)
        self.assertEqual(self.run_(), 0)
        self.assertEqual(self.links(), ("6.2.0", ""))
        self.assertFalse((self.run_dir / appupdate.ROLLBACK_REQUEST).exists())
        self.assertEqual(len(self.opener.requests), fetched, "a rollback request does not also update")

    def test_two_failed_starts_roll_back_automatically(self):
        self.install("6.2.0", "6.3.0")
        self.env.health_dir.mkdir()
        (self.env.health_dir / "moonlight").write_text("6.3.0 2\n")
        (self.run_dir / appupdate.HEALTH_REQUEST).touch()
        fetched = len(self.opener.requests)
        self.assertEqual(self.run_(), 0)
        self.assertEqual(self.links(), ("6.2.0", ""))
        self.assertEqual(appupdate.read_skip(self.app_dir()), "6.3.0")
        rolled = self.state()["rolled_back"][-1]
        self.assertEqual((rolled["name"], rolled["from"], rolled["automatic"]), ("moonlight", "6.3.0", True))
        self.assertFalse((self.env.health_dir / "moonlight").exists())
        self.assertFalse((self.run_dir / appupdate.HEALTH_REQUEST).exists())
        self.assertEqual(len(self.opener.requests), fetched)

    def test_one_failure_or_a_count_for_another_version_does_not_roll_back(self):
        self.install("6.2.0", "6.3.0")
        self.env.health_dir.mkdir()
        for text in ("6.3.0 1\n", "6.2.0 5\n", "garbage\n"):
            with self.subTest(text=text):
                (self.env.health_dir / "moonlight").write_text(text)
                (self.run_dir / appupdate.HEALTH_REQUEST).touch()
                self.run_()
                self.assertEqual(self.links(), ("6.3.0", "6.2.0"))

    def test_rollback_history_is_short(self):
        status = {}
        for number in range(appupdate.ROLLBACK_HISTORY + 2):
            appupdate._record_rollback(status, "moonlight", f"6.{number}.1", f"6.{number}.0", False, number)
        self.assertEqual(len(status["rolled_back"]), appupdate.ROLLBACK_HISTORY)
        self.assertEqual(status["rolled_back"][-1]["at"], appupdate.ROLLBACK_HISTORY + 1)


class PruneTest(Case):
    def install(self, *versions):
        for version in versions:
            self.publish(("moonlight", version))
            self.assertEqual(self.run_(manual=True), 0)

    def image(self, version):
        lock = self.root / appupdate.IMAGE_LOCK_REL
        lock.write_text(lock.read_text().replace("|6.1.0|", f"|{version}|"))

    def test_copies_the_new_image_supersedes_are_dropped(self):
        self.install("6.2.0", "6.3.0")
        self.image("6.3.0")
        self.assertEqual(self.run_(command="prune"), 0)
        self.assertEqual(self.links(), ("", ""))
        self.assertEqual(self.versions(), [])
        self.assertEqual(self.row()["active"], "6.3.0")

    def test_a_newer_download_survives_an_older_image(self):
        self.install("6.2.0", "6.4.0")
        self.image("6.3.0")
        self.run_(command="prune")
        self.assertEqual(self.links(), ("6.4.0", ""), "the previous one is older than the image now")
        self.assertEqual(self.versions(), ["6.4.0"])

    def test_a_skip_the_image_has_passed_is_forgotten(self):
        self.install("6.2.0")
        self.run_(command="rollback", target="moonlight")
        self.image("6.2.0")
        self.run_(command="prune")
        self.assertFalse((self.app_dir() / "skip").exists())

    def test_every_run_prunes_first(self):
        self.install("6.2.0")
        self.image("6.2.0")
        self.busy = "app"
        self.run_()
        self.assertEqual(self.links(), ("", ""))


class AppImageTest(unittest.TestCase):
    def test_the_squashfs_offset_is_read_from_the_elf_header(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "app.AppImage"
            path.write_bytes(appimage())
            self.assertEqual(appupdate.squashfs_offset(path), 192)
            for data in (b"MZ" + bytes(100), appimage()[:190] + b"xxxxxx"):
                path.write_bytes(data)
                with self.subTest(data=data[:4]), self.assertRaises(appupdate.AppUpdateError):
                    appupdate.squashfs_offset(path)


class DefaultTestRunTest(unittest.TestCase):
    def test_the_new_copy_is_checked_as_the_apps_user_never_as_root(self):
        calls = []

        def runner(argv, **_kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="libc.so.6 => /lib/libc.so.6\n")

        env = appupdate.Env(runner=runner)
        app_dir = pathlib.Path("/var/lib/couchliteos/apps/moonlight/6.2.0")
        self.assertTrue(appupdate._default_test_run(env, app_dir, app_dir / "usr/bin/moonlight", ("--version",)))
        for argv in calls:
            self.assertEqual(argv[:5], ["setpriv", "--reuid=couchliteos", "--regid=couchliteos", "--init-groups",
                                        "--no-new-privs"])
        self.assertEqual(calls[0][-2], "ldd")
        self.assertEqual(calls[1][-1], "--version")

    def test_a_missing_library_fails_the_check(self):
        env = appupdate.Env(runner=lambda argv, **_k: subprocess.CompletedProcess(argv, 0, stdout="libQt6.so => not found"))
        self.assertFalse(appupdate._default_test_run(env, pathlib.Path("/a"), pathlib.Path("/a/b"), ()))

    def test_a_hang_fails_the_check(self):
        def runner(argv, **_kwargs):
            raise subprocess.TimeoutExpired(argv, 60)

        self.assertFalse(appupdate._default_test_run(appupdate.Env(runner=runner), pathlib.Path("/a"),
                                                     pathlib.Path("/a/b"), ()))


class MainTest(Case):
    def test_only_root_may_change_apps(self):
        self.env.euid = lambda: 1000
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(appupdate.main(["run"], self.env), 2)

    def test_status_prints_the_file(self):
        self.env.state.write_text('{"result": "ok"}')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(appupdate.main(["status"], self.env), 0)
        self.assertEqual(json.loads(out.getvalue()), {"result": "ok"})


if __name__ == "__main__":
    unittest.main()
