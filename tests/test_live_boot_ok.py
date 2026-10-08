"""couchliteos-live-boot-ok: a live stick's running update slot is marked booted once the launcher is up."""

import importlib.machinery
import importlib.util
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "launcher"))
import couchliteos_liveslot as liveslot  # noqa: E402

SCRIPT = ROOT / "scripts" / "couchliteos-live-boot-ok"
UNIT = (ROOT / "services" / "couchliteos-live-boot-ok.service").read_text(encoding="utf-8")
LIVE_CMDLINE = ("BOOT_IMAGE=/live/vmlinuz boot=live live-media=/dev/disk/by-label/persistence "
                "live-media-path=/live-update/{slot} persistence quiet")


def load_script():
    loader = importlib.machinery.SourceFileLoader("couchliteos_live_boot_ok", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeRunner:
    """Stands in for subprocess.run: blkid names DEVICE, every other command succeeds and is recorded."""

    def __init__(self, device: str = "/dev/sdb3", blkid_code: int = 0) -> None:
        self.device = device
        self.blkid_code = blkid_code
        self.calls: list[list[str]] = []

    def __call__(self, argv, input=None, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "blkid":
            out = f"{self.device}\n" if self.blkid_code == 0 else ""
            return subprocess.CompletedProcess(argv, self.blkid_code, stdout=out, stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


class UnitFileTest(unittest.TestCase):
    def test_unit_waits_for_a_live_medium_and_for_the_launcher(self):
        self.assertIn("ConditionPathExists=/run/live/medium", UNIT)
        self.assertIn("ConditionPathExists=/run/couchliteos/launcher-ready", UNIT)
        self.assertIn("After=couchliteos-launcher.service", UNIT)

    def test_unit_does_not_need_a_ventoy_update_to_be_pending(self):
        # A dd/Etcher stick's update has no live-update.json, so the slot step must run on every live boot.
        self.assertNotIn("live-update.json", UNIT)

    def test_unit_is_never_ordered_before_a_path_socket_or_timer(self):
        self.assertNotIn("Before=", UNIT)

    def test_unit_runs_the_slot_step_and_the_ventoy_step_and_never_fails_the_boot(self):
        self.assertIn("ExecStart=-/usr/libexec/couchliteos-live-boot-ok\n", UNIT)
        self.assertIn("ExecStart=-/usr/libexec/couchliteos-persist-setup boot-ok\n", UNIT)

    def test_build_installs_the_script_and_the_hook_enables_the_unit(self):
        configure = (ROOT / "build" / "configure.sh").read_text(encoding="utf-8")
        hook = (ROOT / "config" / "live-build" / "hooks" / "live" / "0100-couchliteos.hook.chroot").read_text(
            encoding="utf-8")
        self.assertIn('install -D -m 0755 "$ROOT/scripts/couchliteos-live-boot-ok" '
                      '"$CHROOT/usr/libexec/couchliteos-live-boot-ok"', configure)
        self.assertIn("couchliteos-live-boot-ok.service", hook)


class ScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.script = load_script()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.stick = self.root / "stick"  # where the slot partition is mounted, rw by default
        self.stick.mkdir()
        self.runner = FakeRunner()
        self.logged: list[str] = []
        self.env = self.make_env()
        self.write("run/couchliteos/launcher-ready", "")
        self.set_cmdline(LIVE_CMDLINE.format(slot="current"))
        self.set_mounts("rw,noatime")
        self.make_slot("current", "0.3.0")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_env(self):
        return self.script.Env(root=self.root, runner=self.runner, log=self.logged.append)

    def write(self, relative: str, text: str) -> pathlib.Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def set_cmdline(self, text: str) -> None:
        self.write("proc/cmdline", text + "\n")

    def set_mounts(self, options: str) -> None:
        line = f"36 1 8:19 / {self.stick} {options} shared:1 - ext4 /dev/sdb3 rw\n"
        self.write("proc/self/mountinfo", line)

    def make_slot(self, name: str, version: str, ok: bool = True) -> pathlib.Path:
        slot = self.stick / liveslot.SLOT_DIR / name
        slot.mkdir(parents=True, exist_ok=True)
        for file in liveslot.FILES:
            (slot / file).write_text("x", encoding="utf-8")
        (slot / liveslot.VERSION).write_text(version + "\n", encoding="utf-8")
        if ok:
            (slot / liveslot.OK).write_text(version + "\n", encoding="utf-8")
        return slot

    def marker(self, name: str = "current") -> pathlib.Path:
        return self.stick / liveslot.SLOT_DIR / name / self.script.BOOTED

    def test_running_slot_is_the_one_the_grub_entry_names(self):
        self.assertEqual(self.script.running_slot(LIVE_CMDLINE.format(slot="current")), "current")
        self.assertEqual(self.script.running_slot(LIVE_CMDLINE.format(slot="previous")), "previous")

    def test_no_slot_for_a_plain_live_boot_or_an_unknown_path(self):
        self.assertEqual(self.script.running_slot("BOOT_IMAGE=/live/vmlinuz boot=live persistence quiet"), "")
        self.assertEqual(self.script.running_slot("live-media-path=/live-update/other"), "")
        self.assertEqual(self.script.running_slot(""), "")

    def test_marks_the_running_slot_once_the_launcher_is_up(self):
        self.assertEqual(self.script.boot_ok(self.env), "marked")
        self.assertEqual(self.marker().read_text(encoding="utf-8"), "0.3.0\n")
        self.assertEqual(self.script.main(env=self.env), 0)

    def test_marking_again_for_the_same_version_writes_nothing(self):
        self.assertEqual(self.script.boot_ok(self.env), "marked")
        self.assertEqual(self.script.boot_ok(self.env), "already")

    def test_a_new_version_in_the_same_slot_gets_its_own_marker(self):
        self.marker().write_text("0.2.9\n", encoding="utf-8")
        self.assertEqual(self.script.boot_ok(self.env), "marked")
        self.assertEqual(self.marker().read_text(encoding="utf-8"), "0.3.0\n")

    def test_an_incomplete_slot_is_left_alone(self):
        (self.stick / liveslot.SLOT_DIR / "current" / liveslot.OK).unlink()
        self.assertEqual(self.script.boot_ok(self.env), "incomplete")
        self.assertFalse(self.marker().exists())

    def test_nothing_happens_before_the_launcher_is_up(self):
        (self.root / "run/couchliteos/launcher-ready").unlink()
        self.assertEqual(self.script.boot_ok(self.env), "")
        self.assertEqual(self.runner.calls, [])
        self.assertFalse(self.marker().exists())

    def test_a_read_only_stick_is_remounted_for_the_marker_and_back(self):
        self.set_mounts("ro,relatime")
        self.assertEqual(self.script.boot_ok(self.env), "marked")
        self.assertIn(["mount", "-o", "remount,rw", str(self.stick)], self.runner.calls)
        self.assertIn(["mount", "-o", "remount,ro", str(self.stick)], self.runner.calls)
        self.assertEqual(self.marker().read_text(encoding="utf-8"), "0.3.0\n")

    def test_a_plain_live_or_installed_boot_touches_no_stick(self):
        self.set_cmdline("BOOT_IMAGE=/vmlinuz root=UUID=1234 ro quiet")
        self.assertEqual(self.script.main(env=self.env), 0)
        self.assertEqual(self.runner.calls, [])

    def test_a_stick_that_cannot_be_found_fails_quietly(self):
        self.runner = FakeRunner(blkid_code=2)
        self.env = self.make_env()
        self.assertEqual(self.script.main(env=self.env), 0)
        self.assertTrue(any("STORAGE" in line for line in self.logged), self.logged)


if __name__ == "__main__":
    unittest.main()
