"""apply-disk: the live USB stick updates an installed system, including 0.2.0 and MoonlightOS 0.1.x.

The legacy steps run for real on fake 0.1.13 and 0.2.0 root trees in a temp directory; only the
commands (mount, rsync, chroot) are recorded instead of run.
"""

import testenv  # noqa: F401  (first: scratch run and state directories)
import contextlib
import io
import json
import os
import pathlib
import shutil
import stat
import unittest
from unittest import mock

import couchliteos_update as update
import couchliteos_updater as updater
from test_softwareupdate import ENTER, ESC, DOWN, UiTest, flat
from test_updater import (
    IMAGE_GSHADOW, IMAGE_SHADOW, RecordingStatus, Runner, TmpCase, link, make_env, put,
)

# The box's old moonlightos group: a gid the test files can really carry (root may set any).
OLD_GID = 4242 if os.geteuid() == 0 else os.getgid()
NEW_GID = 3333 if OLD_GID != 3333 else 3334
IMAGE_PASSWD = """root:x:0:0:root:/root:/bin/bash
couchliteos:x:1000:1000:CouchLiteOS:/var/lib/couchliteos/home:/bin/bash
"""
IMAGE_GROUP = f"""root:x:0:
shadow:x:42:
video:x:44:couchliteos
couchliteos:x:{NEW_GID}:
"""
OLD_PASSWD = """root:x:0:0:root:/root:/bin/bash
moonlightos:x:1000:{gid}:MoonlightOS:/var/lib/moonlightos/home:/bin/bash
installer-test:x:1001:1001::/home/installer-test:/bin/bash
""".format(gid=OLD_GID)  # rename:keep
OLD_GROUP = f"""root:x:0:
shadow:x:42:
video:x:44:moonlightos
moonlightos:x:{OLD_GID}:installer-test
installer-test:x:1001:
"""  # rename:keep
OLD_SHADOW = "root:*:1:0:99999:7:::\nmoonlightos:!:1:0:99999:7:::\ninstaller-test:$6$x$y:1:0:99999:7:::\n"
OLD_GSHADOW = "root:*::\nmoonlightos:!::installer-test\ninstaller-test:!::\n"
OLD_CONFIG = """[launcher]
log = /var/log/moonlightos/launcher.log
home = /var/lib/moonlightos/home
other = /var/lib/moonlightos-extra/keep
"""  # rename:keep
FSTAB_EFI = "# /etc/fstab\nUUID=1111 / ext4 errors=remount-ro 0 1\nUUID=AB-CD /boot/efi vfat umask=0077 0 1\n"
LSBLK = {"blockdevices": [
    {"name": "/dev/sda", "type": "disk", "fstype": None, "mountpoint": None, "children": [
        {"name": "/dev/sda1", "type": "part", "fstype": "vfat", "mountpoint": None},
        {"name": "/dev/sda2", "type": "part", "fstype": "ext4", "mountpoint": None},
        {"name": "/dev/sda3", "type": "part", "fstype": "swap", "mountpoint": None},
    ]},
    {"name": "/dev/sdb", "type": "disk", "fstype": None, "mountpoint": None, "children": [
        {"name": "/dev/sdb1", "type": "part", "fstype": "ext4", "mountpoint": None},
        {"name": "/dev/sdb2", "type": "part", "fstype": "ext4", "mountpoint": "/run/live/persistence/sdb2"},
    ]},
    {"name": "/dev/sr0", "type": "rom", "fstype": "iso9660", "mountpoint": "/run/live/medium"},
]}


def image_tree(root, version="0.3.0", profile="general"):
    put(root, "etc/couchliteos-version", f"{version}\n")
    put(root, "usr/share/couchliteos/profile.conf", f"PROFILE_NAME={profile}\nISO_SUFFIX=\n")
    put(root, "etc/passwd", IMAGE_PASSWD)
    put(root, "etc/group", IMAGE_GROUP)
    put(root, "etc/shadow", IMAGE_SHADOW)
    put(root, "etc/gshadow", IMAGE_GSHADOW)
    put(root, "etc/grub.d/01_couchliteos_bootcheck", "#!/bin/sh\n")
    put(root, "etc/default/grub.d/20-couchliteos.cfg", "GRUB_TIMEOUT=3\n")
    put(root, "usr/lib/systemd/system/couchliteos-launcher.service", "[Unit]\n")
    put(root, "var/lib/dpkg/status", "Package: base-files\nStatus: install ok installed\nArchitecture: amd64\n")


