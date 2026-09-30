import dataclasses
import pathlib
import tempfile
import unittest
from unittest import mock

import moonlightos_app_runner as runner
import moonlightos_apps as apps
import moonlightos_rdp as rdp


PASSWORD = "Fake-Session-Password-77"


class RunnerTest(unittest.TestCase):
    def app(self, **changes):
        base = apps.Application(
            id="demo", name="DEMO", kind="command", command="/bin/sleep",
            arguments="0.02", status_id="demo", environment={"DEMO_VALUE": "safe"},
        )
        return apps.dataclasses.replace(base, **changes)

    def test_argument_vector_and_terminal_wrapper(self):
        self.assertEqual(runner.command_vector(self.app(arguments="1 'two words'")), ["/bin/sleep", "1", "two words"])
        self.assertEqual(
            runner.command_vector(self.app(terminal=True))[:6],
            ["/usr/bin/foot", "--fullscreen", "--title", "DEMO", "--", "/bin/sleep"],
        )

    def test_environment_is_applied_without_losing_base_environment(self):
        with mock.patch.dict(runner.os.environ, {"BASE": "kept"}, clear=True):
            environment = runner.configured_environment(self.app())
        self.assertEqual(environment["BASE"], "kept")
        self.assertEqual(environment["DEMO_VALUE"], "safe")

    def test_request_id_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "request"
            path.write_text("../bad\n", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "invalid"):
                runner.read_request(path)

    def test_run_creates_ready_and_exit_status_and_cleans_active(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest = root / "demo.ini"
            manifest.write_text(apps.serialize(self.app()), encoding="utf-8")
            app = apps.read_manifest(manifest)
            request = root / "launch-app.request"
            request.write_text("demo\n", encoding="ascii")
            real_status = runner.atomic_status
            ready_seen = []

            def record_status(path, value):
                if value == "started":
                    ready_seen.append((root / "demo-ready").exists())
                real_status(path, value)

            with mock.patch.object(runner, "RUN", root), mock.patch.object(
                runner, "READY_SECONDS", 0.0
            ), mock.patch.object(
                runner.apps, "load_applications", return_value=apps.LoadResult((app,), ())
            ), mock.patch.object(runner, "atomic_status", side_effect=record_status):
                self.assertEqual(runner.run(request), 0)
            self.assertEqual(ready_seen, [True])
            self.assertEqual((root / "demo-status").read_text(), "exited: status 0\n")
            self.assertFalse((root / "app-active").exists())
            self.assertFalse((root / "demo-ready").exists())

    def active_marker_while_running(self, **changes):
        """Whether app-active exists while the application process is alive."""
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest = root / "demo.ini"
            manifest.write_text(apps.serialize(self.app(**changes)), encoding="utf-8")
            app = apps.read_manifest(manifest)
            request = root / "launch-app.request"
            request.write_text("demo\n", encoding="ascii")
            process = mock.Mock(pid=55)
            process.poll.return_value = 0
            process.wait.return_value = 0
            seen = []

            def spawn(*_args, **_kwargs):
                seen.append((root / "app-active").exists())
                return process

            with mock.patch.object(runner, "RUN", root), mock.patch.object(
                runner, "READY_SECONDS", 0.0
            ), mock.patch.object(
                runner.apps, "load_applications", return_value=apps.LoadResult((app,), ())
            ), mock.patch.object(runner.subprocess, "Popen", side_effect=spawn):
                runner.run(request)
            return seen

    def test_terminal_apps_keep_the_controller_but_other_apps_take_it(self):
        # A terminal app (nmtui, "Press ENTER to return") is driven by the controller
        # through gamepad-nav's keyboard events; app-active would silence them.
        self.assertEqual(self.active_marker_while_running(terminal=True), [False])
        self.assertEqual(self.active_marker_while_running(terminal=False), [True])

    def test_quick_clean_exit_is_not_a_start_failure(self):
        # nmtui (or any tool) may legitimately finish before the 5 s ready mark.
        for code, expected in ((0, "exited: status 0\n"),
                               (3, "failed: exited before the application became ready (status 3)\n")):
            with tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                manifest = root / "demo.ini"
                manifest.write_text(apps.serialize(self.app()), encoding="utf-8")
                app = apps.read_manifest(manifest)
                request = root / "launch-app.request"
                request.write_text("demo\n", encoding="ascii")
                process = mock.Mock(pid=55)
                process.poll.return_value = code
                process.wait.return_value = code
                with mock.patch.object(runner, "RUN", root), mock.patch.object(
                    runner, "READY_SECONDS", 0.0
                ), mock.patch.object(
                    runner.apps, "load_applications", return_value=apps.LoadResult((app,), ())
                ), mock.patch.object(runner.subprocess, "Popen", return_value=process):
                    self.assertEqual(runner.run(request), code)
                self.assertEqual((root / "demo-status").read_text(), expected)

    def test_missing_command_writes_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            app_value = self.app(command="/missing/demo")
            manifest = root / "demo.ini"
            manifest.write_text(apps.serialize(app_value), encoding="utf-8")
            app = apps.read_manifest(manifest)
            request = root / "launch-app.request"
            request.write_text("demo\n", encoding="ascii")
            with mock.patch.object(runner, "RUN", root), mock.patch.object(
                runner.apps, "load_applications", return_value=apps.LoadResult((app,), ())
            ):
                self.assertEqual(runner.run(request), 66)
            self.assertIn("executable is missing", (root / "demo-status").read_text())

    def test_subprocess_is_never_invoked_through_a_shell(self):
        process = mock.Mock()
        process.poll.return_value = 0
        process.wait.return_value = 0
        app = self.app(path=pathlib.Path("/tmp/demo.ini"))
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest = root / "demo.ini"
            manifest.write_text(apps.serialize(app), encoding="utf-8")
            loaded = apps.read_manifest(manifest)
            request = root / "request"
            request.write_text("demo\n")
            with mock.patch.object(runner, "RUN", root), mock.patch.object(
                runner.apps, "load_applications", return_value=apps.LoadResult((loaded,), ())
            ), mock.patch.object(runner.pathlib.Path, "is_file", return_value=True), mock.patch.object(
                runner.os, "access", return_value=True
            ), mock.patch.object(runner.subprocess, "Popen", return_value=process) as popen:
                runner.run(request)
        self.assertNotIn("shell", popen.call_args.kwargs)

    def test_signal_is_forwarded_and_markers_are_cleaned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest = root / "demo.ini"
            manifest.write_text(apps.serialize(self.app()), encoding="utf-8")
            loaded = apps.read_manifest(manifest)
            request = root / "request"
            request.write_text("demo\n")
            handlers = {}
            process = mock.Mock(pid=123)
            process.poll.side_effect = [None, None, 0, None]
            process.wait.side_effect = lambda: (handlers[runner.signal.SIGTERM](runner.signal.SIGTERM, None), -15)[1]

            def install(signum, handler):
                handlers[signum] = handler
                return runner.signal.SIG_DFL

            with mock.patch.object(runner, "RUN", root), mock.patch.object(
                runner, "READY_SECONDS", 0.0
            ), mock.patch.object(
                runner.apps, "load_applications", return_value=apps.LoadResult((loaded,), ())
            ), mock.patch.object(runner.subprocess, "Popen", return_value=process), mock.patch.object(
                runner.signal, "signal", side_effect=install
            ), mock.patch.object(runner.os, "killpg") as killpg:
                self.assertEqual(runner.run(request), 143)
            killpg.assert_called_once_with(123, runner.signal.SIGTERM)
            self.assertFalse((root / "app-active").exists())
            self.assertFalse((root / "demo-ready").exists())

    def test_close_request_gracefully_stops_owned_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest = root / "demo.ini"
            manifest.write_text(apps.serialize(self.app()), encoding="utf-8")
            loaded = apps.read_manifest(manifest)
            request = root / "request"
            request.write_text("demo\n")
            process = mock.Mock(pid=321)
            process.poll.return_value = None
            process.wait.return_value = -15

            def mark_close(*_args, **_kwargs):
                (root / "close-demo").touch()
                return process

            with mock.patch.object(runner, "RUN", root), mock.patch.object(
                runner, "READY_SECONDS", 0.0
            ), mock.patch.object(
                runner.apps, "load_applications", return_value=apps.LoadResult((loaded,), ())
            ), mock.patch.object(runner.subprocess, "Popen", side_effect=mark_close), mock.patch.object(
                runner.os, "killpg"
            ) as killpg:
                self.assertEqual(runner.run(request), 0)
            killpg.assert_called_once_with(321, runner.signal.SIGTERM)
            self.assertEqual((root / "demo-status").read_text(), "exited: status 0\n")


class RdpRunnerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.run_dir = pathlib.Path(self.temporary.name) / "run"
        self.run_dir.mkdir()
        self.store = pathlib.Path(self.temporary.name) / "connections.ini"
        rdp.upsert_connection(rdp.Connection(
            id="rdp-work-pc", name="Work PC", host="192.168.50.20", username="alice",
            certificate="ab" * 32,
        ), self.store)

    def tearDown(self):
        self.temporary.cleanup()

    def start(self, returncode=0, prepare=None):
        (self.run_dir / "rdp.request").write_text("rdp-work-pc\n")
        rdp.write_handoff("rdp-work-pc", PASSWORD, self.run_dir / "rdp-session.secret")
        process = mock.Mock(pid=4242, stdin=mock.Mock())
        process.poll.return_value = returncode
        process.wait.return_value = returncode
        with mock.patch.object(runner, "READY_SECONDS", 0.0), mock.patch.object(
            runner.os, "access", return_value=True
        ), mock.patch.object(runner, "native_size", return_value="1920x1080"), mock.patch.object(
            runner.subprocess, "Popen", return_value=process
        ) as popen:
            if prepare:
                prepare()
            code = runner.run_rdp(self.run_dir, self.store)
        return code, popen, process

    def test_password_goes_to_stdin_never_to_the_command_line(self):
        code, popen, process = self.start(0)
        self.assertEqual(code, 0)
        vector = popen.call_args.args[0]
        self.assertNotIn(PASSWORD, " ".join(vector))
        self.assertIn("/from-stdin:force", vector)
        self.assertEqual(popen.call_args.kwargs["stdin"], runner.subprocess.PIPE)
        self.assertNotIn("shell", popen.call_args.kwargs)
        self.assertNotIn("DISPLAY", popen.call_args.kwargs["env"])
        process.stdin.write.assert_called_once_with((PASSWORD + "\n").encode())
        process.stdin.close.assert_called_once_with()
        self.assertEqual((self.run_dir / "rdp-work-pc-status").read_text(), "exited: disconnected\n")
        for name in ("rdp.request", "rdp-session", "rdp-session.secret", "app-active", "rdp-work-pc-ready"):
            self.assertFalse((self.run_dir / name).exists(), name)

    def test_crash_keeps_session_for_systemd_restart(self):
        code, _popen, _process = self.start(-11)
        self.assertEqual(code, runner.EXIT_RETRY)
        self.assertEqual((self.run_dir / "rdp-session").read_text(), "rdp-work-pc\n")
        self.assertTrue((self.run_dir / "rdp-session.secret").exists())
        self.assertFalse((self.run_dir / "rdp.request").exists())
        # The restarted unit finds no new request and resumes the same session.
        process = mock.Mock(pid=4343, stdin=mock.Mock())
        process.poll.return_value = 0
        process.wait.return_value = 0
        with mock.patch.object(runner, "READY_SECONDS", 0.0), mock.patch.object(
            runner.os, "access", return_value=True
        ), mock.patch.object(runner, "native_size", return_value=None), mock.patch.object(
            runner.subprocess, "Popen", return_value=process
        ):
            self.assertEqual(runner.run_rdp(self.run_dir, self.store), 0)
        process.stdin.write.assert_called_once_with((PASSWORD + "\n").encode())

    def test_authentication_failure_is_final_and_forgets_the_password(self):
        code, _popen, _process = self.start(132)
        self.assertEqual(code, runner.EXIT_FINAL)
        self.assertIn("authentication failed", (self.run_dir / "rdp-work-pc-status").read_text())
        self.assertFalse((self.run_dir / "rdp-session.secret").exists())
        self.assertFalse((self.run_dir / "rdp-session").exists())

    def test_missing_password_or_certificate_never_starts_the_client(self):
        (self.run_dir / "rdp.request").write_text("rdp-work-pc\n")
        with mock.patch.object(runner.subprocess, "Popen") as popen:
            self.assertEqual(runner.run_rdp(self.run_dir, self.store), runner.EXIT_FINAL)
        popen.assert_not_called()
        self.assertIn("password is required", (self.run_dir / "rdp-work-pc-status").read_text())
        rdp.upsert_connection(dataclasses.replace(rdp.get_connection("rdp-work-pc", self.store), certificate=""), self.store)
        (self.run_dir / "rdp.request").write_text("rdp-work-pc\n")
        rdp.write_handoff("rdp-work-pc", PASSWORD, self.run_dir / "rdp-session.secret")
        with mock.patch.object(runner.subprocess, "Popen") as popen:
            self.assertEqual(runner.run_rdp(self.run_dir, self.store), runner.EXIT_FINAL)
        popen.assert_not_called()
        self.assertFalse((self.run_dir / "rdp-session.secret").exists())

    def test_close_request_is_a_normal_exit(self):
        (self.run_dir / "rdp.request").write_text("rdp-work-pc\n")
        rdp.write_handoff("rdp-work-pc", PASSWORD, self.run_dir / "rdp-session.secret")
        process = mock.Mock(pid=77, stdin=mock.Mock())
        process.poll.return_value = None
        process.wait.return_value = -15

        def mark_close(*_args, **_kwargs):
            (self.run_dir / "close-rdp-work-pc").touch()
            return process

        with mock.patch.object(runner, "READY_SECONDS", 0.0), mock.patch.object(
            runner.os, "access", return_value=True
        ), mock.patch.object(runner, "native_size", return_value=None), mock.patch.object(
            runner.subprocess, "Popen", side_effect=mark_close
        ), mock.patch.object(runner.os, "killpg") as killpg:
            self.assertEqual(runner.run_rdp(self.run_dir, self.store), 0)
        killpg.assert_called_once_with(77, runner.signal.SIGTERM)
        self.assertEqual((self.run_dir / "rdp-work-pc-status").read_text(), "exited: disconnected\n")
        self.assertFalse((self.run_dir / "rdp-session.secret").exists())

    def test_lost_compositor_stops_the_client_and_keeps_the_session_for_restart(self):
        (self.run_dir / "rdp.request").write_text("rdp-work-pc\n")
        rdp.write_handoff("rdp-work-pc", PASSWORD, self.run_dir / "rdp-session.secret")
        process = mock.Mock(pid=88, stdin=mock.Mock())
        process.poll.return_value = None
        process.wait.return_value = -9
        with mock.patch.object(runner, "READY_SECONDS", 0.0), mock.patch.object(
            runner.os, "access", return_value=True
        ), mock.patch.object(runner, "native_size", return_value=None), mock.patch.object(
            runner, "compositor_watch", return_value=lambda: False
        ), mock.patch.object(runner.subprocess, "Popen", return_value=process), mock.patch.object(
            runner.os, "killpg"
        ) as killpg:
            self.assertEqual(runner.run_rdp(self.run_dir, self.store), runner.EXIT_RETRY)
        killpg.assert_called_once_with(88, runner.signal.SIGTERM)
        self.assertTrue((self.run_dir / "rdp-session").exists())
        self.assertIn("reconnecting", (self.run_dir / "rdp-work-pc-status").read_text())

    def test_compositor_watch_detects_a_replaced_socket(self):
        runtime = pathlib.Path(self.temporary.name) / "xdg"
        runtime.mkdir()
        socket_file = runtime / "wayland-0"
        socket_file.touch()
        alive = runner.compositor_watch({"XDG_RUNTIME_DIR": str(runtime), "WAYLAND_DISPLAY": "wayland-0"})
        self.assertTrue(alive())
        replacement = runtime / "new-socket"
        replacement.touch()  # exists alongside the original, so its inode differs
        replacement.replace(socket_file)
        self.assertFalse(alive())
        socket_file.unlink()
        self.assertFalse(alive())
        self.assertIsNone(runner.compositor_watch({"XDG_RUNTIME_DIR": str(runtime)}))

    def test_password_handed_to_another_connection_is_refused(self):
        (self.run_dir / "rdp.request").write_text("rdp-work-pc\n")
        rdp.write_handoff("rdp-other", PASSWORD, self.run_dir / "rdp-session.secret")
        with mock.patch.object(runner.subprocess, "Popen") as popen:
            self.assertEqual(runner.run_rdp(self.run_dir, self.store), runner.EXIT_FINAL)
        popen.assert_not_called()
        self.assertIn("another connection", (self.run_dir / "rdp-work-pc-status").read_text())
        self.assertFalse((self.run_dir / "rdp-session.secret").exists())

    def test_exit_status_mapping(self):
        self.assertEqual(runner.rdp_result(0)[0], 0)
        self.assertEqual(runner.rdp_result(11)[0], 0)
        self.assertEqual(runner.rdp_result(154)[0], runner.EXIT_FINAL)
        self.assertEqual(runner.rdp_result(143)[0], runner.EXIT_FINAL)
        self.assertEqual(runner.rdp_result(131)[0], runner.EXIT_RETRY)
        self.assertIn("could not read the password", runner.rdp_result(24)[1])  # -1000 & 0xff
        self.assertEqual(runner.rdp_result(-9)[0], runner.EXIT_RETRY)

    def test_on_failure_cleanup(self):
        (self.run_dir / "rdp-session").write_text("rdp-work-pc\n")
        rdp.write_handoff("rdp-work-pc", PASSWORD, self.run_dir / "rdp-session.secret")
        (self.run_dir / "rdp-work-pc-ready").touch()
        (self.run_dir / "app-active").write_text("rdp-work-pc\n")
        (self.run_dir / "rdp.request").write_text("rdp-newer\n")  # a newer launch
        self.assertEqual(runner.cleanup_rdp(self.run_dir), 0)
        self.assertEqual(sorted(path.name for path in self.run_dir.iterdir()), ["rdp.request"])

    def test_on_failure_cleanup_keeps_the_password_for_the_pending_request(self):
        # The same connection was launched again while the old session was giving
        # up: its request and freshly handed-off password must survive so the
        # re-armed path unit can start it.
        (self.run_dir / "rdp-session").write_text("rdp-work-pc\n")
        rdp.write_handoff("rdp-work-pc", PASSWORD, self.run_dir / "rdp-session.secret")
        (self.run_dir / "rdp-work-pc-ready").touch()
        (self.run_dir / "rdp.request").write_text("rdp-work-pc\n")
        self.assertEqual(runner.cleanup_rdp(self.run_dir), 0)
        self.assertEqual(
            sorted(path.name for path in self.run_dir.iterdir()), ["rdp-session.secret", "rdp.request"]
        )


if __name__ == "__main__":
    unittest.main()
