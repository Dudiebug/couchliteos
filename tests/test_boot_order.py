"""Units that run before udev must not wait for anything udev provides.

On an installed system /boot/efi and swap are mounted by UUID, and their
device units only appear once systemd-udevd and udev-trigger have run. A unit
ordered before udev that (directly or through another of our units) waits for
local-fs.target, swap.target, a mount, or a device holds udev back forever:
the device jobs time out after 90 s and the box lands in emergency mode.
v0.2.1 shipped exactly that (couchliteos-migrate.service).
"""

import configparser
import pathlib
import unittest

SERVICES = pathlib.Path(__file__).resolve().parent.parent / "services"
UDEV = {"systemd-udevd.service", "systemd-udev-trigger.service"}
NEEDS_UDEV = {"local-fs.target", "swap.target", "remote-fs.target", "sysinit.target", "basic.target"}


def parse(text):
    parser = configparser.ConfigParser(strict=False, interpolation=None, delimiters=("=",))
    parser.optionxform = str
    parser.read_string(text)
    unit = parser["Unit"] if parser.has_section("Unit") else {}
    words = lambda key: unit.get(key, "").split()
    return {
        "after": set(words("After")),
        "before": set(words("Before")),
        "mounts_for": words("RequiresMountsFor"),
        "default_deps": unit.get("DefaultDependencies", "yes").strip().lower() not in {"no", "false", "0"},
    }


def violations(units):
    """Problems for every unit (by name) that runs before udev, directly or via our units."""
    runs_before = {name: set(spec["before"]) for name, spec in units.items()}
    for name, spec in units.items():
        for other in spec["after"]:
            runs_before.setdefault(other, set()).add(name)

    early, todo = set(), set(UDEV)
    while todo:
        target = todo.pop()
        for name, later in runs_before.items():
            if target in later and name not in early:
                early.add(name)
                todo.add(name)

    problems = []
    for name in sorted(early & units.keys()):
        spec = units[name]
        waits = sorted(dep for dep in spec["after"]
                       if dep in NEEDS_UDEV or dep.endswith((".mount", ".swap", ".device")))
        if waits:
            problems.append(f"{name} runs before udev but waits for {', '.join(waits)}")
        if spec["mounts_for"]:
            problems.append(f"{name} runs before udev but has RequiresMountsFor=")
        if spec["default_deps"]:
            problems.append(f"{name} runs before udev but keeps DefaultDependencies")
    return problems


def load(directory=SERVICES):
    return {path.name: parse(path.read_text(encoding="utf-8")) for path in sorted(directory.iterdir())
            if path.suffix in {".service", ".target", ".mount", ".path", ".timer", ".socket"}}


class BootOrderTest(unittest.TestCase):
    def test_shipped_units_never_hold_udev_back(self):
        units = load()
        self.assertIn("couchliteos-hwdetect-early.service", units)
        self.assertEqual(violations(units), [])

    def test_catches_the_v021_migrate_deadlock(self):
        units = {
            "early.service": parse("[Unit]\nDefaultDependencies=no\nBefore=systemd-udevd.service\n"),
            "migrate.service": parse("[Unit]\nDefaultDependencies=no\nAfter=local-fs.target\n"
                                     "Before=sysinit.target early.service\n"),
        }
        self.assertEqual(violations(units),
                         ["migrate.service runs before udev but waits for local-fs.target"])

    def test_catches_after_written_on_the_early_unit(self):
        units = {
            "early.service": parse("[Unit]\nDefaultDependencies=no\nAfter=late.service\n"
                                   "Before=systemd-udev-trigger.service\n"),
            "late.service": parse("[Unit]\nDefaultDependencies=no\nAfter=boot-efi.mount\n"),
        }
        self.assertEqual(violations(units), ["late.service runs before udev but waits for boot-efi.mount"])

    def test_catches_default_dependencies_and_mounts(self):
        units = {"early.service": parse("[Unit]\nRequiresMountsFor=/var\nBefore=systemd-udevd.service\n")}
        self.assertEqual(violations(units), [
            "early.service runs before udev but has RequiresMountsFor=",
            "early.service runs before udev but keeps DefaultDependencies",
        ])

    def test_after_local_fs_is_fine_when_nothing_waits_on_us_before_udev(self):
        units = {"migrate.service": parse("[Unit]\nDefaultDependencies=no\nAfter=local-fs.target\n"
                                          "Before=sysinit.target systemd-tmpfiles-setup.service\n")}
        self.assertEqual(violations(units), [])


if __name__ == "__main__":
    unittest.main()