def moonlightos_0113(root):
    """A MoonlightOS 0.1.13 box as its installer left it, with an owner's state."""  # rename:keep
    put(root, "etc/moonlightos-version", "0.1.13\n")  # rename:keep
    put(root, "usr/share/moonlightos/profile.conf", "PROFILE_NAME=general\n")  # rename:keep
    put(root, "etc/fstab", FSTAB_EFI)
    put(root, "etc/passwd", OLD_PASSWD)
    put(root, "etc/group", OLD_GROUP)
    put(root, "etc/shadow", OLD_SHADOW)
    put(root, "etc/gshadow", OLD_GSHADOW)
    put(root, "etc/default/grub.d/20-moonlightos.cfg", "GRUB_TIMEOUT=0\n")  # rename:keep
    put(root, "etc/NetworkManager/system-connections/home.nmconnection", "[connection]\nid=home\n", 0o600)
    put(root, "usr/lib/systemd/system/moonlightos-launcher.service", "[Unit]\n")  # rename:keep
    put(root, "usr/lib/systemd/system/moonlightos-usbip@.service", "[Unit]\n")  # rename:keep
    put(root, "usr/lib/systemd/system/ssh.service", "[Unit]\n")
    link(root, "lib", "usr/lib")
    link(root, "etc/systemd/system/graphical.target.wants/moonlightos-launcher.service",  # rename:keep
         "/lib/systemd/system/moonlightos-launcher.service")  # rename:keep
    link(root, "etc/systemd/system/multi-user.target.wants/moonlightos-usbip@sda.service",  # rename:keep
         "/lib/systemd/system/moonlightos-usbip@.service")  # rename:keep
    link(root, "etc/systemd/system/multi-user.target.wants/ssh.service", "/lib/systemd/system/ssh.service")
    put(root, "etc/systemd/system/moonlightos-mine.service", "[Unit]\n")  # rename:keep  (the owner's own unit)
    link(root, "etc/systemd/system/multi-user.target.wants/moonlightos-mine.service",  # rename:keep
         "/etc/systemd/system/moonlightos-mine.service")  # rename:keep
    put(root, "var/lib/moonlightos/config.ini", OLD_CONFIG, 0o640)  # rename:keep
    put(root, "var/lib/moonlightos/home/.config/Moonlight Game Streaming Project/Moonlight.conf",  # rename:keep
        "[hosts]\n1\\uuid=PC\n")
    put(root, "var/log/moonlightos/launcher.log", "old log\n")  # rename:keep
    put(root, "var/lib/dpkg/status",
        "Package: base-files\nStatus: install ok installed\nArchitecture: amd64\n\n"
        "Package: grub-efi-amd64\nStatus: install ok installed\nArchitecture: amd64\n")
    put(root, "boot/vmlinuz-6.1.0-9-amd64", "k")
    put(root, "boot/initrd.img-6.1.0-9-amd64", "i")
    if os.geteuid() == 0:  # otherwise the files already carry OLD_GID, this user's group
        for path in [root / "var/lib/moonlightos", *(root / "var/lib/moonlightos").rglob("*")]:  # rename:keep
            os.lchown(path, -1, OLD_GID)
    os.chmod(root / "var/lib/moonlightos", 0o750)  # rename:keep


def couchliteos_020(root):
    """0.2.0: the new names already, but no version file and no updater."""
    put(root, "usr/share/couchliteos/profile.conf", "PROFILE_NAME=general\n")
    put(root, "etc/fstab", FSTAB_EFI)
    put(root, "etc/passwd", IMAGE_PASSWD)
    put(root, "etc/group", IMAGE_GROUP)
    put(root, "etc/shadow", IMAGE_SHADOW)
    put(root, "etc/gshadow", IMAGE_GSHADOW)
    put(root, "var/lib/couchliteos/config.ini", "[launcher]\nlog = /var/log/couchliteos/launcher.log\n")
    put(root, "var/lib/dpkg/status", "Package: base-files\nStatus: install ok installed\nArchitecture: amd64\n")
    put(root, "boot/vmlinuz-6.12.1", "k")
    put(root, "boot/initrd.img-6.12.1", "i")


def fake_rsync(image, target):
    """Just enough of the copy for what happens after it: the image's units and GRUB files arrive,
    the old units go."""
    def script(argv):
        if argv[0] != "rsync":
            return None
        if argv[-1] == f"{target}/":
            units = target / "usr/lib/systemd/system"
            for path in units.glob("moonlightos-*"):  # rename:keep
                path.unlink()
            shutil.copytree(image / "usr/lib/systemd/system", units, dirs_exist_ok=True)
        elif argv[-1] == f"{target}/etc/":
            for rel in ("grub.d/01_couchliteos_bootcheck", "default/grub.d/20-couchliteos.cfg"):
                put(target / "etc", rel, (image / "etc" / rel).read_text())
        return None
    return script


def chown_if_allowed(path, uid, gid):
    """The real chown where this user may (config.ini keeps its group); a no-op for root's files."""
    with contextlib.suppress(PermissionError):
        os.chown(path, uid, gid)


class LegacyCase(TmpCase):
    def setUp(self):
        super().setUp()
        self.image, self.target = self.tmp / "image", self.tmp / "target"
        image_tree(self.image)
        self.chowned = []
        self.runner = Runner(fake_rsync(self.image, self.target))
        self.env = make_env(self.tmp, runner=self.runner, lchown=lambda *args: self.chowned.append(args),
                            chown=chown_if_allowed)
        self.status = RecordingStatus(self.tmp / "status.json")
        self.saved = []

    def save(self, env, status, root):
        self.saved.append((root, os.path.exists(root / "var/lib/moonlightos"), len(self.runner.calls)))  # rename:keep
        env.note("saved")

    def apply(self, **kwargs):
        kwargs.setdefault("save", self.save)
        updater.apply_root(self.target, self.status, self.env, image=self.image, **kwargs)

    def read(self, rel):
        return (self.target / rel).read_text()


