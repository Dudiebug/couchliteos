"""scripts/couchliteos-session: TV interface, cairo retry, classic launcher (fake commands)."""

import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "couchliteos-session"
# The fake TV interface: exits with the next status from $FAKE_STATUSES ("3 0" = fails, then works),
# writing launcher-ready first when the status is listed in $FAKE_READY_ON. "hang" never draws a
# frame; "segv" draws one (launcher-ready) and is killed by SIGSEGV at once, "late-segv" 2 s later;
# "slow" draws its first frame after 2 s and then runs normally.
FAKE_TV = """#!/bin/bash
count=$(wc -l < "$FAKE_LOG" 2>/dev/null || echo 0)
printf 'tv GSK_RENDERER=%s\\n' "${GSK_RENDERER:-}" >> "$FAKE_LOG"
read -r -a statuses <<< "$FAKE_STATUSES"
status=${statuses[$count]:-0}
case $status in
  hang) exec sleep 60 ;;
  segv) touch "$COUCHLITEOS_RUN_DIR/launcher-ready"; kill -SEGV $$ ;;
  late-segv) touch "$COUCHLITEOS_RUN_DIR/launcher-ready"; sleep 2; kill -SEGV $$ ;;
  slow) sleep 2; touch "$COUCHLITEOS_RUN_DIR/launcher-ready"; exit 0 ;;
esac
for ready in $FAKE_READY_ON; do
  [[ $ready == "$status" ]] && touch "$COUCHLITEOS_RUN_DIR/launcher-ready"
done
exit "$status"
"""
FAKE_FOOT = """#!/bin/bash
printf 'foot %s\\n' "$*" >> "$FAKE_LOG"
"""


class FallbackTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = pathlib.Path(self.temporary.name)
        self.state, self.run = base / "state", base / "run"
        self.state.mkdir()
        self.run.mkdir()
        (self.state / "setup-complete").touch()
        self.log = base / "calls.log"
        self.tv, self.foot = base / "tv", base / "foot"
        self.tv.write_text(FAKE_TV)
        self.foot.write_text(FAKE_FOOT)
        self.tv.chmod(0o755)
        self.foot.chmod(0o755)

    def session(self, statuses="0", ready_on="0", config=None, scale=None):
        if config is not None:
            (self.state / "config.ini").write_text(config)
        scale_file = self.run.parent / "timeout-scale"
        if scale is not None:
            scale_file.write_bytes(scale)
        env = {
            "COUCHLITEOS_SCALE_FILE": str(scale_file),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "COUCHLITEOS_STATE_DIR": str(self.state), "COUCHLITEOS_RUN_DIR": str(self.run),
            "COUCHLITEOS_TV": str(self.tv), "COUCHLITEOS_FOOT": str(self.foot),
            "COUCHLITEOS_LAUNCHER": "/usr/libexec/couchliteos-launcher",
            "FAKE_LOG": str(self.log), "FAKE_STATUSES": statuses, "FAKE_READY_ON": ready_on,
            "COUCHLITEOS_READY_SECONDS": "1", "COUCHLITEOS_EARLY_SECONDS": "1",
        }
        result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=20)
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return result, calls

    def interface(self):
        return (self.run / "interface").read_text().splitlines()

    CLASSIC = "foot --fullscreen --title CouchLiteOS Launcher /usr/libexec/couchliteos-launcher"

    def test_the_tv_interface_runs_when_it_starts(self):
        result, calls = self.session("0")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls, ["tv GSK_RENDERER="])
        self.assertEqual(self.interface(), ["tv", "first try"])
        self.assertIn("interface: tv (first try)", result.stderr)

    def test_exit_3_retries_once_with_cairo(self):
        result, calls = self.session("3 0")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls, ["tv GSK_RENDERER=", "tv GSK_RENDERER=cairo"])
        self.assertIn("trying again without GL", result.stderr)
        self.assertEqual(self.interface(), ["tv", "GSK_RENDERER=cairo after exit 3"])

    def test_two_failures_start_the_classic_launcher(self):
        result, calls = self.session("3 3")
        self.assertEqual(result.returncode, 0)  # foot's status: exec replaced the wrapper
        self.assertEqual(calls, ["tv GSK_RENDERER=", "tv GSK_RENDERER=cairo", self.CLASSIC])
        self.assertEqual(self.interface(), ["classic", "fallback after exit 3, then exit 3 with cairo"])
        self.assertIn("interface: classic (fallback after exit 3", result.stderr)

    def test_a_crash_before_launcher_ready_counts_as_could_not_start(self):
        result, calls = self.session("139 1", ready_on="")
        self.assertEqual(calls, ["tv GSK_RENDERER=", "tv GSK_RENDERER=cairo", self.CLASSIC])

    def test_a_failure_after_launcher_ready_is_passed_on_for_systemd_to_restart(self):
        result, calls = self.session("1", ready_on="1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(calls, ["tv GSK_RENDERER="])

    def test_cairo_that_comes_up_and_later_fails_is_not_followed_by_classic(self):
        result, calls = self.session("3 2", ready_on="2")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(calls, ["tv GSK_RENDERER=", "tv GSK_RENDERER=cairo"])

    def test_a_hang_before_the_first_frame_is_stopped_and_counts_as_could_not_start(self):
        result, calls = self.session("hang 0")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls, ["tv GSK_RENDERER=", "tv GSK_RENDERER=cairo"])
        self.assertIn("no first frame within 1 s", result.stderr)

    def test_a_crash_right_after_the_first_frame_counts_as_could_not_start(self):
        # GL that drew one frame, then crashed: not a restart loop back into the same renderer.
        result, calls = self.session("segv segv")
        self.assertEqual(calls, ["tv GSK_RENDERER=", "tv GSK_RENDERER=cairo", self.CLASSIC])
        self.assertIn("right after its first frame", result.stderr)
        self.assertFalse((self.run / "launcher-ready").exists())

    def test_a_crash_well_after_the_first_frame_is_passed_on(self):
        result, calls = self.session("late-segv")
        self.assertEqual(result.returncode, 128 + 11)
        self.assertEqual(calls, ["tv GSK_RENDERER="])

    def test_the_qemu_timeout_scale_gives_a_slow_first_frame_more_time(self):
        # QEMU without KVM (tests/qemu-*.sh pass opt/couchliteos.timeout-scale): 1 s x 3.
        result, calls = self.session("slow", scale=b"3")
        self.assertEqual(calls, ["tv GSK_RENDERER="])
        self.assertEqual(self.interface(), ["tv", "first try"])
        self.assertNotIn("no first frame", result.stderr)

    def test_without_a_usable_scale_a_slow_first_frame_is_stopped(self):
        for scale in (None, b"", b"0", b"abc", b"10", b"3; touch x"):
            self.log.unlink(missing_ok=True)
            result, calls = self.session("slow 0", scale=scale)
            self.assertEqual(calls, ["tv GSK_RENDERER=", "tv GSK_RENDERER=cairo"], scale)
            self.assertIn("no first frame within 1 s", result.stderr)

    def test_interface_classic_skips_the_tv_interface(self):
        for config in ("[appearance]\ninterface = classic\n", "[Appearance]\nInterface=CLASSIC\n"):
            self.log.unlink(missing_ok=True)
            result, calls = self.session("0", config=config)
            self.assertEqual(calls, [self.CLASSIC])
            self.assertEqual(self.interface(), ["classic", "forced: [appearance] interface = classic"])

    def test_interface_tv_or_anything_else_runs_the_tv_interface(self):
        for config in ("[appearance]\ninterface = tv\n", "[appearance]\ntheme = slate\n",
                       "[other]\ninterface = classic\n", "not an ini file"):
            self.log.unlink(missing_ok=True)
            result, calls = self.session("0", config=config)
            self.assertEqual(calls, ["tv GSK_RENDERER="], config)

    def test_setup_not_complete_still_runs_the_tv_interface(self):
        # couchliteos-tv runs the wizard itself (couchliteos-launcher --screen setup on top).
        (self.state / "setup-complete").unlink()
        result, calls = self.session("0")
        self.assertEqual(calls, ["tv GSK_RENDERER="])


