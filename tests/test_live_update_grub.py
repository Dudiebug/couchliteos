"""The ISO's GRUB menu boots a system update installed on the stick (live-update/ slots).

Runs the binary hooks on a live-build-like tree, then evaluates the generated grub.cfg with a
tiny interpreter of the GRUB commands it uses, for sticks with and without updates.
"""
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
AUTOBOOT = ROOT / "config/live-build/hooks/live/0100-autoboot.hook.binary"
BASIC = ROOT / "config/profiles/nvidia/hooks/0300-basic-graphics.hook.binary"
ARGS = "boot=live components persistence ipv6.disable=1"
GRUB_CFG = (
    'menuentry "Live system (amd64)" --hotkey=l {\n'
    f" linux /live/vmlinuz {ARGS}\n"
    " initrd /live/initrd.img\n"
    "}\n"
    "submenu 'Utilities...' {}\n"
)


def render(cfg, labels=(), files=()):
    """Menu entries (title, linux line) GRUB would show.

    labels: filesystem labels present -> device name; files: paths "(dev)/x" that exist.
    Supports only what the hooks write: set, export, search --label --set, if/elif/else/fi
    with [ -f ] and [ -n ], and menuentry blocks.
    """
    labels = dict(labels)
    variables, entries = {}, []
    stack = []  # (active, any_branch_taken)

    def expand(text):
        return re.sub(r"\$\{(\w+)\}", lambda m: variables.get(m.group(1), ""), text)

    def test(condition):
        condition = condition.strip()
        if condition.startswith("search "):
            parts = condition.split()
            var = next(p.split("=", 1)[1] for p in parts if p.startswith("--set="))
            if parts[-1] in labels:
                variables[var] = labels[parts[-1]]
                return True
            return False
        match = re.fullmatch(r'\[ -(f|n) "([^"]*)" \]', condition)
        assert match, condition
        value = expand(match.group(2))
        return value in files if match.group(1) == "f" else bool(value)

    lines = iter(cfg.splitlines())
    for raw in lines:
        line = raw.strip()
        active = all(frame[0] for frame in stack)
        if line.startswith("if "):
            taken = active and test(line[3:].removesuffix("; then"))
            stack.append([taken, taken])
        elif line.startswith("elif "):
            parent = all(frame[0] for frame in stack[:-1])
            taken = parent and not stack[-1][1] and test(line[5:].removesuffix("; then"))
            stack[-1] = [taken, stack[-1][1] or taken]
        elif line == "else":
            stack[-1] = [not stack[-1][1], True]
        elif line == "fi":
            stack.pop()
        elif line.startswith("menuentry "):
            body = []
            for inner in lines:
                if inner.strip() == "}":
                    break
                body.append(inner.strip())
            if active:
                title = re.match(r'menuentry "([^"]+)"', line).group(1)
                linux = next((expand(b) for b in body if b.startswith("linux ")), "")
                entries.append((title, linux))
        elif not active:
            continue
        elif line.startswith("set ") and "=" in line:
            name, value = line[4:].split("=", 1)
            variables[name] = expand(value)
    assert not stack, "unbalanced if/fi"
    return entries


class GrubLiveUpdateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bash = shutil.which("bash")
        if not cls.bash:
            raise unittest.SkipTest("bash is required")

    def build(self, nvidia=False):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        tree = pathlib.Path(directory.name)
        grub = tree / "binary/boot/grub"
        grub.mkdir(parents=True)
        (grub / "grub.cfg").write_bytes(GRUB_CFG.encode())
        hooks = [AUTOBOOT, AUTOBOOT] + ([BASIC] if nvidia else [])
        for hook in hooks:  # twice: the hooks must be idempotent
            subprocess.run([self.bash, str(hook)], cwd=tree, check=True)
        return (grub / "grub.cfg").read_text()

    def test_block_sits_before_the_iso_entry_once(self):
        cfg = self.build()
        self.assertEqual(cfg.count("# couchliteos live-update"), 1)
        self.assertLess(cfg.index("# couchliteos live-update"), cfg.index('menuentry "Start CouchLiteOS" '))
        self.assertLess(cfg.index("couchliteos-sys;"), cfg.index("--set=couchliteos_slot persistence;"))
        top = [line for line in cfg.splitlines() if line.startswith("menuentry ")]
        self.assertEqual([re.search(r'"([^"]+)"', line).group(1) for line in top],
                         ["Start CouchLiteOS", "Start CouchLiteOS (No Persistence)"])

    def test_plain_stick_boots_its_own_system(self):
        cfg = self.build()
        for labels in ((), {"persistence": "hd0,msdos3"}):
            with self.subTest(labels=labels):
                entries = render(cfg, labels)
                self.assertEqual([title for title, _ in entries], ["Start CouchLiteOS", "Start CouchLiteOS (No Persistence)"])
                self.assertEqual(entries[0][1], f"linux /live/vmlinuz {ARGS}")

    def test_update_on_the_persistence_partition_boots_first(self):
        cfg = self.build()
        entries = render(cfg, {"persistence": "hd0,msdos3"}, {"(hd0,msdos3)/live-update/current/ok"})
        self.assertEqual(entries[0], ("Start CouchLiteOS (Updated)",
                                      f"linux (hd0,msdos3)/live-update/current/vmlinuz {ARGS} "
                                      "live-media=/dev/disk/by-label/persistence live-media-path=/live-update/current"))
        self.assertEqual([title for title, _ in entries[1:]], ["Start CouchLiteOS", "Start CouchLiteOS (No Persistence)"])

    def test_previous_entry_and_sys_partition_preferred(self):
        cfg = self.build()
        labels = {"persistence": "hd0,msdos4", "couchliteos-sys": "hd0,msdos3"}
        files = {"(hd0,msdos3)/live-update/current/ok", "(hd0,msdos3)/live-update/previous/ok",
                 "(hd0,msdos4)/live-update/current/ok"}
        entries = render(cfg, labels, files)
        self.assertEqual([title for title, _ in entries[:2]], ["Start CouchLiteOS (Updated)", "Start CouchLiteOS (Previous Update)"])
        self.assertIn("(hd0,msdos3)/live-update/previous/vmlinuz", entries[1][1])
        self.assertTrue(entries[1][1].endswith("live-media=/dev/disk/by-label/couchliteos-sys live-media-path=/live-update/previous"))

    def test_only_previous_after_an_interrupted_update(self):
        cfg = self.build()
        entries = render(cfg, {"persistence": "hd0,msdos3"}, {"(hd0,msdos3)/live-update/previous/ok"})
        self.assertEqual(entries[0][0], "Start CouchLiteOS (Previous Update)")
        self.assertEqual(entries[1][0], "Start CouchLiteOS")

    def test_initrd_comes_from_the_same_slot(self):
        cfg = self.build()
        for slot in ("current", "previous"):
            self.assertIn(f"initrd (${{couchliteos_slot}})/live-update/{slot}/initrd.img", cfg)

    def test_nvidia_basic_graphics_still_follows_no_persistence(self):
        cfg = self.build(nvidia=True)
        top = [re.search(r'"([^"]+)"', line).group(1) for line in cfg.splitlines() if line.startswith("menuentry ")]
        self.assertEqual(top, ["Start CouchLiteOS", "Start CouchLiteOS (No Persistence)", "Start CouchLiteOS (Basic Graphics)"])
        self.assertEqual(cfg.count("couchliteos.gpu=basic"), 1)


if __name__ == "__main__":
    unittest.main()