# ---------------------------------------------------------------- finding an installed system


class IdentityTest(TmpCase):
    def test_a_moonlightos_box_is_found_by_its_old_names(self):
        moonlightos_0113(self.tmp)
        self.assertEqual(updater.box_identity(self.tmp), ("0.1.13", "general"))

    def test_0_2_0_has_no_version_file(self):
        couchliteos_020(self.tmp)
        self.assertEqual(updater.box_identity(self.tmp), ("", "general"))

    def test_the_new_names_win(self):
        put(self.tmp, "etc/couchliteos-version", "0.2.7\n")
        put(self.tmp, "etc/moonlightos-version", "0.1.13\n")  # rename:keep
        put(self.tmp, "usr/share/couchliteos/profile.conf", "PROFILE_NAME=nvidia\n")
        put(self.tmp, "usr/share/moonlightos/profile.conf", "PROFILE_NAME=general\n")  # rename:keep
        self.assertEqual(updater.box_identity(self.tmp), ("0.2.7", "nvidia"))

    def test_a_disk_without_a_profile_is_not_one_of_ours(self):
        put(self.tmp, "etc/couchliteos-version", "0.2.7\n")
        self.assertIsNone(updater.box_identity(self.tmp))


class FindInstallsTest(TmpCase):
    def setUp(self):
        super().setUp()
        self.trees = {"/dev/sda2": moonlightos_0113, "/dev/sdb1": lambda root: put(root, "etc/hostname", "x")}
        self.runner = Runner(self.script)
        self.env = make_env(self.tmp, runner=self.runner)
        self.probe = self.env.run_dir / "probe"

    def script(self, argv):
        if argv[0] == "lsblk":
            return 0, json.dumps(LSBLK)
        if argv[0] == "mount":
            self.trees.get(argv[-2], lambda root: None)(self.probe)
        if argv[0] == "umount":
            for path in list(self.probe.iterdir()):
                shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()
        return None

    def test_each_free_linux_partition_is_probed_read_only_and_unmounted_again(self):
        found = updater.find_installs(self.env)
        self.assertEqual(found, [updater.Install("/dev/sda2", "0.1.13", "general", "/dev/sda")])
        mounts = self.runner.commands("mount")
        self.assertEqual([call[1:] for call in mounts], [
            ["-o", "ro,noload", "/dev/sda2", str(self.probe)], ["-o", "ro,noload", "/dev/sdb1", str(self.probe)]])
        self.assertEqual(len(self.runner.commands("umount")), 2)

    def test_partitions_in_use_swap_and_the_iso_are_never_mounted(self):
        updater.find_installs(self.env)
        mounted = {call[-2] for call in self.runner.commands("mount")}
        self.assertFalse(mounted & {"/dev/sda1", "/dev/sda3", "/dev/sdb2", "/dev/sr0"})

    def test_a_system_without_a_version_file_is_older_than_0_2_1(self):
        self.trees["/dev/sda2"] = couchliteos_020
        self.assertEqual(updater.find_installs(self.env)[0].version, "OLDER THAN 0.2.1")

    def test_a_partition_that_does_not_mount_or_a_broken_lsblk_finds_nothing(self):
        self.runner.script = lambda argv: (32, "") if argv[0] == "mount" else self.script(argv)
        self.assertEqual(updater.find_installs(self.env), [])
        self.runner.script = lambda argv: (0, "not json") if argv[0] == "lsblk" else None
        self.assertEqual(updater.find_installs(self.env), [])

    def test_the_list_round_trips_to_the_launcher(self):
        path = self.tmp / "installs.json"
        updater.write_installs(path, updater.find_installs(self.env))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        self.assertEqual(update.read_installs(path), [
            {"device": "/dev/sda2", "version": "0.1.13", "profile": "general", "disk": "/dev/sda"}])


# ---------------------------------------------------------------- the target's layout


class TargetLayoutTest(TmpCase):
    def test_the_esp_is_found_and_comments_are_ignored(self):
        put(self.tmp, "etc/fstab", FSTAB_EFI + "# UUID=9 /var ext4 defaults 0 2\n")
        self.assertEqual(updater.target_layout(self.tmp), "UUID=AB-CD")

    def test_a_bios_box_has_no_esp(self):
        put(self.tmp, "etc/fstab", "UUID=1111 / ext4 defaults 0 1\nUUID=2 none swap sw 0 0\n")
        self.assertEqual(updater.target_layout(self.tmp), "")

    def test_a_separate_usr_var_or_boot_is_refused(self):
        for point in ("/usr", "/var", "/boot", "/var/"):
            put(self.tmp, "etc/fstab", f"UUID=1111 / ext4 defaults 0 1\nUUID=2 {point} ext4 defaults 0 2\n")
            with self.assertRaises(updater.UpdateFailed) as caught:
                updater.target_layout(self.tmp)
            self.assertIn("USE INSTALL INSTEAD", caught.exception.message)

    def test_a_missing_fstab_is_unknown_and_refused(self):
        with self.assertRaises(updater.UpdateFailed):
            updater.target_layout(self.tmp)