class UnitTest(unittest.TestCase):
    def test_the_launcher_unit_runs_the_session_wrapper_and_waits_for_a_double_fallback(self):
        unit = (ROOT / "services/couchliteos-launcher.service").read_text()
        self.assertIn("/usr/bin/cage -s -- /usr/libexec/couchliteos-session 2>&1", unit)
        # 45 s (x the QEMU timeout scale): two TV attempts (12 s each) and the classic launcher.
        self.assertIn("for ((i = 0; i < 450 * s; i++))", unit)
        self.assertIn("read -r s 2>/dev/null < /run/couchliteos-timeout-scale;"
                      " [[ $${s-} =~ ^[1-9]$$ ]] || s=1;", unit)
        # fw_cfg files are root-only (0400): a root step copies the scale for the service user.
        self.assertIn("\nExecStartPre=+/bin/sh -c 'cat /sys/firmware/qemu_fw_cfg/by_name/opt/"
                      "couchliteos.timeout-scale/raw > /run/couchliteos-timeout-scale 2>/dev/null; exit 0'\n", unit)
        self.assertIn("\nTimeoutStartSec=600\n", unit)  # the wait above, not systemd's 90 s, decides
        script = SCRIPT.read_text()
        self.assertIn("READY_SECONDS=$(( ${COUCHLITEOS_READY_SECONDS:-12} * scale ))", script)
        self.assertIn("SCALE_FILE=${COUCHLITEOS_SCALE_FILE:-/run/couchliteos-timeout-scale}", script)

    def test_the_build_installs_both_front_ends(self):
        configure = (ROOT / "build/configure.sh").read_text()
        for line in ('"$ROOT/scripts/couchliteos-session" "$CHROOT/usr/libexec/couchliteos-session"',
                     '"$ROOT/launcher/couchliteos-tv.py" "$CHROOT/usr/libexec/couchliteos-tv"',
                     '"$ROOT/launcher/couchliteos-launcher.py" "$CHROOT/usr/libexec/couchliteos-launcher"'):
            self.assertIn(line, configure)


if __name__ == "__main__":
    unittest.main()
