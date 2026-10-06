"""Security fixes between releases, and the shared "is the box in use?" check."""

import contextlib
import io
import json
import pathlib
import subprocess
import tempfile
import unittest

import couchliteos_busy as busy
import couchliteos_securityupdate as security

UU_OUTPUT = """Checking: chromium ([<Origin component:'main' archive:'trixie-security'>])
Packages that will be upgraded: libssl3t64 openssl
Packages that will be upgraded: google-chrome-stable
All upgrades installed
"""


class Runner:
    def __init__(self, replies=None):
        self.calls = []
        self.replies = replies or {}

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.environment = kwargs.get("env", {})
        code, output = self.replies.get(argv[0], (0, ""))
        return subprocess.CompletedProcess(argv, code, stdout=output)

    def programs(self):
        return [call[0] for call in self.calls]


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)
        self.root = self.tmp / "root"
        (self.root / "run").mkdir(parents=True)
        (self.root / "proc").mkdir()
        (self.root / "proc/cmdline").write_text("BOOT_IMAGE=/vmlinuz root=UUID=1 quiet\n")
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()
        self.runner = Runner({"unattended-upgrade": (0, UU_OUTPUT)})
        self.busy_answers = []
        self.env = security.Env(
            runner=self.runner, root=self.root, run_dir=self.run_dir, state=self.tmp / "security-update.json",
            config=self.tmp / "config.ini", clock=lambda: 5000.0, busy=self._busy, log=lambda _text: None,
        )

    def _busy(self, _run_dir):
        return self.busy_answers.pop(0) if self.busy_answers else ""

    def state(self):
        return json.loads(self.env.state.read_text())


class RunTest(Case):
    def test_a_run_updates_the_lists_then_applies_the_allowed_fixes(self):
        self.assertEqual(security.run(self.env), 0)
        self.assertEqual(self.runner.calls, [["apt-get", "-q", "update"], ["unattended-upgrade", "-v"]])
        self.assertEqual(self.runner.environment["DEBIAN_FRONTEND"], "noninteractive")
        state = self.state()
        self.assertEqual(state["result"], "ok")
        self.assertEqual(state["message"], security.MSG_FIXED)
        self.assertEqual(state["upgraded"], ["google-chrome-stable", "libssl3t64", "openssl"])
        self.assertEqual((state["checked_at"], state["attempted_at"]), (5000, 5000))
        self.assertFalse(state["restart_needed"])

    def test_a_fix_that_needs_a_restart_is_reported_and_the_box_does_not_restart(self):
        (self.root / "run/reboot-required").touch()
        (self.root / "run/reboot-required.pkgs").write_text("linux-image-6.12.0-amd64\nlibc6\n$(evil)\n")
        security.run(self.env)
        state = self.state()
        self.assertTrue(state["restart_needed"])
        self.assertEqual(state["restart_packages"], ["libc6", "linux-image-6.12.0-amd64"])
        self.assertEqual(state["message"], security.MSG_RESTART)
        self.assertFalse([call for call in self.runner.calls if "reboot" in " ".join(call)])

    def test_nothing_runs_while_the_box_is_in_use(self):
        self.busy_answers = ["app"]
        self.assertEqual(security.run(self.env), 0)
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.state()["result"], "busy")

    def test_a_stream_that_starts_during_the_list_download_stops_the_run(self):
        self.busy_answers = ["", "inhibited"]
        security.run(self.env)
        self.assertEqual(self.runner.programs(), ["apt-get"])
        self.assertEqual(self.state()["result"], "busy")

    def test_timer_runs_obey_auto_security_and_owner_requests_do_not(self):
        self.env.config.write_text("[update]\nauto_security = off\n")
        security.run(self.env)
        self.assertEqual(self.state()["result"], "disabled")
        self.assertEqual(self.runner.calls, [])
        (self.run_dir / security.REQUEST_NAME).touch()
        security.run(self.env)
        self.assertEqual(self.state()["result"], "ok")
        self.assertFalse((self.run_dir / security.REQUEST_NAME).exists())

    def test_auto_security_defaults_to_on(self):
        self.assertTrue(security.auto_enabled(self.tmp / "missing.ini"))

    def test_a_live_system_without_persistent_usr_is_skipped(self):
        (self.root / "proc/cmdline").write_text("boot=live components persistence quiet\n")
        security.run(self.env)
        self.assertEqual(self.state()["result"], "live")
        self.assertEqual(self.state()["message"], security.MSG_LIVE)
        self.assertEqual(self.runner.calls, [])

    def test_a_live_system_whose_whole_root_persists_is_updated(self):
        (self.root / "run/live/medium").mkdir(parents=True)
        conf = self.root / "run/live/persistence/sdb2/persistence.conf"
        conf.parent.mkdir(parents=True)
        conf.write_text("/var/lib/couchliteos union\n")
        security.run(self.env)
        self.assertEqual(self.state()["result"], "live")
        conf.write_text("/ union\n")
        security.run(self.env)
        self.assertEqual(self.state()["result"], "ok")

    def test_apt_failures_fail_the_service_and_keep_the_last_check_time(self):
        self.env.state.write_text(json.dumps({"checked_at": 100}))
        self.runner.replies["apt-get"] = (100, "Could not resolve deb.debian.org")
        self.assertEqual(security.run(self.env), 1)
        self.assertEqual((self.state()["result"], self.state()["message"]), ("failed", security.MSG_NETWORK))
        self.assertEqual(self.state()["checked_at"], 100)
        self.runner.replies["apt-get"] = (0, "")
        self.runner.replies["unattended-upgrade"] = (1, "dpkg was interrupted")
        self.assertEqual(security.run(self.env), 1)
        self.assertEqual(self.state()["message"], security.MSG_FAILED)

    def test_an_unexpected_error_still_writes_the_status(self):
        def broken(_argv, **_kwargs):
            raise RuntimeError("boom")

        self.env.runner = broken
        self.assertEqual(security.run(self.env), 1)
        self.assertEqual(self.state()["result"], "failed")

    def test_only_root_may_apply_fixes(self):
        self.env.euid = lambda: 1000
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(security.main(["run"], self.env), 2)
        self.assertEqual(self.runner.calls, [])


class BusyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = pathlib.Path(self._tmp.name)

    def reason(self, inhibitors=(), held=False, own=(10, 11)):
        return busy.reason(self.run_dir, inhibitors=lambda: list(inhibitors), own_pids=own, held=lambda _p: held)

    def test_an_idle_box_is_not_busy(self):
        self.assertEqual(self.reason(), "")

    def test_an_open_app_or_stream_makes_it_busy(self):
        (self.run_dir / "app-active").write_text("moonlight\n")
        self.assertEqual(self.reason(), "app")
        (self.run_dir / "app-active").unlink()
        (self.run_dir / "moonlight-ready").touch()
        self.assertEqual(self.reason(), "app")

    def test_a_couchliteos_update_makes_it_busy(self):
        self.assertEqual(self.reason(held=True), "update")
        (self.run_dir / "update-install").touch()
        self.assertEqual(self.reason(), "update")

    def test_another_programs_blocking_inhibitor_makes_it_busy(self):
        updater = ("sleep:idle", "systemd-inhibit", "Installing a CouchLiteOS update", "block", 0, 99)
        self.assertEqual(self.reason([updater]), "inhibited")

    def test_its_own_inhibitor_and_delay_locks_do_not_count(self):
        own = ("sleep:idle", "systemd-inhibit", "Installing security fixes", "block", 0, 11)
        network = ("sleep", "NetworkManager", "NetworkManager needs to turn off networks", "delay", 0, 50)
        keys = ("handle-power-key", "systemd-logind", "Logind handles keys", "block", 0, 1)
        self.assertEqual(self.reason([own, network, keys]), "")

    def test_inhibitors_are_read_from_busctl_json(self):
        text = json.dumps({"type": "a(ssssuu)", "data": [[
            ["sleep:idle", "systemd-inhibit", "Installing", "block", 0, 99],
            ["bad"],
        ]]})
        self.assertEqual(busy.parse_inhibitors(text), [("sleep:idle", "systemd-inhibit", "Installing", "block", 0, 99)])
        self.assertEqual(busy.parse_inhibitors("garbage"), [])

    def test_a_missing_busctl_counts_as_no_inhibitors(self):
        def runner(*_args, **_kwargs):
            raise FileNotFoundError("busctl")

        self.assertEqual(busy.list_inhibitors(runner), [])

    def test_the_lock_probe_sees_no_lock_on_a_missing_file(self):
        self.assertFalse(busy.lock_held(self.run_dir / "update.lock"))

    @unittest.skipUnless(hasattr(__import__("os"), "fork"), "flock needs Linux")
    def test_the_lock_probe_sees_the_updaters_lock(self):
        import fcntl

        path = self.run_dir / "update.lock"
        with open(path, "a+") as stream:
            self.assertFalse(busy.lock_held(path))
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(busy.lock_held(path))


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(hasattr(security.os, "O_NOFOLLOW"), "needs O_NOFOLLOW")
class RootPathsTest(unittest.TestCase):
    """Root never writes through a name the couchliteos user could swap (couchliteos_safefile)."""

    def test_the_failure_message_names_the_log_root_writes(self):
        self.assertIn("/" + security.LOG_REL, security.MSG_FAILED)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = pathlib.Path(self._tmp.name)
        self.victim = self.base / "victim"
        self.victim.write_text("keep\n")

    def test_the_log_is_in_roots_own_folder_and_never_followed(self):
        self.assertTrue(str(security.LOG_REL).startswith("var/log/couchliteos-update/"))
        log = self.base / "log" / "security-update.log"
        log.parent.mkdir(mode=0o755)
        log.symlink_to(self.victim)
        security.Log(log)("hello")
        self.assertEqual(self.victim.read_text(), "keep\n")

    def test_the_status_is_never_written_through_a_symlink(self):
        state = self.base / "security-update.json"
        state.symlink_to(self.victim)
        security.write_json(state, {"ok": True})
        self.assertEqual(self.victim.read_text(), "keep\n")
        self.assertFalse(state.is_symlink())