# ---------------------------------------------------------------- the legacy steps, one by one


class LegacyStepsTest(TmpCase):
    def test_old_data_directories_take_the_new_names_with_their_mode(self):
        moonlightos_0113(self.tmp)
        log = []
        updater.move_legacy_dirs(self.tmp, log.append)
        self.assertFalse((self.tmp / "var/lib/moonlightos").exists())  # rename:keep
        self.assertTrue((self.tmp / "var/lib/couchliteos/home/.config/Moonlight Game Streaming Project").is_dir())
        self.assertEqual((self.tmp / "var/log/couchliteos/launcher.log").read_text(), "old log\n")
        self.assertEqual(stat.S_IMODE((self.tmp / "var/lib/couchliteos").stat().st_mode), 0o750)
        self.assertEqual(len(log), 2)
        updater.move_legacy_dirs(self.tmp, log.append)  # a retry after a failure
        self.assertEqual(len(log), 2, "nothing left to move")

    def test_what_the_snapshot_made_under_the_new_name_joins_the_old_tree(self):
        moonlightos_0113(self.tmp)
        put(self.tmp, "var/lib/couchliteos/snapshot/previous.json", "{}")
        put(self.tmp, "var/log/couchliteos/update.log", "x\n")
        updater.move_legacy_dirs(self.tmp, lambda _text: None)
        self.assertTrue((self.tmp / "var/lib/couchliteos/snapshot/previous.json").exists())
        self.assertTrue((self.tmp / "var/lib/couchliteos/config.ini").exists())
        self.assertEqual(sorted(os.listdir(self.tmp / "var/log/couchliteos")), ["launcher.log", "update.log"])
        self.assertEqual(stat.S_IMODE((self.tmp / "var/lib/couchliteos").stat().st_mode), 0o750)

    def test_a_name_in_both_or_a_symlinked_old_directory_is_a_conflict(self):
        moonlightos_0113(self.tmp)
        self.assertEqual(updater.legacy_conflicts(self.tmp), [])
        put(self.tmp, "var/lib/couchliteos/config.ini", "")
        self.assertEqual(updater.legacy_conflicts(self.tmp), ["/var/lib/couchliteos/config.ini"])
        shutil.rmtree(self.tmp / "var/log/moonlightos")  # rename:keep
        link(self.tmp, "var/log/moonlightos", "/somewhere")  # rename:keep
        self.assertIn("/var/log/moonlightos", updater.legacy_conflicts(self.tmp))  # rename:keep

    def test_config_values_naming_old_paths_are_rewritten_keeping_the_mode(self):
        path = put(self.tmp, "config.ini", OLD_CONFIG, 0o640)
        self.assertTrue(updater.rewrite_legacy_config(path, chown=lambda *a: None))
        text = path.read_text()
        self.assertIn("log = /var/log/couchliteos/launcher.log", text)
        self.assertIn("home = /var/lib/couchliteos/home", text)
        self.assertIn("/var/lib/moonlightos-extra/keep", text, "only the exact old path")  # rename:keep
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        self.assertFalse(updater.rewrite_legacy_config(path, chown=lambda *a: None), "nothing left to change")
        self.assertFalse(updater.rewrite_legacy_config(self.tmp / "missing.ini"))

    def test_the_old_group_is_renamed_so_its_files_get_the_new_gid(self):
        image = {"passwd": IMAGE_PASSWD, "group": IMAGE_GROUP, "shadow": IMAGE_SHADOW, "gshadow": IMAGE_GSHADOW}
        box = {"passwd": OLD_PASSWD, "group": OLD_GROUP, "shadow": OLD_SHADOW, "gshadow": OLD_GSHADOW}
        accounts = updater.merge_accounts(image, box, updater.LEGACY_GROUPS)
        self.assertEqual(accounts.gid_map, {OLD_GID: NEW_GID})
        groups = {line.split(":")[0]: line for line in accounts.files["group"].splitlines()}
        self.assertNotIn("moonlightos", groups)  # rename:keep
        self.assertEqual(groups["couchliteos"], f"couchliteos:x:{NEW_GID}:installer-test")
        self.assertIn("couchliteos:!::installer-test", accounts.files["gshadow"])
        users = [line.split(":")[0] for line in accounts.files["passwd"].splitlines()]
        self.assertEqual(users, ["root", "couchliteos", "installer-test"])
        self.assertEqual(accounts.skipped, ["user moonlightos (uid 1000 is taken)"])  # rename:keep
        # Without the map (an ordinary update) the old group would simply be kept as box-only.
        plain = updater.merge_accounts(image, box)
        self.assertEqual(plain.gid_map, {})
        self.assertIn("moonlightos", plain.files["group"])  # rename:keep

    def test_only_old_links_whose_unit_is_gone_are_stale(self):
        moonlightos_0113(self.tmp)
        self.assertEqual(updater.stale_legacy_units(self.tmp), [], "the old units are still installed")
        for path in (self.tmp / "usr/lib/systemd/system").glob("moonlightos-*"):  # rename:keep
            path.unlink()
        self.assertEqual(updater.stale_legacy_units(self.tmp), [
            "systemd/system/graphical.target.wants/moonlightos-launcher.service",  # rename:keep
            "systemd/system/multi-user.target.wants/moonlightos-usbip@sda.service",  # rename:keep
        ])


