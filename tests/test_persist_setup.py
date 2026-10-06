"""couchliteos-persist-setup: persistence.conf, dd/Etcher partition planning, Ventoy setup."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "launcher"))
LOADER = importlib.machinery.SourceFileLoader("couchliteos_persist_setup", str(ROOT / "scripts" / "couchliteos-persist-setup"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
persist = importlib.util.module_from_spec(SPEC)
sys.modules[LOADER.name] = persist  # dataclasses look their module up by name
LOADER.exec_module(persist)
import couchliteos_liveslot as liveslot  # noqa: E402

GIB = 1 << 30
MIB = 1 << 20

# `sfdisk --json` of a 16 GB stick written with dd from a Debian live-build hybrid ISO:
# partition 1 covers the image from sector 0, partition 2 is the EFI image inside it.
DOS_ISO = {"partitiontable": {"label": "dos", "id": "0x1d2e3f4a", "device": "/dev/sdb", "unit": "sectors",
                              "sectorsize": 512, "partitions": [
                                  {"node": "/dev/sdb1", "start": 0, "size": 4194304, "type": "0", "bootable": True},
                                  {"node": "/dev/sdb2", "start": 9028, "size": 5760, "type": "ef"}]}}
GPT_ISO = {"partitiontable": {"label": "gpt", "device": "/dev/sdb", "unit": "sectors", "firstlba": 64,
                              "lastlba": 4194300, "sectorsize": 512, "partitions": [
                                  {"node": "/dev/sdb1", "start": 64, "size": 4194200, "type": "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"},
                                  {"node": "/dev/sdb2", "start": 9028, "size": 5760, "type": "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"}]}}
STICK = 16 * GIB


def lsblk(children, disk="/dev/sdb", size=STICK, kind="disk"):
    return {"blockdevices": [{"name": disk.rsplit("/", 1)[-1], "path": disk, "type": kind, "size": size,
                              "log-sec": 512, "ro": False, "rm": True, "children": children}]}


DD_CHILDREN = [
    {"name": "sdb1", "path": "/dev/sdb1", "type": "part", "fstype": "iso9660", "label": "CouchLiteOS", "size": 2 * GIB,
     "mountpoint": "/run/live/medium"},
    {"name": "sdb2", "path": "/dev/sdb2", "type": "part", "fstype": "vfat", "label": "", "size": 2949120},
]
VENTOY_CHILDREN = [
    {"name": "sdb1", "path": "/dev/sdb1", "type": "part", "fstype": "exfat", "label": "Ventoy", "size": STICK - 32 * MIB},
    {"name": "sdb2", "path": "/dev/sdb2", "type": "part", "fstype": "vfat", "label": "VTOYEFI", "size": 32 * MIB},
]


class FakeRunner:
    def __init__(self, medium="/dev/sdb1", children=DD_CHILDREN, table=DOS_ISO, iso_size=4096, inverse_kind="disk"):
        self.calls = []
        self.inputs = {}
        self.medium, self.children, self.table, self.iso_size = medium, children, table, iso_size
        self.inverse_kind = inverse_kind
        self.fail = set()

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if kwargs.get("input") is not None:
            self.inputs[argv[0]] = kwargs["input"]
        out, code = "", 0
        if argv[0] in self.fail:
            code = 1
        elif argv[:2] == ["findmnt", "-n"]:
            out = self.medium + "\n"
        elif argv[0] == "lsblk" and "--inverse" in argv:
            out = json.dumps({"blockdevices": [{"name": "x", "path": self.medium, "type": "part",
                                                "children": [{"name": "sdb", "path": "/dev/sdb", "type": self.inverse_kind}]}]})
        elif argv[0] == "lsblk":
            out = json.dumps(lsblk(self.children))
        elif argv[:2] == ["sfdisk", "--json"]:
            out = json.dumps(self.table)
        elif argv[:2] == ["blockdev", "--getsize64"]:
            out = f"{self.iso_size}\n"
        elif argv[:2] == ["dmsetup", "info"]:
            code = 1
        return subprocess.CompletedProcess(argv, code, out, "")

    def ran(self, *prefix):
        return [call for call in self.calls if call[:len(prefix)] == list(prefix)]


class Base(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = pathlib.Path(directory.name)
        self.root = self.tmp / "root"
        (self.root / "run/live/medium").mkdir(parents=True)
        (self.root / "proc/self").mkdir(parents=True)
        (self.root / "proc/cmdline").write_text("boot=live components persistence\n")
        (self.root / "proc/self/mountinfo").write_text("22 1 0:20 / / rw - overlay overlay rw\n")
        (self.root / "etc").mkdir()
        (self.root / "etc/machine-id").write_text("0123456789abcdef0123456789abcdef\n")
        (self.root / "var/lib/couchliteos").mkdir(parents=True)
        (self.root / "var/lib/couchliteos/config.ini").write_text("[launcher]\n")

    def env(self, runner, **kwargs):
        return persist.Env(runner=runner, root=self.root, free_bytes=kwargs.pop("free", lambda path: 10 * GIB),
                           euid=lambda: 0, status_file=self.tmp / "status.json", work_dir=self.tmp / "work",
                           log=lambda text: None, **kwargs)


class ConfTest(unittest.TestCase):
    def test_bind_mounts_only_and_the_known_paths(self):
        conf = persist.persistence_conf()
        lines = [line for line in conf.splitlines() if line and not line.startswith("#")]
        self.assertFalse(any("union" in line for line in lines))
        smoke = (ROOT / "tests" / "qemu-persistence-smoke.sh").read_text()
        for line in ("/var/lib/couchliteos source=couchliteos-state", "/var/log/couchliteos source=couchliteos-logs",
                     "/var/lib/tailscale source=tailscale-state", "/var/lib/bluetooth source=bluetooth-state",
                     "/etc/NetworkManager/system-connections source=nm-connections"):
            self.assertIn(line, lines)
            self.assertIn(line, smoke)  # existing sticks keep their data under the same names
        for path in ("/home/couchliteos", "/var/lib/flatpak"):
            self.assertTrue(any(line.startswith(path + " ") for line in lines), path)
        self.assertIn("/etc link,source=couchliteos-identity", lines)
        self.assertNotIn("/etc/ssh", conf)
        self.assertIn("/etc/ssh link,source=ssh-host-keys", persist.persistence_conf(ssh_keys=True))
        sources = [line.split("source=")[1] for line in lines]
        self.assertEqual(len(sources), len(set(sources)))
        for line in lines:  # persistence.conf(5): absolute path, no '/' itself
            path = line.split()[0]
            self.assertTrue(path.startswith("/") and path != "/", line)


class DdPlanTest(unittest.TestCase):
    def test_dos_hybrid_iso_appends_after_the_iso(self):
        plan = persist.plan_dd(DOS_ISO, STICK, 512, "persistence")
        self.assertEqual(len(plan.partitions), 1)
        part = plan.partitions[0]
        self.assertEqual(part.number, 3)
        self.assertEqual(part.start, 4194304)  # end of the ISO, already 1 MiB aligned
        self.assertEqual(part.start % 2048, 0)
        self.assertLessEqual(part.start + part.size, STICK // 512)
        self.assertEqual(plan.sfdisk_script, f"start=4194304, size={part.size}, type=83\n")
        self.assertFalse(plan.relocate_gpt)

    def test_unaligned_end_rounds_up(self):
        table = json.loads(json.dumps(DOS_ISO))
        table["partitiontable"]["partitions"][0]["size"] = 4194305
        plan = persist.plan_dd(table, STICK, 512, "persistence")
        self.assertEqual(plan.partitions[0].start, 4196352)

    def test_never_overlaps_any_partition(self):
        plan = persist.plan_dd(DOS_ISO, STICK, 512, "sys")
        ends = [p["start"] + p["size"] for p in DOS_ISO["partitiontable"]["partitions"]]
        for part in plan.partitions:
            self.assertGreaterEqual(part.start, max(ends))
        self.assertEqual([p.label for p in plan.partitions], ["couchliteos-sys", "persistence"])
        self.assertEqual([p.number for p in plan.partitions], [3, 4])
        sys_part, data = plan.partitions
        self.assertEqual(sys_part.size * 512, liveslot.SYS_PARTITION_BYTES)
        self.assertEqual(data.start, sys_part.start + sys_part.size)

    def test_gpt_relocates_backup_header_and_names_partition(self):
        plan = persist.plan_dd(GPT_ISO, STICK, 512, "persistence")
        self.assertTrue(plan.relocate_gpt)
        self.assertIn(f"type={persist.LINUX_GPT_TYPE}, name=persistence", plan.sfdisk_script)
        part = plan.partitions[0]
        self.assertLessEqual(part.start + part.size, STICK // 512 - 34)

    def test_refuses_under_1_gb(self):
        with self.assertRaises(persist.SetupError) as caught:
            persist.plan_dd(DOS_ISO, 4194304 * 512 + 900 * MIB, 512, "persistence")
        self.assertEqual(caught.exception.message, persist.MSG_SMALL)

    def test_refuses_full_mbr(self):
        table = json.loads(json.dumps(DOS_ISO))
        table["partitiontable"]["partitions"] += [
            {"node": "/dev/sdb3", "start": 4194304, "size": 2048, "type": "83"},
            {"node": "/dev/sdb4", "start": 4196352, "size": 2048, "type": "83"}]
        with self.assertRaises(persist.SetupError):
            persist.plan_dd(table, STICK, 512, "persistence")

    def test_rufus_iso_mode_fills_the_stick_and_is_refused(self):
        table = {"partitiontable": {"label": "dos", "sectorsize": 512,
                                    "partitions": [{"node": "/dev/sdb1", "start": 2048, "size": STICK // 512 - 2048}]}}
        with self.assertRaises(persist.SetupError):
            persist.plan_dd(table, STICK, 512, "persistence")

    def test_requested_size(self):
        plan = persist.plan_dd(DOS_ISO, STICK, 512, "persistence", size_bytes=4 * GIB)
        self.assertEqual(plan.partitions[0].size * 512, 4 * GIB)

    def test_partition_node_names(self):
        self.assertEqual(persist.partition_node("/dev/sdb", 3), "/dev/sdb3")
        self.assertEqual(persist.partition_node("/dev/mmcblk0", 3), "/dev/mmcblk0p3")
        self.assertEqual(persist.partition_node("/dev/nvme0n1", 3), "/dev/nvme0n1p3")


class DetectAndSetupTest(Base):
    def test_detect_dd_ventoy_and_optical(self):
        self.assertEqual(persist.detect(self.env(FakeRunner())).kind, "dd")
        self.assertEqual(persist.detect(self.env(FakeRunner("/dev/mapper/ventoy", VENTOY_CHILDREN))).kind, "ventoy")
        self.assertEqual(persist.detect(self.env(FakeRunner("/dev/sr0", [], inverse_kind="rom"))).kind, "optical")

    def test_plan_prints_and_changes_nothing(self):
        runner = FakeRunner()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(persist.main(["setup", "--plan"], self.env(runner)), 0)
        text = output.getvalue()
        self.assertIn("sfdisk --no-reread --no-tell-kernel --append /dev/sdb", text)
        self.assertIn("mkfs.ext4 -F -q -m 1 -L persistence /dev/sdb3", text)
        self.assertIn("start=4194304", text)
        for tool in ("mkfs.ext4", "mount", "partx", "cp"):
            self.assertEqual(runner.ran(tool), [], tool)
        self.assertEqual([c for c in runner.calls if c[0] == "sfdisk" and "--json" not in c], [])
        self.assertFalse((self.tmp / "status.json").exists())

    def test_dd_setup_runs_steps_in_order_and_writes_conf(self):
        runner = FakeRunner()
        self.assertEqual(persist.main(["setup"], self.env(runner)), 0)
        tools = [call[0] for call in runner.calls if call[0] not in ("findmnt", "lsblk", "cp", "sync")]
        self.assertEqual(tools, ["sfdisk", "sfdisk", "partx", "udevadm", "mkfs.ext4", "mount", "umount"])
        self.assertTrue(runner.inputs["sfdisk"].startswith("start=4194304, size="))
        data = self.tmp / "work" / "data"
        conf = (data / "persistence.conf").read_text()
        self.assertEqual(conf, persist.persistence_conf())
        copied = [call for call in runner.ran("cp", "-a", "--")]
        self.assertIn(["cp", "-a", "--", str(self.root / "var/lib/couchliteos"), str(data / "couchliteos-state")], copied)
        self.assertIn(["cp", "-a", "--", str(self.root / "etc/machine-id"), str(data / "couchliteos-identity/machine-id")], copied)
        self.assertEqual(json.loads((self.tmp / "status.json").read_text())["phase"], "done")

    def test_failure_reports_in_status(self):
        runner = FakeRunner()
        runner.fail.add("mkfs.ext4")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(persist.main(["setup"], self.env(runner)), 1)
        status = json.loads((self.tmp / "status.json").read_text())
        self.assertEqual(status["phase"], "failed")

    def test_already_persistent_or_installed_refuses(self):
        (self.root / "proc/self/mountinfo").write_text("40 26 8:3 /couchliteos-state /run/live/persistence/sdb3 rw - ext4 /dev/sdb3 rw\n")
        report = persist.status_report(self.env(FakeRunner()))
        self.assertTrue(report["persistent"])
        self.assertFalse(report["can_setup"])
        (self.root / "run/live/medium").rmdir()
        (self.root / "proc/cmdline").write_text("root=/dev/sda2 ro quiet\n")
        self.assertFalse(persist.status_report(self.env(FakeRunner()))["live"])

    def test_status_offers_setup_on_a_dd_stick(self):
        report = persist.status_report(self.env(FakeRunner()))
        self.assertTrue(report["can_setup"])
        self.assertEqual(report["medium"], "dd")

    def test_ssh_keys_only_when_present(self):
        (self.root / "etc/ssh").mkdir()
        (self.root / "etc/ssh/ssh_host_ed25519_key").write_text("k")
        (self.root / "etc/ssh/sshd_config").write_text("x")
        env = self.env(FakeRunner())
        self.assertEqual([p.name for p in persist.ssh_host_keys(env)], ["ssh_host_ed25519_key"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            persist.main(["conf"], env)
        self.assertIn("/etc/ssh link,source=ssh-host-keys", output.getvalue())


class VentoyTest(Base):
    def setUp(self):
        super().setUp()
        self.ventoy = self.tmp / "work" / "ventoy"
        self.ventoy.mkdir(parents=True)
        (self.ventoy / "ISO").mkdir()
        (self.ventoy / "ISO" / "couchliteos-0.3.0-amd64.iso").write_bytes(b"i" * 4096)
        (self.ventoy / "ventoy").mkdir()
        self.config = self.ventoy / "ventoy" / "ventoy.json"

    def runner(self):
        return FakeRunner("/dev/mapper/ventoy", VENTOY_CHILDREN)

    def test_setup_creates_image_and_adds_entry_keeping_others(self):
        self.config.write_text(json.dumps({"theme": {"file": "/t.txt"},
                                           "persistence": [{"image": "/kali.iso", "backend": "/kali.dat"}]}))
        runner = self.runner()
        self.assertEqual(persist.main(["setup"], self.env(runner)), 0)
        self.assertEqual(runner.ran("dmsetup", "create"), [["dmsetup", "create", liveslot.VENTOY_DM]])
        self.assertEqual(runner.inputs["dmsetup"], f"0 {(STICK - 32 * MIB) // 512} linear /dev/sdb1 0\n")
        image = str(self.ventoy / "couchliteos-persistence.dat")
        self.assertEqual(runner.ran("fallocate"), [["fallocate", "-l", str((10 * GIB - 4096 - 256 * MIB) // MIB * MIB), image]])
        self.assertEqual(runner.ran("mkfs.ext4")[0][-3:], ["-L", "persistence", image])
        self.assertEqual(runner.ran("dmsetup", "remove"), [["dmsetup", "remove", liveslot.VENTOY_DM]])
        data = json.loads(self.config.read_text())
        self.assertEqual(data["theme"], {"file": "/t.txt"})
        self.assertEqual(data["persistence"], [
            {"image": "/kali.iso", "backend": "/kali.dat"},
            {"image": "/ISO/couchliteos-0.3.0-amd64.iso", "backend": "/couchliteos-persistence.dat", "autosel": 1}])

    def test_size_capped_at_16_gb_and_4_gb_on_fat32(self):
        self.assertEqual(persist.ventoy_image_bytes(100 * GIB, 2 * GIB, "exfat"), 16 * GIB)
        self.assertEqual(persist.ventoy_image_bytes(100 * GIB, 2 * GIB, "vfat"), 4 * GIB - MIB)
        with self.assertRaises(persist.SetupError):
            persist.ventoy_image_bytes(3 * GIB, 2 * GIB, "exfat")

    def test_creates_ventoy_json_when_absent(self):
        persist.main(["setup"], self.env(self.runner()))
        self.assertEqual(json.loads(self.config.read_text())["persistence"][0]["image"], "/ISO/couchliteos-0.3.0-amd64.iso")

    def test_bad_json_refused_before_anything_is_written(self):
        self.config.write_text("{ broken")
        runner = self.runner()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(persist.main(["setup"], self.env(runner)), 1)
        self.assertIn("NOT VALID JSON", err.getvalue())
        self.assertEqual(runner.ran("fallocate"), [])
        self.assertEqual(runner.ran("mkfs.ext4"), [])
        self.assertEqual(self.config.read_text(), "{ broken")

    def test_plan_mode_mounts_read_only_and_writes_nothing(self):
        runner = self.runner()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(persist.main(["setup", "--plan"], self.env(runner)), 0)
        self.assertIn("couchliteos-persistence.dat", output.getvalue())
        self.assertIn("ventoy/ventoy.json", output.getvalue())
        mounts = runner.ran("mount")
        self.assertEqual(mounts[0][:3], ["mount", "-o", "ro"])
        self.assertEqual(runner.ran("fallocate"), [])
        self.assertFalse(self.config.exists())

    def test_fallocate_failure_falls_back_to_truncate(self):
        runner = self.runner()
        runner.fail.add("fallocate")
        persist.main(["setup"], self.env(runner))
        self.assertEqual(len(runner.ran("truncate", "-s")), 1)

    def test_iso_not_found(self):
        runner = self.runner()
        runner.iso_size = 1
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(persist.main(["setup"], self.env(runner)), 1)


if __name__ == "__main__":
    unittest.main()
