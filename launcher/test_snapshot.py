"""The snapshot before an update: one slot, replaced only once the new one is complete."""

import hashlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

import couchliteos_snapshot as snapshot
import couchliteos_softwareupdate as softwareupdate
import couchliteos_updater as updater

GIB = 1 << 30
KERNEL = "6.12.48+deb13-amd64"
SQUASHFS_KO = f"/lib/modules/{KERNEL}/kernel/fs/squashfs/squashfs.ko.xz"


def put(root, rel, data=b""):
    path = pathlib.Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return path


class Runner:
    """Records argv; modprobe answers like kmod (squashfs a module, loop built in)."""

    def __init__(self):
        self.calls = []
        self.fail = set()

    def __call__(self, argv, **_kwargs):
        argv = [str(part) for part in argv]
        self.calls.append(argv)
        code, output = 0, ""
        if argv[0] == "modprobe":
            output = f"insmod {SQUASHFS_KO} \n" if argv[-1] == "squashfs" else "builtin loop\n"
        if argv[0] in self.fail:
            code = 1
        return subprocess.CompletedProcess(argv, code, stdout=output)


class FakePopen:
    """mksquashfs: writes `content` to the output file and prints percentages."""

    content = b"image"
    code = 0
    calls = []

    def __init__(self, argv, **_kwargs):
        FakePopen.calls.append(list(argv))
        output = pathlib.Path(argv[argv.index("mksquashfs") + 2])
        output.write_bytes(FakePopen.content)
        self.stdout = iter(["Parallel mksquashfs: Using 4 processors\n", "0\n", "50\n", "50\n", "100\n"])

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def wait(self):
        return FakePopen.code


class RecordingStatus:
    def __init__(self):
        self.history = []

    def set(self, phase, message, percent=None):
        self.history.append((phase, message, percent))


class SnapshotCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        self.root = self.tmp / "root"
        FakePopen.content, FakePopen.code, FakePopen.calls = b"image", 0, []
        self.runner = Runner()
        self.log = []
        self.free = 100 * GIB
        self.env = updater.Env(
            runner=self.runner, popen=FakePopen, free_bytes=lambda _path: self.free, root=self.root,
            run_dir=self.tmp / "run", cache_dir=self.tmp / "cache", euid=os.geteuid, log=self.log.append)
        self.status = RecordingStatus()
        self.box("0.2.7", b"kernel 0.2.7")

    def box(self, version, kernel_bytes):
        put(self.root, "etc/couchliteos-version", version + "\n")
        put(self.root, f"boot/vmlinuz-{KERNEL}", kernel_bytes)
        put(self.root, f"boot/initrd.img-{KERNEL}", b"initrd " + kernel_bytes)
        put(self.root, SQUASHFS_KO.lstrip("/"), b"squashfs module")

    def create(self, content=b"image", **kwargs):
        FakePopen.content = content
        kwargs.setdefault("running", KERNEL)
        kwargs.setdefault("now", lambda: 1_790_000_000.0)
        return snapshot.create(self.env, self.status, self.root, **kwargs)

    def where(self):
        return snapshot.paths(self.root)

    def leftovers(self):
        where = self.where()
        return [name for name in ("image_new", "info_new", "boot_new", "boot_old") if os.path.lexists(where[name])]