# ---------------------------------------------------------------- apply-root with legacy, on fake old boxes


class LegacyApplyRootTest(LegacyCase):
    def test_a_moonlightos_0_1_13_box_is_updated_keeping_its_state_under_the_new_names(self):
        moonlightos_0113(self.target)
        self.apply(grub_install=["grub-install", "--target=x86_64-efi", "--efi-directory=/boot/efi"], legacy=True)
        self.assertEqual(self.saved, [(self.target, True, 0)], "saved before anything moved or ran")
        self.assertEqual(self.read("etc/couchliteos-version"), "0.3.0\n")
        self.assertFalse((self.target / "var/lib/moonlightos").exists())  # rename:keep
        self.assertFalse((self.target / "var/log/moonlightos").exists())  # rename:keep
        self.assertIn("home = /var/lib/couchliteos/home", self.read("var/lib/couchliteos/config.ini"))
        self.assertIn("1\\uuid=PC", self.read(
            "var/lib/couchliteos/home/.config/Moonlight Game Streaming Project/Moonlight.conf"))
        self.assertEqual(self.read("var/log/couchliteos/launcher.log"), "old log\n")
        self.assertIn("id=home", self.read("etc/NetworkManager/system-connections/home.nmconnection"))
        # Accounts: the old user and group are gone, their files carry couchliteos's ids.
        self.assertNotIn("moonlightos", self.read("etc/passwd") + self.read("etc/group"))  # rename:keep
        self.assertIn("installer-test", self.read("etc/passwd"))
        remapped = {path for path, _uid, gid in self.chowned if gid == NEW_GID}
        self.assertIn(str(self.target / "var/lib/couchliteos/config.ini"), remapped)
        # Old GRUB settings and old enablements go; the owner's own unit and other links stay.
        self.assertFalse((self.target / "etc/default/grub.d/20-moonlightos.cfg").exists())  # rename:keep
        self.assertTrue((self.target / "etc/default/grub.d/20-couchliteos.cfg").exists())
        wants = self.target / "etc/systemd/system/multi-user.target.wants"
        self.assertEqual(sorted(os.listdir(wants)), ["moonlightos-mine.service", "ssh.service"])  # rename:keep
        self.assertFalse((self.target / "etc/systemd/system/graphical.target.wants").exists())
        # The boot loader is installed again before the menu is written.
        chrooted = [call[2:] for call in self.runner.commands("chroot")]
        self.assertLess(chrooted.index(["grub-install", "--target=x86_64-efi", "--efi-directory=/boot/efi"]),
                        chrooted.index(["update-grub"]))
        log = self.read("var/log/couchliteos/update.log")
        for text in ("legacy: moved /var/lib/moonlightos to /var/lib/couchliteos",  # rename:keep
                     "legacy: old paths rewritten in config.ini",
                     "the old moonlightos user has the uid couchliteos has",  # rename:keep
                     "legacy: removing /etc/systemd/system/graphical.target.wants/moonlightos-launcher.service",  # rename:keep
                     "update to 0.3.0 done"):
            self.assertIn(text, log)
        self.assertEqual(self.status.phases()[-1], "restarting")

    def test_a_0_2_0_box_without_a_version_file_is_updated(self):
        couchliteos_020(self.target)
        self.apply(legacy=True)
        self.assertEqual(self.read("etc/couchliteos-version"), "0.3.0\n")
        self.assertIn("/var/log/couchliteos/launcher.log", self.read("var/lib/couchliteos/config.ini"))
        self.assertNotIn("grub-install", " ".join(" ".join(call) for call in self.runner.calls))

    def test_without_legacy_neither_old_box_is_taken(self):
        for build in (couchliteos_020, moonlightos_0113):
            shutil.rmtree(self.target, ignore_errors=True)
            build(self.target)
            with self.assertRaises(updater.UpdateFailed) as caught:
                self.apply()
            self.assertEqual(caught.exception.message, "THIS DISK IS NOT A COUCHLITEOS BOX")
        self.assertEqual(self.saved, [])

    def test_an_unknown_layout_is_refused_with_nothing_written(self):
        moonlightos_0113(self.target)
        put(self.target, "var/lib/couchliteos/config.ini", "")  # a name in both trees
        before = sorted(str(path) for path in self.target.rglob("*"))
        with self.assertRaises(updater.UpdateFailed) as caught:
            self.apply(legacy=True)
        self.assertIn("NOTHING WAS CHANGED", caught.exception.message)
        self.assertEqual(sorted(str(path) for path in self.target.rglob("*")), before)
        self.assertEqual((self.saved, self.runner.calls), ([], []))

    def test_another_kind_of_box_is_refused_with_nothing_written(self):
        moonlightos_0113(self.target)
        put(self.target, "usr/share/moonlightos/profile.conf", "PROFILE_NAME=nvidia\n")  # rename:keep
        with self.assertRaises(updater.UpdateFailed) as caught:
            self.apply(legacy=True)
        self.assertEqual(caught.exception.message, updater.MSG_OTHER_BOX)
        self.assertFalse((self.target / "var/log/couchliteos").exists())
        self.assertEqual(self.saved, [])

    def test_a_disk_without_any_install_is_refused(self):
        put(self.target, "etc/fstab", FSTAB_EFI)
        with self.assertRaises(updater.UpdateFailed) as caught:
            self.apply(legacy=True)
        self.assertEqual(caught.exception.message, updater.MSG_NO_INSTALL)


