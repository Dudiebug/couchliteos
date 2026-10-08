"""The updater checks inside scripts/couchliteos-qemu-smoke run only in QEMU, hours into a gauntlet: catch
a renamed function or a broken module load here instead."""

import importlib.machinery
import importlib.util
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SMOKE = (ROOT / "scripts/couchliteos-qemu-smoke").read_text(encoding="utf-8")


class SmokeUpdaterTest(unittest.TestCase):
    def test_the_smoke_loads_the_updater_the_way_python_needs_and_calls_what_exists(self):
        snippet = SMOKE[SMOKE.index("loader = importlib.machinery.SourceFileLoader(\"couchliteos_updater\""):]
        self.assertIn("sys.modules[loader.name] = updater", snippet.split("loader.exec_module(updater)")[0])
        sys.path.insert(0, str(ROOT / "launcher"))
        self.addCleanup(sys.path.remove, str(ROOT / "launcher"))
        loader = importlib.machinery.SourceFileLoader("couchliteos_updater_smoke",
                                                      str(ROOT / "launcher/couchliteos_updater.py"))
        updater = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        sys.modules[loader.name] = updater
        self.addCleanup(sys.modules.pop, loader.name, None)
        loader.exec_module(updater)
        called = set(re.findall(r"\bupdater\.(\w+)\(", SMOKE))
        self.assertTrue(called)
        self.assertEqual([name for name in sorted(called) if not hasattr(updater, name)], [])


if __name__ == "__main__":
    unittest.main()
