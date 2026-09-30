import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
HWDETECT = ROOT / "scripts" / "moonlightos-hwdetect"

INTEL_GPU = ("0000:00:02.0", "0x8086", "0x0412", "0x030000", "i915")
SUPPORTED_NVIDIA = ("0000:01:00.0", "0x10de", "0x2684", "0x030000", "")
KEPLER_NVIDIA = ("0000:01:00.0", "0x10de", "0x0fe9", "0x030000", "")
BCM4360 = ("0000:03:00.0", "0x14e4", "0x43a0", "0x028000", "")
BCM43602 = ("0000:03:00.0", "0x14e4", "0x43ba", "0x028000", "brcmfmac")
NVIDIA_MODULES = {
    f"{name}{part}"
    for name in ("nvidia", "nvidia-current")
    for part in ("", "-drm", "-modeset", "-uvm", "-peermem")
}
BROADCOM_OPEN_MODULES = {"b43", "b43legacy", "b44", "bcma", "brcm80211", "brcmsmac", "ssb"}

# Stand-ins for modprobe and udevadm: record each call, and make "modprobe
# nouveau" bind the NVIDIA GPU the way the real module would.
FAKE_MODPROBE = """#!/bin/sh
printf 'modprobe %s\\n' "$*" >> "$FAKE_LOG"
[ "${FAKE_MODPROBE_FAIL:-0}" = 1 ] && exit 1
if [ "$1" = nouveau ]; then
  mkdir -p "$MOONLIGHTOS_SYSFS_ROOT/module/nouveau"
  ln -sfn ../../../bus/pci/drivers/nouveau "$MOONLIGHTOS_SYSFS_ROOT/bus/pci/devices/0000:01:00.0/driver"
fi
"""
FAKE_UDEVADM = """#!/bin/sh
printf 'udevadm %s\\n' "$*" >> "$FAKE_LOG"
"""


class HardwareDetectionTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.temporary.name)
        self.sysfs = base / "sys"
        self.run_root = base / "run"
        self.modules = base / "modules"
        self.share = base / "share"
        self.bin = base / "bin"
        self.log = base / "calls.log"
        for directory in (self.sysfs / "bus" / "pci" / "devices", self.run_root, self.modules, self.share, self.bin):
            directory.mkdir(parents=True)
        self.cmdline = base / "cmdline"
        self.cmdline.write_text("BOOT_IMAGE=/live/vmlinuz boot=live components\n")
        self.nvidia_ids = self.share / "nvidia.ids"
        self.broadcom_ids = self.share / "broadcom-sta.ids"
        self.modprobe = self.bin / "modprobe"
        self.udevadm = self.bin / "udevadm"
        self.modprobe.write_text(FAKE_MODPROBE)
        self.udevadm.write_text(FAKE_UDEVADM)
        self.modprobe.chmod(0o755)
        self.udevadm.chmod(0o755)
        self.state = self.run_root / "moonlightos-hardware"

    def tearDown(self):
        self.temporary.cleanup()

    def add_device(self, slot, vendor, device, pci_class, driver, boot_vga=None):
        entry = self.sysfs / "bus" / "pci" / "devices" / slot
        entry.mkdir()
        (entry / "vendor").write_text(vendor + "\n")
        (entry / "device").write_text(device + "\n")
        (entry / "class").write_text(pci_class + "\n")
        if boot_vga is not None:
            (entry / "boot_vga").write_text("1\n" if boot_vga else "0\n")
        if driver:
            (entry / "driver").symlink_to(f"../../../bus/pci/drivers/{driver}")

    def nvidia_iso(self):
        """What the NVIDIA ISO installs: the supported-ID list and the DKMS modules."""
        self.nvidia_ids.write_text("# NVIDIA 550 supported devices\n10DE2684\n10DE1B80\n")
        dkms = self.modules / "updates" / "dkms"
        dkms.mkdir(parents=True, exist_ok=True)
        for name in ("nvidia-current", "nvidia-current-drm", "nvidia-current-modeset", "nvidia-current-uvm"):
            (dkms / f"{name}.ko.xz").write_bytes(b"")

    def broadcom_sta(self):
        """What both ISOs install for Broadcom: the wl module and its device list."""
        self.broadcom_ids.write_text("14E443A0\n14E44331\n")
        dkms = self.modules / "updates" / "dkms"
        dkms.mkdir(parents=True, exist_ok=True)
        (dkms / "wl.ko.xz").write_bytes(b"")

    def apple(self):
        dmi = self.sysfs / "class" / "dmi" / "id"
        dmi.mkdir(parents=True)
        (dmi / "sys_vendor").write_text("Apple Inc.\n")

    def hwdetect(self, command, extra_env=None):
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "MOONLIGHTOS_SYSFS_ROOT": str(self.sysfs),
            "MOONLIGHTOS_PROC_CMDLINE": str(self.cmdline),
            "MOONLIGHTOS_RUN_ROOT": str(self.run_root),
            "MOONLIGHTOS_MODULE_DIR": str(self.modules),
            "MOONLIGHTOS_NVIDIA_IDS": str(self.nvidia_ids),
            "MOONLIGHTOS_BROADCOM_STA_IDS": str(self.broadcom_ids),
            "MOONLIGHTOS_MODPROBE": str(self.modprobe),
            "MOONLIGHTOS_UDEVADM": str(self.udevadm),
            "FAKE_LOG": str(self.log),
            **(extra_env or {}),
        }
        return subprocess.run(
            [sys.executable, str(HWDETECT), command], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
        )

    def early(self):
        result = self.hwdetect("early")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads((self.state / "state.json").read_text())

    def modprobe_config(self):
        return (self.run_root / "modprobe.d" / "moonlightos-hardware.conf").read_text()

    def blacklisted(self):
        return {
            line.split()[1] for line in self.modprobe_config().splitlines()
            if line.startswith("blacklist ")
        }

    def loaded(self):
        text = (self.run_root / "modules-load.d" / "moonlightos-hardware.conf").read_text()
        return [line for line in text.splitlines() if line and not line.startswith("#")]

    def hardware_env(self):
        text = (self.state / "hardware.env").read_text()
        return dict(line.split("=", 1) for line in text.splitlines())

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_shebang_isolates_python_from_the_callers_environment(self):
        self.assertEqual(HWDETECT.read_text().splitlines()[0], "#!/usr/bin/python3 -I")

    def test_usage(self):
        self.assertEqual(self.hwdetect("bogus").returncode, 64)

    def test_intel_machine_on_general_iso_changes_nothing(self):
        self.add_device(*INTEL_GPU, boot_vga=True)
        state = self.early()
        self.assertEqual(state["nvidia_mode"], "none")
        self.assertEqual(self.blacklisted(), set())
        self.assertEqual(self.loaded(), [])
        result = self.hwdetect("late")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.hardware_env(), {
            "MOONLIGHTOS_GPU_DRIVER": "i915",
            "MOONLIGHTOS_GPU_ID": "8086:0412",
            "MOONLIGHTOS_VIDEO_DECODE": "auto",
        })
        self.assertEqual((self.state / "compositor.env").read_text(), "")
        self.assertNotIn("modprobe nouveau", self.calls())

    def test_intel_machine_on_nvidia_iso_blocks_every_nvidia_module(self):
        self.nvidia_iso()
        self.add_device(*INTEL_GPU, boot_vga=True)
        state = self.early()
        self.assertEqual(state["nvidia_mode"], "none")
        self.assertFalse(state["nouveau_fallback"])
        self.assertEqual(self.blacklisted(), NVIDIA_MODULES)
        self.assertEqual(self.loaded(), [])
        self.assertEqual(self.hwdetect("late").returncode, 0)
        self.assertNotIn("modprobe nouveau", self.calls())
        self.assertEqual(self.hardware_env()["MOONLIGHTOS_GPU_DRIVER"], "i915")

    def test_supported_nvidia_gpu_loads_the_proprietary_driver(self):
        self.nvidia_iso()
        self.add_device(*SUPPORTED_NVIDIA, boot_vga=True)
        state = self.early()
        self.assertEqual(state["nvidia_mode"], "proprietary")
        self.assertEqual(self.loaded(), ["nvidia-drm"])
        self.assertEqual(self.blacklisted(), {"nouveau"})
        # udev binds the proprietary driver after the early stage.
        (self.sysfs / "bus" / "pci" / "devices" / SUPPORTED_NVIDIA[0] / "driver").symlink_to(
            "../../../bus/pci/drivers/nvidia")
        self.assertEqual(self.hwdetect("late").returncode, 0)
        env = self.hardware_env()
        self.assertEqual(env["MOONLIGHTOS_GPU_DRIVER"], "nvidia")
        self.assertEqual(env["MOONLIGHTOS_VIDEO_DECODE"], "auto")
        self.assertEqual((self.state / "compositor.env").read_text(), "WLR_NO_HARDWARE_CURSORS=1\n")
        self.assertNotIn("modprobe nouveau", self.calls())

    def test_kepler_gpu_on_nvidia_iso_falls_back_to_nouveau(self):
        self.nvidia_iso()
        self.add_device(*INTEL_GPU, boot_vga=False)
        self.add_device(*KEPLER_NVIDIA, boot_vga=True)
        state = self.early()
        self.assertEqual(state["nvidia_mode"], "nouveau")
        self.assertTrue(state["nouveau_fallback"])
        self.assertEqual(self.blacklisted(), NVIDIA_MODULES)
        self.assertNotIn("nouveau", self.blacklisted())
        self.assertEqual(self.loaded(), [])
        result = self.hwdetect("late")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("modprobe nouveau", self.calls())
        self.assertEqual(self.hardware_env(), {
            "MOONLIGHTOS_GPU_DRIVER": "nouveau",
            "MOONLIGHTOS_GPU_ID": "10de:0fe9",
            "MOONLIGHTOS_VIDEO_DECODE": "software",
        })
        self.assertEqual((self.state / "compositor.env").read_text(), "")
        summary = (self.state / "summary.txt").read_text()
        self.assertIn("Moonlight video decode: software H.264", summary)
        self.assertIn("not supported by the installed NVIDIA driver: 10de:0fe9", summary)

    def test_nouveau_is_not_loaded_twice(self):
        self.nvidia_iso()
        self.add_device(*KEPLER_NVIDIA, boot_vga=True)
        self.early()
        (self.sysfs / "module" / "nouveau").mkdir(parents=True)
        self.assertEqual(self.hwdetect("late").returncode, 0)
        self.assertNotIn("modprobe nouveau", self.calls())

    def test_failed_nouveau_fallback_is_reported(self):
        self.nvidia_iso()
        self.add_device(*KEPLER_NVIDIA, boot_vga=True)
        self.early()
        result = self.hwdetect("late", {"FAKE_MODPROBE_FAIL": "1"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("modprobe nouveau failed", result.stderr)
        # The launcher still gets hints: the GPU has no driver.
        self.assertEqual(self.hardware_env()["MOONLIGHTOS_GPU_DRIVER"], "none")

    def test_basic_graphics_uses_nouveau_on_a_supported_gpu(self):
        self.nvidia_iso()
        self.add_device(*SUPPORTED_NVIDIA, boot_vga=True)
        self.cmdline.write_text("boot=live components moonlightos.gpu=basic\n")
        state = self.early()
        self.assertTrue(state["basic_graphics"])
        self.assertEqual(state["nvidia_mode"], "nouveau")
        self.assertTrue(state["nouveau_fallback"])
        self.assertEqual(self.blacklisted(), NVIDIA_MODULES)
        self.assertEqual(self.loaded(), [])
        self.assertIn("Basic Graphics", " ".join(state["notes"]))

    def test_nvidia_gpu_on_general_iso_uses_nouveau_without_a_fallback_step(self):
        self.add_device(*SUPPORTED_NVIDIA, boot_vga=True)
        state = self.early()
        self.assertFalse(state["nvidia_driver_installed"])
        self.assertEqual(state["nvidia_mode"], "nouveau")
        self.assertFalse(state["nouveau_fallback"])
        self.assertEqual(self.blacklisted(), set())
        (self.sysfs / "bus" / "pci" / "devices" / SUPPORTED_NVIDIA[0] / "driver").symlink_to(
            "../../../bus/pci/drivers/nouveau")
        self.assertEqual(self.hwdetect("late").returncode, 0)
        self.assertNotIn("modprobe nouveau", self.calls())
        self.assertEqual(self.hardware_env()["MOONLIGHTOS_VIDEO_DECODE"], "software")

    def test_ids_file_without_the_dkms_module_is_not_an_installed_driver(self):
        self.nvidia_ids.write_text("10DE2684\n")
        self.add_device(*SUPPORTED_NVIDIA, boot_vga=True)
        state = self.early()
        self.assertFalse(state["nvidia_driver_installed"])
        self.assertEqual(self.loaded(), [])

    def test_bcm4360_uses_wl_and_blocks_the_open_drivers(self):
        self.broadcom_sta()
        self.add_device(*INTEL_GPU, boot_vga=True)
        self.add_device(*BCM4360)
        state = self.early()
        self.assertEqual(state["broadcom"], "wl")
        self.assertEqual(self.blacklisted(), BROADCOM_OPEN_MODULES)

    def test_open_driver_broadcom_chip_keeps_its_driver_and_blocks_wl(self):
        self.broadcom_sta()
        self.add_device(*INTEL_GPU, boot_vga=True)
        self.add_device(*BCM43602)
        state = self.early()
        self.assertEqual(state["broadcom"], "open")
        self.assertEqual(self.blacklisted(), {"wl"})

    def test_machine_without_broadcom_blocks_wl(self):
        self.broadcom_sta()
        self.add_device(*INTEL_GPU, boot_vga=True)
        self.early()
        self.assertEqual(self.blacklisted(), {"wl"})

    def test_apple_hardware_loads_applesmc(self):
        self.apple()
        self.add_device(*INTEL_GPU, boot_vga=True)
        state = self.early()
        self.assertTrue(state["apple"])
        self.assertEqual(self.loaded(), ["applesmc"])

    def test_imac_2013_on_nvidia_iso(self):
        self.nvidia_iso()
        self.broadcom_sta()
        self.apple()
        self.add_device(*INTEL_GPU, boot_vga=False)
        self.add_device(*KEPLER_NVIDIA, boot_vga=True)
        self.add_device(*BCM4360)
        state = self.early()
        self.assertEqual(self.blacklisted(), NVIDIA_MODULES | BROADCOM_OPEN_MODULES)
        self.assertEqual(self.loaded(), ["applesmc"])
        self.assertTrue(state["nouveau_fallback"])

    def test_early_rewrites_rather_than_appends(self):
        self.nvidia_iso()
        self.add_device(*INTEL_GPU, boot_vga=True)
        self.early()
        first = self.modprobe_config()
        self.early()
        self.assertEqual(self.modprobe_config(), first)

    def test_late_without_early_decides_itself(self):
        self.nvidia_iso()
        self.add_device(*KEPLER_NVIDIA, boot_vga=True)
        result = self.hwdetect("late")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("early did not run", result.stderr)
        self.assertIn("modprobe nouveau", self.calls())

    def test_late_waits_for_udev(self):
        self.add_device(*INTEL_GPU, boot_vga=True)
        self.early()
        self.hwdetect("late")
        self.assertEqual(self.calls()[0], "udevadm settle --timeout=10")

    def test_report(self):
        self.add_device(*INTEL_GPU, boot_vga=True)
        result = self.hwdetect("report")
        self.assertEqual(result.returncode, 1)
        self.assertIn("has not run on this boot", result.stdout)
        self.early()
        self.hwdetect("late")
        result = self.hwdetect("report")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Display driver: i915", result.stdout)
        self.assertIn("Blacklisted for this boot: none", result.stdout)

    def test_output_files_are_world_readable(self):
        self.add_device(*INTEL_GPU, boot_vga=True)
        self.early()
        self.hwdetect("late")
        for path in (
            self.run_root / "modprobe.d" / "moonlightos-hardware.conf",
            self.run_root / "modules-load.d" / "moonlightos-hardware.conf",
            self.state / "hardware.env",
            self.state / "compositor.env",
            self.state / "summary.txt",
        ):
            self.assertEqual(path.stat().st_mode & 0o777, 0o644, path)


if __name__ == "__main__":
    unittest.main()
