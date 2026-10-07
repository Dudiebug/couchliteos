"""The root-side updater: pure logic on temp trees, orchestration with a recording runner."""

import testenv  # noqa: F401  (first: scratch run and state directories)
import contextlib
import hashlib
import io
import json
import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

import couchliteos_update as update
import couchliteos_updater as updater

GIB = 1 << 30


def put(root, rel, text="", mode=None):
    path = pathlib.Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)
    return path


def link(root, rel, target):
    path = pathlib.Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)
    return path


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)


class Runner:
    """Records every argv; `script(argv)` may return (returncode, output) to override the default (0, "")."""

    def __init__(self, script=None):
        self.calls = []
        self.script = script

    def __call__(self, argv, **_kwargs):
        argv = [str(part) for part in argv]
        self.calls.append(argv)
        reply = self.script(argv) if self.script else None
        if reply is None:
            reply = (0, "/\n" if argv[0] == "findmnt" else "")
        return subprocess.CompletedProcess(argv, reply[0], stdout=reply[1])

    def commands(self, name):
        return [call for call in self.calls if call[0] == name]


# ---------------------------------------------------------------- status


class StatusTest(TmpCase):
    def test_writes_the_documented_json_atomically_and_world_readable(self):
        path = self.tmp / "run" / "update-status.json"
        status = updater.Status(path, version="0.2.2")
        status.set("downloading", "DOWNLOADING: 5 OF 10 MB", 50)
        self.assertEqual(json.loads(path.read_text()), {
            "phase": "downloading", "percent": 50, "message": "DOWNLOADING: 5 OF 10 MB", "version": "0.2.2",
        })
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        self.assertEqual([p.name for p in path.parent.iterdir()], ["update-status.json"], "no temp file left")

    def test_percent_is_null_when_unknown_and_version_can_arrive_later(self):
        path = self.tmp / "s.json"
        status = updater.Status(path)
        status.set("checking", "CHECKING...")
        self.assertIsNone(json.loads(path.read_text())["percent"])
        self.assertEqual(json.loads(path.read_text())["version"], "")
        status.version = "0.2.2"
        status.set("installing", "X", 3)
        self.assertEqual(json.loads(path.read_text())["version"], "0.2.2")

    def test_only_the_documented_phases_are_accepted(self):
        status = updater.Status(self.tmp / "s.json")
        for phase in ("checking", "downloading", "verifying", "installing", "restarting", "failed",
                      "cancelled", "uptodate"):
            status.set(phase, "X")
        with self.assertRaises(ValueError):
            status.set("exploding", "X")

    def test_an_unwritable_status_file_never_stops_the_update(self):
        (self.tmp / "dir").write_text("a file where the directory should be")
        updater.Status(self.tmp / "dir" / "s.json").set("checking", "X")  # must not raise
        self.assertEqual((self.tmp / "dir").read_text(), "a file where the directory should be")
        self.assertEqual(sorted(path.name for path in self.tmp.iterdir()), ["dir"], "no temporary file left")

    def test_percent_is_clamped(self):
        path = self.tmp / "s.json"
        updater.Status(path).set("downloading", "X", 140)
        self.assertEqual(json.loads(path.read_text())["percent"], 100)


# ---------------------------------------------------------------- dpkg status

STATUS_TEXT = """Package: grub-efi-amd64
Status: install ok installed
Architecture: amd64
Description: GRand Unified Bootloader
 This is a long
 .
 description.

Package: oldpkg
Status: deinstall ok config-files
Architecture: all

Package: libfoo
Status: install ok installed
Architecture: amd64
Multi-Arch: same

Package: libfoo
Status: install ok installed
Architecture: i386
Multi-Arch: same

Package: halfway
Status: install ok unpacked
Architecture: all
"""


class DpkgStatusTest(TmpCase):
    def test_only_fully_installed_packages_are_keyed_by_name_and_architecture(self):
        packages = updater.parse_dpkg_status(STATUS_TEXT)
        self.assertEqual(list(packages), [("grub-efi-amd64", "amd64"), ("libfoo", "amd64"), ("libfoo", "i386")])

    def test_stanza_text_is_kept_verbatim_without_trailing_blank_lines(self):
        stanza = updater.parse_dpkg_status(STATUS_TEXT)[("grub-efi-amd64", "amd64")]
        self.assertTrue(stanza.startswith("Package: grub-efi-amd64\n"))
        self.assertIn("\n .\n description.", stanza)
        self.assertFalse(stanza.endswith("\n"))

    def test_extras_are_what_the_box_has_and_the_image_lacks(self):
        box = updater.parse_dpkg_status(STATUS_TEXT)
        image = updater.parse_dpkg_status(
            "Package: libfoo\nStatus: install ok installed\nArchitecture: amd64\n"
        )
        extras = updater.extra_packages(box, image)
        self.assertEqual(list(extras), [("grub-efi-amd64", "amd64"), ("libfoo", "i386")])

    def test_package_present_with_a_different_architecture_is_still_an_extra(self):
        box = updater.parse_dpkg_status("Package: x\nStatus: install ok installed\nArchitecture: i386\n")
        image = updater.parse_dpkg_status("Package: x\nStatus: install ok installed\nArchitecture: amd64\n")
        self.assertEqual(list(updater.extra_packages(box, image)), [("x", "i386")])

    def test_info_files_use_the_stem_dot_rule_so_python3_does_not_claim_python313(self):
        info = self.tmp / "info"
        for name in ("python3.list", "python3.md5sums", "python3.13.list", "python3.13.md5sums",
                     "python3-minimal.list", "libfoo:amd64.list", "libfoo:amd64.md5sums", "libfoo:i386.list"):
            put(info, name)
        self.assertEqual(updater.info_files(info, "python3", "amd64"), ["python3.list", "python3.md5sums"])
        self.assertEqual(updater.info_files(info, "python3.13", "amd64"), ["python3.13.list", "python3.13.md5sums"])
        self.assertEqual(updater.info_files(info, "libfoo", "amd64"), ["libfoo:amd64.list", "libfoo:amd64.md5sums"])
        self.assertEqual(updater.info_files(self.tmp / "missing", "x", "all"), [])


# ---------------------------------------------------------------- protect filter


class ProtectTest(TmpCase):
    def setUp(self):
        super().setUp()
        self.target = self.tmp / "target"
        self.image = self.tmp / "image"
        info = self.target / "var/lib/dpkg/info"
        put(info, "grub-efi-amd64.list", "\n".join([
            "/.", "/usr", "/usr/lib", "/usr/lib/grub", "/usr/lib/grub/x86_64-efi",
            "/usr/lib/grub/x86_64-efi/acpi.mod", "/usr/share", "/usr/share/doc",
            "/usr/share/doc/grub-efi-amd64", "/usr/share/doc/grub-efi-amd64/copyright", "/lib",
        ]) + "\n")
        put(info, "grub-efi-amd64.md5sums", "x")
        put(info, "grub-efi-amd64.postinst", "x")
        put(info, "unrelated.list", "/usr/bin/unrelated\n")
        put(self.target, "usr/lib/grub/x86_64-efi/acpi.mod", "x")
        put(self.target, "usr/share/doc/grub-efi-amd64/copyright", "x")
        link(self.target, "lib", "usr/lib")
        (self.image / "usr/lib").mkdir(parents=True)
        (self.image / "usr/share/doc").mkdir(parents=True)
        self.extras = {("grub-efi-amd64", "amd64"): "Package: grub-efi-amd64"}

    def protected(self):
        return updater.protected_paths(self.target, self.image, self.extras)

    def test_files_info_files_and_parents_missing_from_the_image_are_protected(self):
        self.assertEqual(self.protected(), sorted([
            "/lib",  # a symlink to a directory is not a directory entry
            "/usr/lib/grub", "/usr/lib/grub/x86_64-efi", "/usr/lib/grub/x86_64-efi/acpi.mod",
            "/usr/share/doc/grub-efi-amd64", "/usr/share/doc/grub-efi-amd64/copyright",
            "/var/lib/dpkg/info/grub-efi-amd64.list", "/var/lib/dpkg/info/grub-efi-amd64.md5sums",
            "/var/lib/dpkg/info/grub-efi-amd64.postinst",
        ]))

    def test_directories_the_image_has_and_other_packages_are_not_protected(self):
        protected = self.protected()
        for path in ("/", "/usr", "/usr/lib", "/usr/share", "/usr/share/doc", "/usr/bin/unrelated"):
            self.assertNotIn(path, protected)

    def test_no_extras_means_nothing_is_protected(self):
        self.assertEqual(updater.protected_paths(self.target, self.image, {}), [])

    def test_a_package_without_a_list_file_protects_only_what_exists(self):
        extras = {("ghost", "all"): "Package: ghost"}
        self.assertEqual(updater.protected_paths(self.target, self.image, extras), [])

    def test_arch_qualified_list_files_are_found(self):
        info = self.target / "var/lib/dpkg/info"
        put(info, "libfoo:amd64.list", "/usr/lib/x86_64-linux-gnu/libfoo.so.1\n")
        extras = {("libfoo", "amd64"): "Package: libfoo"}
        self.assertEqual(updater.protected_paths(self.target, self.image, extras), [
            "/usr/lib/x86_64-linux-gnu",  # the image has /usr/lib but not this multiarch directory
            "/usr/lib/x86_64-linux-gnu/libfoo.so.1",
            "/var/lib/dpkg/info/libfoo:amd64.list",
        ])


class FilterRulesTest(unittest.TestCase):
    def test_plain_paths_become_anchored_protect_rules(self):
        self.assertEqual(updater.filter_rules(["/usr/lib/grub", "/boot/x"]), ["P /usr/lib/grub", "P /boot/x"])

    def test_wildcard_characters_are_escaped_and_then_so_is_the_backslash(self):
        self.assertEqual(updater.filter_rules(["/usr/share/foo[1]/a*b?.txt"]), [r"P /usr/share/foo\[1]/a\*b\?.txt"])
        self.assertEqual(updater.filter_rules([r"/a\b*"]), [r"P /a\\b\*"])

    def test_a_backslash_without_wildcards_stays_literal(self):
        # rsync only honours escapes in patterns that contain a wildcard.
        self.assertEqual(updater.filter_rules([r"/a\b"]), [r"P /a\b"])

    def test_paths_that_cannot_be_written_on_one_line_are_dropped(self):
        self.assertEqual(updater.filter_rules(["/a\nb", "/ok", "/c\rd"]), ["P /ok"])

    def test_a_transfer_root_keeps_only_its_own_paths_made_relative(self):
        paths = ["/usr/lib/grub", "/var/lib/dpkg/info/x.list", "/var/lib/dpkg/info/y:amd64.list", "/var/lib/other"]
        self.assertEqual(
            updater.filter_rules(paths, "/var/lib/dpkg"),
            ["P /info/x.list", "P /info/y:amd64.list"],
        )

    def test_the_root_itself_is_never_a_rule(self):
        self.assertEqual(updater.filter_rules(["/", "/var/lib/dpkg"], "/var/lib/dpkg"), [])


# ---------------------------------------------------------------- accounts

IMAGE_PASSWD = """root:x:0:0:root:/root:/bin/bash
daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin
sshd:x:106:65534::/run/sshd:/usr/sbin/nologin
couchliteos:x:1000:1000:CouchLiteOS:/var/lib/couchliteos/home:/bin/bash
"""
TARGET_PASSWD = """root:x:0:0:root:/root:/bin/bash
daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin
sshd:x:105:65534::/run/sshd:/usr/sbin/nologin
couchliteos:x:1000:1000:CouchLiteOS:/var/lib/couchliteos/home:/bin/bash
alice:x:1001:1001:Alice:/home/alice:/bin/bash
mallory:x:1000:1002::/home/mallory:/bin/sh
"""
IMAGE_GROUP = """root:x:0:
wheel:x:27:
video:x:44:couchliteos
shadow:x:42:
netdev:x:109:
couchliteos:x:1000:
"""
TARGET_GROUP = """root:x:0:
wheel:x:27:alice
video:x:44:alice,ghost
shadow:x:42:
netdev:x:107:
couchliteos:x:1000:
alice:x:1001:
docker:x:1000:alice
staff:x:50:
"""
IMAGE_SHADOW = """root:*:19000:0:99999:7:::
daemon:*:19000:0:99999:7:::
sshd:*:19000:0:99999:7:::
couchliteos:!:19000:0:99999:7:::
"""
TARGET_SHADOW = """root:$6$rootsalt$roothash:20000:0:99999:7:::
daemon:*:18000:0:99999:7:::
couchliteos:!:20000:0:99999:7:::
alice:$6$alicesalt$alicehash:20001:0:99999:7:::
"""
IMAGE_GSHADOW = """root:*::
wheel:*::
video:*::couchliteos
shadow:*::
netdev:*::
couchliteos:!::
"""
TARGET_GSHADOW = """root:*::
wheel:!::alice
video:*::alice,ghost
alice:!::
staff:!::
"""


def merge(**overrides):
    image = {"passwd": IMAGE_PASSWD, "group": IMAGE_GROUP, "shadow": IMAGE_SHADOW, "gshadow": IMAGE_GSHADOW}
    target = {"passwd": TARGET_PASSWD, "group": TARGET_GROUP, "shadow": TARGET_SHADOW, "gshadow": TARGET_GSHADOW}
    target.update(overrides)
    return updater.merge_accounts(image, target)


class AccountsTest(unittest.TestCase):
    def test_passwd_is_the_image_plus_target_only_users_without_id_collisions(self):
        accounts = merge()
        self.assertEqual(accounts.files["passwd"].splitlines(), IMAGE_PASSWD.splitlines() + [
            "alice:x:1001:1001:Alice:/home/alice:/bin/bash",
        ])
        self.assertEqual(accounts.skipped, ["user mallory (uid 1000 is taken)", "group docker (gid 1000 is taken)"])

    def test_image_entries_win_for_names_in_both(self):
        accounts = merge(passwd=TARGET_PASSWD.replace(
            "couchliteos:x:1000:1000:CouchLiteOS", "couchliteos:x:1000:1000:Old Name"))
        self.assertIn("couchliteos:x:1000:1000:CouchLiteOS:/var/lib/couchliteos/home:/bin/bash",
                      accounts.files["passwd"].splitlines())
        self.assertNotIn("Old Name", accounts.files["passwd"])

    def test_uid_map_lists_names_in_both_whose_ids_changed(self):
        accounts = merge()
        self.assertEqual(accounts.uid_map, {105: 106})
        self.assertEqual(accounts.gid_map, {107: 109})

    def test_shadow_keeps_the_boxs_password_lines_and_uses_the_image_line_otherwise(self):
        lines = merge().files["shadow"].splitlines()
        self.assertEqual(lines[0], "root:$6$rootsalt$roothash:20000:0:99999:7:::")
        self.assertIn("alice:$6$alicesalt$alicehash:20001:0:99999:7:::", lines)
        self.assertIn("sshd:*:19000:0:99999:7:::", lines)  # only the image has a line for it
        self.assertEqual([line.split(":")[0] for line in lines], ["root", "daemon", "sshd", "couchliteos", "alice"])

    def test_a_user_without_any_shadow_line_is_locked_not_dropped(self):
        accounts = merge(shadow="")
        lines = {line.split(":")[0]: line for line in accounts.files["shadow"].splitlines()}
        self.assertEqual(lines["alice"].split(":")[1], "!")

    def test_group_members_are_unioned_and_filtered_to_known_users(self):
        groups = {line.split(":")[0]: line for line in merge().files["group"].splitlines()}
        self.assertEqual(groups["wheel"], "wheel:x:27:alice")
        self.assertEqual(groups["video"], "video:x:44:couchliteos,alice")  # ghost is not a user any more
        self.assertEqual(groups["alice"], "alice:x:1001:")
        self.assertEqual(groups["staff"], "staff:x:50:")
        self.assertNotIn("docker", groups)

    def test_gshadow_follows_the_merged_member_lists(self):
        lines = {line.split(":")[0]: line for line in merge().files["gshadow"].splitlines()}
        self.assertEqual(lines["wheel"], "wheel:!::alice")  # the boxs line, members merged
        self.assertEqual(lines["video"], "video:*::couchliteos,alice")
        self.assertEqual(lines["alice"], "alice:!::")
        self.assertEqual(lines["netdev"], "netdev:*::")
        self.assertNotIn("docker", lines)

    def test_a_group_without_a_gshadow_line_gets_one(self):
        lines = {line.split(":")[0]: line for line in merge(gshadow="").files["gshadow"].splitlines()}
        self.assertEqual(lines["staff"], "staff:!::")

    def test_shadow_group_id_comes_from_the_merged_group_file(self):
        self.assertEqual(merge().shadow_gid, 42)

    def test_blank_and_malformed_lines_are_ignored(self):
        accounts = merge(passwd=TARGET_PASSWD + "\n# comment\nbroken line\nbad:x:notanumber:1::/:/bin/sh\n")
        self.assertNotIn("broken", accounts.files["passwd"])
        self.assertNotIn("bad:", accounts.files["passwd"])

    def test_merging_twice_changes_nothing(self):
        first = merge()
        again = updater.merge_accounts(
            {"passwd": IMAGE_PASSWD, "group": IMAGE_GROUP, "shadow": IMAGE_SHADOW, "gshadow": IMAGE_GSHADOW},
            first.files,
        )
        self.assertEqual(again.files, first.files)
        self.assertEqual((again.uid_map, again.gid_map), ({}, {}))