# ---------------------------------------------------------------- apply-disk orchestration


class ApplyDiskTest(LegacyCase):
    def setUp(self):
        super().setUp()
        put(self.env.root, "run/live/medium/live/filesystem.squashfs", "")
        self.target = self.env.run_dir / "disk"  # what `mount DEVICE run/disk` shows
        self.disk_image = self.env.run_dir / "disk-image"
        moonlightos_0113(self.target)
        image_tree(self.disk_image)
        self.applied = []

    def fake_apply(self, target, status, env, **kwargs):
        self.applied.append((target, kwargs, len(self.saved)))
        env.note("applied")

    def disk(self, device="/dev/sda2", **kwargs):
        kwargs.setdefault("save", self.save)
        kwargs.setdefault("apply", self.fake_apply)
        updater.apply_disk(device, self.env, self.status, **kwargs)

    def mounts(self):
        return [call[1:] for call in self.runner.commands("mount")]

    def test_the_whole_flow_in_order(self):
        self.disk()
        self.assertEqual(self.mounts(), [
            ["/dev/sda2", str(self.target)],
            ["-t", "squashfs", "-o", "ro,loop", str(self.env.root / "run/live/medium/live/filesystem.squashfs"),
             str(self.disk_image)],
            ["UUID=AB-CD", str(self.target / "boot/efi")],
        ])
        self.assertEqual(self.saved, [(self.target, True, 3)], "saved after the mounts, before the install")
        (target, kwargs, saved_before), = self.applied
        self.assertEqual((target, saved_before), (self.target, 1))
        self.assertEqual(kwargs, {"force": True, "image": self.disk_image, "save_first": False, "legacy": True,
                                  "grub_install": ["grub-install", "--target=x86_64-efi", "--efi-directory=/boot/efi"]})
        self.assertEqual([call[-1] for call in self.runner.commands("umount")],
                         [str(self.target / "boot/efi"), str(self.disk_image), str(self.target)])
        self.assertEqual(self.status.history[-1], ("updated", updater.MSG_DISK_DONE, 100))
        log = (self.target / "var/log/couchliteos/update.log").read_text()
        self.assertIn("apply-disk /dev/sda2: 0.1.13 (general) to 0.3.0 from the USB stick", log)
        self.assertIn("applied", log)

    def test_a_box_with_the_boot_check_keeps_its_boot_loader(self):
        put(self.target, "etc/grub.d/01_couchliteos_bootcheck", "")
        self.disk()
        self.assertIsNone(self.applied[0][1]["grub_install"])

    def test_a_bios_box_gets_grub_on_its_whole_disk(self):
        put(self.target, "etc/fstab", "UUID=1111 / ext4 defaults 0 1\n")
        self.runner.script = lambda argv: (0, "/dev/sda\n") if argv[0] == "lsblk" else None
        self.disk()
        self.assertEqual(self.applied[0][1]["grub_install"], ["grub-install", "--target=i386-pc", "/dev/sda"])
        self.assertEqual(len(self.mounts()), 2, "no ESP to mount")

    def test_only_from_the_live_stick(self):
        shutil.rmtree(self.env.root / "run/live")
        with self.assertRaises(updater.UpdateFailed) as caught:
            self.disk()
        self.assertEqual(caught.exception.message, updater.MSG_NOT_LIVE)
        self.assertEqual(self.runner.calls, [])

    def assert_refused_before_writing(self, message, failed_mounts=0):
        with self.assertRaises(updater.UpdateFailed) as caught:
            self.disk()
        self.assertIn(message, caught.exception.message)
        self.assertEqual((self.saved, self.applied), ([], []))
        self.assertFalse((self.target / "var/log/couchliteos").exists(), "not even the log was written")
        self.assertEqual(len(self.runner.commands("umount")), len(self.runner.commands("mount")) - failed_mounts)

    def test_a_separate_var_is_refused_before_anything_is_written(self):
        put(self.target, "etc/fstab", FSTAB_EFI + "UUID=3 /var ext4 defaults 0 2\n")
        self.assert_refused_before_writing("USE INSTALL INSTEAD")

    def test_a_disk_without_an_install_is_refused(self):
        shutil.rmtree(self.target / "usr/share")
        self.assert_refused_before_writing(updater.MSG_NO_INSTALL)

    def test_another_kind_of_box_is_refused(self):
        image_tree(self.disk_image, profile="nvidia")
        self.assert_refused_before_writing(updater.MSG_OTHER_BOX)

    def test_a_box_newer_than_the_stick_needs_force_and_the_same_version_is_a_repair(self):
        put(self.target, "etc/couchliteos-version", "0.3.1\n")
        self.assert_refused_before_writing(updater.MSG_DISK_NEWER)
        self.disk(force=True)
        put(self.target, "etc/couchliteos-version", "0.3.0\n")
        self.disk()
        self.assertEqual(len(self.applied), 2)

    def test_a_failing_mount_unwinds_what_was_mounted(self):
        self.runner.script = lambda argv: (32, "") if argv[0] == "mount" and "squashfs" in argv else None
        self.assert_refused_before_writing("COULD NOT OPEN THE NEW SYSTEM ON THE USB STICK", failed_mounts=1)

    def test_a_failed_save_installs_nothing(self):
        def broken(env, status, root):
            raise updater.UpdateFailed("NOT ENOUGH FREE SPACE")
        with self.assertRaises(updater.UpdateFailed):
            self.disk(save=broken)
        self.assertEqual(self.applied, [])
        self.assertNotEqual(self.status.phases()[-1], "updated")

    def test_the_real_apply_root_runs_against_the_disk(self):
        self.runner.script = fake_rsync(self.disk_image, self.target)
        self.disk(apply=None)
        self.assertEqual((self.target / "etc/couchliteos-version").read_text(), "0.3.0\n")
        self.assertTrue((self.target / "var/lib/couchliteos/config.ini").exists())
        self.assertEqual(self.status.phases()[-2:], ["restarting", "updated"])


