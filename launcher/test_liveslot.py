import json
import pathlib
import subprocess
import tempfile
import unittest

import couchliteos_liveslot as liveslot


class PulledOut(Exception):
    """The stick was pulled out at a given step."""


def pull_at(step_name):
    def step(name):
        if name == step_name:
            raise PulledOut(name)
    return step


def env(free=1 << 40, step=None):
    return liveslot.Env(runner=lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""),
                        free_bytes=lambda path: free, step=step or (lambda name: None))


class SlotTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = pathlib.Path(directory.name)
        self.base = self.tmp / "live-update"

    def system(self, tag):
        source = self.tmp / f"src-{tag}"
        source.mkdir(exist_ok=True)
        for name in liveslot.FILES:
            (source / name).write_bytes(f"{tag}:{name}".encode() * 100)
        return {name: source / name for name in liveslot.FILES}

    def booted(self):
        """What the GRUB menu would start first: current, else previous, else the ISO."""
        for name in (liveslot.CURRENT, liveslot.PREVIOUS):
            slot = self.base / name
            if (slot / liveslot.OK).is_file():
                self.assertTrue(liveslot.verify_slot(slot), f"{name} has ok but bad files")
                return (slot / liveslot.VERSION).read_text().strip()
        return "iso"

    def test_first_install_writes_checked_current_with_ok_last(self):
        liveslot.install(self.base, self.system("a"), "0.3.1", env())
        current = self.base / "current"
        self.assertTrue(liveslot.slot_is_good(current))
        self.assertTrue(liveslot.verify_slot(current))
        self.assertEqual(liveslot.slot_state(self.base), {"current": "0.3.1", "previous": ""})
        self.assertEqual(self.booted(), "0.3.1")

    def test_second_install_keeps_previous(self):
        liveslot.install(self.base, self.system("a"), "0.3.1", env())
        liveslot.install(self.base, self.system("b"), "0.3.2", env())
        liveslot.install(self.base, self.system("c"), "0.3.3", env())
        self.assertEqual(liveslot.slot_state(self.base), {"current": "0.3.3", "previous": "0.3.2"})
        self.assertEqual(sorted(p.name for p in self.base.iterdir()), ["current", "previous"])

    def test_pulled_out_at_every_step_boots_a_checked_system_and_recovers(self):
        for step in ("written", "previous-aside", "current-to-previous", "new-to-current"):
            with self.subTest(step=step):
                liveslot.clear(self.base)
                liveslot.install(self.base, self.system("a"), "old", env())
                liveslot.install(self.base, self.system("b"), "mid", env())
                with self.assertRaises(PulledOut):
                    liveslot.install(self.base, self.system("c"), "new", env(step=pull_at(step)))
                before = self.booted()
                self.assertIn(before, ("mid", "new"))
                liveslot.recover(self.base)
                after = liveslot.slot_state(self.base)
                # Recovery keeps what booted, or finishes the update once the new
                # system is complete; the other system stays as previous.
                self.assertIn(after["current"], (before, "new"))
                self.assertEqual({after["current"], after["previous"]} - {"old"}, {"mid", "new"}
                                 if after["current"] == "new" else {"mid"})
                self.assertFalse(any(p.name.endswith((".new", ".old")) for p in self.base.iterdir()))

    def test_pulled_out_while_copying_leaves_unmarked_slot_that_is_ignored_and_removed(self):
        liveslot.install(self.base, self.system("a"), "old", env())
        new = self.base / liveslot.NEW
        new.mkdir()
        (new / "vmlinuz").write_bytes(b"half")
        self.assertEqual(self.booted(), "old")
        self.assertIn("removed an unfinished update", liveslot.recover(self.base))
        self.assertFalse(new.exists())

    def test_wrong_checksum_refuses_and_keeps_current(self):
        liveslot.install(self.base, self.system("a"), "old", env())
        sources = self.system("b")
        with self.assertRaises(liveslot.SlotError):
            liveslot.install(self.base, sources, "new", env(), expected={"vmlinuz": "0" * 64})
        self.assertEqual(self.booted(), "old")
        self.assertFalse((self.base / liveslot.NEW).exists())

    def test_not_enough_space_says_how_much(self):
        with self.assertRaises(liveslot.SlotError) as caught:
            liveslot.install(self.base, self.system("a"), "x", env(free=0))
        self.assertIn("MB MORE NEEDED", caught.exception.message)
        self.assertFalse((self.base / "current").exists())

    def test_rollback_swaps_and_survives_interruption(self):
        liveslot.install(self.base, self.system("a"), "old", env())
        liveslot.install(self.base, self.system("b"), "new", env())
        liveslot.rollback(self.base, env())
        self.assertEqual(liveslot.slot_state(self.base), {"current": "old", "previous": "new"})
        for step in ("current-aside", "previous-to-current"):
            with self.subTest(step=step):
                state = liveslot.slot_state(self.base)
                with self.assertRaises(PulledOut):
                    liveslot.rollback(self.base, env(step=pull_at(step)))
                self.assertIn(self.booted(), (state["current"], state["previous"]))
                liveslot.recover(self.base)
                after = liveslot.slot_state(self.base)
                self.assertEqual(sorted(after.values()), sorted(state.values()))

    def test_rollback_without_previous_refuses(self):
        liveslot.install(self.base, self.system("a"), "only", env())
        with self.assertRaises(liveslot.SlotError):
            liveslot.rollback(self.base, env())

    def test_clear_returns_to_the_iso(self):
        liveslot.install(self.base, self.system("a"), "x", env())
        liveslot.clear(self.base)
        self.assertEqual(self.booted(), "iso")

    def test_iso_sums_reads_live_files(self):
        root = self.tmp / "iso"
        root.mkdir()
        (root / "sha256sum.txt").write_text(f"{'a' * 64}  ./live/vmlinuz\n{'b' * 64}  ./live/filesystem.squashfs\n")
        self.assertEqual(liveslot.iso_sums(root), {"vmlinuz": "a" * 64, "filesystem.squashfs": "b" * 64})

    def test_mount_target_reads_mountinfo(self):
        text = ("36 25 8:3 / /run/live/persistence/sdb3 rw,noatime - ext4 /dev/sdb3 rw\n"
                "37 25 8:4 / /run/live/medium ro,noatime - ext4 /dev/sdb4 ro\n")
        self.assertEqual(liveslot.mount_target(text, "/dev/sdb3"), ("/run/live/persistence/sdb3", True))
        self.assertEqual(liveslot.mount_target(text, "/dev/sdb4"), ("/run/live/medium", False))
        self.assertIsNone(liveslot.mount_target(text, "/dev/sdc1"))

    def test_find_slot_device_prefers_sys_label(self):
        def runner(argv, **kwargs):
            out = "/dev/sdb4\n" if argv[-3] == "LABEL=couchliteos-sys" else "/dev/sdb3\n"
            return subprocess.CompletedProcess(argv, 0, out, "")
        self.assertEqual(liveslot.find_slot_device(liveslot.Env(runner=runner)), ("/dev/sdb4", "couchliteos-sys"))

        def runner_no_sys(argv, **kwargs):
            if argv[-3] == "LABEL=couchliteos-sys":
                return subprocess.CompletedProcess(argv, 2, "", "")
            return subprocess.CompletedProcess(argv, 0, "/dev/sdb3\n", "")
        self.assertEqual(liveslot.find_slot_device(liveslot.Env(runner=runner_no_sys)), ("/dev/sdb3", "persistence"))

    def test_slot_design_is_one_known_setting(self):
        self.assertIn(liveslot.SLOT_DESIGN, liveslot.SLOT_LABELS)

    def test_updates_live_on_their_own_partition(self):
        # The QEMU persistence smoke test (0.3.0 beta): booted from live-update/ on the persistence
        # partition, live-boot mounts that partition read-only as the medium and keeps no persistence.
        self.assertEqual(liveslot.SLOT_DESIGN, "sys")

    def test_the_update_partition_leaves_room_on_an_8_gb_stick(self):
        stick, iso, persistence_min = 7_450_000_000, 1_200_000_000, 1 << 30  # an "8 GB" stick holds 7.45 GB
        self.assertLessEqual(liveslot.SYS_PARTITION_BYTES + iso + persistence_min, stick)
        self.assertGreaterEqual(liveslot.SYS_PARTITION_BYTES, liveslot.space_needed([1_200_000_000] * 2))


class VentoyJsonTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name)
        self.config = self.root / "ventoy" / "ventoy.json"

    def test_missing_file_is_empty(self):
        self.assertEqual(liveslot.load_ventoy_json(self.config), {})

    def test_bad_json_is_refused(self):
        self.config.parent.mkdir()
        for text in ("{not json", "[1, 2]", '{"persistence": {}}'):
            self.config.write_text(text)
            with self.subTest(text=text), self.assertRaises(liveslot.VentoyJsonError):
                liveslot.load_ventoy_json(self.config)

    def test_add_entry_keeps_other_keys_and_entries(self):
        data = {
            "control": [{"VTOY_DEFAULT_MENU_MODE": "1"}],
            "theme": {"file": "/ventoy/theme/x.txt"},
            "persistence": [{"image": "/kali.iso", "backend": "/kali.dat", "timeout": 5}],
            "unknown_future_plugin": [1, 2, 3],
        }
        result = liveslot.with_persistence(data, "/ISO/couchliteos.iso", "/couchliteos-persistence.dat")
        self.assertEqual(result["persistence"][0], data["persistence"][0])
        self.assertEqual(result["persistence"][1],
                         {"image": "/ISO/couchliteos.iso", "backend": "/couchliteos-persistence.dat", "autosel": 1})
        for key in ("control", "theme", "unknown_future_plugin"):
            self.assertEqual(result[key], data[key])
        self.assertEqual(len(data["persistence"]), 1, "input must not be modified")

    def test_existing_entry_for_the_image_is_updated_not_duplicated(self):
        data = {"persistence": [{"image": "/c.iso", "backend": "/old.dat", "autosel": 2}]}
        result = liveslot.with_persistence(data, "/c.iso", "/couchliteos-persistence.dat")
        self.assertEqual(result["persistence"], [{"image": "/c.iso", "backend": "/couchliteos-persistence.dat", "autosel": 2}])

    def test_save_round_trip_and_bom(self):
        self.config.parent.mkdir()
        self.config.write_bytes(b'\xef\xbb\xbf{"a": 1}')
        data = liveslot.load_ventoy_json(self.config)
        liveslot.save_ventoy_json(self.config, liveslot.with_persistence(data, "/x.iso", "/y.dat"))
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["a"], 1)
        self.assertEqual(saved["persistence"][0]["image"], "/x.iso")
        self.assertEqual(list(self.config.parent.iterdir()), [self.config])

    def test_find_ventoy_iso_by_size(self):
        (self.root / "ISO").mkdir()
        (self.root / "ISO" / "couchliteos-0.3.0-amd64.iso").write_bytes(b"x" * 4096)
        (self.root / "other.iso").write_bytes(b"x" * 4096)
        (self.root / "small.iso").write_bytes(b"x" * 2048)
        found = liveslot.find_ventoy_iso(self.root, 4096)
        self.assertEqual([p.name for p in found], ["couchliteos-0.3.0-amd64.iso", "other.iso"])
        self.assertEqual(liveslot.image_path(self.root, found[0]), "/ISO/couchliteos-0.3.0-amd64.iso")

    def test_ventoy_update_keeps_old_iso_until_good_boot(self):
        old = self.root / "couchliteos-0.3.0-amd64.iso"
        old.write_bytes(b"old" * 1000)
        self.config.parent.mkdir()
        liveslot.save_ventoy_json(self.config, {
            "persistence": [{"image": "/couchliteos-0.3.0-amd64.iso", "backend": "/couchliteos-persistence.dat", "autosel": 1},
                            {"image": "/other.iso", "backend": "/other.dat"}],
            "control": [{"VTOY_DEFAULT_IMAGE": "/couchliteos-0.3.0-amd64.iso"}],
        })
        download = self.root.parent / f"{self.root.name}-download.iso"
        download.write_bytes(b"new" * 1000)
        self.addCleanup(download.unlink)
        state = self.root / "state.json"
        sha = liveslot.sha256_file(download)
        installed = liveslot.ventoy_install_iso(self.root, old, download, "couchliteos-0.3.1-amd64.iso", "0.3.1",
                                                env(), expected_sha=sha, state_file=state)
        self.assertEqual(installed.read_bytes(), download.read_bytes())
        data = liveslot.load_ventoy_json(self.config)
        images = [entry["image"] for entry in data["persistence"]]
        self.assertEqual(images, ["/couchliteos-0.3.0-amd64.iso", "/other.iso", "/couchliteos-0.3.1-amd64.iso"])
        self.assertEqual(data["persistence"][2]["backend"], "/couchliteos-persistence.dat")
        self.assertEqual(data["control"], [{"VTOY_DEFAULT_IMAGE": "/couchliteos-0.3.1-amd64.iso"}])
        # Still on the old ISO, or new one not yet at the launcher: nothing is deleted.
        self.assertEqual(liveslot.ventoy_finish(self.root, "/couchliteos-0.3.0-amd64.iso", True, state), "waiting")
        self.assertEqual(liveslot.ventoy_finish(self.root, "/couchliteos-0.3.1-amd64.iso", False, state), "waiting")
        self.assertTrue(old.exists())
        self.assertEqual(liveslot.ventoy_finish(self.root, "/couchliteos-0.3.1-amd64.iso", True, state), "done")
        self.assertFalse(old.exists())
        self.assertFalse(state.exists())
        images = [entry["image"] for entry in liveslot.load_ventoy_json(self.config)["persistence"]]
        self.assertEqual(images, ["/other.iso", "/couchliteos-0.3.1-amd64.iso"])

    def test_ventoy_update_refuses_bad_json_before_copying(self):
        old = self.root / "c.iso"
        old.write_bytes(b"o")
        self.config.parent.mkdir()
        self.config.write_text("{oops")
        with self.assertRaises(liveslot.VentoyJsonError):
            liveslot.ventoy_install_iso(self.root, old, old, "d.iso", "1", env(), state_file=self.root / "s")
        self.assertFalse((self.root / "d.iso").exists())
        self.assertEqual(self.config.read_text(), "{oops")

    def test_ventoy_update_bad_checksum_leaves_nothing(self):
        old = self.root / "c.iso"
        old.write_bytes(b"o")
        source = self.root / "download.bin"
        source.write_bytes(b"n" * 10)
        with self.assertRaises(liveslot.SlotError):
            liveslot.ventoy_install_iso(self.root, old, source, "d.iso", "1", env(), expected_sha="0" * 64,
                                        state_file=self.root / "s")
        self.assertFalse((self.root / "d.iso").exists())
        self.assertFalse((self.root / "d.iso.part").exists())
        self.assertFalse(self.config.exists())


if __name__ == "__main__":
    unittest.main()
