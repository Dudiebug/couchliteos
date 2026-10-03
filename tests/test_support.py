import errno
import fcntl
import importlib.machinery
import importlib.util
import io
import json
import pathlib
import os
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "launcher"))
import couchliteos_support as support

loader = importlib.machinery.SourceFileLoader(
    "support_exporter", str(ROOT / "scripts" / "couchliteos-support-export")
)
spec = importlib.util.spec_from_loader(loader.name, loader)
exporter = importlib.util.module_from_spec(spec)
loader.exec_module(exporter)


class DestinationTest(unittest.TestCase):
    def test_selects_mounted_and_ordinary_unmounted_removable_partitions(self):
        data = {
            "blockdevices": [
                {
                    "path": "/dev/sdb",
                    "type": "disk",
                    "tran": "usb",
                    "rm": True,
                    "ro": False,
                    "children": [
                        {
                            "path": "/dev/sdb1",
                            "type": "part",
                            "rm": False,
                            "ro": False,
                            "fstype": "vfat",
                            "label": "FILES",
                            "mountpoints": ["/media/files"],
                        },
                        {
                            "path": "/dev/sdb2",
                            "type": "part",
                            "rm": False,
                            "ro": False,
                            "fstype": "ext4",
                            "label": "ARCHIVE",
                            "mountpoints": [None],
                        },
                    ],
                }
            ]
        }
        found = support.destinations_from_lsblk(data, lambda path: path == "/media/files")
        self.assertEqual({item.device for item in found}, {"/dev/sdb1", "/dev/sdb2"})
        self.assertTrue(found[0].mounted)
        unmounted = next(item for item in found if item.device == "/dev/sdb2")
        self.assertFalse(unmounted.mounted)
        self.assertEqual(unmounted.label, support.SUPPORT_MOUNT_LABEL)
        self.assertEqual(unmounted.display_label, "ARCHIVE")
        self.assertIn("WILL MOUNT TEMPORARILY", unmounted.display_name)

    def test_rejects_internal_nvme_and_sata(self):
        data = {
            "blockdevices": [
                {
                    "path": "/dev/nvme0n1",
                    "type": "disk",
                    "tran": "nvme",
                    "rm": False,
                    "children": [
                        {
                            "path": "/dev/nvme0n1p1",
                            "type": "part",
                            "ro": False,
                            "fstype": "ext4",
                            "label": "COUCHLITEOS_SUPPORT",
                            "mountpoints": ["/mnt/internal"],
                        }
                    ],
                },
                {
                    "path": "/dev/sda",
                    "type": "disk",
                    "tran": "sata",
                    "rm": False,
                    "children": [
                        {
                            "path": "/dev/sda1",
                            "type": "part",
                            "ro": False,
                            "fstype": "ext4",
                            "mountpoints": ["/mnt/sata"],
                        }
                    ],
                },
            ]
        }
        self.assertEqual(support.destinations_from_lsblk(data, lambda _path: True), [])

    def test_rejects_read_only_and_iso9660(self):
        data = {
            "blockdevices": [
                {
                    "path": "/dev/sdc",
                    "type": "disk",
                    "tran": "usb",
                    "rm": True,
                    "children": [
                        {
                            "path": "/dev/sdc1",
                            "type": "part",
                            "ro": False,
                            "fstype": "iso9660",
                            "mountpoints": ["/run/live/medium"],
                        },
                        {
                            "path": "/dev/sdc2",
                            "type": "part",
                            "ro": True,
                            "fstype": "ext4",
                            "mountpoints": ["/media/readonly"],
                        },
                    ],
                }
            ]
        }
        self.assertEqual(support.destinations_from_lsblk(data, lambda _path: True), [])

    def test_live_boot_usb_entire_tree_is_rejected(self):
        data = {
            "blockdevices": [
                {
                    "path": "/dev/sdd",
                    "type": "disk",
                    "tran": "usb",
                    "rm": True,
                    "children": [
                        {
                            "path": "/dev/sdd1",
                            "type": "part",
                            "ro": False,
                            "fstype": "iso9660",
                            "label": "COUCHLITEOS",
                            "mountpoints": ["/run/live/medium"],
                        },
                        {
                            "path": "/dev/sdd2",
                            "type": "part",
                            "ro": False,
                            "fstype": "ext4",
                            "label": "persistence",
                            "mountpoints": ["/run/live/persistence/sdd2"],
                        },
                    ],
                }
            ]
        }
        self.assertEqual(support.destinations_from_lsblk(data, lambda _path: True), [])

    def test_whole_disk_usb_filesystem_is_allowed(self):
        data = {
            "blockdevices": [
                {
                    "path": "/dev/sde",
                    "type": "disk",
                    "tran": "usb",
                    "rm": True,
                    "ro": False,
                    "fstype": "exfat",
                    "label": "SUPPORT",
                    "mountpoints": ["/media/support"],
                }
            ]
        }
        found = support.destinations_from_lsblk(data, lambda _path: True)
        self.assertEqual([item.device for item in found], ["/dev/sde"])

    def test_unmounted_whole_disk_usb_filesystem_is_allowed(self):
        data = {
            "blockdevices": [
                {
                    "path": "/dev/sdf",
                    "type": "disk",
                    "tran": "usb",
                    "rm": True,
                    "ro": False,
                    "fstype": "vfat",
                    "label": "DIAGS",
                    "mountpoints": [None],
                }
            ]
        }
        found = support.destinations_from_lsblk(data, lambda _path: False)
        self.assertEqual([item.device for item in found], ["/dev/sdf"])
        self.assertFalse(found[0].mounted)
        self.assertEqual(found[0].display_label, "DIAGS")

    def test_unsafe_label_is_terminal_safe(self):
        self.assertEqual(support.safe_terminal_text("USB\x1b[2J\nBAD"), "USB??2J?BAD")