class ApplyDiskMainTest(TmpCase):
    def setUp(self):
        super().setUp()
        self.env = make_env(self.tmp, runner=Runner())

    def main(self, *argv):
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                return updater.main(list(argv), env=self.env)
            except SystemExit as error:
                return error.code

    def test_apply_disk_takes_a_device_or_found_but_not_both(self):
        self.assertEqual(self.main("apply-disk"), 2)
        self.assertEqual(self.main("apply-disk", "/dev/sda2", "--found"), 2)

    def test_apply_disk_runs_without_restarting_and_lists_the_disks_again(self):
        install = updater.Install("/dev/sda2", "0.3.0", "general", "/dev/sda")
        with mock.patch.object(updater, "apply_disk") as apply, \
                mock.patch.object(updater, "find_installs", return_value=[install]):
            self.assertEqual(self.main("apply-disk", "/dev/sda2", "--force"), 0)
        self.assertEqual(apply.call_args.args[0], "/dev/sda2")
        self.assertTrue(apply.call_args.kwargs["force"])
        self.assertEqual(apply.call_args.args[2].path, self.env.run_dir / "update-status.json")
        self.assertNotIn(["systemctl", "reboot"], self.env.runner.calls)
        self.assertEqual(update.read_installs(self.env.run_dir / "installs.json")[0]["version"], "0.3.0")

    def test_found_takes_the_one_install_and_refuses_none_or_several(self):
        one = updater.Install("/dev/sda2", "OLDER THAN 0.2.1", "general", "/dev/sda")
        with mock.patch.object(updater, "apply_disk") as apply, \
                mock.patch.object(updater, "find_installs", return_value=[one]):
            self.assertEqual(self.main("apply-disk", "--found"), 0)
        self.assertEqual(apply.call_args.args[0], "/dev/sda2")
        for installs, text in (([], "NO INSTALLED SYSTEM"), ([one, one], "MORE THAN ONE")):
            with mock.patch.object(updater, "apply_disk") as apply, \
                    mock.patch.object(updater, "find_installs", return_value=installs):
                self.assertEqual(self.main("apply-disk", "--found"), 1)
            apply.assert_not_called()
            self.assertIn(text, updater.read_status(self.env.run_dir / "update-status.json")["message"])

    def test_find_installs_writes_the_list(self):
        with mock.patch.object(updater, "find_installs", return_value=[]):
            self.assertEqual(self.main("find-installs"), 0)
        self.assertEqual(json.loads((self.env.run_dir / "installs.json").read_text()), [])

    def test_the_updated_phase_is_accepted(self):
        updater.Status(self.tmp / "s.json").set("updated", updater.MSG_DISK_DONE, 100)


# ---------------------------------------------------------------- the live launcher


INSTALL = {"device": "/dev/sda2", "version": "0.1.13", "profile": "general", "disk": "/dev/sda"}