class WriteAccountsTest(TmpCase):
    def test_files_are_written_atomically_with_the_right_modes_and_shadow_group(self):
        chowns = []
        accounts = merge()
        etc = self.tmp / "etc"
        etc.mkdir()
        (etc / "passwd").write_text("old")
        updater.write_accounts(self.tmp, accounts, chown=lambda path, uid, gid: chowns.append((uid, gid)))
        self.assertEqual((etc / "passwd").read_text(), accounts.files["passwd"])
        self.assertEqual(stat.S_IMODE((etc / "passwd").stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE((etc / "group").stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE((etc / "shadow").stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE((etc / "gshadow").stat().st_mode), 0o640)
        self.assertEqual(chowns, [(0, 42), (0, 42)])
        self.assertEqual(sorted(p.name for p in etc.iterdir()), ["group", "gshadow", "passwd", "shadow"])


@unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "needs root to chown")
class RemapTest(TmpCase):
    def owner(self, path):
        status = os.lstat(path)
        return status.st_uid, status.st_gid

    def make(self, rel, uid, gid, mode=None):
        path = put(self.tmp, rel, "x", mode)
        os.chown(path, uid, gid)
        if mode is not None:
            os.chmod(path, mode)  # chown itself clears setuid and setgid
        return path

    def test_files_that_carry_an_old_id_get_the_new_one(self):
        owned = self.make("var/lib/app/data", 105, 107)
        other = self.make("var/lib/app/other", 33, 33)
        count = updater.remap_ids(self.tmp, {105: 106}, {107: 109})
        self.assertEqual(self.owner(owned), (106, 109))
        self.assertEqual(self.owner(other), (33, 33))
        self.assertEqual(count, 1)

    def test_only_the_changed_half_of_an_owner_is_touched(self):
        path = self.make("home/alice/f", 1001, 105)
        updater.remap_ids(self.tmp, {}, {105: 106})
        self.assertEqual(self.owner(path), (1001, 106))

    def test_symlinks_are_chowned_not_followed(self):
        outside = self.make("usr/bin/tool", 105, 105)
        link(self.tmp, "var/log/ln", "../../usr/bin/tool")
        os.lchown(self.tmp / "var/log/ln", 105, 105)
        updater.remap_ids(self.tmp, {105: 106}, {105: 106})
        self.assertEqual(self.owner(self.tmp / "var/log/ln"), (106, 106))
        self.assertEqual(self.owner(outside), (105, 105))

    def test_directories_outside_the_state_trees_are_left_alone(self):
        unrelated = self.make("usr/share/x", 105, 105)
        updater.remap_ids(self.tmp, {105: 106}, {105: 106})
        self.assertEqual(self.owner(unrelated), (105, 105))

    def test_a_chain_of_shifts_moves_each_file_exactly_once(self):
        a = self.make("var/lib/a", 100, 0)
        b = self.make("var/lib/b", 101, 0)
        updater.remap_ids(self.tmp, {100: 101, 101: 102}, {})
        self.assertEqual(self.owner(a)[0], 101)
        self.assertEqual(self.owner(b)[0], 102)

    def test_setuid_and_setgid_bits_survive_the_chown(self):
        path = self.make("var/lib/helper", 105, 105, 0o6755)
        updater.remap_ids(self.tmp, {105: 106}, {105: 106})
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o6755)

    def test_nothing_to_remap_does_not_even_walk_the_disk(self):
        with mock.patch("os.walk") as walk:
            self.assertEqual(updater.remap_ids(self.tmp, {}, {}), 0)
        walk.assert_not_called()


# ---------------------------------------------------------------- dpkg status merge, atomic writes


class MergeStatusTest(TmpCase):
    def test_image_status_followed_by_the_extras_stanzas(self):
        image = "Package: a\nStatus: install ok installed\n\nPackage: b\nStatus: install ok installed\n"
        grub = "Package: grub-pc\nStatus: install ok installed"
        extra = "Package: x\nStatus: install ok installed"
        text = updater.merge_dpkg_status(image, [grub, extra])
        self.assertEqual(text, image + "\n" + grub + "\n\n" + extra + "\n")
        self.assertEqual([key[0] for key in updater.parse_dpkg_status(text)], ["a", "b", "grub-pc", "x"])

    def test_no_extras_is_the_image_status_unchanged(self):
        self.assertEqual(updater.merge_dpkg_status("Package: a\n", []), "Package: a\n")

    def test_an_image_status_without_a_final_newline_still_separates_the_stanzas(self):
        self.assertEqual(updater.merge_dpkg_status("Package: a", ["Package: b"]), "Package: a\n\nPackage: b\n")

    def test_write_atomic_replaces_the_file_and_leaves_no_temp_file(self):
        path = self.tmp / "d" / "status"
        updater.write_atomic(path, "one\n", 0o644)
        updater.write_atomic(path, "two\n", 0o600)
        self.assertEqual(path.read_text(), "two\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in path.parent.iterdir()], ["status"])


# ---------------------------------------------------------------- rsync command lines

BASE = ["rsync", "-aHAX", "--numeric-ids", "--delay-updates"]


class RsyncStepsTest(unittest.TestCase):
    def steps(self, image="/", target="/mnt"):
        steps = updater.rsync_steps(pathlib.Path(image), pathlib.Path(target),
                                    pathlib.Path("/run/f/a.rules"), pathlib.Path("/run/f/e.rules"))
        return dict(steps), [name for name, _ in steps]

    def test_four_runs_in_a_fixed_order(self):
        _steps, names = self.steps()
        self.assertEqual(names, ["system", "etc", "var", "dpkg"])

    def test_every_run_starts_with_the_same_options_and_never_deletes_excluded_files(self):
        steps, _ = self.steps()
        for argv in steps.values():
            self.assertEqual(argv[:4], BASE)
            self.assertNotIn("--delete-excluded", argv)
            self.assertNotIn("--delete", argv)
            self.assertNotIn("--delete-before", argv)

    def test_system_run_deletes_after_with_the_protect_filter_and_leaves_state_dirs_alone(self):
        argv = self.steps()[0]["system"]
        self.assertEqual(argv[4:6], ["--delete-after", "--filter=merge /run/f/a.rules"])
        self.assertEqual(argv[-2:], ["/", "/mnt/"])
        for pattern in ("/dev/", "/proc/", "/sys/", "/run/", "/tmp/", "/mnt/", "/media/", "/lost+found",
                        "/boot/efi/", "/boot/grub/", "/home/", "/root/", "/usr/local/", "/swapfile",
                        "/etc/", "/var/", "/usr/lib/locale/locale-archive"):
            self.assertIn(f"--exclude={pattern}", argv)

    @unittest.skipUnless(shutil.which("rsync"), "needs rsync (the build VMs have it)")
    def test_the_system_run_keeps_the_boxs_initrds_until_the_boot_menu_step(self):
        # The image has no initrd (config/live-build/rootfs/excludes). Deleting the box's would leave
        # it unbootable until update-initramfs runs, minutes later: a power cut in between is fatal.
        with tempfile.TemporaryDirectory() as tmp:
            image, target, rules = pathlib.Path(tmp, "image"), pathlib.Path(tmp, "target"), pathlib.Path(tmp, "a.rules")
            put(image, "boot/vmlinuz-6.12.2", "new kernel")
            put(target, "boot/vmlinuz-6.12.1", "old kernel")
            put(target, "boot/initrd.img-6.12.1", "old initrd")
            rules.write_text("")
            argv = dict(updater.rsync_steps(image, target, rules, rules))["system"]
            subprocess.run(argv, check=True, capture_output=True)
            self.assertEqual(sorted(p.name for p in (target / "boot").iterdir()),
                             ["initrd.img-6.12.1", "vmlinuz-6.12.2"])

    def test_etc_run_has_no_delete_and_excludes_the_keep_list(self):
        argv = self.steps()[0]["etc"]
        self.assertNotIn("--delete-after", argv)
        self.assertEqual(argv[-2:], ["/etc/", "/mnt/etc/"])
        for pattern in ("/fstab", "/crypttab", "/hostname", "/hosts", "/machine-id", "/adjtime", "/localtime",
                        "/timezone", "/resolv.conf", "/default/grub", "/default/locale", "/default/keyboard",
                        "/default/console-setup", "/locale.gen", "/apt/sources.list",
                        "/initramfs-tools/conf.d/resume", "/NetworkManager/system-connections/",
                        "/ssh/ssh_host_*", "/passwd", "/group", "/shadow", "/gshadow", "/passwd-", "/group-",
                        "/shadow-", "/gshadow-", "/subuid", "/subgid", "/sudoers.d/", "/.pwd.lock",
                        "/couchliteos/tailscale-auth.key", "/couchliteos/usbip-allowlist.conf",
                        "/couchliteos-usbip-client.conf"):
            self.assertIn(f"--exclude={pattern}", argv)

    def test_the_version_file_is_left_for_the_end(self):
        self.assertIn("--exclude=/couchliteos-version", self.steps()[0]["etc"])

    def test_var_run_has_no_delete_and_excludes_state(self):
        argv = self.steps()[0]["var"]
        self.assertNotIn("--delete-after", argv)
        self.assertEqual(argv[-2:], ["/var/", "/mnt/var/"])
        for pattern in ("/lib/couchliteos/", "/lib/bluetooth/", "/lib/NetworkManager/", "/lib/tailscale/",
                        "/lib/systemd/random-seed", "/lib/systemd/timers", "/lib/systemd/backlight",
                        "/lib/systemd/rfkill", "/lib/systemd/timesync", "/lib/systemd/coredump",
                        "/lib/systemd/pstore", "/lib/systemd/linger", "/log/", "/cache/", "/tmp/", "/spool/",
                        "/mail/", "/backups/", "/lib/dhcp/", "/lib/alsa/", "/lib/upower/",
                        "/lib/private/", "/lib/dbus/machine-id", "/lib/couchliteos-update/", "/lib/dpkg/"):
            self.assertIn(f"--exclude={pattern}", argv)

    def test_dpkg_run_deletes_after_with_its_own_filter_and_leaves_status_to_the_merge(self):
        argv = self.steps()[0]["dpkg"]
        self.assertEqual(argv[4:6], ["--delete-after", "--filter=merge /run/f/e.rules"])
        self.assertIn("--exclude=/status", argv)
        self.assertEqual(argv[-2:], ["/var/lib/dpkg/", "/mnt/var/lib/dpkg/"])

    def test_a_non_root_image_is_addressed_below_its_own_directory(self):
        steps, _ = self.steps(image="/tmp/img", target="/tmp/tgt/")
        self.assertEqual(steps["system"][-2:], ["/tmp/img/", "/tmp/tgt/"])
        self.assertEqual(steps["etc"][-2:], ["/tmp/img/etc/", "/tmp/tgt/etc/"])
        self.assertEqual(steps["dpkg"][-2:], ["/tmp/img/var/lib/dpkg/", "/tmp/tgt/var/lib/dpkg/"])

    def test_no_run_touches_remote_desktop_state(self):
        steps, _ = self.steps()
        self.assertIn("--exclude=/lib/couchliteos/", steps["var"])  # /var/lib/couchliteos/rdp lives below it
        self.assertNotIn("--exclude=/lib/couchliteos/rdp", steps["var"])


# ---------------------------------------------------------------- /etc keep-list, stale files, manifest


class EtcTest(TmpCase):
    def setUp(self):
        super().setUp()
        self.image = self.tmp / "image-etc"
        self.target = self.tmp / "target-etc"
        self.image.mkdir()
        self.target.mkdir()

    def test_etc_kept_matches_the_keep_list_but_not_image_owned_neighbours(self):
        for rel in ("fstab", "default/grub", "ssh/ssh_host_rsa_key", "ssh/ssh_host_ed25519_key.pub",
                    "NetworkManager/system-connections/home.nmconnection", "sudoers.d/90-x",
                    "couchliteos/tailscale-auth.key", "couchliteos/usbip-allowlist.conf",
                    "couchliteos-version", "initramfs-tools/conf.d/resume"):
            self.assertTrue(updater.etc_kept(rel), rel)
        for rel in ("default/grub.d/20-couchliteos.cfg", "ssh/ssh_config", "couchliteos/nftables.template",
                    "NetworkManager/NetworkManager.conf", "grub.d/01_couchliteos_bootcheck",
                    "systemd/system/couchliteos-launcher.service", "fstab.d/x"):
            self.assertFalse(updater.etc_kept(rel), rel)

    def test_manifest_lists_the_images_etc_files_and_symlinks_not_directories(self):
        put(self.image, "a.conf")
        put(self.image, "sub/b.conf")
        link(self.image, "sub/ln", "b.conf")
        (self.image / "emptydir").mkdir()
        self.assertEqual(updater.etc_manifest(self.image), ["a.conf", "sub/b.conf", "sub/ln"])

    def test_manifest_round_trips_and_a_missing_one_reads_as_none(self):
        path = self.tmp / "var/lib/couchliteos-update/etc-manifest"
        self.assertIsNone(updater.read_manifest(path))
        updater.write_manifest(path, ["a", "b/c"])
        self.assertEqual(updater.read_manifest(path), {"a", "b/c"})

    def test_with_a_manifest_files_the_new_image_dropped_are_stale(self):
        put(self.target, "old.conf")
        put(self.target, "kept-by-image.conf")
        put(self.target, "user-made.conf")
        put(self.target, "fstab")
        put(self.image, "kept-by-image.conf")
        manifest = {"old.conf", "kept-by-image.conf", "fstab", "gone-already.conf"}
        self.assertEqual(updater.stale_etc_files(self.target, self.image, manifest), ["old.conf"])

    def test_without_a_manifest_only_our_own_units_and_the_couchliteos_dir_are_image_owned(self):
        put(self.target, "systemd/system/couchliteos-old.service")
        link(self.target, "systemd/system/multi-user.target.wants/couchliteos-old.service", "../couchliteos-old.service")
        put(self.target, "systemd/system/couchliteos-new.service")
        link(self.target, "systemd/system/sysinit.target.wants/grub-common.service", "/lib/systemd/system/grub-common.service")
        put(self.target, "systemd/system/admin-made.service")
        put(self.target, "couchliteos/nftables.template.old")
        put(self.target, "couchliteos/tailscale-auth.key")
        put(self.target, "couchliteos/usbip-allowlist.conf")
        put(self.target, "couchliteos-usbip-client.conf")
        put(self.target, "random.conf")
        put(self.image, "systemd/system/couchliteos-new.service")
        self.assertEqual(updater.stale_etc_files(self.target, self.image, None), [
            "couchliteos/nftables.template.old",
            "systemd/system/couchliteos-old.service",
            "systemd/system/multi-user.target.wants/couchliteos-old.service",
        ])

    def test_remove_stale_deletes_files_and_prunes_directories_it_emptied(self):
        put(self.target, "a/b/old.conf")
        put(self.target, "a/keep.conf")
        put(self.target, "c/d/old2.conf")
        updater.remove_stale(self.target, ["a/b/old.conf", "c/d/old2.conf", "already/gone"])
        self.assertFalse((self.target / "a/b").exists())
        self.assertTrue((self.target / "a/keep.conf").exists())
        self.assertFalse((self.target / "c").exists())
        self.assertTrue(self.target.exists())

    def test_remove_stale_never_leaves_the_etc_tree(self):
        put(self.tmp, "outside.conf")
        updater.remove_stale(self.target, ["../outside.conf", "/abs/path"])
        self.assertTrue((self.tmp / "outside.conf").exists())


# ---------------------------------------------------------------- fixtures for download / run / apply


class Clock:
    def __init__(self, step=0.4):
        self.now = 0.0
        self.step = step

    def __call__(self):
        self.now += self.step
        return self.now


class RecordingStatus(updater.Status):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.history = []

    def set(self, phase, message, percent=None):
        self.history.append((phase, message, percent))
        super().set(phase, message, percent)

    def phases(self):
        return [phase for phase, _message, _percent in self.history]


class Response(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(body)
        self.status = status


class Flaky(Response):
    """Serves `limit` bytes and then loses the connection."""

    def __init__(self, body, limit, status=200):
        super().__init__(body, status)
        self.limit = limit

    def read(self, size=-1):
        if self.tell() >= self.limit:
            raise TimeoutError("read timed out")
        return super().read(min(size if size > 0 else self.limit, self.limit - self.tell()))


class Net:
    """urlopen stand-in: routes URL -> bytes | Exception, honours Range unless told not to."""

    def __init__(self, routes=None, honour_range=True):
        self.routes = {} if routes is None else routes  # shared: a test may change a route later
        self.honour_range = honour_range
        self.requests = []
        self.on_request = None
        self.flaky_after = None

    def __call__(self, request, timeout=None):
        url = request.full_url
        wanted = request.get_header("Range")
        self.requests.append((url, wanted, timeout))
        if self.on_request:
            self.on_request(url)
        body = self.routes[url]
        if isinstance(body, Exception):
            raise body
        start, status = 0, 200
        if wanted and self.honour_range:
            start, status = int(wanted.removeprefix("bytes=").rstrip("-")), 206
        if self.flaky_after is not None:
            return Flaky(body[start:], self.flaky_after, status)
        return Response(body[start:], status)


ISO_BYTES = bytes(range(256)) * 40
ISO_SHA = hashlib.sha256(ISO_BYTES).hexdigest()
ISO_NAME = "couchliteos-0.2.2-amd64.iso"
BASE_URL = "https://github.com/Dudiebug/couchliteos/releases/download/v0.2.2/"
ASSET = update.Asset(ISO_NAME, BASE_URL + ISO_NAME, len(ISO_BYTES))
SUMS = f"{ISO_SHA}  {ISO_NAME}\n{'0' * 64}  couchliteos-0.2.2-nvidia-amd64.iso\n"


def make_env(tmp, **overrides):
    values = dict(
        runner=Runner(), root=tmp / "root", run_dir=tmp / "run", work_dir=tmp / "work", cache_dir=tmp / "cache",
        opener=Net(), free_bytes=lambda _path: 100 * GIB, lchown=lambda *a: None, chown=lambda *a: None,
        clock=Clock(), euid=lambda: 0,
    )
    values.update(overrides)
    return updater.Env(**values)


# ---------------------------------------------------------------- SHA256SUMS and the download


class SumsTest(TmpCase):
    def test_both_sha256sum_formats_and_a_dot_slash_prefix_are_read(self):
        text = f"{'a' * 64}  one.iso\n{'B' * 64} *two.iso\n{'c' * 64}  ./three.iso\n"
        self.assertEqual(updater.parse_sums(text), {"one.iso": "a" * 64, "two.iso": "b" * 64, "three.iso": "c" * 64})

    def test_garbage_short_hashes_and_blank_lines_are_ignored(self):
        text = f"\nnot a line\n{'a' * 63}  short.iso\n{'g' * 64}  nothex.iso\n{'a' * 64}  ok.iso\n"
        self.assertEqual(updater.parse_sums(text), {"ok.iso": "a" * 64})

    def test_fetch_sums_reads_the_asset_and_finds_the_entry_for_the_exact_file(self):
        env = make_env(self.tmp, opener=Net({BASE_URL + "SHA256SUMS": SUMS.encode()}))
        sums = updater.fetch_sums(update.Asset("SHA256SUMS", BASE_URL + "SHA256SUMS", 200), env)
        self.assertEqual(updater.expected_sha(sums, ISO_NAME), ISO_SHA)
        self.assertEqual(env.opener.requests[0][2], 30)

    def test_a_checksum_file_without_the_isos_line_is_an_error(self):
        with self.assertRaises(updater.UpdateFailed) as caught:
            updater.expected_sha({"other.iso": "a" * 64}, ISO_NAME)
        self.assertIn("CHECKSUM", caught.exception.message)

    def test_an_oversized_checksum_file_is_refused(self):
        env = make_env(self.tmp, opener=Net({BASE_URL + "SHA256SUMS": b"x" * (updater.SUMS_MAX + 1)}))
        with self.assertRaises(updater.UpdateFailed):
            updater.fetch_sums(update.Asset("SHA256SUMS", BASE_URL + "SHA256SUMS", 200), env)


class RedirectTest(unittest.TestCase):
    def test_redirects_may_only_stay_on_https(self):
        handler = updater.HttpsOnlyRedirect()
        request = urllib.request.Request("https://github.com/x")
        followed = handler.redirect_request(request, io.BytesIO(), 302, "Found", {}, "https://objects.githubusercontent.com/y")
        self.assertEqual(followed.full_url, "https://objects.githubusercontent.com/y")
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(request, io.BytesIO(), 302, "Found", {}, "http://evil.example/y")


class DownloadTest(TmpCase):
    def setUp(self):
        super().setUp()
        self.net = Net({ASSET.url: ISO_BYTES})
        self.env = make_env(self.tmp, opener=self.net)
        self.status = RecordingStatus(self.tmp / "status.json")
        self.iso = self.env.cache_dir / ISO_NAME
        self.part = self.env.cache_dir / (ISO_NAME + ".part")

    def download(self, expected=ISO_SHA, asset=ASSET):
        return updater.download_iso(asset, expected, self.env, self.status)

    def test_a_fresh_download_is_verified_and_renamed(self):
        self.assertEqual(self.download(), self.iso)
        self.assertEqual(self.iso.read_bytes(), ISO_BYTES)
        self.assertFalse(self.part.exists())
        url, wanted, timeout = self.net.requests[0]
        self.assertEqual((url, wanted, timeout), (ASSET.url, None, 30))
        self.assertEqual(self.status.phases()[0], "downloading")
        self.assertEqual(self.status.phases()[-1], "verifying")

    def test_progress_is_reported_in_percent_and_megabytes_and_in_caps(self):
        with mock.patch.object(updater, "CHUNK", 1000):
            self.download()
        downloading = [item for item in self.status.history if item[0] == "downloading"]
        percents = [percent for _phase, _message, percent in downloading]
        self.assertEqual(percents, sorted(percents))
        self.assertTrue(all(0 <= percent <= 100 for percent in percents))
        self.assertGreater(len(downloading), 1)
        for _phase, message, _percent in downloading:
            self.assertRegex(message, r"^DOWNLOADING: \d+ OF \d+ MB$")

    def test_a_partial_file_is_resumed_with_a_range_request(self):
        self.env.cache_dir.mkdir(parents=True)
        self.part.write_bytes(ISO_BYTES[:4000])
        self.assertEqual(self.download(), self.iso)
        self.assertEqual(self.net.requests[0][1], "bytes=4000-")
        self.assertEqual(self.iso.read_bytes(), ISO_BYTES)

    def test_a_partial_file_the_server_answers_416_for_is_thrown_away_and_fetched_again(self):
        self.env.cache_dir.mkdir(parents=True)
        self.part.write_bytes(b"\xff" * 4000)  # left over from a file of another size
        seen = []

        def opener(request, timeout=None):
            seen.append(request.get_header("Range"))
            if request.get_header("Range"):
                raise urllib.error.HTTPError(request.full_url, 416, "Range Not Satisfiable", {}, None)
            return Response(ISO_BYTES)

        self.env.opener = opener
        self.assertEqual(self.download(), self.iso)
        self.assertEqual(seen, ["bytes=4000-", None])
        self.assertEqual(self.iso.read_bytes(), ISO_BYTES)
        self.assertFalse(self.part.exists())

    def test_a_second_416_is_a_failed_download(self):
        def opener(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 416, "Range Not Satisfiable", {}, None)

        self.env.opener = opener
        with self.assertRaises(updater.UpdateFailed) as caught:
            self.download()
        self.assertEqual(caught.exception.message, "DOWNLOAD FAILED: CHECK THE NETWORK AND TRY AGAIN")

    def test_a_server_that_ignores_range_restarts_from_zero(self):
        self.net.honour_range = False
        self.env.cache_dir.mkdir(parents=True)
        self.part.write_bytes(ISO_BYTES[:4000])
        self.assertEqual(self.download(), self.iso)
        self.assertEqual(self.iso.read_bytes(), ISO_BYTES)

    def test_a_damaged_partial_file_fails_the_checksum_and_is_thrown_away(self):
        self.env.cache_dir.mkdir(parents=True)
        self.part.write_bytes(b"\xff" * 4000)
        with self.assertRaises(updater.UpdateFailed) as caught:
            self.download()
        self.assertIn("DAMAGED", caught.exception.message)
        self.assertFalse(self.part.exists() or self.iso.exists())

    def test_a_checksum_mismatch_deletes_everything_and_names_the_problem(self):
        with self.assertRaises(updater.UpdateFailed) as caught:
            self.download(expected="0" * 64)
        self.assertEqual(caught.exception.message, "THE DOWNLOAD IS DAMAGED. TRY AGAIN.")
        self.assertEqual(list(self.env.cache_dir.iterdir()), [])

    def test_a_dropped_connection_keeps_the_partial_file_and_the_retry_resumes(self):
        self.net.flaky_after = 3000
        with self.assertRaises(updater.UpdateFailed) as caught:
            self.download()
        self.assertIn("NETWORK", caught.exception.message)
        self.assertEqual(self.part.stat().st_size, 3000)
        self.net.flaky_after = None
        self.assertEqual(self.download(), self.iso)
        self.assertEqual(self.net.requests[1][1], "bytes=3000-")

    def test_cancel_stops_the_download_and_keeps_the_partial_file_for_a_retry(self):
        self.env.run_dir.mkdir(parents=True)
        (self.env.run_dir / "update-cancel").write_text("")
        with mock.patch.object(updater, "CHUNK", 1000), self.assertRaises(updater.Cancelled):
            self.download()
        self.assertTrue(self.part.exists())
        self.assertFalse(self.iso.exists())

    def test_cancel_during_the_transfer_is_noticed_between_chunks(self):
        def request(url):
            self.env.run_dir.mkdir(parents=True, exist_ok=True)
            (self.env.run_dir / "update-cancel").write_text("")

        self.net.on_request = request
        with mock.patch.object(updater, "CHUNK", 1000), self.assertRaises(updater.Cancelled):
            self.download()
        self.assertLess(self.part.stat().st_size, len(ISO_BYTES))

    def test_not_enough_free_space_fails_before_any_request(self):
        big = update.Asset(ISO_NAME, ASSET.url, 1_500_000_000)
        self.env.free_bytes = lambda _path: 5 * GIB
        with self.assertRaises(updater.UpdateFailed) as caught:
            self.download(asset=big)
        self.assertEqual(caught.exception.message, "NOT ENOUGH FREE SPACE: NEED 6 GB")
        self.assertEqual(self.net.requests, [])

    def test_the_resumed_bytes_count_towards_the_free_space_needed(self):
        self.env.cache_dir.mkdir(parents=True)
        self.part.write_bytes(ISO_BYTES[:4000])
        need = len(ISO_BYTES) - 4000 + 4 * GIB
        self.env.free_bytes = lambda _path: need - 1
        with self.assertRaises(updater.UpdateFailed):
            self.download()
        self.env.free_bytes = lambda _path: need
        self.assertEqual(self.download(), self.iso)

    def test_a_complete_verified_iso_is_reused_without_a_request(self):
        self.env.cache_dir.mkdir(parents=True)
        self.iso.write_bytes(ISO_BYTES)
        self.assertEqual(self.download(), self.iso)
        self.assertEqual(self.net.requests, [])

    def test_a_damaged_cached_iso_is_replaced(self):
        self.env.cache_dir.mkdir(parents=True)
        self.iso.write_bytes(b"junk")
        self.assertEqual(self.download(), self.iso)
        self.assertEqual(self.iso.read_bytes(), ISO_BYTES)

    def test_files_of_other_versions_are_removed_first(self):
        self.env.cache_dir.mkdir(parents=True)
        for name in ("couchliteos-0.2.0-amd64.iso", "couchliteos-0.2.0-amd64.iso.part", "notes.txt"):
            (self.env.cache_dir / name).write_text("x")
        self.download()
        self.assertEqual(sorted(p.name for p in self.env.cache_dir.iterdir()), ["couchliteos-0.2.2-amd64.iso", "notes.txt"])

    def test_a_partial_file_longer_than_the_asset_starts_over(self):
        self.env.cache_dir.mkdir(parents=True)
        self.part.write_bytes(b"x" * (len(ISO_BYTES) + 10))
        self.assertEqual(self.download(), self.iso)
        self.assertIsNone(self.net.requests[0][1])

    def test_only_github_asset_urls_are_followed(self):
        for url in ("http://github.com/x.iso", "https://evil.example/x.iso", "https://github.com.evil.example/x.iso",
                    "file:///etc/passwd", "ftp://github.com/x"):
            with self.assertRaises(updater.UpdateFailed, msg=url):
                self.download(asset=update.Asset(ISO_NAME, url, len(ISO_BYTES)))
        self.assertEqual(self.net.requests, [])
        self.net.routes["https://api.github.com/repos/x/releases/assets/1"] = ISO_BYTES
        self.download(asset=update.Asset(ISO_NAME, "https://api.github.com/repos/x/releases/assets/1", len(ISO_BYTES)))

    def test_clean_cache_removes_isos_and_partials_only(self):
        self.env.cache_dir.mkdir(parents=True)
        for name in ("a.iso", "b.iso.part", "keep.txt"):
            (self.env.cache_dir / name).write_text("x")
        updater.clean_cache(self.env.cache_dir)
        self.assertEqual([p.name for p in self.env.cache_dir.iterdir()], ["keep.txt"])
        updater.clean_cache(self.tmp / "no-such-dir")  # must not raise


# ---------------------------------------------------------------- refusals


class RefusalTest(TmpCase):
    def test_live_boot_is_refused_by_the_medium_or_the_command_line(self):
        env = make_env(self.tmp)
        updater.refuse_live(env)  # an installed box passes
        (env.root / "run/live/medium").mkdir(parents=True)
        with self.assertRaises(updater.UpdateFailed) as caught:
            updater.refuse_live(env)
        self.assertEqual(caught.exception.message, "UPDATES NEED COUCHLITEOS INSTALLED TO A DISK")
        (env.root / "run/live/medium").rmdir()
        put(env.root, "proc/cmdline", "BOOT_IMAGE=/live/vmlinuz boot=live quiet\n")
        with self.assertRaises(updater.UpdateFailed):
            updater.refuse_live(env)

    def test_separate_boot_usr_or_var_mounts_are_refused(self):
        for mount in ("/boot", "/usr", "/var"):
            def script(argv, mount=mount):
                return (0, f"{mount}\n") if argv[-1] == mount else None

            env = make_env(self.tmp, runner=Runner(script))
            with self.assertRaises(updater.UpdateFailed, msg=mount) as caught:
                updater.refuse_layout(env)
            self.assertEqual(caught.exception.message, "UNSUPPORTED DISK LAYOUT")

    def test_layout_check_asks_findmnt_about_each_directory(self):
        env = make_env(self.tmp)
        updater.refuse_layout(env)
        self.assertEqual(env.runner.calls, [
            ["findmnt", "-n", "-o", "TARGET", "--target", path] for path in ("/boot", "/usr", "/var")
        ])

    def test_a_findmnt_that_fails_is_not_assumed_to_be_fine(self):
        env = make_env(self.tmp, runner=Runner(lambda argv: (1, "")))
        with self.assertRaises(updater.UpdateFailed):
            updater.refuse_layout(env)


class GuardedTest(TmpCase):
    def setUp(self):
        super().setUp()
        self.logged = []
        self.env = make_env(self.tmp, log=self.logged.append)
        self.status = RecordingStatus(self.tmp / "s.json")

    def test_a_cancel_is_the_cancelled_phase_and_exit_0(self):
        def cancel():
            raise updater.Cancelled

        self.assertEqual(updater._guarded(self.env, self.status, cancel), 0)
        self.assertEqual(self.status.history[-1], ("cancelled", "UPDATE CANCELLED", None))

    def test_an_unexpected_exception_is_a_plain_failure_and_its_traceback_goes_to_the_log(self):
        def crash():
            raise KeyError("secret detail")

        self.assertEqual(updater._guarded(self.env, self.status, crash), 1)
        self.assertEqual(self.status.history[-1], ("failed", updater.MSG_GENERIC, None))
        self.assertNotIn("secret detail", json.dumps(updater.read_status(self.status.path)))
        self.assertTrue(any("KeyError" in line and "Traceback" in line for line in self.logged))


class RootPlacesTest(TmpCase):
    """Root's work never goes through /run/couchliteos, /var/lib/couchliteos or /var/log/couchliteos."""

    def test_the_documented_root_places(self):
        self.assertEqual(updater.WORK_DIR, pathlib.Path("/run/couchliteos-update"))
        self.assertEqual(updater.Env().work_dir, pathlib.Path("/run/couchliteos-update"))
        self.assertEqual(updater.LOG_REL, "var/log/couchliteos-update/update.log")
        for message in (updater.MSG_GENERIC, updater.MSG_INSTALL, updater.MSG_RESTORE):
            self.assertIn("/var/log/couchliteos-update/update.log", message)

    def test_own_dir_makes_root_only_directories(self):
        env = make_env(self.tmp)
        path = updater.own_dir(env, "update", "iso")
        self.assertEqual(path, self.tmp / "work/update/iso")
        for directory in (self.tmp / "work", self.tmp / "work/update", path):
            found = os.lstat(directory)
            self.assertEqual((found.st_uid, stat.S_IMODE(found.st_mode)), (os.geteuid(), 0o700), directory)

    def test_own_dir_refuses_a_symlink_or_a_file(self):
        env = make_env(self.tmp, log=[].append)
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (self.tmp / "work").symlink_to(elsewhere)
        with self.assertRaises(updater.UpdateFailed) as caught:
            updater.own_dir(env)
        self.assertEqual(caught.exception.message, updater.MSG_GENERIC)
        (self.tmp / "work").unlink()
        (self.tmp / "work").mkdir()
        (self.tmp / "work/probe").write_text("")
        with self.assertRaises(updater.UpdateFailed):
            updater.own_dir(env, "probe")

    @unittest.skipUnless(os.geteuid() == 0, "needs root to give the directory away")
    def test_own_dir_refuses_a_directory_the_launchers_user_made(self):
        env = make_env(self.tmp, log=[].append)
        (self.tmp / "work").mkdir(mode=0o700)
        os.chown(self.tmp / "work", 1000, 1000)
        with self.assertRaises(updater.UpdateFailed):
            updater.own_dir(env, "update")
        self.assertFalse((self.tmp / "work/update").exists())

    def test_apply_iso_refuses_a_symlinked_mount_point_before_mounting_anything(self):
        env = make_env(self.tmp, log=[].append)
        put(env.root, "usr/share/couchliteos/profile.conf", "PROFILE_NAME=general\n")
        elsewhere = self.tmp / "etc"
        elsewhere.mkdir()
        (self.tmp / "work/update").mkdir(parents=True)
        (self.tmp / "work/update/iso").symlink_to(elsewhere)
        with self.assertRaises(updater.UpdateFailed):
            updater.apply_iso(self.tmp / "x.iso", env, RecordingStatus(self.tmp / "s.json"), save_first=False)
        self.assertEqual(env.runner.commands("mount"), [])

    def test_the_log_never_follows_a_symlink(self):
        secret = put(self.tmp, "shadow", "root:x\n")
        link(self.tmp, "var/log/couchliteos-update/update.log", secret)
        updater.Log(self.tmp / LOG)("evil")  # never raises
        self.assertEqual(secret.read_text(), "root:x\n")

    def test_the_log_refuses_a_directory_others_own_or_a_symlinked_one(self):
        user = self.tmp / "user-log"
        user.mkdir()
        link(self.tmp, "var/log/couchliteos-update", user)
        updater.Log(self.tmp / LOG)("evil")
        self.assertEqual(os.listdir(user), [])

    def test_the_status_never_writes_through_a_symlink(self):
        secret = put(self.tmp, "shadow", "root:x\n")
        link(self.tmp, "run/update-status.json", secret)
        updater.Status(self.tmp / "run/update-status.json").set("checking", "X")
        self.assertEqual(secret.read_text(), "root:x\n")
        self.assertEqual(updater.read_status(self.tmp / "run/update-status.json")["phase"], "checking")

    def test_read_status_never_follows_a_symlink(self):
        secret = put(self.tmp, "secret.json", '{"phase": "failed", "message": "ROOT ONLY"}')
        link(self.tmp, "run/update-status.json", secret)
        self.assertEqual(updater.read_status(self.tmp / "run/update-status.json"), {})


LOG = "var/log/couchliteos-update/update.log"


class LockTest(TmpCase):
    def test_a_second_holder_is_refused_and_the_lock_is_released_afterwards(self):
        path = updater.own_dir(make_env(self.tmp)) / "update.lock"
        with updater.acquire_lock(path):
            with self.assertRaises(updater.UpdateFailed) as caught:
                with updater.acquire_lock(path):
                    pass
            self.assertEqual(caught.exception.message, "AN UPDATE IS ALREADY RUNNING")
        with updater.acquire_lock(path):
            pass


# ---------------------------------------------------------------- run (the service)


def release_json(version="0.2.2"):
    names = [f"couchliteos-{version}-amd64.iso", f"couchliteos-{version}-nvidia-amd64.iso", "SHA256SUMS"]
    return json.dumps({"tag_name": f"v{version}", "assets": [
        {"name": name, "size": len(ISO_BYTES), "browser_download_url": f"{BASE_URL}{name}"} for name in names
    ]}).encode()


API = "https://api.github.com/repos/Dudiebug/couchliteos/releases/latest"


class RunTest(TmpCase):
    def setUp(self):
        super().setUp()
        self.routes = {
            API: release_json(),
            BASE_URL + "SHA256SUMS": SUMS.encode(),
            BASE_URL + ISO_NAME: ISO_BYTES,
        }
        self.net = Net(self.routes)
        self.runner = Runner()
        self.env = make_env(self.tmp, opener=self.net, runner=self.runner)
        put(self.env.root, "etc/couchliteos-version", "0.2.1\n")
        put(self.env.root, "usr/share/couchliteos/profile.conf", "PROFILE_NAME=general\nISO_SUFFIX=\n")
        self.status = RecordingStatus(self.env.run_dir / "update-status.json")
        self.applied = []
        self.saved = []
        self.save_error = None

    def apply(self, path, env, status, **kwargs):
        self.applied.append((path, kwargs))

    def save(self, env, status):
        self.saved.append(len(self.applied))
        if self.save_error:
            raise self.save_error

    def run_update(self, apply=None, **kwargs):
        return updater.run(self.env, self.status, apply=apply or self.apply, save=self.save, **kwargs)

    def final(self):
        return json.loads(self.status.path.read_text())

    def test_a_newer_release_is_downloaded_verified_applied_and_the_box_restarts(self):
        self.assertEqual(self.run_update(), 0)
        self.assertEqual(self.status.phases()[0], "checking")
        self.assertIn("downloading", self.status.phases())
        self.assertIn("verifying", self.status.phases())
        self.assertEqual([item[0] for item in self.applied], [self.env.cache_dir / ISO_NAME])
        self.assertEqual(self.applied[0][1], {})
        self.assertIn(["systemctl", "reboot"], self.runner.calls)
        self.assertEqual(self.status.version, "0.2.2")
        self.assertEqual(list(self.env.cache_dir.iterdir()), [], "the ISO is deleted after a good install")

    def test_the_status_file_carries_the_version_being_installed(self):
        self.run_update()
        self.assertEqual(self.final()["version"], "0.2.2")

    def test_the_boxs_profile_chooses_the_iso(self):
        put(self.env.root, "usr/share/couchliteos/profile.conf", "PROFILE_NAME=nvidia\nISO_SUFFIX=nvidia\n")
        self.routes[BASE_URL + "couchliteos-0.2.2-nvidia-amd64.iso"] = ISO_BYTES
        self.routes[BASE_URL + "SHA256SUMS"] = f"{ISO_SHA}  couchliteos-0.2.2-nvidia-amd64.iso\n".encode()
        self.assertEqual(self.run_update(), 0)
        self.assertEqual(self.applied[0][0].name, "couchliteos-0.2.2-nvidia-amd64.iso")

    def test_already_newest_downloads_nothing_and_does_not_restart(self):
        put(self.env.root, "etc/couchliteos-version", "0.2.2\n")
        self.assertEqual(self.run_update(), 0)
        self.assertEqual(self.final()["phase"], "uptodate")
        self.assertEqual(self.final()["message"], "THIS IS THE NEWEST VERSION")
        self.assertEqual(self.applied, [])
        self.assertNotIn(["systemctl", "reboot"], self.runner.calls)
        self.assertEqual([url for url, _r, _t in self.net.requests], [API])

    def test_live_boot_is_refused_before_any_network_use(self):
        (self.env.root / "run/live/medium").mkdir(parents=True)
        self.assertEqual(self.run_update(), 1)
        self.assertEqual(self.final()["phase"], "failed")
        self.assertEqual(self.final()["message"], "UPDATES NEED COUCHLITEOS INSTALLED TO A DISK")
        self.assertEqual(self.net.requests, [])

    def test_an_unsupported_layout_is_refused_before_the_download(self):
        self.env.runner = Runner(lambda argv: (0, "/boot\n") if argv[-1] == "/boot" else None)
        self.assertEqual(self.run_update(), 1)
        self.assertEqual(self.final()["message"], "UNSUPPORTED DISK LAYOUT")
        self.assertEqual(self.net.requests, [])

    def test_a_release_without_a_file_for_this_profile_says_so(self):
        put(self.env.root, "usr/share/couchliteos/profile.conf", "PROFILE_NAME=intel\nISO_SUFFIX=intel\n")
        self.assertEqual(self.run_update(), 1)
        self.assertEqual(self.final()["message"], "THIS RELEASE HAS NO FILE FOR THIS BOX")

    def test_a_release_whose_nvidia_iso_is_not_out_yet_says_so(self):
        put(self.env.root, "usr/share/couchliteos/profile.conf", "PROFILE_NAME=nvidia\nISO_SUFFIX=nvidia\nRELEASE=1\n")
        names = ["couchliteos-0.2.2-amd64.iso", "SHA256SUMS"]  # the NVIDIA ISO is published later
        self.routes[API] = json.dumps({"tag_name": "v0.2.2", "assets": [
            {"name": name, "size": len(ISO_BYTES), "browser_download_url": f"{BASE_URL}{name}"} for name in names
        ]}).encode()
        self.assertEqual(self.run_update(), 1)
        self.assertEqual(self.final()["message"], "THE NVIDIA VERSION OF 0.2.2 IS NOT READY YET. TRY AGAIN LATER.")
        self.assertEqual(self.applied, [])
        self.assertEqual([url for url, _r, _t in self.net.requests], [API])

    def test_github_being_unreachable_points_at_the_network_settings(self):
        self.routes[API] = urllib.error.URLError("offline")
        self.assertEqual(self.run_update(), 1)
        self.assertEqual(self.final()["message"], "COULD NOT REACH GITHUB: CHECK SETTINGS > NETWORK")

    def test_a_bad_checksum_fails_without_applying_or_restarting(self):
        self.routes[BASE_URL + "SHA256SUMS"] = f"{'0' * 64}  {ISO_NAME}\n".encode()
        self.assertEqual(self.run_update(), 1)
        self.assertEqual(self.final()["phase"], "failed")
        self.assertIn("DAMAGED", self.final()["message"])
        self.assertEqual(self.applied, [])
        self.assertNotIn(["systemctl", "reboot"], self.runner.calls)

    def test_cancel_during_the_download_ends_cancelled_and_keeps_the_partial_file(self):
        def request(url):
            if url.endswith(".iso"):
                (self.env.run_dir / "update-cancel").write_text("")

        self.net.on_request = request
        with mock.patch.object(updater, "CHUNK", 1000):
            self.assertEqual(self.run_update(), 0)
        self.assertEqual(self.final()["phase"], "cancelled")
        self.assertEqual(self.final()["message"], "UPDATE CANCELLED")
        self.assertEqual(self.applied, [])
        self.assertTrue((self.env.cache_dir / (ISO_NAME + ".part")).exists())
        self.assertNotIn(["systemctl", "reboot"], self.runner.calls)

    def test_a_stale_cancel_file_from_an_earlier_run_is_ignored(self):
        self.env.run_dir.mkdir(parents=True)
        (self.env.run_dir / "update-cancel").write_text("")
        self.assertEqual(self.run_update(), 0)
        self.assertEqual(len(self.applied), 1)
        self.assertFalse((self.env.run_dir / "update-cancel").exists())

    def test_an_install_failure_is_reported_and_the_verified_iso_is_kept_for_a_retry(self):
        def failing(path, env, status, **kwargs):
            raise updater.UpdateFailed("INSTALL FAILED WHILE COPYING FILES.")

        self.assertEqual(self.run_update(apply=failing), 1)
        self.assertEqual(self.final()["message"], "INSTALL FAILED WHILE COPYING FILES.")
        self.assertTrue((self.env.cache_dir / ISO_NAME).exists())
        self.assertNotIn(["systemctl", "reboot"], self.runner.calls)

    def test_a_crash_is_reported_in_plain_words_without_leaking_the_exception(self):
        def crashing(path, env, status, **kwargs):
            raise RuntimeError("secret internals")

        self.assertEqual(self.run_update(apply=crashing), 1)
        self.assertEqual(self.final()["phase"], "failed")
        self.assertEqual(self.final()["message"], "UPDATE FAILED: SEE /var/log/couchliteos-update/update.log")
        self.assertNotIn("secret", json.dumps(self.final()))

    def test_the_box_is_saved_after_the_download_and_before_the_install(self):
        self.assertEqual(self.run_update(), 0)
        self.assertEqual(self.saved, [0], "saved once, before apply ran")
        self.assertIn("verifying", self.status.phases())

    def test_a_failed_save_stops_the_update_before_anything_is_installed(self):
        self.save_error = updater.UpdateFailed("NOT ENOUGH FREE SPACE TO SAVE THE CURRENT VERSION: NEED 7 GB.")
        self.assertEqual(self.run_update(), 1)
        self.assertEqual(self.final()["phase"], "failed")
        self.assertIn("SAVE THE CURRENT VERSION", self.final()["message"])
        self.assertEqual(self.applied, [])
        self.assertNotIn(["systemctl", "reboot"], self.runner.calls)
        self.assertTrue((self.env.cache_dir / ISO_NAME).exists(), "the verified ISO is kept for a retry")

    def test_no_snapshot_skips_the_save(self):
        self.assertEqual(self.run_update(save_first=False), 0)
        self.assertEqual(self.saved, [])
        self.assertEqual(len(self.applied), 1)

    def test_nothing_is_saved_when_there_is_nothing_newer_or_on_a_live_boot(self):
        put(self.env.root, "etc/couchliteos-version", "0.2.2\n")
        self.run_update()
        (self.env.root / "run/live/medium").mkdir(parents=True)
        put(self.env.root, "etc/couchliteos-version", "0.2.1\n")
        self.assertEqual(self.run_update(), 1)
        self.assertEqual(self.saved, [])

    def test_the_default_install_does_not_save_a_second_time(self):
        with mock.patch.object(updater, "apply_iso") as apply_iso:
            self.assertEqual(updater.run(self.env, self.status, save=self.save), 0)
        self.assertEqual(self.saved, [0])
        self.assertFalse(apply_iso.call_args.kwargs["save_first"])

    def test_a_missing_version_file_refuses_to_guess(self):
        (self.env.root / "etc/couchliteos-version").unlink()
        self.assertEqual(self.run_update(), 1)
        self.assertEqual(self.final()["phase"], "failed")
        self.assertEqual(self.net.requests, [])


# ---------------------------------------------------------------- apply_iso (the handover)


class ApplyIsoTest(TmpCase):
    def setUp(self):
        super().setUp()
        self.env = make_env(self.tmp)
        put(self.env.root, "etc/couchliteos-version", "0.2.1\n")
        put(self.env.root, "usr/share/couchliteos/profile.conf", "PROFILE_NAME=general\nISO_SUFFIX=\n")
        self.work = self.env.work_dir / "update"
        self.new = self.work / "root"  # where the (mocked) mount of the squashfs shows the new image
        put(self.new, "usr/libexec/couchliteos-updater", "#!/usr/bin/python3\n")
        put(self.new, "usr/share/couchliteos/profile.conf", "PROFILE_NAME=general\n")
        self.iso = self.tmp / "update.iso"
        self.iso.write_bytes(b"iso")
        self.status = RecordingStatus(self.env.run_dir / "update-status.json")
        self.saved = []

    def save(self, env, status):
        self.saved.append(len(self.mounts()))

    def apply(self, **kwargs):
        kwargs.setdefault("save", self.save)
        return updater.apply_iso(self.iso, self.env, self.status, **kwargs)

    def mounts(self):
        return [call for call in self.env.runner.calls if call[0] in ("mount", "umount", "chroot")]

    def test_the_whole_handover_in_order(self):
        self.apply()
        w, r = str(self.work), str(self.new)
        self.assertEqual(self.mounts(), [
            ["mount", "-o", "ro,loop", str(self.iso), f"{w}/iso"],
            ["mount", "-t", "squashfs", "-o", "ro,loop", f"{w}/iso/live/filesystem.squashfs", r],
            ["mount", "--bind", "/", f"{r}/mnt"], ["mount", "--make-rslave", f"{r}/mnt"],
            ["mount", "-t", "proc", "proc", f"{r}/proc"],
            ["mount", "--rbind", "/sys", f"{r}/sys"], ["mount", "--make-rslave", f"{r}/sys"],
            ["mount", "--rbind", "/dev", f"{r}/dev"], ["mount", "--make-rslave", f"{r}/dev"],
            ["mount", "--bind", "/run", f"{r}/run"], ["mount", "--make-rslave", f"{r}/run"],
            ["chroot", r, "/usr/libexec/couchliteos-updater", "apply-root", "/mnt", "--status",
             str(self.status.path)],
            ["umount", "-R", f"{r}/run"], ["umount", "-R", f"{r}/dev"], ["umount", "-R", f"{r}/sys"],
            ["umount", "-R", f"{r}/proc"], ["umount", "-R", f"{r}/mnt"], ["umount", "-R", r],
            ["umount", "-R", f"{w}/iso"],
        ])

    def test_the_box_is_saved_after_the_iso_is_checked_and_before_the_box_is_mounted(self):
        self.apply()
        self.assertEqual(self.saved, [2], "after the ISO and its squashfs, before the bind mount of /")

    def test_no_snapshot_skips_the_save(self):
        self.apply(save_first=False)
        self.assertEqual(self.saved, [])
        self.assertEqual(len(self.env.runner.commands("chroot")), 1)

    def test_a_failed_save_stops_before_the_new_updater_runs(self):
        def failing(env, status):
            raise updater.UpdateFailed("COULD NOT SAVE THE CURRENT VERSION")

        with self.assertRaises(updater.UpdateFailed):
            self.apply(save=failing)
        self.assertEqual(self.env.runner.commands("chroot"), [])
        self.assertNotIn(["mount", "--bind", "/", f"{self.new}/mnt"], self.mounts())

    def test_an_iso_of_another_profile_is_refused_before_saving(self):
        put(self.new, "usr/share/couchliteos/profile.conf", "PROFILE_NAME=nvidia\n")
        with self.assertRaises(updater.UpdateFailed):
            self.apply()
        self.assertEqual(self.saved, [])

    def test_the_layout_is_checked_before_anything_is_mounted(self):
        self.apply()
        self.assertEqual([call[0] for call in self.env.runner.calls[:4]], ["findmnt"] * 3 + ["mount"])

    def test_force_is_passed_to_the_new_images_updater(self):
        self.apply(force=True)
        self.assertIn("--force", self.env.runner.commands("chroot")[0])

    def test_the_new_updater_is_told_the_box_was_saved(self):
        put(self.new, "usr/libexec/couchliteos-updater", "#!/usr/bin/python3\n# --snapshot-done\n")
        for save_first in (True, False):  # skipped on purpose (--no-snapshot, run) counts as handled too
            self.env.runner.calls.clear()
            self.apply(save_first=save_first)
            self.assertEqual(self.env.runner.commands("chroot")[0][-1], "--snapshot-done")

    def test_an_older_updater_in_the_iso_is_not_given_a_flag_it_would_refuse(self):
        self.apply()
        self.assertNotIn("--snapshot-done", self.env.runner.commands("chroot")[0])

    def test_a_block_device_is_mounted_without_loop(self):
        with mock.patch.object(updater, "is_block_device", return_value=True):
            self.apply()
        self.assertEqual(self.mounts()[0][:4], ["mount", "-o", "ro", str(self.iso)])

    def test_status_says_installing_from_the_start(self):
        self.apply()
        self.assertEqual(self.status.history[0][0], "installing")

    def test_live_boot_is_refused_before_any_mount(self):
        (self.env.root / "run/live/medium").mkdir(parents=True)
        with self.assertRaises(updater.UpdateFailed) as caught:
            self.apply()
        self.assertEqual(caught.exception.message, "UPDATES NEED COUCHLITEOS INSTALLED TO A DISK")
        self.assertEqual(self.env.runner.calls, [])

    def test_a_separate_usr_is_refused_before_any_mount(self):
        self.env.runner = Runner(lambda argv: (0, "/usr\n") if argv[-1] == "/usr" else None)
        with self.assertRaises(updater.UpdateFailed) as caught:
            self.apply()
        self.assertEqual(caught.exception.message, "UNSUPPORTED DISK LAYOUT")
        self.assertEqual(self.mounts(), [])

    def test_an_iso_without_the_updater_is_refused_and_unmounted(self):
        (self.new / "usr/libexec/couchliteos-updater").unlink()
        with self.assertRaises(updater.UpdateFailed) as caught:
            self.apply()
        self.assertEqual(caught.exception.message, "THIS ISO CANNOT UPDATE A BOX")
        self.assertEqual(self.env.runner.commands("chroot"), [])
        self.assertEqual([call[2] for call in self.env.runner.commands("umount")][-2:], [str(self.new), f"{self.work}/iso"])

    def test_an_iso_of_another_profile_is_refused(self):
        put(self.new, "usr/share/couchliteos/profile.conf", "PROFILE_NAME=nvidia\n")
        with self.assertRaises(updater.UpdateFailed) as caught:
            self.apply()
        self.assertEqual(caught.exception.message, "THIS ISO IS FOR A DIFFERENT KIND OF BOX")
        self.assertEqual(self.env.runner.commands("chroot"), [])

    def test_a_failing_mount_unwinds_only_what_was_mounted(self):
        self.env.runner = Runner(lambda argv: (32, "mount: wrong fs type") if "squashfs" in argv else None)
        with self.assertRaises(updater.UpdateFailed) as caught:
            self.apply()
        self.assertEqual(caught.exception.message, "COULD NOT OPEN THE UPDATE FILE")
        self.assertEqual(self.env.runner.commands("umount"), [["umount", "-R", f"{self.work}/iso"]])

    def test_an_unmount_that_fails_is_retried_lazily_and_never_fails_the_update(self):
        busy = str(self.new / "dev")
        self.env.runner = Runner(
            lambda argv: (1, "busy") if argv[:2] == ["umount", "-R"] and argv[-1] == busy else None)
        self.apply()
        umounts = self.env.runner.commands("umount")
        index = umounts.index(["umount", "-R", busy])
        self.assertEqual(umounts[index + 1], ["umount", "-R", "-l", busy])

    def test_the_new_updaters_own_failure_message_is_passed_on_and_everything_is_unmounted(self):
        def script(argv):
            if argv[0] == "chroot":
                updater.Status(self.status.path).set("failed", "INSTALL FAILED WHILE COPYING FILES.")
                return (1, "")

        self.env.runner = Runner(script)
        with self.assertRaises(updater.UpdateFailed) as caught:
            self.apply()
        self.assertEqual(caught.exception.message, "INSTALL FAILED WHILE COPYING FILES.")
        self.assertEqual(len(self.env.runner.commands("umount")), 7)

    def test_a_silent_failure_gets_a_generic_message(self):
        self.env.runner = Runner(lambda argv: (1, "") if argv[0] == "chroot" else None)
        with self.assertRaises(updater.UpdateFailed) as caught:
            self.apply()
        self.assertEqual(caught.exception.message, "INSTALL FAILED: SEE /var/log/couchliteos-update/update.log")


# ---------------------------------------------------------------- apply_root (inside the new image)


class ApplyRootTest(TmpCase):
    def setUp(self):
        super().setUp()
        self.make_trees()

    def make_trees(self):
        self.image = self.tmp / "image"
        self.target = self.tmp / "target"
        put(self.image, "etc/couchliteos-version", "0.2.2\n")
        put(self.image, "usr/share/couchliteos/profile.conf", "PROFILE_NAME=general\nISO_SUFFIX=\n")
        put(self.image, "etc/passwd", IMAGE_PASSWD)
        put(self.image, "etc/group", IMAGE_GROUP)
        put(self.image, "etc/shadow", IMAGE_SHADOW)
        put(self.image, "etc/gshadow", IMAGE_GSHADOW)
        put(self.image, "etc/foo.conf", "new")
        put(self.image, "etc/systemd/system/couchliteos-a.service", "")
        put(self.image, "var/lib/dpkg/status",
            "Package: base-files\nStatus: install ok installed\nArchitecture: amd64\n")
        (self.image / "usr/lib").mkdir(parents=True, exist_ok=True)
        put(self.target, "etc/couchliteos-version", "0.2.1\n")
        put(self.target, "usr/share/couchliteos/profile.conf", "PROFILE_NAME=general\n")
        put(self.target, "etc/passwd", TARGET_PASSWD)
        put(self.target, "etc/group", TARGET_GROUP)
        put(self.target, "etc/shadow", TARGET_SHADOW)
        put(self.target, "etc/gshadow", TARGET_GSHADOW)
        put(self.target, "var/lib/dpkg/status",
            "Package: base-files\nStatus: install ok installed\nArchitecture: amd64\n\n"
            "Package: grub-efi-amd64\nStatus: install ok installed\nArchitecture: amd64\nVersion: 2.12\n")
        put(self.target, "var/lib/dpkg/info/grub-efi-amd64.list",
            "/.\n/usr\n/usr/lib\n/usr/lib/grub\n/usr/lib/grub/x86_64-efi\n/usr/lib/grub/x86_64-efi/acpi.mod\n")
        put(self.target, "var/lib/dpkg/info/grub-efi-amd64.md5sums", "")
        put(self.target, "usr/lib/grub/x86_64-efi/acpi.mod", "x")
        put(self.target, "boot/vmlinuz-6.12.1", "k")
        put(self.target, "boot/initrd.img-6.12.1", "i")
        put(self.target, "boot/vmlinuz-6.12.2", "k")
        self.snapshots = {}
        self.filters = {}
        self.fail = {}
        self.saved = []
        self.save_error = None
        self.runner = Runner(self.script)
        self.env = make_env(self.tmp, runner=self.runner)
        self.status = RecordingStatus(self.tmp / "status.json")

    def read(self, rel):
        path = self.target / rel
        return path.read_text() if path.exists() else None

    def script(self, argv):
        if argv[0] == "rsync":
            name = argv[-1].removeprefix(str(self.target)).strip("/") or "system"
            self.snapshots.setdefault(
                "first rsync", {"passwd": self.read("etc/passwd"), "version": self.read("etc/couchliteos-version")})
            for part in argv:
                if part.startswith("--filter=merge "):
                    self.filters[name] = pathlib.Path(part.removeprefix("--filter=merge ")).read_text()
            self.snapshots[f"rsync {name}"] = {"status": self.read("var/lib/dpkg/status")}
        if argv[0] == "chroot":
            self.snapshots.setdefault("first chroot", {"version": self.read("etc/couchliteos-version")})
        if argv[0] == "sync":
            self.snapshots["sync"] = {
                "version": self.read("etc/couchliteos-version"), "status": self.read("var/lib/dpkg/status")}
        for key, reply in self.fail.items():
            if key in " ".join(argv):
                return reply
        return None

    def apply(self, **kwargs):
        kwargs.setdefault("save", self.save)
        return updater.apply_root(self.target, self.status, self.env, image=self.image, **kwargs)

    def save(self, env, status, root):
        self.saved.append((root, len(self.runner.calls), self.read("etc/passwd")))
        env.note("saved")
        if self.save_error:
            raise self.save_error

    def test_without_the_callers_snapshot_the_target_is_saved_before_anything_is_written(self):
        self.apply()
        self.assertEqual(self.saved, [(self.target, 0, TARGET_PASSWD)], "saved first, accounts still the box's")
        self.assertIn("saved", self.read("var/log/couchliteos-update/update.log"), "notes go to the box's own log")

    def test_with_the_callers_snapshot_nothing_is_saved_again(self):
        self.apply(save_first=False)
        self.assertEqual(self.saved, [])
        self.assertTrue(self.runner.commands("rsync"))

    def test_a_failed_save_leaves_the_target_untouched(self):
        self.save_error = updater.UpdateFailed("COULD NOT SAVE THE CURRENT VERSION")
        files = {path: path.read_bytes() for path in self.target.rglob("*") if path.is_file()
                 and "var/log" not in str(path)}
        with self.assertRaises(updater.UpdateFailed):
            self.apply()
        self.assertEqual(self.runner.calls, [])
        self.assertEqual({path: path.read_bytes() for path in self.target.rglob("*") if path.is_file()
                          and "var/log" not in str(path)}, files)
        self.assertEqual(self.read("etc/couchliteos-version"), "0.2.1\n")

    def test_a_refused_update_saves_nothing(self):
        put(self.target, "etc/couchliteos-version", "0.2.2\n")
        with self.assertRaises(updater.UpdateFailed):
            self.apply()
        self.assertEqual(self.saved, [])

    def test_the_real_save_works_on_the_target_and_honours_its_off_setting(self):
        put(self.target, "var/lib/couchliteos/config.ini", "[update]\nsnapshot = off\n")
        with mock.patch.object(updater.snapshot, "create") as create:
            self.apply(save=None)
        create.assert_not_called()
        (self.target / "var/lib/couchliteos/config.ini").unlink()
        with mock.patch.object(updater.snapshot, "create") as create:
            self.apply(save=None, force=True)
        env, _status, root = create.call_args.args
        self.assertEqual(root, self.target)
        self.assertEqual(env.log.path, self.target / "var/log/couchliteos-update/update.log")

    def test_an_old_caller_with_too_little_room_to_save_still_gets_the_update(self):
        # A 0.2.x updater runs this apply-root without --snapshot-done: its box has no switch to turn
        # saving off, so too little room for the copy must not block the update forever.
        self.env.free_bytes = lambda _path: 4 * GIB  # enough to install, too little to save first
        self.env.euid = os.geteuid
        self.apply(save=None)
        self.assertEqual(self.read("etc/couchliteos-version"), "0.2.2\n")
        self.assertIn(updater.MSG_NO_ROOM_OLD_CALLER, [message for _phase, message, _ in self.status.history])
        log = self.read("var/log/couchliteos-update/update.log")
        self.assertIn(updater.MSG_NO_ROOM_OLD_CALLER, log)
        self.assertIn("NOT ENOUGH FREE SPACE TO SAVE THE CURRENT VERSION: NEED", log)
        self.assertIsNone(updater.snapshot.info(self.target))
        self.assertEqual(self.status.phases()[-1], "restarting")

    def test_an_old_caller_still_stops_for_any_other_failed_save(self):
        self.save_error = updater.UpdateFailed(updater.snapshot.MSG_FAILED)
        with self.assertRaises(updater.UpdateFailed) as caught:
            self.apply()
        self.assertEqual(caught.exception.message, updater.snapshot.MSG_FAILED)
        self.assertEqual(self.runner.commands("rsync"), [])

    def test_a_0_3_0_caller_with_too_little_room_is_refused_and_told_about_the_switch(self):
        self.env.free_bytes = lambda _path: 4 * GIB
        self.env.euid = os.geteuid
        with self.assertRaises(updater.UpdateFailed) as caught:
            updater.save_snapshot(self.env, self.status, self.target)
        self.assertIsInstance(caught.exception, updater.NoRoomToSave)
        self.assertIn("TURN OFF SAVE BEFORE UPDATE", caught.exception.message)
        # run and apply-iso call save_snapshot themselves, outside apply_root, so the refusal stands.
        self.assertTrue(issubclass(updater.NoRoomToSave, updater.UpdateFailed))

    def after_rsync(self):
        calls = self.runner.calls
        return calls[max(i for i, call in enumerate(calls) if call[0] == "rsync") + 1:]

    def test_the_four_syncs_run_first_and_in_order(self):
        self.apply()
        names = [call[-1] for call in self.runner.commands("rsync")]
        self.assertEqual(names, [
            f"{self.target}/", f"{self.target}/etc/", f"{self.target}/var/", f"{self.target}/var/lib/dpkg/"])

    def test_after_the_boot_menu_only_initrds_whose_kernel_is_gone_are_removed(self):
        put(self.target, "boot/initrd.img-6.1.0", "old")  # its vmlinuz went with the system step
        self.apply()
        self.assertEqual(sorted(p.name for p in (self.target / "boot").glob("initrd.img-*")), ["initrd.img-6.12.1"])

    def test_a_failed_boot_menu_removes_no_initrd(self):
        put(self.target, "boot/initrd.img-6.1.0", "old")
        self.fail = {"update-grub": (1, "")}
        with self.assertRaises(updater.UpdateFailed):
            self.apply()
        self.assertTrue((self.target / "boot/initrd.img-6.1.0").exists())

    def test_after_the_syncs_the_target_gets_its_boot_menu_rebuilt_in_a_chroot(self):
        t = str(self.target)
        self.apply()
        self.assertEqual(self.after_rsync(), [
            ["mount", "-t", "proc", "proc", f"{t}/proc"],
            ["mount", "--rbind", "/sys", f"{t}/sys"], ["mount", "--make-rslave", f"{t}/sys"],
            ["mount", "--rbind", "/dev", f"{t}/dev"], ["mount", "--make-rslave", f"{t}/dev"],
            ["mount", "--bind", "/run", f"{t}/run"], ["mount", "--make-rslave", f"{t}/run"],
            ["chroot", t, "ldconfig"],
            ["chroot", t, "update-initramfs", "-u", "-k", "6.12.1"],   # has an initrd already
            ["chroot", t, "update-initramfs", "-c", "-k", "6.12.2"],   # has none yet
            ["chroot", t, "update-grub"],
            ["umount", "-R", f"{t}/run"], ["umount", "-R", f"{t}/dev"], ["umount", "-R", f"{t}/sys"],
            ["umount", "-R", f"{t}/proc"],
            ["sync"],
        ])

    def test_accounts_are_merged_before_the_first_file_is_copied(self):
        self.apply()
        passwd = self.snapshots["first rsync"]["passwd"]
        self.assertIn("alice:x:1001", passwd)
        self.assertIn("sshd:x:106", passwd)
        self.assertEqual(self.read("etc/passwd"), passwd)

    def test_the_uid_remap_runs_with_the_computed_maps_before_the_first_copy(self):
        order = []
        real = updater.remap_ids

        def spy(target, uid_map, gid_map, **kwargs):
            order.append((dict(uid_map), dict(gid_map), len(self.runner.commands("rsync"))))
            return real(target, uid_map, gid_map, **kwargs)

        with mock.patch.object(updater, "remap_ids", spy):
            self.apply()
        self.assertEqual(order, [({105: 106}, {107: 109}, 0)])

    def test_the_version_file_changes_last_so_a_failed_update_can_be_retried(self):
        self.apply()
        self.assertEqual(self.snapshots["first rsync"]["version"], "0.2.1\n")
        self.assertEqual(self.snapshots["first chroot"]["version"], "0.2.1\n")
        self.assertEqual(self.snapshots["sync"]["version"], "0.2.2\n")
        self.assertEqual(self.read("etc/couchliteos-version"), "0.2.2\n")

    def test_dpkg_status_keeps_the_images_packages_plus_the_boxs_extras_after_the_copy(self):
        self.apply()
        self.assertEqual(list(updater.parse_dpkg_status(self.snapshots["sync"]["status"])),
                         [("base-files", "amd64"), ("grub-efi-amd64", "amd64")])
        self.assertIn("Version: 2.12", self.snapshots["sync"]["status"])  # the old stanza text

    def test_the_protect_filters_cover_the_extras_in_the_right_transfer_root(self):
        self.apply()
        system = self.filters["system"].splitlines()
        self.assertIn("P /usr/lib/grub", system)
        self.assertIn("P /usr/lib/grub/x86_64-efi/acpi.mod", system)
        self.assertFalse([line for line in system if "/var/lib/dpkg" in line])
        self.assertEqual(sorted(self.filters["var/lib/dpkg"].splitlines()),
                         ["P /info/grub-efi-amd64.list", "P /info/grub-efi-amd64.md5sums"])
        self.assertEqual(list(self.env.work_dir.glob("update-filter-*")), [], "temporary filter files are removed")

    def test_the_etc_manifest_is_written_for_the_next_update(self):
        self.apply()
        manifest = updater.read_manifest(self.target / "var/lib/couchliteos-update/etc-manifest")
        self.assertIn("foo.conf", manifest)
        self.assertIn("systemd/system/couchliteos-a.service", manifest)

    def test_stale_etc_files_from_the_previous_manifest_are_removed(self):
        put(self.target, "etc/gone.conf", "old")
        put(self.target, "etc/mine.conf", "mine")
        updater.write_manifest(self.target / "var/lib/couchliteos-update/etc-manifest", ["gone.conf", "foo.conf"])
        self.apply()
        self.assertFalse((self.target / "etc/gone.conf").exists())
        self.assertTrue((self.target / "etc/mine.conf").exists())

    def test_every_step_is_logged_in_the_targets_update_log(self):
        self.apply()
        log = (self.target / "var/log/couchliteos-update/update.log").read_text()
        for word in ("rsync", "update-grub", "0.2.1", "0.2.2"):
            self.assertIn(word, log)

    def test_status_ends_restarting_after_passing_through_installing(self):
        self.apply()
        self.assertEqual(self.status.phases()[-1], "restarting")
        self.assertEqual(set(self.status.phases()[:-1]), {"installing"})
        self.assertEqual(self.status.version, "0.2.2")

    def test_a_box_with_a_browser_gets_the_browser_refresh_marker(self):
        marker = self.target / "var/lib/couchliteos-update/browser-refresh"
        self.apply()
        self.assertFalse(marker.exists(), "no browser installed: nothing to refresh")
        status = self.read("var/lib/dpkg/status")
        put(self.target, "var/lib/dpkg/status",
            status + "\nPackage: firefox-esr\nStatus: install ok installed\nArchitecture: amd64\n")
        put(self.target, "etc/couchliteos-version", "0.2.1\n")
        self.apply()
        self.assertTrue(marker.exists())
        self.assertIn("firefox-esr", self.read("var/lib/dpkg/status"), "the browser stays a box package")

    def test_exit_code_24_from_rsync_is_fine(self):
        self.fail = {"/etc/": (24, "some files vanished")}
        self.apply()
        self.assertEqual(self.read("etc/couchliteos-version"), "0.2.2\n")

    def test_any_other_rsync_failure_stops_before_the_boot_menu_step(self):
        self.fail = {"/etc/": (23, "partial transfer")}
        with self.assertRaises(updater.UpdateFailed) as caught:
            self.apply()
        self.assertTrue(caught.exception.message.startswith("INSTALL FAILED WHILE COPYING FILES"))
        self.assertEqual(self.runner.commands("chroot"), [])
        self.assertEqual(self.read("etc/couchliteos-version"), "0.2.1\n")

    def test_a_failing_initramfs_is_reported_as_a_boot_menu_failure_and_still_unmounts(self):
        self.fail = {"update-initramfs": (1, "update-initramfs: failed")}
        with self.assertRaises(updater.UpdateFailed) as caught:
            self.apply()
        self.assertTrue(caught.exception.message.startswith("INSTALL FAILED WHILE SETTING UP THE BOOT MENU"))
        self.assertEqual(len(self.runner.commands("umount")), 4)
        self.assertEqual(self.read("etc/couchliteos-version"), "0.2.1\n")
        self.assertIn("update-initramfs: failed", (self.target / "var/log/couchliteos-update/update.log").read_text())

    def test_a_failing_update_grub_is_a_boot_menu_failure(self):
        self.fail = {"update-grub": (1, "")}
        with self.assertRaises(updater.UpdateFailed) as caught:
            self.apply()
        self.assertIn("BOOT MENU", caught.exception.message)

    def test_a_box_with_no_kernel_after_the_copy_is_a_failure_not_a_silent_success(self):
        for kernel in (self.target / "boot").glob("vmlinuz-*"):
            kernel.unlink()
        with self.assertRaises(updater.UpdateFailed) as caught:
            self.apply()
        self.assertIn("BOOT MENU", caught.exception.message)

    def test_preflight_refuses_without_writing_anything(self):
        before = self.read("etc/passwd")
        (self.target / "etc/couchliteos-version").unlink()
        with self.assertRaises(updater.UpdateFailed):
            self.apply()
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.read("etc/passwd"), before)

    def test_the_same_or_an_older_version_needs_force(self):
        for installed in ("0.2.2", "0.3.0"):
            put(self.target, "etc/couchliteos-version", installed + "\n")
            with self.assertRaises(updater.UpdateFailed) as caught:
                self.apply()
            self.assertEqual(caught.exception.message, "THIS UPDATE IS NOT NEWER THAN THE BOX")
            self.assertEqual(self.runner.calls, [])
            self.apply(force=True)
            self.assertEqual(self.read("etc/couchliteos-version"), "0.2.2\n")
            self.runner.calls.clear()

    def test_less_than_3_gib_free_is_refused(self):
        self.env.free_bytes = lambda path: 3 * GIB - 1
        with self.assertRaises(updater.UpdateFailed) as caught:
            self.apply()
        self.assertEqual(caught.exception.message, "NOT ENOUGH FREE SPACE: NEED 3 GB")
        self.assertEqual(self.runner.calls, [])

    def test_a_different_profile_is_refused_even_with_force(self):
        put(self.target, "usr/share/couchliteos/profile.conf", "PROFILE_NAME=nvidia\n")
        with self.assertRaises(updater.UpdateFailed) as caught:
            self.apply(force=True)
        self.assertEqual(caught.exception.message, "THIS ISO IS FOR A DIFFERENT KIND OF BOX")


# ---------------------------------------------------------------- the command line


class MainTest(TmpCase):
    def setUp(self):
        super().setUp()
        self.runner = Runner()
        self.env = make_env(self.tmp, runner=self.runner)
        self.status_path = self.env.run_dir / "update-status.json"

    def main(self, *argv):
        with contextlib.redirect_stderr(io.StringIO()):  # usage errors and refusals print to stderr
            return updater.main(list(argv), env=self.env)

    def test_run_hands_the_service_status_to_run(self):
        with mock.patch.object(updater, "run", return_value=0) as run:
            self.assertEqual(self.main("run"), 0)
        env, status = run.call_args.args
        self.assertIs(env, self.env)
        self.assertEqual(status.path, self.status_path)

    def test_apply_iso_passes_the_path_and_flags_and_restarts_unless_told_not_to(self):
        with mock.patch.object(updater, "apply_iso") as apply:
            self.assertEqual(self.main("apply-iso", "/dev/sr0", "--force", "--no-reboot"), 0)
            self.assertEqual(apply.call_args.args[0], pathlib.Path("/dev/sr0"))
            self.assertTrue(apply.call_args.kwargs["force"])
            self.assertNotIn(["systemctl", "reboot"], self.runner.calls)
            self.assertEqual(self.main("apply-iso", "/x.iso"), 0)
            self.assertFalse(apply.call_args.kwargs["force"])
            self.assertIn(["systemctl", "reboot"], self.runner.calls)

    def test_apply_iso_failure_is_a_failed_status_and_exit_1_without_a_restart(self):
        with mock.patch.object(updater, "apply_iso", side_effect=updater.UpdateFailed("UNSUPPORTED DISK LAYOUT")):
            self.assertEqual(self.main("apply-iso", "/x.iso"), 1)
        self.assertEqual(json.loads(self.status_path.read_text())["message"], "UNSUPPORTED DISK LAYOUT")
        self.assertNotIn(["systemctl", "reboot"], self.runner.calls)

    def test_no_snapshot_is_passed_on_by_run_and_apply_iso(self):
        with mock.patch.object(updater, "run", return_value=0) as run:
            self.main("run")
            self.assertTrue(run.call_args.kwargs["save_first"])
            self.main("run", "--no-snapshot")
            self.assertFalse(run.call_args.kwargs["save_first"])
        with mock.patch.object(updater, "apply_iso") as apply:
            self.main("apply-iso", "/x.iso", "--no-reboot")
            self.assertTrue(apply.call_args.kwargs["save_first"])
            self.main("apply-iso", "/x.iso", "--no-reboot", "--no-snapshot")
            self.assertFalse(apply.call_args.kwargs["save_first"])

    def test_delete_snapshot_deletes_and_leaves_the_update_status_alone(self):
        with mock.patch.object(updater.snapshot, "delete", return_value=True) as delete:
            self.assertEqual(self.main("delete-snapshot"), 0)
        self.assertEqual(delete.call_args.args, (self.env, self.env.root))
        self.assertFalse(self.status_path.exists())
        with mock.patch.object(updater.snapshot, "delete", return_value=False):
            self.assertEqual(self.main("delete-snapshot"), 1)

    def test_delete_snapshot_that_hits_a_disk_error_exits_1_and_says_why_in_the_log(self):
        logged = []
        self.env.log = logged.append
        with mock.patch.object(updater.snapshot, "delete", side_effect=PermissionError("read-only")):
            self.assertEqual(self.main("delete-snapshot"), 1)
        self.assertIn("snapshot: delete failed: read-only", logged)
        self.assertFalse(self.status_path.exists())

    def test_the_lock_is_in_roots_work_directory(self):
        with mock.patch.object(updater, "run", return_value=0):
            self.assertEqual(self.main("run"), 0)
        self.assertTrue((self.env.work_dir / "update.lock").is_file())
        self.assertEqual(stat.S_IMODE(os.lstat(self.env.work_dir).st_mode), 0o700)
        self.assertFalse((self.env.run_dir / "update.lock").exists())

    def test_a_symlinked_lock_is_refused(self):
        secret = put(self.tmp, "shadow", "root:x\n")
        updater.own_dir(self.env)
        (self.env.work_dir / "update.lock").symlink_to(secret)
        with mock.patch.object(updater, "run", return_value=0) as run:
            self.assertEqual(self.main("run"), 1)
        run.assert_not_called()
        self.assertEqual(secret.read_text(), "root:x\n")

    def test_apply_root_needs_a_status_file_and_passes_force(self):
        with mock.patch.object(updater, "apply_root") as apply:
            self.assertEqual(self.main("apply-root", "/mnt", "--status", "/run/couchliteos/s.json", "--force"), 0)
            target, status, env = apply.call_args.args
            self.assertEqual((target, status.path), (pathlib.Path("/mnt"), pathlib.Path("/run/couchliteos/s.json")))
            self.assertTrue(apply.call_args.kwargs["force"])
            with self.assertRaises(SystemExit):
                self.main("apply-root", "/mnt")

    def test_apply_root_saves_unless_the_caller_did_or_said_not_to(self):
        with mock.patch.object(updater, "apply_root") as apply:
            for flags, save_first in (((), True), (("--snapshot-done",), False), (("--no-snapshot",), False)):
                self.assertEqual(self.main("apply-root", "/mnt", "--status", str(self.tmp / "s.json"), *flags), 0)
                self.assertEqual(apply.call_args.kwargs["save_first"], save_first, flags)

    def test_this_updater_takes_its_own_snapshot_done_flag(self):
        self.assertTrue(updater.handles_snapshot_done(pathlib.Path(updater.__file__)))

    def test_apply_root_failure_is_written_to_the_given_status_file_and_exits_1(self):
        path = self.tmp / "s.json"
        with mock.patch.object(updater, "apply_root", side_effect=updater.UpdateFailed("X FAILED")):
            self.assertEqual(self.main("apply-root", "/mnt", "--status", str(path)), 1)
        self.assertEqual(json.loads(path.read_text())["phase"], "failed")

    def test_only_root_may_run_it(self):
        self.env.euid = lambda: 1000
        with mock.patch.object(updater, "run") as run:
            self.assertEqual(self.main("run"), 2)
        run.assert_not_called()
        self.assertFalse(self.status_path.exists())

    def test_a_second_update_while_one_runs_changes_nothing(self):
        with updater.acquire_lock(updater.own_dir(self.env) / "update.lock"):
            with mock.patch.object(updater, "run") as run:
                self.assertEqual(self.main("run"), 1)
        run.assert_not_called()
        self.assertFalse(self.status_path.exists(), "the running update's status must not be overwritten")

    def test_restore_runs_the_boot_restore_under_the_lock(self):
        with mock.patch.object(updater, "restore_at_boot", return_value=0) as restore:
            self.assertEqual(self.main("restore"), 0)
        env, status = restore.call_args.args
        self.assertEqual(status.path, self.status_path)
        with updater.acquire_lock(updater.own_dir(self.env) / "update.lock"):
            with mock.patch.object(updater, "restore_at_boot") as restore:
                self.assertEqual(self.main("restore"), 1)
            restore.assert_not_called()

    def test_request_restore_restarts_only_when_the_request_was_made(self):
        with mock.patch.object(updater, "request_restore"):
            self.assertEqual(self.main("request-restore"), 0)
        self.assertEqual(self.runner.calls[-1], ["systemctl", "reboot"])
        self.runner.calls.clear()
        with mock.patch.object(updater, "request_restore", side_effect=updater.UpdateFailed(updater.MSG_NO_SAVED)):
            self.assertEqual(self.main("request-restore"), 1)
        self.assertNotIn(["systemctl", "reboot"], self.runner.calls)
        self.assertEqual(json.loads(self.status_path.read_text())["message"], updater.MSG_NO_SAVED)

    def test_apply_root_restore_is_passed_on_and_prints_its_progress(self):
        with mock.patch.object(updater, "apply_root") as apply:
            self.main("apply-root", "/mnt", "--status", str(self.tmp / "s.json"), "--force", "--snapshot-done",
                      "--restore")
            self.assertTrue(apply.call_args.kwargs["restore"])
            self.assertFalse(apply.call_args.kwargs["save_first"])
            self.assertIsNotNone(apply.call_args.args[1].echo)
            self.main("apply-root", "/mnt", "--status", str(self.tmp / "s.json"))
            self.assertFalse(apply.call_args.kwargs["restore"])
            self.assertIsNone(apply.call_args.args[1].echo)

    def test_unknown_commands_are_a_usage_error(self):
        with self.assertRaises(SystemExit):
            self.main("explode")


# ---------------------------------------------------------------- restore (the saved previous version)


class RestoreStepsTest(unittest.TestCase):
    def steps(self, restore):
        return updater.rsync_steps(pathlib.Path("/"), pathlib.Path("/mnt"), pathlib.Path("/run/f/a.rules"),
                                   pathlib.Path("/run/f/e.rules"), restore)

    def test_a_restore_adds_the_state_step_last(self):
        self.assertEqual([name for name, _ in self.steps(True)], ["system", "etc", "var", "dpkg", "state"])
        self.assertEqual(self.steps(True)[:4], self.steps(False), "the four update steps are unchanged")

    def test_the_state_step_brings_the_launchers_state_back_but_never_the_snapshot(self):
        argv = dict(self.steps(True))["state"]
        self.assertEqual(argv, [*BASE, "--delete-after", "/var/lib/couchliteos/", "/mnt/var/lib/couchliteos/"])
        self.assertIn("/lib/couchliteos/", updater.VAR_EXCLUDES, "only the state step touches it")
        # The snapshot is root's own, outside the launcher's state, and no step copies over it.
        self.assertFalse(updater.snapshot.SNAP_REL.startswith(updater.STATE_REL + "/"))
        self.assertTrue(updater.snapshot.SNAP_REL.startswith("var/lib/couchliteos-update/"))
        self.assertIn("/lib/couchliteos-update/", updater.VAR_EXCLUDES)
        self.assertIn("/var/", updater.SYSTEM_EXCLUDES)


class ApplyRootRestoreTest(TmpCase):
    """apply_root with restore=True: the image is the snapshot, mounted, of an older version."""

    def setUp(self):
        super().setUp()
        ApplyRootTest.make_trees(self)

    read = ApplyRootTest.read
    script = ApplyRootTest.script

    def restore(self, **kwargs):
        put(self.image, "etc/couchliteos-version", "0.2.0\n")
        put(self.target, "var/lib/couchliteos/config.ini", "[power]\nblank_minutes = 10\n")
        self.env.wall_clock = lambda: 1_790_000_000.0
        kwargs.setdefault("save_first", False)
        return updater.apply_root(self.target, self.status, self.env, image=self.image, restore=True, **kwargs)

    def test_an_older_version_is_fine_without_force(self):
        self.restore()
        self.assertEqual(self.read("etc/couchliteos-version"), "0.2.0\n")
        self.assertEqual(self.status.version, "0.2.0")
        put(self.target, "etc/couchliteos-version", "0.2.1\n")
        with self.assertRaises(updater.UpdateFailed):
            updater.apply_root(self.target, self.status, self.env, image=self.image, save_first=False)

    def test_another_kind_of_box_is_still_refused(self):
        put(self.image, "usr/share/couchliteos/profile.conf", "PROFILE_NAME=nvidia\n")
        with self.assertRaises(updater.UpdateFailed):
            self.restore()
        self.assertEqual(self.runner.calls, [])

    def test_five_syncs_the_last_one_the_state(self):
        self.restore()
        self.assertEqual([call[-1] for call in self.runner.commands("rsync")], [
            f"{self.target}/", f"{self.target}/etc/", f"{self.target}/var/", f"{self.target}/var/lib/dpkg/",
            f"{self.target}/var/lib/couchliteos/"])
        self.assertIn("RESTORING YOUR SETTINGS...", [message for _phase, message, _ in self.status.history])

    def test_nothing_only_the_box_has_is_kept(self):
        self.restore()
        self.assertEqual(self.filters, {"system": "", "var/lib/dpkg": ""})
        self.assertEqual(list(updater.parse_dpkg_status(self.read("var/lib/dpkg/status"))), [("base-files", "amd64")])

    def test_the_update_check_pauses_a_week_and_whats_new_counts_as_seen(self):
        self.restore()
        self.assertEqual(self.read("var/lib/couchliteos/config.ini"),
                         f"[power]\nblank_minutes = 10\n\n[update]\npaused_until = {1_790_000_000 + 7 * 86400}\n")
        self.assertEqual(self.read("var/lib/couchliteos/whatsnew-seen"), "0.2.0\n")
        self.assertEqual(update.paused_until(self.target / "var/lib/couchliteos/config.ini"), 1_790_604_800)

    def test_a_restore_whose_courtesies_fail_still_finishes_and_says_so_in_the_log(self):
        with mock.patch.object(updater.snapshot, "set_option", side_effect=PermissionError("read-only")):
            self.restore()
        self.assertEqual(self.read("etc/couchliteos-version"), "0.2.0\n")
        self.assertIsNone(self.read("var/lib/couchliteos/whatsnew-seen"))
        self.assertIn("restore: could not pause the update check or mark What's New: read-only",
                      self.read("var/log/couchliteos-update/update.log"))
        self.assertEqual(self.status.phases()[-1], "restarting")

    def test_whats_new_is_written_through_the_directory_never_a_planted_link(self):
        secret = put(self.tmp, "shadow", "root:x\n")
        link(self.target, "var/lib/couchliteos/whatsnew-seen", secret)
        self.restore()
        self.assertEqual(secret.read_text(), "root:x\n")
        self.assertFalse((self.target / "var/lib/couchliteos/whatsnew-seen").is_symlink())
        self.assertEqual(self.read("var/lib/couchliteos/whatsnew-seen"), "0.2.0\n")

    def test_the_log_says_restore(self):
        self.restore()
        log = self.read("var/log/couchliteos-update/update.log")
        self.assertIn("restore to 0.2.0 from 0.2.1", log)
        self.assertIn("restore to 0.2.0 done", log)
        self.assertEqual(self.status.phases()[-1], "restarting")

    def test_a_restore_is_never_a_legacy_update(self):
        with self.assertRaises(ValueError):
            self.restore(legacy=True)
        self.assertEqual(self.runner.calls, [])

    def test_a_state_step_that_fails_stops_before_the_boot_menu(self):
        self.fail["var/lib/couchliteos/"] = (23, "partial transfer")
        with self.assertRaises(updater.UpdateFailed) as caught:
            self.restore()
        self.assertIn("(STATE)", caught.exception.message)
        self.assertEqual(self.runner.commands("chroot"), [])
        self.assertEqual(self.read("etc/couchliteos-version"), "0.2.1\n")


class EchoStatusTest(TmpCase):
    def test_each_new_message_is_printed_once(self):
        said = []
        status = updater.Status(self.tmp / "s.json", echo=said.append)
        for message, percent in (("COPYING SYSTEM FILES...", 10), ("COPYING SYSTEM FILES...", 20), ("DONE", 100)):
            status.set("installing", message, percent)
        self.assertEqual(said, ["COPYING SYSTEM FILES...", "DONE"])


def make_snapshot(root, version="0.2.0", image=b"saved image"):
    directory = pathlib.Path(root) / "var/lib/couchliteos-update/snapshot"
    directory.mkdir(parents=True)
    directory.chmod(0o711)
    (directory / "previous.squashfs").write_bytes(image)
    (directory / "previous.json").write_text(json.dumps({
        "version": version, "date": "2026-10-02T09:00:00Z", "size": len(image),
        "sha256": hashlib.sha256(image).hexdigest(), "kernel": "6.12.1"}))
    put(root, "boot/couchliteos-previous/vmlinuz", "k")
    put(root, "boot/couchliteos-previous/initrd.img", "i")
    put(root, "boot/couchliteos-previous/modules/squashfs.ko.xz", "m")
    put(root, "boot/couchliteos-previous/modules/loop.ko.xz", "m")
    put(root, "boot/couchliteos-previous/modules/load-order", "squashfs.ko.xz\nloop.ko.xz\n")
    return directory


class RestoreTest(TmpCase):
    def setUp(self):
        super().setUp()
        self.fail = {}
        self.seen = {}
        self.runner = Runner(self.script)
        self.env = make_env(self.tmp, runner=self.runner, euid=os.geteuid,
                            sleep=self.slept)
        self.sleeps = []
        put(self.env.root, "etc/couchliteos-version", "0.2.1\n")
        self.snap = make_snapshot(self.env.root)
        self.status = RecordingStatus(self.env.run_dir / "update-status.json")
        self.held = self.env.root / "var/lib/couchliteos-update/restore.squashfs"

    def slept(self, seconds):
        self.sleeps.append(seconds)

    def script(self, argv):
        if argv[0] == "mount" and "squashfs" in argv:
            self.seen["mounted"] = pathlib.Path(argv[-2]).read_bytes()
        if argv[0] == "chroot":
            tool = self.tmp / "work/restore/tool"
            self.seen["tool"] = sorted(path.name for path in tool.iterdir())
        for key, reply in self.fail.items():
            if key in " ".join(argv):
                return reply
        return None

    def mounts(self):
        return [call for call in self.runner.calls if call[0] in ("mount", "umount", "chroot", "modprobe", "insmod")]

    def test_the_whole_restore_in_order(self):
        self.assertEqual(updater.restore(self.env, self.status), "0.2.0")
        r = str(self.tmp / "work/restore/root")
        self.assertEqual(self.mounts(), [
            ["modprobe", "-a", "squashfs", "loop"],
            ["mount", "-t", "squashfs", "-o", "ro,loop", str(self.held), r],
            ["mount", "--bind", "/", f"{r}/mnt"], ["mount", "--make-rslave", f"{r}/mnt"],
            ["mount", "-t", "proc", "proc", f"{r}/proc"],
            ["mount", "--rbind", "/sys", f"{r}/sys"], ["mount", "--make-rslave", f"{r}/sys"],
            ["mount", "--rbind", "/dev", f"{r}/dev"], ["mount", "--make-rslave", f"{r}/dev"],
            ["mount", "--bind", "/run", f"{r}/run"], ["mount", "--make-rslave", f"{r}/run"],
            ["chroot", r, "python3", f"{self.tmp}/work/restore/tool/couchliteos_updater.py", "apply-root", "/mnt",
             "--status", str(self.status.path), "--force", "--snapshot-done", "--restore"],
            ["umount", "-R", f"{r}/run"], ["umount", "-R", f"{r}/dev"], ["umount", "-R", f"{r}/sys"],
            ["umount", "-R", f"{r}/proc"], ["umount", "-R", f"{r}/mnt"], ["umount", "-R", r],
        ])
        self.assertEqual([call[0] for call in self.runner.calls[:3]], ["findmnt"] * 3, "the layout is checked first")

    def test_the_held_link_of_the_checked_image_is_mounted_then_removed(self):
        updater.restore(self.env, self.status)
        self.assertEqual(self.seen["mounted"], b"saved image")
        self.assertFalse(os.path.lexists(self.held))
        self.assertTrue((self.snap / "previous.squashfs").exists(), "the snapshot itself stays")

    def test_todays_updater_and_its_modules_run_inside_and_are_removed_afterwards(self):
        updater.restore(self.env, self.status)
        self.assertEqual(self.seen["tool"], ["couchliteos_browser.py", "couchliteos_safefile.py",
                                             "couchliteos_snapshot.py", "couchliteos_update.py",
                                             "couchliteos_updater.py"])
        self.assertFalse((self.tmp / "work/restore/tool").exists())

    def test_the_copied_tool_is_everything_the_updater_imports(self):
        import ast
        names = {path.stem for path in updater.TOOL}
        for path in updater.TOOL:
            tree = ast.parse(path.read_text())
            imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
            imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
            ours = {name for name in imported if name.startswith("couchliteos_")}
            self.assertLessEqual(ours, names - {"couchliteos-updater"} | {"couchliteos_updater"}, path)

    def test_without_the_kernels_modules_the_saved_ones_are_loaded_in_order(self):
        self.fail["modprobe"] = (1, "modprobe: FATAL: Module squashfs not found")
        updater.restore(self.env, self.status)
        modules = self.env.root / "boot/couchliteos-previous/modules"
        self.assertEqual(self.mounts()[1:3], [["insmod", str(modules / "squashfs.ko.xz")],
                                              ["insmod", str(modules / "loop.ko.xz")]])

    def test_no_or_a_damaged_snapshot_is_refused_before_any_mount(self):
        (self.snap / "previous.squashfs").write_bytes(b"saved imagf")
        with self.assertRaises(updater.UpdateFailed) as caught:
            updater.restore(self.env, self.status)
        self.assertEqual(caught.exception.message, updater.MSG_NO_SAVED)
        self.assertEqual(self.mounts(), [])

    def test_a_snapshot_folder_that_is_not_roots_is_refused(self):
        self.snap.chmod(0o777)
        with self.assertRaises(updater.UpdateFailed):
            updater.restore(self.env, self.status)
        self.assertEqual(self.mounts(), [])

    def test_live_is_refused(self):
        (self.env.root / "run/live/medium").mkdir(parents=True)
        with self.assertRaises(updater.UpdateFailed):
            updater.restore(self.env, self.status)
        self.assertEqual(self.runner.calls, [])

    def test_the_inner_updaters_failure_is_passed_on_and_everything_is_unwound(self):
        def failing(argv):
            if argv[0] == "chroot":
                updater.Status(self.status.path).set("failed", "INSTALL FAILED WHILE COPYING FILES (STATE).")
                return (1, "")
            return None

        self.runner.script = failing
        with self.assertRaises(updater.UpdateFailed) as caught:
            updater.restore(self.env, self.status)
        self.assertEqual(caught.exception.message, "INSTALL FAILED WHILE COPYING FILES (STATE).")
        self.assertEqual(self.runner.calls[-1], ["umount", "-R", str(self.tmp / "work/restore/root")])
        self.assertFalse(os.path.lexists(self.held))

    def test_at_boot_success_drops_the_request_and_restarts(self):
        self.assertTrue(updater.snapshot.request(self.env, self.env.root))
        said = []
        self.status.echo = said.append
        self.assertEqual(updater.restore_at_boot(self.env, self.status), 0)
        self.assertFalse(updater.snapshot.requested(self.env.root))
        self.assertEqual(self.runner.calls[-1], ["systemctl", "reboot"])
        self.assertEqual(said[0], "RESTORING THE SAVED VERSION. KEEP THE BOX PLUGGED IN.")
        self.assertEqual(said[-1], "RESTORED COUCHLITEOS 0.2.0. RESTARTING...")
        self.assertEqual(self.sleeps, [])

    def test_at_boot_a_failure_before_any_write_says_why_drops_the_request_and_lets_the_box_start(self):
        self.assertTrue(updater.snapshot.request(self.env, self.env.root))
        self.fail["squashfs"] = (32, "")  # the saved image cannot be mounted: nothing was written
        said = []
        self.status.echo = said.append
        self.assertEqual(updater.restore_at_boot(self.env, self.status), 1)
        self.assertFalse(updater.snapshot.requested(self.env.root))
        self.assertFalse((self.snap / updater.snapshot.ATTEMPTS).exists())
        self.assertNotIn("chroot", [call[0] for call in self.runner.calls])
        self.assertNotIn(["systemctl", "reboot"], self.runner.calls)
        self.assertIn("COULD NOT OPEN THE SAVED VERSION", said)
        self.assertEqual(said[-1], "THE BOX STARTS AS IT IS. TRY AGAIN FROM SETTINGS > SOFTWARE UPDATE OR THE BOOT MENU.")
        self.assertEqual(self.sleeps, [updater.RESTORE_FAILED_PAUSE])

    def test_at_boot_a_failure_during_the_copy_keeps_the_request_for_one_more_start(self):
        self.assertTrue(updater.snapshot.request(self.env, self.env.root))
        self.fail["chroot"] = (1, "")  # the copy inside ran and failed: the box may be half written
        said = []
        self.status.echo = said.append
        self.assertEqual(updater.restore_at_boot(self.env, self.status), 1)
        self.assertTrue(updater.snapshot.requested(self.env.root), "the next start tries again")
        attempts = self.snap / updater.snapshot.ATTEMPTS
        self.assertEqual(attempts.read_text(), "1\n")
        self.assertEqual(stat.S_IMODE(attempts.stat().st_mode), 0o600)
        self.assertNotIn(["systemctl", "reboot"], self.runner.calls)
        self.assertIn(updater.MSG_RESTORE, said)
        self.assertEqual(said[-1], updater.MSG_RESTORE_RETRY)
        self.assertEqual(self.sleeps, [updater.RESTORE_FAILED_PAUSE])

        said.clear()  # the second start fails the same way: the request goes, the screen says why
        self.assertEqual(updater.restore_at_boot(self.env, self.status), 1)
        self.assertFalse(updater.snapshot.requested(self.env.root))
        self.assertFalse(attempts.exists())
        self.assertEqual(said[-1], updater.MSG_RESTORE_GAVE_UP)
        self.assertIn("REINSTALLING", updater.MSG_RESTORE_GAVE_UP)
        self.assertNotIn(["systemctl", "reboot"], self.runner.calls)

    def test_at_boot_a_retry_that_works_drops_the_request_and_its_count(self):
        self.status.echo = [].append
        self.assertTrue(updater.snapshot.request(self.env, self.env.root))
        self.fail["chroot"] = (1, "")
        updater.restore_at_boot(self.env, self.status)
        self.assertTrue((self.snap / updater.snapshot.ATTEMPTS).exists())
        del self.fail["chroot"]
        self.assertEqual(updater.restore_at_boot(self.env, self.status), 0)
        self.assertFalse(updater.snapshot.requested(self.env.root))
        self.assertFalse((self.snap / updater.snapshot.ATTEMPTS).exists())
        self.assertEqual(self.runner.calls[-1], ["systemctl", "reboot"])

    def test_at_boot_from_the_boot_menu_without_a_request_nothing_is_counted(self):
        self.status.echo = [].append
        self.fail["chroot"] = (1, "")
        self.assertEqual(updater.restore_at_boot(self.env, self.status), 1)
        self.assertFalse(updater.snapshot.requested(self.env.root))
        self.assertFalse((self.snap / updater.snapshot.ATTEMPTS).exists())

    def test_the_work_directory_is_roots_own_and_a_symlink_there_is_refused(self):
        updater.restore(self.env, self.status)
        work = self.env.work_dir
        self.assertEqual(stat.S_IMODE(os.lstat(work).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(work / "restore").st_mode), 0o700)
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        shutil.rmtree(work / "restore")
        (work / "restore").symlink_to(elsewhere)
        self.runner.calls.clear()
        with self.assertRaises(updater.UpdateFailed) as caught:
            updater.restore(self.env, self.status)
        self.assertEqual(caught.exception.message, updater.MSG_GENERIC)
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertNotIn("mount", [call[0] for call in self.runner.calls])

    def test_settings_request_needs_a_snapshot_and_then_restarts(self):
        updater.request_restore(self.env, self.status)
        self.assertTrue(updater.snapshot.requested(self.env.root))
        self.assertEqual(self.status.phases()[-1], "restarting")
        self.assertEqual(self.status.version, "0.2.0")
        updater.snapshot.delete(self.env, self.env.root)
        with self.assertRaises(updater.UpdateFailed):
            updater.request_restore(self.env, self.status)


@unittest.skipUnless(shutil.which("rsync"), "needs rsync")
class RealRsyncRestoreTest(TmpCase):
    """apply_root(restore=True) with the real rsync on small trees (chroot and mounts are faked)."""

    def setUp(self):
        super().setUp()
        self.image, self.target = self.tmp / "image", self.tmp / "target"
        for root, version, kernel in ((self.image, "0.2.0", "6.12.1"), (self.target, "0.3.0", "6.12.9")):
            put(root, "etc/couchliteos-version", version + "\n")
            put(root, "usr/share/couchliteos/profile.conf", "PROFILE_NAME=general\n")
            put(root, "etc/passwd", IMAGE_PASSWD)
            put(root, "etc/group", IMAGE_GROUP)
            put(root, "etc/shadow", IMAGE_SHADOW)
            put(root, "etc/gshadow", IMAGE_GSHADOW)
            put(root, "var/lib/dpkg/status", "Package: base-files\nStatus: install ok installed\nArchitecture: amd64\n")
            put(root, f"boot/vmlinuz-{kernel}", version)
            put(root, f"boot/initrd.img-{kernel}", version)
            put(root, "usr/bin/tool", version)
        for path in self.image.rglob("*"):  # older files, as a saved version's are
            os.utime(path, (1_700_000_000, 1_700_000_000), follow_symlinks=False)
        put(self.image, "var/lib/couchliteos/config.ini", "[qemu-smoke]\ncold_boot = verified\n")
        put(self.image, "var/lib/couchliteos/home/.config/moonlight/hosts", "old pairing")
        put(self.target, "var/lib/couchliteos/config.ini", "[qemu-smoke]\ncold_boot = verified\nafter = yes\n")
        put(self.target, "var/lib/couchliteos/added-later.json", "{}")
        make_snapshot(self.target)
        put(self.target, "var/lib/couchliteos-update/snapshot/restore-request")
        put(self.target, "boot/couchliteos-previous/vmlinuz", "saved kernel")
        put(self.target, "usr/bin/new-only", "0.3.0")
        put(self.target, "etc/fstab", "UUID=box / ext4\n")

        def runner(argv, **kwargs):
            if argv[0] == "rsync":
                return subprocess.run(argv, **kwargs)
            return subprocess.CompletedProcess(argv, 0, stdout="")

        self.env = make_env(self.tmp, runner=runner, euid=os.geteuid)

    def test_the_box_is_the_saved_version_again_with_its_settings(self):
        updater.apply_root(self.target, RecordingStatus(self.tmp / "s.json"), self.env, image=self.image,
                           save_first=False, restore=True)
        read = lambda rel: (self.target / rel).read_text()  # noqa: E731
        self.assertEqual(read("etc/couchliteos-version"), "0.2.0\n")
        self.assertEqual(read("usr/bin/tool"), "0.2.0")
        self.assertFalse((self.target / "usr/bin/new-only").exists())
        self.assertEqual(sorted(path.name for path in (self.target / "boot").glob("vmlinuz-*")), ["vmlinuz-6.12.1"])
        self.assertEqual(read("boot/couchliteos-previous/vmlinuz"), "saved kernel")
        self.assertTrue(read("var/lib/couchliteos/config.ini").startswith("[qemu-smoke]\ncold_boot = verified\n\n"))
        self.assertNotIn("after = yes", read("var/lib/couchliteos/config.ini"))
        self.assertIn("paused_until = ", read("var/lib/couchliteos/config.ini"))
        self.assertEqual(read("var/lib/couchliteos/home/.config/moonlight/hosts"), "old pairing")
        self.assertFalse((self.target / "var/lib/couchliteos/added-later.json").exists())
        self.assertTrue((self.target / "var/lib/couchliteos-update/snapshot/previous.squashfs").exists())
        self.assertTrue((self.target / "var/lib/couchliteos-update/snapshot/restore-request").exists())
        self.assertEqual(read("etc/fstab"), "UUID=box / ext4\n", "the box's own /etc files stay")


class GrubInstallTest(unittest.TestCase):
    def test_a_bios_box_whose_disk_cannot_be_found_is_refused(self):
        for reply in ((1, ""), (0, ""), (0, "sda\n")):
            env = make_env(pathlib.Path("/nonexistent"), runner=Runner(lambda _argv, reply=reply: reply))
            with self.assertRaises(updater.UpdateFailed) as caught:
                updater.grub_install_argv(env, "/dev/sda2", "")
            self.assertEqual(caught.exception.message, updater.MSG_DISK_LAYOUT, reply)
            self.assertEqual(env.runner.calls, [["lsblk", "-n", "-p", "-o", "PKNAME", "/dev/sda2"]])

    def test_a_bios_box_gets_its_whole_disk_and_uefi_needs_no_lookup(self):
        env = make_env(pathlib.Path("/nonexistent"), runner=Runner(lambda _argv: (0, "/dev/sda\n")))
        self.assertEqual(updater.grub_install_argv(env, "/dev/sda2", ""), ["grub-install", "--target=i386-pc", "/dev/sda"])
        env.runner.calls.clear()
        self.assertEqual(updater.grub_install_argv(env, "/dev/sda2", "UUID=ABCD")[1], "--target=x86_64-efi")
        self.assertEqual(env.runner.calls, [])


class DefaultsTest(unittest.TestCase):
    def test_defaults_are_the_documented_paths(self):
        env = updater.Env()
        self.assertEqual(env.run_dir, pathlib.Path("/run/couchliteos"))
        self.assertEqual(env.cache_dir, pathlib.Path("/var/cache/couchliteos/update"))
        self.assertEqual(env.root, pathlib.Path("/"))

    def test_the_default_opener_refuses_plain_http_redirects(self):
        handlers = [type(handler) for handler in updater.build_opener().handlers]
        self.assertIn(updater.HttpsOnlyRedirect, handlers)
        self.assertNotIn(urllib.request.HTTPRedirectHandler, handlers)


if __name__ == "__main__":
    unittest.main()