class ExporterTest(unittest.TestCase):
    def make_archive(self, directory: pathlib.Path, secret: str = "FAKESECRET-123") -> pathlib.Path:
        bundle = directory / "couchliteos-support"
        bundle.mkdir()
        exporter.write_text(
            bundle / "status.txt",
            f"password={secret}\nBearer {secret}\nsdl-freerdp3 /u:alice /p:{secret}\n",
        )
        archive = directory / "fixture.tar.gz"
        with tarfile.open(archive, "w:gz", dereference=False) as container:
            container.add(bundle, arcname="couchliteos-support")
        exporter.verify_archive(archive)
        return archive

    def test_filename_syntax(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            exporter.os.environ,
            {"COUCHLITEOS_SUPPORT_MACHINE_ID": str(pathlib.Path(directory) / "machine-id")},
        ):
            pathlib.Path(directory, "machine-id").write_text("abcdef1234567890\n")
            self.assertRegex(
                exporter.archive_filename(),
                r"^couchliteos-support-\d{8}-\d{6}Z-abcdef12\.tar\.gz$",
            )

    def test_destination_identity_is_revalidated(self):
        selected = support.Destination(
            "/dev/sdb1", "/media/usb", "FILES", "ext4", True, "8:17", "uuid-one"
        )
        swapped = support.Destination(
            "/dev/sdb1", "/media/usb", "FILES", "ext4", True, "8:17", "uuid-two"
        )
        request = {
            "device": selected.device,
            "mountpoint": selected.mountpoint,
            "label": selected.label,
            "fstype": selected.fstype,
            "mounted": True,
            "majmin": selected.majmin,
            "uuid": selected.uuid,
        }
        with mock.patch.object(exporter, "discover_destinations", return_value=[selected]):
            self.assertEqual(exporter.validate_destination(request), selected)
        with mock.patch.object(exporter, "discover_destinations", return_value=[swapped]):
            with self.assertRaises(RuntimeError):
                exporter.validate_destination(request)

    def test_plain_and_structured_secrets_are_redacted(self):
        fake = "FAKESECRET-123456789"
        text = exporter.redact_text(
            f"password={fake}\nAuthorization: Bearer {fake}\n"
            f"url=https://login.tailscale.com/a/{fake}\ntskey-auth-{fake}\n"
            f'{{"api_key": "{fake}"}}\n--auth-key {fake}\n'
        )
        self.assertNotIn(fake, text)
        structured = exporter.redact_json({"AuthKey": fake, "Peer": {"DNSName": "host.ts.net"}})
        self.assertEqual(structured["AuthKey"], "[REDACTED]")
        self.assertEqual(structured["Peer"]["DNSName"], "host.ts.net")

    def test_remote_desktop_passwords_are_redacted(self):
        fake = "FAKE-RDP-PASSWORD-31337"
        text = exporter.redact_text(
            f"sdl-freerdp3 /v:host.example /u:alice /p:{fake} /cert:deny\n"
            f"launch: '/gp:{fake}' /port:3389\n"
            f"xfreerdp /gateway:g:gw.example,u:bob,p:{fake} /v:x\n"
            f"xfreerdp /gateway:p:{fake},g:gw.example\n"
            f'xfreerdp /p:"{fake} two words" -p:{fake} /gateway:u:x,p:"{fake} spaced"\n'
            f'{{"request_id": "{"a" * 24}", "op": "set", "id": "rdp-a", "password": "{fake}"}}\n'
            f"password = {fake}\n"
        )
        self.assertNotIn(fake, text)
        for kept in ("/u:alice", "/cert:deny", "/port:3389", "/v:host.example", "g:gw.example"):
            self.assertIn(kept, text)

    def test_fixture_archive_has_no_secret_leak_or_manifest(self):
        secret = "FAKESECRET-UNIQUE-999"
        with tempfile.TemporaryDirectory() as directory:
            archive = self.make_archive(pathlib.Path(directory), secret)
            with tarfile.open(archive, "r:gz") as container:
                names = container.getnames()
                combined = b"\n".join(
                    container.extractfile(member).read()
                    for member in container.getmembers()
                    if member.isfile()
                )
            self.assertNotIn(secret.encode(), combined)
            self.assertIn(b"[REDACTED]", combined)
            self.assertEqual(names, ["couchliteos-support", "couchliteos-support/status.txt"])

    def test_user_command_uses_one_way_numeric_privilege_drop(self):
        account = types.SimpleNamespace(pw_uid=1000, pw_gid=1001)
        environment = {"HOME": "/run/couchliteos", "WAYLAND_DISPLAY": "wayland-0"}
        with mock.patch.object(exporter.pwd, "getpwnam", return_value=account):
            command = exporter.user_command(["wpctl", "status"], environment)
        self.assertEqual(command[:6], [
            "/usr/bin/setpriv",
            "--reuid=1000",
            "--regid=1001",
            "--init-groups",
            "--",
            "/usr/bin/env",
        ])
        self.assertNotIn("runuser", command)
        self.assertIn("HOME=/run/couchliteos", command)
        self.assertEqual(command[-2:], ["wpctl", "status"])

    def test_archive_rejects_links_traversal_and_special_members(self):
        for kind in ("link", "traversal", "device"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                archive = pathlib.Path(directory) / f"{kind}.tar.gz"
                with tarfile.open(archive, "w:gz") as container:
                    root = tarfile.TarInfo("couchliteos-support")
                    root.type = tarfile.DIRTYPE
                    container.addfile(root)
                    regular = tarfile.TarInfo("couchliteos-support/status.txt")
                    regular.size = 2
                    container.addfile(regular, io.BytesIO(b"ok"))
                    if kind == "link":
                        unsafe = tarfile.TarInfo("couchliteos-support/link")
                        unsafe.type = tarfile.SYMTYPE
                        unsafe.linkname = "/etc/passwd"
                    elif kind == "traversal":
                        unsafe = tarfile.TarInfo("couchliteos-support/../outside")
                        unsafe.size = 1
                    else:
                        unsafe = tarfile.TarInfo("couchliteos-support/device")
                        unsafe.type = tarfile.CHRTYPE
                    container.addfile(unsafe, io.BytesIO(b"x") if unsafe.isreg() else None)
                with self.assertRaises(RuntimeError):
                    exporter.verify_archive(archive)

    def test_archive_rejects_truncated_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = self.make_archive(pathlib.Path(directory))
            data = archive.read_bytes()
            archive.write_bytes(data[: len(data) // 2])
            with self.assertRaises((OSError, EOFError, tarfile.TarError)):
                exporter.verify_archive(archive)

    def test_symlink_and_binary_files_are_not_embedded(self):
        secret = b"FAKESECRET-LINKED"
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "secret"
            source.write_bytes(secret)
            link = root / "link"
            link.symlink_to(source)
            binary = root / "binary"
            binary.write_bytes(b"prefix\0" + secret)
            bundle = root / "bundle"
            exporter.add_file(bundle, "linked.txt", link)
            exporter.add_file(bundle, "binary.txt", binary)
            self.assertNotIn(secret, (bundle / "linked.txt").read_bytes())
            self.assertNotIn(secret, (bundle / "binary.txt").read_bytes())

    def test_atomic_copy_creates_one_archive_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source_dir = root / "source"
            target = root / "target"
            source_dir.mkdir()
            target.mkdir()
            archive = self.make_archive(source_dir)
            final = exporter.atomic_export(archive, target, "support.tar.gz")
            self.assertEqual(sorted(item.name for item in target.iterdir()), ["support.tar.gz"])
            self.assertEqual(final.name, "support.tar.gz")

    def test_failed_copy_cleans_partial_and_final_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source_dir = root / "source"
            target = root / "target"
            source_dir.mkdir()
            target.mkdir()
            archive = self.make_archive(source_dir)

            def fail_copy(incoming, outgoing, _size):
                outgoing.write(incoming.read(32))
                raise OSError("fixture failure")

            with mock.patch.object(exporter.shutil, "copyfileobj", side_effect=fail_copy):
                with self.assertRaises(OSError):
                    exporter.atomic_export(archive, target, "support.tar.gz")
            self.assertEqual(list(target.iterdir()), [])

    def test_success_status_is_written_after_temporary_unmount(self):
        destination = support.Destination(
            "/dev/sdb1", "/run/couchliteos/support-media", "COUCHLITEOS_SUPPORT",
            "vfat", True
        )
        events = []
        with mock.patch.object(
            exporter, "checked_unmount", side_effect=lambda _path: events.append("unmount")
        ), mock.patch.object(
            exporter, "write_status", side_effect=lambda *_args: events.append("success")
        ), mock.patch.object(exporter, "journal_message"):
            mounted = exporter.finish_export(
                "request-id", destination, pathlib.Path(destination.mountpoint) / "support.tar.gz", True
            )
        self.assertFalse(mounted)
        self.assertEqual(events, ["unmount", "success"])

    def test_unmount_failure_never_publishes_success(self):
        destination = support.Destination(
            "/dev/sdb1", "/run/couchliteos/support-media", "COUCHLITEOS_SUPPORT",
            "vfat", True
        )
        with mock.patch.object(
            exporter, "checked_unmount", side_effect=RuntimeError("drive is not safe to remove")
        ), mock.patch.object(exporter, "write_status") as status:
            with self.assertRaisesRegex(RuntimeError, "not safe to remove"):
                exporter.finish_export(
                    "request-id", destination,
                    pathlib.Path(destination.mountpoint) / "support.tar.gz", True
                )
        status.assert_not_called()


class RunDirectoryTrustTest(unittest.TestCase):
    """/run/couchliteos belongs to the unprivileged appliance user, so the
    root exporter must never follow anything it finds there."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run_dir = pathlib.Path(self.tmp.name) / "run"
        self.run_dir.mkdir()
        for name, value in (
            ("RUN", self.run_dir),
            ("LOCK", self.run_dir / "support-export.lock"),
            ("REQUEST", self.run_dir / "support-export.request"),
            ("STATUS", self.run_dir / "support-export.status"),
        ):
            patcher = mock.patch.object(exporter, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def labeled_destination(self):
        return support.Destination(
            "/dev/sdb1", "", "COUCHLITEOS_SUPPORT", "vfat", False, "8:17", "uuid-one"
        )

    def mount_with_recorded_argv(self, returncode=0):
        calls = []

        def fake_run(argv, **_kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, returncode, "", "mount failed")

        with mock.patch.object(exporter.subprocess, "run", side_effect=fake_run), mock.patch.object(
            exporter, "path_is_writable", return_value=True
        ):
            try:
                result = exporter.mount_labeled_destination(self.labeled_destination())
            except RuntimeError:
                result = None
        mounts = [argv for argv in calls if argv[0] == "mount"]
        self.assertEqual(len(mounts), 1)
        self.addCleanup(
            lambda: os.path.isdir(mounts[0][-1])
            and not os.path.islink(mounts[0][-1])
            and os.rmdir(mounts[0][-1])
        )
        return result, mounts[0][-1]

    def test_symlinked_support_media_is_not_a_mount_target(self):
        victim = pathlib.Path(self.tmp.name) / "usr-libexec"
        victim.mkdir()
        (self.run_dir / "support-media").symlink_to(victim)
        result, target = self.mount_with_recorded_argv()
        self.assertIsNotNone(result)
        self.assertNotEqual(os.path.realpath(target), str(victim))
        self.assertFalse(target.startswith(str(self.run_dir)), target)
        self.assertFalse(os.path.islink(target))
        self.assertEqual(result[0].mountpoint, target)

    def test_private_mountpoint_is_removed_after_unmount(self):
        (mounted, created), target = self.mount_with_recorded_argv()
        self.assertTrue(created)
        self.assertTrue(os.path.isdir(target))
        with mock.patch.object(exporter, "checked_unmount"), mock.patch.object(
            exporter, "write_status"
        ), mock.patch.object(exporter, "journal_message"):
            exporter.finish_export("request-id", mounted, pathlib.Path(target) / "x.tar.gz", True)
        self.assertFalse(os.path.exists(target))

    def test_failed_mount_removes_the_private_mountpoint(self):
        result, target = self.mount_with_recorded_argv(returncode=32)
        self.assertIsNone(result)
        self.assertFalse(os.path.exists(target))

    def collect_into(self, bundle: pathlib.Path) -> None:
        with mock.patch.object(exporter, "command_output", return_value=""), mock.patch.dict(
            exporter.os.environ,
            {
                "COUCHLITEOS_SUPPORT_LOG_DIR": str(self.run_dir / "no-logs"),
                "COUCHLITEOS_SUPPORT_CONFIG": str(self.run_dir / "no-config"),
            },
        ):
            exporter.collect(bundle)

    def bundle_text(self, bundle: pathlib.Path) -> str:
        return "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in sorted(bundle.rglob("*"))
            if path.is_file()
        )

    def test_roots_update_and_browser_logs_are_collected_next_to_the_launchers(self):
        logs = self.run_dir / "logs"
        logs.mkdir()
        (logs / "launcher.log").write_text("launcher line\n")
        update_logs = self.run_dir / "logs-update"  # /var/log/couchliteos-update beside /var/log/couchliteos
        update_logs.mkdir()
        (update_logs / "update.log").write_text("update to 0.3.0 done\n")
        (update_logs / "browser.log").write_text("apt-get install firefox-esr\n")
        bundle = pathlib.Path(self.tmp.name) / "bundle"
        with mock.patch.object(exporter, "command_output", return_value=""), mock.patch.dict(
            exporter.os.environ,
            {"COUCHLITEOS_SUPPORT_LOG_DIR": str(logs), "COUCHLITEOS_SUPPORT_CONFIG": str(self.run_dir / "no-config")},
        ):
            exporter.collect(bundle)
        index = (bundle / "logs/couchliteos-update/INDEX.txt").read_text()
        self.assertEqual(index, "log-001.txt = browser.log\nlog-002.txt = update.log\n")
        self.assertIn("update to 0.3.0 done", (bundle / "logs/couchliteos-update/log-002.txt").read_text())
        self.assertIn("launcher line", (bundle / "logs/couchliteos/log-001.txt").read_text())

    def test_symlinked_status_file_is_not_collected(self):
        secret = "SHADOWLINE-ROOT-HASH-31337"
        target = pathlib.Path(self.tmp.name) / "shadow"
        target.write_text(secret + "\n")
        (self.run_dir / "x-ready").write_text("")
        (self.run_dir / "x-status").symlink_to(target)
        (self.run_dir / "y-ready").write_text("")
        (self.run_dir / "y-status").write_text("Streaming Y\n")
        bundle = pathlib.Path(self.tmp.name) / "bundle"
        self.collect_into(bundle)
        text = self.bundle_text(bundle)
        self.assertNotIn(secret, text)
        self.assertIn("y: Streaming Y", text)

    def test_the_interface_file_is_in_the_bundle(self):
        (self.run_dir / "interface").write_text("classic\nfallback after exit 3, then exit 3 with cairo\n")
        bundle = pathlib.Path(self.tmp.name) / "bundle"
        self.collect_into(bundle)
        self.assertEqual((bundle / "session/interface.txt").read_text(),
                         "classic\nfallback after exit 3, then exit 3 with cairo\n")

    def test_status_fifo_does_not_block_the_exporter(self):
        (self.run_dir / "x-ready").write_text("")
        os.mkfifo(self.run_dir / "x-status")
        os.mkfifo(self.run_dir / "firefox-drm-status")

        def timed_out(_signum, _frame):
            raise AssertionError("exporter blocked opening a FIFO")

        previous = signal.signal(signal.SIGALRM, timed_out)
        signal.alarm(10)
        try:
            self.collect_into(pathlib.Path(self.tmp.name) / "bundle")
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)

    def test_status_permissions_are_set_on_the_descriptor_not_a_swappable_path(self):
        with mock.patch.object(exporter.os, "chmod", side_effect=AssertionError("chmod by path")):
            exporter.write_status("a" * 24, "working", "Collecting")
        self.assertEqual(os.stat(exporter.STATUS).st_mode & 0o777, 0o644)

    def test_symlinked_lock_is_refused(self):
        victim = pathlib.Path(self.tmp.name) / "victim"
        exporter.LOCK.symlink_to(victim)
        with mock.patch.object(exporter, "read_request") as read_request, mock.patch.object(
            exporter, "write_status"
        ), mock.patch.object(exporter, "journal_message"):
            code = exporter.run()
        self.assertNotEqual(code, 0)
        self.assertFalse(victim.exists(), "root created a file through the symlinked lock")
        read_request.assert_not_called()

    def test_regular_lock_is_created_private_and_excludes_a_second_run(self):
        with mock.patch.object(exporter, "read_request", side_effect=RuntimeError("stop")), mock.patch.object(
            exporter, "write_status"
        ), mock.patch.object(exporter, "journal_message"):
            self.assertEqual(exporter.run(), 1)
            self.assertEqual(os.stat(exporter.LOCK).st_mode & 0o777, 0o600)
            held = exporter.open_lock(exporter.LOCK)
            fcntl.flock(held, fcntl.LOCK_EX)
            try:
                self.assertEqual(exporter.run(), 75)
            finally:
                os.close(held)



class StatusMessageTest(unittest.TestCase):
    """What the launcher shows when the export cannot run or fails."""

    def test_terminal_text_keeps_commas_and_semicolons_but_no_controls(self):
        self.assertEqual(support.safe_terminal_text("FULL, TRY AGAIN; OK"), "FULL, TRY AGAIN; OK")
        cleaned = support.safe_terminal_text("A,B;C\x1b[2J\n\x00\x7f")
        self.assertTrue(cleaned.startswith("A,B;C"))
        self.assertIsNone(re.search(r"[\x00-\x1f\x7f]", cleaned), repr(cleaned))

    def test_status_round_trip_keeps_punctuation(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = pathlib.Path(directory)
            with mock.patch.object(exporter, "RUN", run_dir), mock.patch.object(
                exporter, "STATUS", run_dir / "status"
            ), mock.patch.object(support, "STATUS", run_dir / "status"):
                exporter.write_status("a" * 24, "failed", "USB DRIVE IS FULL, FREE SPACE; TRY AGAIN")
                state = support.read_status("a" * 24)
        self.assertEqual(state["message"], "USB DRIVE IS FULL, FREE SPACE; TRY AGAIN")

    def test_os_errors_map_to_short_plain_messages(self):
        expected = {
            errno.ENOSPC: "USB DRIVE IS FULL",
            errno.EDQUOT: "USB DRIVE IS FULL",
            errno.EROFS: "USB DRIVE IS READ-ONLY",
            errno.EIO: "USB DRIVE ERROR: TRY ANOTHER DRIVE",
            errno.EACCES: "USB DRIVE DOES NOT ALLOW WRITING: TRY ANOTHER DRIVE",
        }
        for number, text in expected.items():
            with self.subTest(errno.errorcode[number]):
                error = OSError(number, os.strerror(number), "/tmp/support-media-x/file")
                self.assertEqual(exporter.failure_message(error, drive=True), text)

    def test_space_error_outside_the_drive_does_not_blame_the_drive(self):
        message = exporter.failure_message(OSError(errno.ENOSPC, "No space left"), drive=False)
        self.assertNotIn("USB", message)
        self.assertIn("SPACE", message)

    def test_other_errors_are_redacted_and_terminal_safe(self):
        message = exporter.failure_message(RuntimeError("password=hunter2 \x1b[31mfailed"), drive=True)
        self.assertNotIn("hunter2", message)
        self.assertNotIn("\x1b", message)

    def run_with_failing_export(self, error):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = pathlib.Path(directory) / "run"
            run_dir.mkdir()
            target = pathlib.Path(directory) / "usb"
            target.mkdir()
            destination = support.Destination("/dev/sdb1", str(target), "USB", "vfat", True)
            statuses = []
            with mock.patch.multiple(
                exporter,
                RUN=run_dir,
                LOCK=run_dir / "lock",
                REQUEST=run_dir / "request",
                STATUS=run_dir / "status",
                read_request=mock.Mock(return_value={"request_id": "b" * 24}),
                settle_devices=mock.Mock(),
                validate_destination=mock.Mock(return_value=destination),
                free_bytes=mock.Mock(return_value=1 << 40),
                collect=mock.Mock(),
                verify_archive=mock.Mock(),
                path_is_writable=mock.Mock(return_value=True),
                atomic_export=mock.Mock(side_effect=error),
                journal_message=mock.Mock(),
                write_status=mock.Mock(side_effect=lambda *args: statuses.append(args)),
            ), mock.patch.object(exporter.os.path, "ismount", return_value=True):
                code = exporter.run()
        self.assertEqual(code, 1)
        return statuses[-1]

    def test_full_drive_is_reported_plainly_by_the_exporter(self):
        status = self.run_with_failing_export(OSError(errno.ENOSPC, "No space left on device"))
        self.assertEqual(status[1], "failed")
        self.assertEqual(status[2], "USB DRIVE IS FULL")

    def test_read_only_drive_is_reported_plainly_by_the_exporter(self):
        status = self.run_with_failing_export(OSError(errno.EROFS, "Read-only file system"))
        self.assertEqual(status[2], "USB DRIVE IS READ-ONLY")

    def test_read_only_mount_is_reported_plainly(self):
        destination = support.Destination(
            "/dev/sdb1", "", "COUCHLITEOS_SUPPORT", "vfat", False, "8:17", "uuid-one"
        )
        with mock.patch.object(
            exporter.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ), mock.patch.object(exporter, "path_is_writable", return_value=False):
            with self.assertRaises(RuntimeError) as caught:
                exporter.mount_labeled_destination(destination)
        self.assertEqual(str(caught.exception), "USB DRIVE IS READ-ONLY")


class NoDestinationReasonTest(unittest.TestCase):
    def boot_usb(self):
        return {
            "path": "/dev/sdd",
            "type": "disk",
            "tran": "usb",
            "rm": True,
            "children": [
                {
                    "path": "/dev/sdd1",
                    "type": "part",
                    "fstype": "iso9660",
                    "mountpoints": ["/run/live/medium"],
                },
                {
                    "path": "/dev/sdd2",
                    "type": "part",
                    "fstype": "ext4",
                    "label": "persistence",
                    "mountpoints": ["/run/live/persistence/sdd2"],
                },
            ],
        }

    def internal_disk(self):
        return {
            "path": "/dev/nvme0n1",
            "type": "disk",
            "tran": "nvme",
            "rm": False,
            "children": [{"path": "/dev/nvme0n1p1", "type": "part", "fstype": "ext4"}],
        }

    def test_only_the_boot_drive_gets_a_specific_message(self):
        data = {"blockdevices": [self.internal_disk(), self.boot_usb()]}
        self.assertEqual(support.destinations_from_lsblk(data, lambda _path: True), [])
        self.assertEqual(support.no_destination_reason(data), support.REASON_BOOT_MEDIUM_ONLY)
        self.assertEqual(
            support.NO_DESTINATION_TEXT[support.REASON_BOOT_MEDIUM_ONLY],
            "INSERT A SECOND USB DRIVE (THE BOOT DRIVE CANNOT BE USED)",
        )

    def test_a_second_drive_that_is_unusable_keeps_the_generic_message(self):
        second = {
            "path": "/dev/sde",
            "type": "disk",
            "tran": "usb",
            "rm": True,
            "children": [
                {"path": "/dev/sde1", "type": "part", "ro": True, "fstype": "vfat"}
            ],
        }
        data = {"blockdevices": [self.boot_usb(), second]}
        self.assertEqual(support.no_destination_reason(data), support.REASON_NO_DRIVE)

    def test_no_boot_drive_and_no_usb_keeps_the_generic_message(self):
        data = {"blockdevices": [self.internal_disk()]}
        self.assertEqual(support.no_destination_reason(data), support.REASON_NO_DRIVE)

    def test_message_helper_uses_the_same_lsblk_data(self):
        data = {"blockdevices": [self.boot_usb()]}
        completed = subprocess.CompletedProcess([], 0, json.dumps(data), "")
        with mock.patch.object(support.subprocess, "run", return_value=completed):
            self.assertEqual(
                support.no_destination_message(),
                "INSERT A SECOND USB DRIVE (THE BOOT DRIVE CANNOT BE USED)",
            )
        failed = subprocess.CompletedProcess([], 1, "", "")
        with mock.patch.object(support.subprocess, "run", return_value=failed):
            self.assertEqual(
                support.no_destination_message(),
                "CONNECT A WRITABLE REMOVABLE USB DRIVE AND TRY AGAIN",
            )



class LargeLogTest(unittest.TestCase):
    """An oversized log is the one most worth having; keep its end, not nothing."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.directory = pathlib.Path(self._directory.name)
        patcher = mock.patch.object(exporter, "MAX_LOG_SIZE", 1024)
        patcher.start()
        self.addCleanup(patcher.stop)

    def bundle_file(self, source: pathlib.Path) -> str:
        bundle = self.directory / "bundle"
        exporter.add_file(bundle, "logs/x.txt", source)
        return (bundle / "logs/x.txt").read_text(encoding="utf-8")

    def test_oversized_log_includes_the_last_bytes_marked_truncated(self):
        source = self.directory / "big.log"
        source.write_text("".join(f"line {number:05d}\n" for number in range(500)), encoding="ascii")
        text = self.bundle_file(source)
        first, _, rest = text.partition("\n")
        self.assertIn("truncated", first.lower())
        self.assertIn("last", first.lower())
        self.assertNotIn("File exceeded", text)
        self.assertTrue(rest.endswith("line 00499\n"), rest[-40:])
        self.assertNotIn("line 00000", text)
        self.assertLessEqual(len(rest.encode("utf-8")), 1024)
        # the partial line the cut landed in is dropped, so every line is whole
        for line in rest.splitlines():
            self.assertRegex(line, r"^line \d{5}$")

    def test_log_within_the_limit_is_unchanged(self):
        source = self.directory / "small.log"
        source.write_text("one\ntwo\n", encoding="ascii")
        self.assertEqual(self.bundle_file(source), "one\ntwo\n")

    def test_oversized_binary_file_is_still_omitted(self):
        source = self.directory / "big.bin"
        source.write_bytes(b"\0" * 4096)
        self.assertEqual(self.bundle_file(source), "Binary file omitted.\n")

    def test_oversized_log_is_still_redacted(self):
        source = self.directory / "secret.log"
        source.write_text("x" * 900 + "\n" * 2 + "password=hunter2-SECRET\n" * 20, encoding="ascii")
        text = self.bundle_file(source)
        self.assertNotIn("hunter2-SECRET", text)

    def test_symlinked_oversized_log_is_not_followed(self):
        target = self.directory / "target"
        target.write_text("ROOT-ONLY\n" * 500, encoding="ascii")
        link = self.directory / "link.log"
        link.symlink_to(target)
        self.assertNotIn("ROOT-ONLY", self.bundle_file(link))



class NtfsMountTest(unittest.TestCase):
    """NTFS sticks used to fail as "mounted read-only"; ask the kernel driver for NTFS."""

    NTFS_MESSAGE = "THIS DRIVE IS NTFS; USE A FAT32 OR EXFAT DRIVE"

    def destination(self, fstype):
        return support.Destination(
            "/dev/sdb1", "", "COUCHLITEOS_SUPPORT", fstype, False, "8:17", "uuid-one", "WINDOWS"
        )

    def mount(self, fstype, *, mount_code=0, writable=True):
        calls = []

        def fake_run(argv, **_kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, mount_code if argv[0] == "mount" else 0, "", "bad")

        error = None
        result = None
        with mock.patch.object(exporter.subprocess, "run", side_effect=fake_run), mock.patch.object(
            exporter, "path_is_writable", return_value=writable
        ):
            try:
                result = exporter.mount_labeled_destination(self.destination(fstype))
            except RuntimeError as caught:
                error = caught
        mounts = [argv for argv in calls if argv[0] == "mount"]
        self.assertEqual(len(mounts), 1)
        self.addCleanup(lambda: os.path.isdir(mounts[0][-1]) and os.rmdir(mounts[0][-1]))
        return mounts[0], calls, result, error

    def test_ntfs_is_mounted_with_the_ntfs3_driver_and_the_safe_options(self):
        argv, _calls, result, error = self.mount("ntfs")
        self.assertIsNone(error)
        self.assertIsNotNone(result)
        self.assertEqual(argv[:5], ["mount", "-t", "ntfs3", "-o", "nosuid,nodev,noexec"])
        self.assertEqual(argv[5:7], ["--", "/dev/sdb1"])

    def test_other_filesystems_are_still_mounted_without_forcing_a_type(self):
        for fstype in ("vfat", "exfat", "ext4"):
            with self.subTest(fstype):
                argv, _calls, result, error = self.mount(fstype)
                self.assertIsNone(error)
                self.assertNotIn("-t", argv)
                self.assertEqual(argv[:3], ["mount", "-o", "nosuid,nodev,noexec"])

    def test_ntfs_mount_failure_names_ntfs_and_the_fix(self):
        _argv, _calls, result, error = self.mount("ntfs", mount_code=32)
        self.assertIsNone(result)
        self.assertEqual(str(error), self.NTFS_MESSAGE)

    def test_ntfs_that_only_mounts_read_only_gets_the_same_message(self):
        _argv, calls, result, error = self.mount("ntfs", writable=False)
        self.assertIsNone(result)
        self.assertEqual(str(error), self.NTFS_MESSAGE)
        self.assertIn("umount", [argv[0] for argv in calls])

    def test_ntfs_failure_message_is_shown_as_written(self):
        self.assertEqual(support.safe_terminal_text(self.NTFS_MESSAGE, 240), self.NTFS_MESSAGE)

    def test_failed_ntfs_mount_removes_the_private_mountpoint(self):
        argv, _calls, _result, _error = self.mount("ntfs", mount_code=32)
        self.assertFalse(os.path.exists(argv[-1]))

    def test_ntfs_type_is_case_insensitive(self):
        argv, _calls, _result, _error = self.mount("NTFS")
        self.assertEqual(argv[1:3], ["-t", "ntfs3"])


if __name__ == "__main__":
    unittest.main()