class NoticeTest(TmpCase):
    def write(self, items):
        path = self.tmp / "installs.json"
        path.write_text(json.dumps(items))
        return path

    def test_home_names_an_older_install_and_where_to_update_it(self):
        path = self.write([INSTALL])
        self.assertEqual(update.disk_notice("0.3.0", path),
                         "INSTALLED SYSTEM 0.1.13 FOUND: UPDATE IT IN SETTINGS > SOFTWARE UPDATE")
        self.write([dict(INSTALL, version="OLDER THAN 0.2.1")])
        self.assertIn("OLDER THAN 0.2.1", update.disk_notice("0.3.0", path))

    def test_nothing_for_none_several_the_same_version_or_a_damaged_list(self):
        self.assertEqual(update.disk_notice("0.3.0", self.write([])), "")
        self.assertEqual(update.disk_notice("0.3.0", self.write([INSTALL, INSTALL])), "")
        self.assertEqual(update.disk_notice("0.3.0", self.write([dict(INSTALL, version="0.3.0")])), "")
        self.assertEqual(update.disk_notice("0.3.0", self.write({"x": 1})), "")
        self.assertEqual(update.read_installs(self.write([{"device": 1}])), [])
        self.assertEqual(update.disk_notice("0.3.0", self.tmp / "missing.json"), "")

    def test_the_checker_shows_it_on_the_live_stick_only(self):
        update.INSTALLS.parent.mkdir(parents=True, exist_ok=True)  # the scratch run directory (testenv)
        update.INSTALLS.write_text(json.dumps([INSTALL]))
        self.addCleanup(update.INSTALLS.unlink)
        live = update.Checker(current="0.3.0", state_path=self.tmp / "state.ini", live=lambda: True)
        installed = update.Checker(current="0.3.0", state_path=self.tmp / "state.ini", live=lambda: False)
        self.assertIn("INSTALLED SYSTEM 0.1.13 FOUND", live.notice())
        self.assertEqual(installed.notice(), "")


class LiveScreenTest(UiTest):
    def make_live(self, installs):
        return self.make(live=True, current="0.3.0", installs=lambda: installs)

    def test_an_install_found_is_offered_above_back(self):
        self.script = [ESC]
        self.make_live([INSTALL]).run()
        frame = self.screen.frames[0]
        self.assertIn("INSTALLED SYSTEM: 0.1.13 (GENERAL)", frame)
        self.assertTrue(self.screen.line_with("UPDATE THE INSTALLED SYSTEM (KEEPS PAIRINGS AND SETTINGS)")
                        .startswith(">"))
        self.assertNotIn("WRITE THE NEW ISO", flat(frame))

    def test_without_exactly_one_install_the_live_text_stays(self):
        for installs in ([], [INSTALL, INSTALL]):
            self.script = [ESC]
            self.make_live(installs).run()
            self.assertIn("WRITE THE NEW ISO", flat(self.screen.frames[-1]))
            self.assertNotIn("UPDATE THE INSTALLED SYSTEM", self.screen.frames[-1])

    def test_confirming_asks_the_root_service_and_follows_it_to_done(self):
        self.script = [ENTER, self.service("saving", 40, "SAVING THE CURRENT VERSION... 40%", "0.3.0"),
                       self.service("installing", 60, "COPYING SYSTEM FILES...", "0.3.0"),
                       self.service("updated", 100, su_done(), "0.3.0"), None, ENTER, ESC]
        self.make_live([INSTALL]).run()
        self.assertIn("UPDATE THE INSTALLED SYSTEM: 0.1.13 (GENERAL) TO COUCHLITEOS 0.3.0?", self.questions[0])
        self.assertIn("PAIRINGS, WI-FI, BLUETOOTH AND SETTINGS ARE KEPT", self.questions[0])
        self.assertTrue((self.tmp / "disk-update").exists())
        self.assertTrue(self.frames_with("COPYING SYSTEM FILES..."))
        # Both home screens: the classic one has REBOOT, the TV one POWER > RESTART.
        self.assertTrue(self.frames_with("REMOVE THE USB STICK, THEN RESTART: REBOOT OR POWER > RESTART ON THE HOME SCREEN"))
        self.assertIn("REMOVE THE USB STICK", flat(self.screen.frames[-1]), "the main screen keeps saying so")

    def test_no_means_no_request_and_running_apps_must_close_first(self):
        self.answer = False
        self.script = [ENTER, ESC]
        self.make_live([INSTALL]).run()
        self.assertFalse((self.tmp / "disk-update").exists())
        self.apps, self.answer = True, True
        self.script = [ENTER, ESC]
        self.make_live([INSTALL]).run()
        self.assertFalse((self.tmp / "disk-update").exists())
        self.assertIn("CLOSE RUNNING APPS FIRST", self.screen.frames[-1])

    def test_back_is_still_one_row_down(self):
        self.script = [DOWN, ENTER]
        self.make_live([INSTALL]).run()
        self.assertEqual(self.questions, [])


def su_done():
    import couchliteos_softwareupdate as su
    return su.DISK_DONE


class WiringTest(unittest.TestCase):
    ROOT = pathlib.Path(__file__).resolve().parents[1]

    def test_the_units_take_no_device_from_the_launcher(self):
        service = (self.ROOT / "services/couchliteos-disk-update.service").read_text()
        self.assertIn("ExecStartPre=/usr/bin/rm -f /run/couchliteos/disk-update\n", service)
        self.assertIn("/usr/libexec/couchliteos-updater apply-disk --found\n", service)
        self.assertIn("PathExists=/run/couchliteos/disk-update\n",
                      (self.ROOT / "services/couchliteos-disk-update.path").read_text())
        finder = (self.ROOT / "services/couchliteos-find-installs.service").read_text()
        self.assertIn("ConditionKernelCommandLine=boot=live\n", finder)
        self.assertIn("ExecStart=/usr/libexec/couchliteos-updater find-installs\n", finder)

    def test_the_updated_message_matches_on_both_sides(self):
        self.assertEqual(updater.MSG_DISK_DONE, su_done())


if __name__ == "__main__":
    unittest.main()