class CreateTest(SnapshotCase):
    def test_a_snapshot_is_the_image_its_json_and_a_boot_copy(self):
        saved = self.create(b"image A")
        where = self.where()
        self.assertEqual(where["image"].read_bytes(), b"image A")
        data = json.loads(where["info"].read_text())
        self.assertEqual(data, {
            "version": "0.2.7", "date": "2026-09-21T14:13:20Z", "size": 7,
            "sha256": hashlib.sha256(b"image A").hexdigest(), "kernel": KERNEL,
        })
        self.assertEqual(snapshot.info(self.root), saved)
        self.assertEqual(snapshot.verify(self.root), saved)
        self.assertEqual((where["boot"] / "vmlinuz").read_bytes(), b"kernel 0.2.7")
        self.assertEqual((where["boot"] / "initrd.img").read_bytes(), b"initrd kernel 0.2.7")
        self.assertEqual((where["boot"] / "modules/squashfs.ko.xz").read_bytes(), b"squashfs module")
        self.assertEqual((where["boot"] / "modules/load-order").read_text(), "squashfs.ko.xz\n")
        self.assertEqual(self.leftovers(), [])

    def test_only_root_can_read_the_image_but_the_launcher_can_read_the_json(self):
        self.create()
        where = self.where()
        self.assertEqual(stat.S_IMODE(where["dir"].stat().st_mode), 0o711)
        self.assertEqual(stat.S_IMODE(where["image"].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(where["info"].stat().st_mode), 0o644)

    def test_mksquashfs_runs_gently_with_zstd_on_one_file_system(self):
        self.create()
        argv = FakePopen.calls[0]
        self.assertEqual(argv[:9], ["nice", "-n", "19", "ionice", "-c", "3", "mksquashfs", str(self.root),
                                    str(self.where()["image_new"])])
        for option in (["-comp", "zstd", "-Xcompression-level", "6"], ["-one-file-system"], ["-noappend"],
                       ["-wildcards", "-e"]):
            index = argv.index(option[0])
            self.assertEqual(argv[index:index + len(option)], option)

    def test_the_exclude_list(self):
        patterns = FakePopen.calls[0][FakePopen.calls[0].index("-e") + 1:] if self.create() else []
        for emptied in ("dev", "proc", "sys", "run", "tmp", "mnt", "media", "var/log", "var/cache", "var/tmp"):
            self.assertIn(f"{emptied}/*", patterns)
            self.assertIn(f"{emptied}/.*", patterns)
            self.assertNotIn(emptied, patterns, "the directory itself stays: a restore mounts on it")
        for left_out in ("var/lib/couchliteos/snapshot", "boot/couchliteos-previous",
                         "var/lib/couchliteos/home/.cache", "swapfile"):
            self.assertIn(left_out, patterns)
        for kept in ("etc", "var/lib/couchliteos", "var/lib/couchliteos/home/.config", "home", "boot"):
            self.assertNotIn(kept, patterns)
            self.assertNotIn(f"{kept}/*", patterns)

    def test_the_image_is_checked_with_unsquashfs(self):
        self.create()
        self.assertIn(["unsquashfs", "-s", str(self.where()["image_new"])], self.runner.calls)

    def test_progress_is_the_saving_phase_with_a_percentage(self):
        self.create()
        self.assertEqual(self.status.history, [
            ("saving", "SAVING THE CURRENT VERSION... 0%", 0),
            ("saving", "SAVING THE CURRENT VERSION... 50%", 50),
            ("saving", "SAVING THE CURRENT VERSION... 100%", 100),
        ])

    def test_the_next_snapshot_replaces_the_old_one(self):
        self.create(b"image A")
        self.box("0.3.0", b"kernel 0.3.0")
        self.create(b"image B")
        self.assertEqual(snapshot.verify(self.root).version, "0.3.0")
        self.assertEqual(self.where()["image"].read_bytes(), b"image B")
        self.assertEqual((self.where()["boot"] / "vmlinuz").read_bytes(), b"kernel 0.3.0")
        self.assertEqual(self.leftovers(), [])

    def test_replacement_order_image_then_boot_copy_then_json(self):
        self.create(b"image A")
        renames = []
        real = snapshot._rename

        def recording(source, destination):
            renames.append((source.name, destination.name))
            real(source, destination)

        with mock.patch.object(snapshot, "_rename", recording):
            self.create(b"image B")
        self.assertEqual(renames, [
            ("previous.squashfs.new", "previous.squashfs"),
            ("couchliteos-previous", "couchliteos-previous.old"),
            ("couchliteos-previous.new", "couchliteos-previous"),
            ("previous.json.new", "previous.json"),
        ])

    def test_a_crash_at_any_replacement_step_leaves_exactly_one_valid_snapshot(self):
        for step in range(4):
            with self.subTest(step=step):
                shutil.rmtree(self.root)
                self.box("0.2.7", b"kernel 0.2.7")
                self.create(b"image A")
                self.box("0.3.0", b"kernel 0.3.0")
                calls = []
                real = snapshot._rename

                def crashing(source, destination):
                    calls.append(source)
                    if len(calls) == step + 1:
                        raise KeyboardInterrupt("power cut")
                    real(source, destination)

                with mock.patch.object(snapshot, "_rename", crashing), self.assertRaises(KeyboardInterrupt):
                    self.create(b"image B")
                snapshot.recover(self.env, self.root)
                saved = snapshot.verify(self.root)
                self.assertIsNotNone(saved)
                self.assertEqual(self.leftovers(), [])
                # The json, the image and the boot copy all belong to the same snapshot.
                expected = {"0.2.7": (b"image A", b"kernel 0.2.7"), "0.3.0": (b"image B", b"kernel 0.3.0")}
                image, kernel = expected[saved.version]
                self.assertEqual(self.where()["image"].read_bytes(), image)
                self.assertEqual((self.where()["boot"] / "vmlinuz").read_bytes(), kernel)
                self.assertEqual(saved.version, "0.3.0", "the commit mark was written, so the new one wins")

    def test_a_crash_before_the_commit_mark_keeps_the_old_snapshot(self):
        self.create(b"image A")
        self.box("0.3.0", b"kernel 0.3.0")
        with mock.patch.object(snapshot, "_write_info", side_effect=KeyboardInterrupt("power cut")):
            with self.assertRaises(KeyboardInterrupt):
                self.create(b"image B")
        snapshot.recover(self.env, self.root)
        self.assertEqual(snapshot.verify(self.root).version, "0.2.7")
        self.assertEqual(self.leftovers(), [])

    def test_leftovers_of_an_unfinished_save_are_removed_by_the_next_one(self):
        self.create(b"image A")
        where = self.where()
        where["image_new"].write_bytes(b"half")
        where["boot_new"].mkdir()
        self.box("0.3.0", b"kernel 0.3.0")
        self.create(b"image B")
        self.assertEqual(snapshot.verify(self.root).version, "0.3.0")
        self.assertEqual(self.leftovers(), [])


class FailureTest(SnapshotCase):
    def setUp(self):
        super().setUp()
        self.create(b"image A")
        self.before = self.tree()
        self.box("0.3.0", b"kernel 0.3.0")

    def tree(self):
        files = {}
        for top in (self.where()["dir"], self.where()["boot"].parent):
            for path in sorted(top.rglob("*")):
                if path.is_file():
                    files[str(path.relative_to(self.root))] = path.read_bytes()
        files = {name: data for name, data in files.items() if not name.startswith("boot/vmlinuz-")
                 and not name.startswith("boot/initrd.img-")}
        return files

    def assert_untouched(self, caught, message=snapshot.MSG_FAILED):
        self.assertEqual(caught.exception.message, message)
        self.assertEqual(self.tree(), self.before)
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(snapshot.verify(self.root).version, "0.2.7")

    def test_mksquashfs_failing(self):
        FakePopen.code = 1
        with self.assertRaises(snapshot.SnapshotFailed) as caught:
            self.create(b"image B")
        self.assert_untouched(caught)

    def test_an_image_unsquashfs_cannot_read(self):
        self.runner.fail.add("unsquashfs")
        with self.assertRaises(snapshot.SnapshotFailed) as caught:
            self.create(b"image B")
        self.assert_untouched(caught)

    def test_modprobe_failing(self):
        self.runner.fail.add("modprobe")
        with self.assertRaises(snapshot.SnapshotFailed) as caught:
            self.create(b"image B")
        self.assert_untouched(caught)

    def test_no_kernel_in_boot(self):
        (self.root / f"boot/initrd.img-{KERNEL}").unlink()
        with self.assertRaises(snapshot.SnapshotFailed) as caught:
            self.create(b"image B")
        self.assert_untouched(caught)
        self.assertEqual(FakePopen.calls[1:], [], "nothing is squashed without a kernel to boot it")

    def test_a_disk_error_while_copying_the_boot_files(self):
        with mock.patch.object(snapshot.shutil, "copyfile", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(snapshot.SnapshotFailed) as caught:
                self.create(b"image B")
        self.assert_untouched(caught)

    def test_too_little_space_refuses_before_anything_runs(self):
        self.free = 1 * GIB
        with self.assertRaises(snapshot.SnapshotFailed) as caught:
            self.create(b"image B")
        self.assertIn("NOT ENOUGH FREE SPACE TO SAVE THE CURRENT VERSION: NEED 7 GB", caught.exception.message)
        self.assertEqual(FakePopen.calls[1:], [])
        self.assertEqual(self.tree(), self.before)

    def test_a_snapshot_folder_that_is_not_ours_is_never_used(self):
        self.env.euid = lambda: os.geteuid() + 1
        with self.assertRaises(snapshot.SnapshotFailed) as caught:
            self.create(b"image B")
        self.assert_untouched(caught, snapshot.MSG_NOT_OURS)
        self.assertFalse(snapshot.delete(self.env, self.root))
        self.assertEqual(self.tree(), self.before)

    def test_a_symlinked_snapshot_folder_is_never_used(self):
        where = self.where()
        where["dir"].rename(self.tmp / "elsewhere")
        where["dir"].symlink_to(self.tmp / "elsewhere")
        with self.assertRaises(snapshot.SnapshotFailed) as caught:
            self.create(b"image B")
        self.assertEqual(caught.exception.message, snapshot.MSG_NOT_OURS)


class SpaceTest(SnapshotCase):
    def test_space_plan_table(self):
        need = int(6.5 * GIB)
        for free, old, plan in (
            (need, 0, "keep-old"), (need, 2 * GIB, "keep-old"), (100 * GIB, 0, "keep-old"),
            (need - 1, 0, "refuse"), (need - 1, 1, "delete-old-first"),
            (5 * GIB, 2 * GIB, "delete-old-first"), (4 * GIB, 2 * GIB, "refuse"), (0, 0, "refuse"),
        ):
            with self.subTest(free=free, old=old):
                self.assertEqual(snapshot.space_plan(free, old), plan)

    def test_the_install_space_matches_the_updater(self):
        self.assertEqual(snapshot.SPACE_FOR_INSTALL, updater.SPACE_FOR_INSTALL)

    def test_the_old_snapshot_is_deleted_first_when_only_that_makes_room(self):
        self.create(b"x" * 100)
        self.free = int(6.5 * GIB) - 50
        order = []
        real_delete = snapshot.delete
        with mock.patch.object(snapshot, "delete", lambda env, root: order.append("delete") or real_delete(env, root)):
            FakePopen.calls = []
            self.create(b"image B")
        self.assertEqual(order, ["delete"])
        self.assertEqual(snapshot.verify(self.root).sha256, hashlib.sha256(b"image B").hexdigest())


class ReadTest(SnapshotCase):
    def test_no_snapshot(self):
        self.assertIsNone(snapshot.info(self.root))
        self.assertIsNone(snapshot.verify(self.root))

    def test_an_image_of_another_size_than_its_json_does_not_count(self):
        self.create(b"image A")
        self.where()["image"].write_bytes(b"image AB")
        self.assertIsNone(snapshot.info(self.root))

    def test_a_damaged_image_fails_verify_but_not_info(self):
        self.create(b"image A")
        self.where()["image"].write_bytes(b"image Z")
        self.assertIsNotNone(snapshot.info(self.root))
        self.assertIsNone(snapshot.verify(self.root))

    def test_describe(self):
        saved = snapshot.Snapshot("0.2.7", "2026-10-02T09:00:00Z", 1_812_000_000, "a" * 64, KERNEL)
        self.assertEqual(saved.describe(), "0.2.7, SAVED 2 OCT 2026, 1.8 GB")

    def test_delete_removes_the_image_json_and_boot_copy(self):
        self.create()
        self.assertTrue(snapshot.delete(self.env, self.root))
        self.assertIsNone(snapshot.info(self.root))
        self.assertEqual(list(self.where()["dir"].iterdir()), [])
        self.assertFalse(self.where()["boot"].exists())


class KernelTest(SnapshotCase):
    def test_the_running_kernel_is_preferred_then_the_newest_with_an_initrd(self):
        put(self.root, "boot/vmlinuz-6.12.9-amd64", b"k")
        put(self.root, "boot/initrd.img-6.12.9-amd64", b"i")
        put(self.root, "boot/vmlinuz-6.12.10-amd64", b"k")
        put(self.root, "boot/initrd.img-6.12.10-amd64", b"i")
        put(self.root, "boot/vmlinuz-6.13.1-amd64", b"no initrd")
        self.assertEqual(snapshot.pick_kernel(self.root, "6.12.9-amd64"), "6.12.9-amd64")
        self.assertEqual(snapshot.pick_kernel(self.root, "5.10.0-live"), KERNEL)
        shutil.rmtree(self.root / "boot")
        self.assertEqual(snapshot.pick_kernel(self.root, "x"), "")

    def test_modules_of_a_disk_mounted_elsewhere_are_looked_up_below_it(self):
        found = snapshot.module_files(self.env, self.root, KERNEL)
        self.assertEqual(found, [self.root / SQUASHFS_KO.lstrip("/")])
        self.assertEqual(self.runner.calls[0], ["modprobe", "--show-depends", "-S", KERNEL, "-d", str(self.root),
                                                "squashfs"])

    def test_module_paths_kmod_already_prefixed_are_kept(self):
        prefixed = f"{self.root}{SQUASHFS_KO}"
        self.env.runner = lambda argv, **_k: subprocess.CompletedProcess(argv, 0, stdout=f"insmod {prefixed}\n")
        self.assertEqual(snapshot.module_files(self.env, self.root, KERNEL), [pathlib.Path(prefixed)])

    def test_the_running_box_needs_no_dirname(self):
        snapshot.module_files(self.env, pathlib.Path("/"), KERNEL)
        self.assertNotIn("-d", self.runner.calls[0])


class SettingTest(SnapshotCase):
    def config(self):
        return self.root / "var/lib/couchliteos/config.ini"

    def test_on_unless_config_says_off(self):
        self.assertTrue(snapshot.enabled(self.root))
        put(self.root, "var/lib/couchliteos/config.ini", "[update]\nsnapshot = off\n")
        self.assertFalse(snapshot.enabled(self.root))
        put(self.root, "var/lib/couchliteos/config.ini", "[update]\nsnapshot = on\n")
        self.assertTrue(snapshot.enabled(self.root))
        put(self.root, "var/lib/couchliteos/config.ini", "not an ini file [")
        self.assertTrue(snapshot.enabled(self.root))

    def test_set_enabled_changes_only_its_line(self):
        put(self.root, "var/lib/couchliteos/config.ini",
            "[power]\nblank_minutes = 10\n\n[update]\npaused_until = 123\n\n[cec]\nenabled = true")
        snapshot.set_enabled(False, self.root)
        self.assertEqual(self.config().read_text(), (
            "[power]\nblank_minutes = 10\n\n[update]\nsnapshot = off\npaused_until = 123\n\n[cec]\nenabled = true\n"))
        self.assertFalse(snapshot.enabled(self.root))
        snapshot.set_enabled(True, self.root)
        self.assertIn("[update]\nsnapshot = on\npaused_until = 123\n", self.config().read_text())
        self.assertTrue(snapshot.enabled(self.root))

    def test_set_enabled_adds_the_section(self):
        put(self.root, "var/lib/couchliteos/config.ini", "[power]\nblank_minutes = 10\n")
        snapshot.set_enabled(False, self.root)
        self.assertEqual(self.config().read_text(), "[power]\nblank_minutes = 10\n\n[update]\nsnapshot = off\n")
        self.config().unlink()
        snapshot.set_enabled(False, self.root)
        self.assertEqual(self.config().read_text(), "[update]\nsnapshot = off\n")

    def test_off_means_the_updater_saves_nothing(self):
        snapshot.set_enabled(False, self.root)
        with mock.patch.object(snapshot, "create") as create:
            updater.save_snapshot(self.env, self.status)
        create.assert_not_called()

    def test_the_updater_turns_a_failed_save_into_a_failed_update(self):
        FakePopen.code = 1
        with self.assertRaises(updater.UpdateFailed) as caught:
            updater.save_snapshot(self.env, self.status)
        self.assertEqual(caught.exception.message, snapshot.MSG_FAILED)

    def test_the_updater_can_save_another_disk(self):
        with mock.patch.object(snapshot, "create") as create:
            updater.save_snapshot(self.env, self.status, root=self.tmp / "disk")
        self.assertEqual(create.call_args.args[2], self.tmp / "disk")


class WiringTest(unittest.TestCase):
    def test_the_update_never_touches_the_boot_copy_and_saving_is_a_phase(self):
        self.assertIn("/boot/couchliteos-previous/", updater.SYSTEM_EXCLUDES)
        self.assertIn("saving", updater.PHASES)
        self.assertIn("saving", softwareupdate.PHASE_TEXT)


@unittest.skipUnless(shutil.which("mksquashfs") and shutil.which("unsquashfs"), "needs squashfs-tools")
class RealMksquashfsTest(SnapshotCase):
    """The real tools on a small tree: the excludes do what the list says."""

    def test_excluded_contents_are_left_out_and_their_directories_kept(self):
        for rel in ("etc/hostname", "var/log/syslog", "var/cache/couchliteos/update/x.iso", "tmp/.X0-lock",
                    "tmp/file", "dev/null-ish", "var/lib/couchliteos/home/.config/moonlight/state",
                    "var/lib/couchliteos/home/.cache/chrome/blob", "var/lib/couchliteos/config.ini"):
            put(self.root, rel, b"x")
        self.env.popen = subprocess.Popen
        real = self.runner

        def runner(argv, **kwargs):  # the real unsquashfs, kmod's answer for modprobe
            if argv[0] == "unsquashfs":
                return subprocess.run(argv, **kwargs)
            return real(argv, **kwargs)

        self.env.runner = runner
        snapshot.create(self.env, self.status, self.root, running=KERNEL)
        listing = subprocess.run(["unsquashfs", "-l", "-d", "", str(self.where()["image"])],
                                 stdout=subprocess.PIPE, text=True, check=True).stdout.split()
        for kept in ("/etc/hostname", "/var/log", "/var/cache", "/tmp", "/dev", f"/boot/vmlinuz-{KERNEL}",
                     "/var/lib/couchliteos/home/.config/moonlight/state", "/var/lib/couchliteos/config.ini"):
            self.assertIn(kept, listing)
        for gone in ("/var/log/syslog", "/var/cache/couchliteos", "/tmp/.X0-lock", "/tmp/file", "/dev/null-ish",
                     "/var/lib/couchliteos/home/.cache", "/var/lib/couchliteos/snapshot", "/boot/couchliteos-previous"):
            self.assertNotIn(gone, listing)
        self.assertIsNotNone(snapshot.verify(self.root))


if __name__ == "__main__":
    unittest.main()
