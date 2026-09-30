"""Boot speed: the installed GRUB menu, the launcher's ordering, and boot-timing diagnostics."""

import collections
import importlib.machinery
import importlib.util
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_script(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class InstalledGrubMenuTest(unittest.TestCase):
    """Installed systems boot straight in; the live ISO keeps its own menu."""

    def test_installed_grub_menu_is_hidden_with_a_one_second_timeout(self):
        cfg = ROOT / "overlay/etc/default/grub.d/20-couchliteos.cfg"
        # grub-mkconfig sources the file with sh, so evaluate it the same way.
        result = subprocess.run(
            ["sh", "-c", '. "$1"; printf "%s|%s|%s" "$GRUB_TIMEOUT_STYLE" "$GRUB_TIMEOUT" "$GRUB_CMDLINE_LINUX_DEFAULT"', "sh", str(cfg)],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(result.stdout, "hidden|1|quiet ipv6.disable=1")

    def test_live_iso_menu_is_not_changed_by_the_installed_grub_settings(self):
        hook = (ROOT / "config/live-build/hooks/live/0100-autoboot.hook.binary").read_text()
        self.assertIn("set timeout=3", hook)
        self.assertNotIn("hidden", hook)
        self.assertNotIn("GRUB_TIMEOUT", hook)
        basic = (ROOT / "config/profiles/nvidia/hooks/0300-basic-graphics.hook.binary").read_text()
        self.assertIn("Basic Graphics", basic)
        self.assertNotIn("hidden", basic)
        # The live menu is generated on the ISO; /etc/default/grub.d only reaches
        # systems that run grub-mkconfig (the installed copy).
        build = (ROOT / "build/build.sh").read_text()
        self.assertNotIn("GRUB_TIMEOUT", build)

    def test_install_smoke_test_never_drives_the_installed_menu(self):
        smoke = (ROOT / "tests/qemu-install-smoke.sh").read_text()
        # Keys are sent only by qemu_iso_boot.py, and only to the ISO's own menu.
        self.assertNotIn("sendkey", smoke)
        self.assertEqual(smoke.count("qemu_iso_boot.py"), 1)
        boot_and_wait = smoke.split("boot_and_wait() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('grep -q "$marker" "$BOOT_LOG"', boot_and_wait)
        self.assertNotIn("screendump", boot_and_wait.split("persistence-write", 2)[0])
        # The installed boot is judged by serial markers, not by a menu screenshot.
        self.assertIn("COUCHLITEOS_SMOKE_PERSISTENCE_WRITTEN", smoke)
        self.assertIn("COUCHLITEOS_SMOKE_PERSISTENCE_READY", smoke)


ORDERING_KEYS = {"After", "Before", "Requires", "Wants", "Requisite", "BindsTo"}
PULLS = ("After", "Requires", "Wants", "Requisite", "BindsTo")


def read_units():
    """Ordering keys of every unit we ship, drop-ins merged: {unit: {key: [names]}}."""
    units = {}
    files = [(p.name, p) for p in sorted((ROOT / "services").glob("*"))
             if p.suffix in {".service", ".path"}]
    files += [(p.parent.name.removesuffix(".d"), p)
              for p in sorted((ROOT / "overlay/etc/systemd/system").glob("*.d/*.conf"))]
    for name, path in files:
        entry = units.setdefault(name, collections.defaultdict(list))
        for line in path.read_text().splitlines():
            key, separator, value = line.strip().partition("=")
            if separator and key in ORDERING_KEYS:
                entry[key].extend(value.split())
    return units


def waits_for(unit, units):
    """Every unit that `unit` starts after or pulls in, directly or through our own units."""
    seen, queue = set(), [unit]
    while queue:
        current = queue.pop()
        found = {name for key in PULLS for name in units.get(current, {}).get(key, [])}
        found |= {other for other, keys in units.items() if current in keys.get("Before", [])}
        for name in sorted(found - seen):
            seen.add(name)
            queue.append(name)
    return seen


class LauncherOrderingTest(unittest.TestCase):
    """The launcher must appear without waiting for the network, Tailscale or USB/IP."""

    NOT_ON_THE_LAUNCHER_PATH = {
        "network-online.target",
        "NetworkManager-wait-online.service",
        "couchliteos-network-ready.service",
        "couchliteos-firewall.service",
        "couchliteos-usbipd.service",
        "tailscaled.service",
        "tailscale-online.target",
        "couchliteos-tailscale-enroll.service",
        "couchliteos-tailscale-ssh.service",
        "couchliteos-usbip-reconcile.service",
    }

    def test_ordering_reader_sees_the_real_launcher_dependencies(self):
        # Guards the checks below against passing because nothing was parsed.
        path = waits_for("couchliteos-launcher.service", read_units())
        for expected in ("seatd.service", "couchliteos-hwdetect.service",
                         "couchliteos-hwdetect-early.service", "bluetooth.service",
                         "couchliteos-audio.service", "couchliteos-bluetooth.service"):
            self.assertIn(expected, path)

    def test_launcher_does_not_wait_for_network_tailscale_or_usbip(self):
        path = waits_for("couchliteos-launcher.service", read_units())
        self.assertEqual(path & self.NOT_ON_THE_LAUNCHER_PATH, set())
        self.assertFalse({name for name in path if "network-online" in name or "wait-online" in name})

    def test_units_the_launcher_waits_for_do_not_wait_for_the_network_either(self):
        units = read_units()
        for name in waits_for("couchliteos-launcher.service", units) & set(units):
            self.assertEqual(waits_for(name, units) & self.NOT_ON_THE_LAUNCHER_PATH, set(), name)

    def test_network_waits_stay_off_the_boot_critical_units(self):
        units = read_units()
        for early in ("couchliteos-hwdetect-early.service", "couchliteos-hwdetect.service",
                      "couchliteos-audio.service", "couchliteos-bluetooth.service"):
            self.assertEqual(waits_for(early, units) & self.NOT_ON_THE_LAUNCHER_PATH, set(), early)

    def test_network_ready_orders_only_usbipd(self):
        units = read_units()
        self.assertEqual(units["couchliteos-network-ready.service"].get("Before"), ["couchliteos-usbipd.service"])
        # Its own bounded wait (TimeoutStartSec) must stay: it holds multi-user.target, not the launcher.
        text = (ROOT / "services/couchliteos-network-ready.service").read_text()
        self.assertRegex(text, r"(?m)^TimeoutStartSec=\d+$")


class BootTimingTest(unittest.TestCase):
    COMMANDS = (
        ["systemd-analyze", "--no-pager"],
        ["systemd-analyze", "--no-pager", "critical-chain", "couchliteos-launcher.service"],
        ["systemd-analyze", "--no-pager", "blame"],
    )

    def test_diagnostics_script_prints_boot_timing(self):
        text = (ROOT / "scripts/couchliteos-diagnostics").read_text()
        self.assertIn("== Boot timing ==", text)
        self.assertRegex(text, r"(?m)^\s*systemd-analyze --no-pager 2>&1 \|\| true$")
        self.assertRegex(text, r"(?m)^\s*systemd-analyze --no-pager critical-chain couchliteos-launcher\.service 2>&1 \|\| true$")
        self.assertRegex(text, r"(?m)^\s*systemd-analyze --no-pager blame 2>&1 \| head -n 30 \|\| true$")
        subprocess.run(["bash", "-n", str(ROOT / "scripts/couchliteos-diagnostics")], check=True)

    def test_support_export_reports_timing_with_the_slowest_thirty_units(self):
        exporter = load_script("support_exporter_timing", ROOT / "scripts/couchliteos-support-export")
        blame = "".join(f"{100 - index}ms unit-{index}.service\n" for index in range(45))
        outputs = {tuple(command): text for command, text in zip(
            self.COMMANDS, ("Startup finished in 1s\n", "launcher chain\n", blame))}
        with mock.patch.object(exporter, "command_output", side_effect=lambda command, **_: outputs[tuple(command)]) as runner:
            report = exporter.boot_timing_report()
        self.assertEqual([call.args[0] for call in runner.call_args_list], [list(c) for c in self.COMMANDS])
        self.assertIn("Startup finished in 1s", report)
        self.assertIn("launcher chain", report)
        self.assertIn("unit-29.service", report)
        self.assertNotIn("unit-30.service", report)

    def test_boot_timing_file_is_redacted_and_named_in_the_archive(self):
        exporter = load_script("support_exporter_collect", ROOT / "scripts/couchliteos-support-export")
        fake = "FAKE-TIMING-SECRET-424242"
        output = f"password={fake}\nunit.service\n"
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(exporter, "command_output", return_value=output):
            bundle = pathlib.Path(directory)
            exporter.add_boot_timing(bundle)
            timing = (bundle / "system/boot-timing.txt").read_text()
        self.assertIn("unit.service", timing)
        self.assertIn("[REDACTED]", timing)
        self.assertNotIn(fake, timing)
        for command in ("systemd-analyze", "systemd-analyze critical-chain couchliteos-launcher.service",
                        "systemd-analyze blame | head -30"):
            self.assertIn(f"$ {command}\n", timing)
        # The real collector calls it, next to the other system facts.
        collector = exporter.collect.__code__.co_names
        self.assertIn("add_boot_timing", collector)

    def test_support_export_sandbox_can_reach_systemd(self):
        # systemd-analyze talks to the manager over a Unix socket; the unit allows AF_UNIX.
        unit = (ROOT / "services/couchliteos-support-export.service").read_text()
        self.assertRegex(unit, r"(?m)^RestrictAddressFamilies=.*\bAF_UNIX\b")


if __name__ == "__main__":
    unittest.main()
