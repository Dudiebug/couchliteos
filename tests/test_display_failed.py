import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "moonlightos-display-failed"
CLEAR_SCREEN = "\x1b[2J\x1b[H"
LIVE = "BOOT_IMAGE=/live/vmlinuz boot=live components persistence\n"
# systemd 257 starts OnFailure= units on every failed exit, even when it then
# restarts the service; is-failed is true only once the unit has given up.
FAKE_SYSTEMCTL = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_LOG"
[ "$FAKE_LAUNCHER_STATE" = failed ]
"""


class DisplayFailedTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.temporary.name)
        self.tty = base / "tty1"
        self.cmdline = base / "cmdline"
        self.nvidia_ids = base / "nvidia.ids"
        self.cmdline.write_text(LIVE)
        self.systemctl = base / "bin" / "systemctl"
        self.systemctl.parent.mkdir()
        self.systemctl.write_text(FAKE_SYSTEMCTL)
        self.systemctl.chmod(0o755)
        self.log = base / "calls.log"

    def tearDown(self):
        self.temporary.cleanup()

    def run_script(self, tty=None, state="failed"):
        return subprocess.run(
            ["bash", str(SCRIPT)],
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "MOONLIGHTOS_TTY": str(tty or self.tty),
                "MOONLIGHTOS_PROC_CMDLINE": str(self.cmdline),
                "MOONLIGHTOS_NVIDIA_IDS": str(self.nvidia_ids),
                "MOONLIGHTOS_SYSTEMCTL": str(self.systemctl),
                "FAKE_LOG": str(self.log),
                "FAKE_LAUNCHER_STATE": state,
            },
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
        )

    def message(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.tty.read_text()

    def test_stays_silent_while_systemd_is_still_restarting_the_launcher(self):
        result = self.run_script(state="activating")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.tty.exists())
        self.assertEqual(self.log.read_text().split(), ["is-failed", "--quiet", "moonlightos-launcher.service"])

    def test_clears_the_screen_before_the_message(self):
        self.assertTrue(self.message().startswith(CLEAR_SCREEN))

    def test_says_plainly_that_the_display_could_not_start(self):
        text = self.message()
        self.assertIn("MOONLIGHTOS COULD NOT START THE SCREEN.", text)
        self.assertIn("HOLD THE POWER BUTTON", text)

    def test_message_is_uppercase_and_fits_a_tv_console(self):
        text = self.message().replace(CLEAR_SCREEN, "", 1)
        self.assertEqual(text, text.upper())
        self.assertTrue(text.isascii())
        self.assertLessEqual(max(len(line) for line in text.splitlines()), 70)
        self.assertLessEqual(len(text.splitlines()), 15)

    def test_makes_no_claim_the_code_cannot_keep(self):
        # Nothing retries in safe graphics by itself, and a support file needs the launcher.
        text = self.message()
        for phrase in ("NEXT BOOT", "AUTOMATIC", "SUPPORT"):
            self.assertNotIn(phrase, text)

    def test_basic_graphics_entry_is_named_only_where_the_boot_menu_has_it(self):
        entry = "START MOONLIGHTOS (BASIC GRAPHICS)"
        self.assertNotIn("BASIC GRAPHICS", self.message())  # general ISO: no NVIDIA driver list
        self.nvidia_ids.write_text("10DE2684\n")
        self.assertIn(entry, self.message())
        self.cmdline.write_text(LIVE.rstrip("\n") + " moonlightos.gpu=basic\n")
        self.assertNotIn("BASIC GRAPHICS", self.message())  # already in it
        self.cmdline.write_text("BOOT_IMAGE=/vmlinuz root=/dev/sda1 ro\n")
        self.assertNotIn("BASIC GRAPHICS", self.message())  # installed system: no live menu

    def test_rerun_replaces_the_message(self):
        first = self.message()
        self.assertEqual(self.message(), first)

    def test_missing_console_is_reported_not_hidden(self):
        result = self.run_script(tty=pathlib.Path(self.temporary.name) / "no-such-dir" / "tty1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot write", result.stderr)

    def test_writes_nothing_but_the_message(self):
        self.message()
        self.assertEqual(sorted(p.name for p in pathlib.Path(self.temporary.name).iterdir()),
                         ["bin", "calls.log", "cmdline", "tty1"])


if __name__ == "__main__":
    unittest.main()
