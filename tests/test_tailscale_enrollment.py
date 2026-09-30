import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
VIEWER = ROOT / "scripts" / "moonlightos-tailscale-enrollment"


class TailscaleEnrollmentViewerTest(unittest.TestCase):
    def run_viewer(
        self, tailscale_script: str, url: str = "", extra: dict | None = None
    ) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            commands = root / "bin"
            commands.mkdir()
            counter = root / "counter"
            url_file = root / "auth-url"
            if url:
                url_file.write_text(url + "\n", encoding="utf-8")

            (commands / "tailscale").write_text(tailscale_script, encoding="utf-8")
            (commands / "systemctl").write_text(
                "#!/bin/bash\nexit 1\n", encoding="utf-8"
            )
            (commands / "qrencode").write_text(
                "#!/bin/bash\nprintf '%s\\n' \"$*\" >> \"$TEST_QR_ARGS\"\n"
                "for ((i = 0; i < ${FAKE_QR_LINES:-1}; i++)); do echo '[QR]'; done\n",
                encoding="utf-8",
            )
            # There is no terminal in the tests: stty fails unless FAKE_STTY gives "rows columns".
            (commands / "stty").write_text(
                '#!/bin/bash\n[[ -n ${FAKE_STTY:-} ]] && echo "$FAKE_STTY"\n', encoding="utf-8"
            )
            for command in commands.iterdir():
                command.chmod(0o755)

            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{commands}:{environment['PATH']}",
                    "MOONLIGHTOS_TAILSCALE_URL_FILE": str(url_file),
                    "MOONLIGHTOS_TAILSCALE_POLL_SECONDS": "0",
                    "MOONLIGHTOS_TAILSCALE_URL_WAIT_SECONDS": "4",
                    "TEST_COUNTER": str(counter),
                    "TEST_QR_ARGS": str(root / "qr-args"),
                    **(extra or {}),
                }
            )
            return subprocess.run(
                [str(VIEWER)], text=True, capture_output=True, env=environment, timeout=5
            )

    # One "NeedsLogin" answer with a URL waiting, then connected.
    WAITING = """#!/bin/bash
count=0
[[ -r $TEST_COUNTER ]] && count=$(< "$TEST_COUNTER")
printf '%s\\n' "$((count + 1))" > "$TEST_COUNTER"
if ((count >= 1)); then
  printf '%s\\n' '{"BackendState":"Running"}'
else
  printf '%s\\n' '{"BackendState":"NeedsLogin"}'
fi
"""
    URL = "https://login.tailscale.com/a/0123456789abcdef"

    def show_qr(self, terminal: str = "", qr_lines: int = 19) -> subprocess.CompletedProcess:
        extra = {"FAKE_QR_LINES": str(qr_lines)}
        if terminal:
            extra["FAKE_STTY"] = terminal
        return self.run_viewer(self.WAITING, self.URL, extra)

    def test_already_connected_ignores_old_url(self):
        result = self.run_viewer(
            '#!/bin/bash\nprintf \'%s\\n\' \'{"BackendState":"Running"}\'\n',
            "https://login.tailscale.com/a/stale",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Tailscale is connected", result.stdout)
        self.assertNotIn("stale", result.stdout)

    def test_successful_authentication_returns_automatically(self):
        result = self.run_viewer(
            """#!/bin/bash
count=0
[[ -r $TEST_COUNTER ]] && count=$(< "$TEST_COUNTER")
count=$((count + 1))
printf '%s\n' "$count" > "$TEST_COUNTER"
if ((count >= 2)); then
  printf '%s\n' '{"BackendState":"Running"}'
else
  printf '%s\n' '{"BackendState":"NeedsLogin"}'
fi
""",
            "https://login.tailscale.com/a/current",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("Or open this address:"), 1)
        self.assertIn("Tailscale is connected", result.stdout)

    def test_the_qr_code_is_indented_clear_of_the_screen_edge_for_tv_overscan(self):
        result = self.show_qr("28 98")
        self.assertEqual(result.returncode, 0, result.stderr)
        qr = [line for line in result.stdout.splitlines() if "[QR]" in line]
        self.assertEqual(len(qr), 19)
        self.assertTrue(all(line.startswith("    [QR]") for line in qr), qr)

    def test_the_qr_code_asks_for_the_standard_four_module_quiet_margin(self):
        with tempfile.TemporaryDirectory() as directory:
            args = pathlib.Path(directory) / "args"
            result = self.run_viewer(
                self.WAITING, self.URL, {"FAKE_QR_LINES": "19", "FAKE_STTY": "28 98", "TEST_QR_ARGS": str(args)}
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(args.read_text(), r"(^| )-m 4( |$)")

    def test_the_whole_enrolment_screen_fits_a_28_row_terminal_without_scrolling(self):
        result = self.show_qr("28 98")
        self.assertIn("[QR]", result.stdout)
        self.assertIn(self.URL, result.stdout)
        shown = result.stdout.split("Tailscale is connected")[0]  # what is on screen while waiting
        self.assertLess(len(shown.splitlines()), 28)

    def test_a_qr_code_that_would_scroll_off_is_replaced_by_the_address(self):
        result = self.show_qr("18 65")  # what the old fixed 24 pt font gave at 720p
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("[QR]", result.stdout)
        self.assertIn(self.URL, result.stdout)
        self.assertIn("too small", result.stdout)

    def test_the_qr_code_needs_every_row_of_its_screen(self):
        self.assertIn("[QR]", self.show_qr("27 98").stdout)  # 26 lines plus the cursor line
        self.assertNotIn("[QR]", self.show_qr("26 98").stdout)

    def test_a_qr_code_wider_than_the_screen_is_replaced_by_the_address(self):
        result = self.show_qr("40 40")
        self.assertNotIn("[QR]", result.stdout)
        self.assertIn(self.URL, result.stdout)

    def test_the_qr_code_still_shows_when_the_screen_size_is_unknown(self):
        result = self.show_qr("")
        self.assertIn("[QR]", result.stdout)
        self.assertIn(self.URL, result.stdout)


if __name__ == "__main__":
    unittest.main()
