"""Wizard steps after the network: controller scan cleanup, sound test, redo, streaming PC.

Shares the fakes in test_setup.py. Only `base.<name>` is used so that the test
classes in that module are not collected a second time from here.
"""

import testenv  # noqa: F401  (first: scratch run and state directories)
import importlib.util
import itertools
import pathlib
import tempfile
import unittest
from unittest import mock

import couchliteos_audio as audio
import couchliteos_setup as setup
import test_setup as base
from test_launcher import Screen

MAX_COLUMNS = 76


class ScanCleanupTest(base.WizardTestCase):
    """Bluetooth discovery must not be left running once the wizard is done with it."""

    def run_controller(self, ui, bluetooth, system=None):
        system = system or base.FakeSystem(after_pairing=[base.PAD])
        bluetooth.system = system
        return self.wizard(ui, system, bluetooth).step_controller()

    @staticmethod
    def scan_commands(bluetooth):
        return [name for name, _fields in bluetooth.requests if name in ("start_scan", "stop_scan", "pair")]

    def assertScanStopped(self, bluetooth):
        commands = self.scan_commands(bluetooth)
        self.assertIn("start_scan", commands)
        self.assertEqual(commands[-1], "stop_scan", commands)

    def test_scanning_stops_before_pairing_starts_and_stays_stopped(self):
        bluetooth = base.FakeBluetooth([base.XBOX], states=["working", "completed"])
        self.assertEqual(self.run_controller(base.FakeUI("PAIR", "XBOX", "OK"), bluetooth), "done")
        commands = self.scan_commands(bluetooth)
        self.assertLess(commands.index("stop_scan"), commands.index("pair"), commands)
        self.assertScanStopped(bluetooth)

    def test_a_pairing_the_service_rejects_still_ends_with_the_scan_stopped(self):
        class Rejecting(base.FakeBluetooth):
            def request(self, command, **fields):
                super().request(command, **fields)
                return None if command == "pair" else {"ok": True}

        bluetooth = Rejecting([base.XBOX])
        ui = base.FakeUI("PAIR", "XBOX", "CONTINUE ANYWAY")
        self.assertEqual(self.run_controller(ui, bluetooth), "failed")
        self.assertScanStopped(bluetooth)

    def test_a_failed_pairing_ends_with_the_scan_stopped(self):
        bluetooth = base.FakeBluetooth([base.XBOX], states=["failed"])
        ui = base.FakeUI("PAIR", "XBOX", "CONTINUE ANYWAY")
        self.assertEqual(self.run_controller(ui, bluetooth), "failed")
        self.assertScanStopped(bluetooth)

    def test_a_controller_that_never_answers_the_a_button_ends_with_the_scan_stopped(self):
        bluetooth = base.FakeBluetooth([base.XBOX])
        system = base.FakeSystem(after_pairing=[base.PAD], pressed=False)
        ui = base.FakeUI("PAIR", "XBOX", "CONTINUE ANYWAY")
        self.assertEqual(self.run_controller(ui, bluetooth, system), "failed")
        self.assertScanStopped(bluetooth)

    def test_nothing_found_and_back_ends_with_the_scan_stopped(self):
        bluetooth = base.FakeBluetooth([])
        ui = base.FakeUI("PAIR", "XBOX", "BACK", "SKIP")
        self.assertEqual(self.run_controller(ui, bluetooth, base.FakeSystem()), "skipped")
        self.assertScanStopped(bluetooth)

    def test_an_error_while_searching_still_stops_the_scan(self):
        class Breaking(base.FakeUI):
            def wait(self, title, lines, done, timeout, *, big=None):
                raise RuntimeError("screen lost")

        bluetooth = base.FakeBluetooth([base.XBOX])
        with self.assertRaises(RuntimeError):
            self.run_controller(Breaking("PAIR", "XBOX"), bluetooth)
        self.assertScanStopped(bluetooth)


class SoundRestoreTest(base.WizardTestCase):
    """The tone test moves the default output around; only a heard tone may keep the move."""

    @staticmethod
    def sinks(default):
        names = {1: "Analog", 2: "HDMI 1", 3: "USB Headset"}
        return [audio.Sink(number, name, number == default) for number, name in names.items()]

    def run_sound(self, ui, default=3, system=None, **extra):
        sinks = self.sinks(default)
        system = system or base.FakeSystem(sink_list=sinks)
        return self.wizard(ui, system, **extra).choose_sound(sinks)

    # Candidates are tried HDMI first (2), then the rest in list order (1, 3).

    def test_skipping_the_sound_test_puts_the_original_output_back(self):
        for answer in ("SKIP SOUND", None):
            with self.subTest(answer=answer):
                outcome = self.run_sound(base.FakeUI("NO, TRY", answer))
                self.assertEqual(outcome, "skipped")
                self.assertEqual(self.system.default_sinks, [2, 1, 3])

    def test_skipping_on_the_first_output_puts_the_original_output_back(self):
        self.assertEqual(self.run_sound(base.FakeUI("SKIP SOUND")), "skipped")
        self.assertEqual(self.system.default_sinks, [2, 3])

    def test_hearing_nothing_anywhere_puts_the_original_output_back(self):
        ui = base.FakeUI("NO, TRY", "NO, TRY", "NO, I HEARD NOTHING", "CONTINUE")
        self.assertEqual(self.run_sound(ui, default=1), "failed")
        self.assertEqual(self.system.default_sinks, [2, 1, 3, 1])

    def test_a_heard_tone_keeps_that_output(self):
        self.assertEqual(self.run_sound(base.FakeUI("NO, TRY", "YES")), "done")
        self.assertEqual(self.system.default_sinks, [2, 1])

    def test_playing_again_never_goes_back_to_the_original_output(self):
        self.assertEqual(self.run_sound(base.FakeUI("PLAY AGAIN", "YES")), "done")
        self.assertEqual(set(self.system.default_sinks), {2})

    def test_an_error_while_playing_the_tone_still_puts_the_original_output_back(self):
        def tone():
            raise RuntimeError("no player")

        with self.assertRaises(RuntimeError):
            self.run_sound(base.FakeUI(), tone=tone)
        self.assertEqual(self.system.default_sinks, [2, 3])

    def test_no_known_default_means_nothing_is_restored(self):
        self.assertEqual(self.run_sound(base.FakeUI("SKIP SOUND"), default=None), "skipped")
        self.assertEqual(self.system.default_sinks, [2])

    def test_a_restore_that_fails_does_not_crash_the_wizard(self):
        class Stubborn(base.FakeSystem):
            def set_default_sink(self, sink_id):
                super().set_default_sink(sink_id)
                if len(self.default_sinks) > 1:
                    raise RuntimeError("wpctl failed")

        system = Stubborn(sink_list=self.sinks(3))
        self.assertEqual(self.run_sound(base.FakeUI("SKIP SOUND"), system=system), "skipped")
        self.assertEqual(system.default_sinks, [2, 3])

    def test_the_display_and_sound_step_restores_too(self):
        modes = base.DisplayAndSoundFlowTest.MODES
        system = base.FakeSystem(modes=modes, sink_list=self.sinks(3))
        ui = base.FakeUI("KEEP CURRENT", "SKIP SOUND")
        self.assertEqual(self.wizard(ui, system).step_display(), "done")
        self.assertEqual(system.default_sinks[-1], 3)


class RedoTest(base.WizardTestCase):
    """REDO SKIPPED OR FAILED STEPS reopens only what is not done, and never undoes a done step."""

    ORDER = ["network", "controller", "display", "streaming", "tailscale", "chiaki-ng", "browser", "applications"]

    def stubbed(self, ui, state, outcomes=None, *, only=None):
        """A wizard that starts from `state`; the steps in `only` (default all) are recorded, not run."""
        setup.save_state(state, self.state_path)
        self.ran = []
        wizard = self.wizard(ui)
        for step in only or self.ORDER:
            outcome = (outcomes or {}).get(step, setup.SKIPPED)
            setattr(
                wizard, "step_" + step.replace("-", "_"),
                lambda step=step, outcome=outcome: self.ran.append(step) or outcome,
            )
        return wizard

    def all_done_except(self, **others):
        return {step: others.get(step.replace("-", "_"), "done") for step in self.ORDER}

    def test_only_steps_that_are_not_done_are_redone(self):
        state = self.all_done_except(controller="failed", tailscale="skipped")
        ui = base.FakeUI("START SETUP", "REDO", "FINISH")
        wizard = self.stubbed(ui, state, {"controller": "done", "tailscale": "skipped"})
        self.assertTrue(wizard.run())
        self.assertEqual(self.ran, ["controller", "tailscale"])
        self.assertEqual(self.state(), self.all_done_except(tailscale="skipped"))

    def test_steps_after_a_redone_one_are_not_asked_again(self):
        # Only the controller step is replaced: the real display, streaming and optional
        # steps would ask the (empty) scripted UI and fail the test if they were opened.
        state = self.all_done_except(controller="failed")
        ui = base.FakeUI("START SETUP", "REDO", "FINISH")
        wizard = self.stubbed(ui, state, only=["controller"])
        wizard.run()
        self.assertEqual(self.ran, ["controller"])
        self.assertEqual(ui.titles().count("SETUP COMPLETE"), 2)
        self.assertEqual(set(self.state().values()), {"done", "skipped"})
        self.assertEqual(self.state()["display"], "done")

    def test_skipping_a_redone_step_with_b_keeps_every_done_step_done(self):
        state = self.all_done_except(network="failed")
        wizard = self.stubbed(base.FakeUI("START SETUP", "REDO", "FINISH"), state, only=["network"])
        wizard.run()
        self.assertEqual(self.state(), self.all_done_except(network="skipped"))

    def test_a_done_step_that_is_ahead_of_the_resume_point_is_not_run_again(self):
        ui = base.FakeUI("CONTINUE SETUP", "FINISH")
        wizard = self.stubbed(ui, {"tailscale": "done"})
        wizard.run()
        self.assertEqual(self.ran, ["network", "controller", "display", "streaming", "chiaki-ng", "browser", "applications"])
        self.assertEqual(self.state()["tailscale"], "done")

    def test_escape_on_the_done_screen_does_not_run_anything_again(self):
        ui = base.FakeUI("START SETUP", None, "FINISH")
        self.stubbed(ui, {}).run()
        self.assertEqual(self.ran, self.ORDER)

    def test_redo_twice_reopens_only_what_is_still_not_done(self):
        state = self.all_done_except(controller="failed", tailscale="skipped")
        ui = base.FakeUI("START SETUP", "REDO", "REDO", "FINISH")
        wizard = self.stubbed(ui, state, {"controller": "done"})
        wizard.run()
        self.assertEqual(self.ran, ["controller", "tailscale", "tailscale"])


class PairedPcTest(base.WizardTestCase):
    """A gaming PC counts as paired only if it is new since the pairing was started."""

    def pairing(self, system, *hosts, result=True):
        """Actions that save `hosts` as paired, the way Moonlight does when the PIN is accepted."""
        def launch(app_id):
            self.calls.append(("launch", app_id))
            system.hosts += hosts
            return True

        def pair(host, pin):
            self.calls.append(("pair", host, pin))
            system.hosts += hosts
            return result

        return {"launch": launch, "pair_moonlight": pair}

    def test_the_new_host_helper_ignores_hosts_that_were_already_paired(self):
        self.assertEqual(setup.new_hosts(["A", "B"], ["A", "B", "C"]), ["C"])
        self.assertEqual(setup.new_hosts(["A"], ["A"]), [])
        self.assertEqual(setup.new_hosts([], ["A"]), ["A"])
        self.assertEqual(setup.new_hosts(["A"], []), [])

    def test_an_old_pairing_does_not_make_the_moonlight_route_done(self):
        system = base.FakeSystem(hosts=["OLD-PC"])
        ui = base.FakeUI("FIND MY GAMING PC", "OPEN MOONLIGHT", "CONTINUE WITHOUT")
        self.assertEqual(self.wizard(ui, system).step_streaming(), "failed")
        self.assertIn("NO NEW GAMING PC", ui.text())

    def test_an_old_pairing_does_not_make_the_pin_route_done(self):
        self.texts = ["192.168.1.20"]
        system = base.FakeSystem(hosts=["OLD-PC"])
        ui = base.FakeUI("PAIR WITH A PIN", "START PAIRING", "CONTINUE WITHOUT")
        self.assertEqual(self.wizard(ui, system).step_streaming(), "failed")

    def test_a_new_pc_next_to_an_old_one_is_done_on_both_routes(self):
        system = base.FakeSystem(hosts=["OLD-PC"])
        ui = base.FakeUI("FIND MY GAMING PC", "OPEN MOONLIGHT")
        wizard = self.wizard(ui, system, **self.pairing(system, "NEW-PC"))
        self.assertEqual(wizard.step_streaming(), "done")
        self.texts = ["192.168.1.21"]
        system = base.FakeSystem(hosts=["OLD-PC"])
        ui = base.FakeUI("PAIR WITH A PIN", "START PAIRING")
        wizard = self.wizard(ui, system, **self.pairing(system, "NEW-PC"))
        self.assertEqual(wizard.step_streaming(), "done")

    def test_a_pc_that_is_already_paired_can_be_kept_without_pairing_again(self):
        system = base.FakeSystem(hosts=["OLD-PC", "DEN-PC"])
        ui = base.FakeUI("KEEP THE PC ALREADY PAIRED")
        self.assertEqual(self.wizard(ui, system).step_streaming(), "done")
        self.assertIn("ALREADY PAIRED: OLD-PC, DEN-PC", ui.text())
        self.assertEqual([call for call in self.calls if call[0] in ("launch", "pair")], [])

    def test_nothing_is_offered_to_keep_when_no_pc_was_ever_paired(self):
        ui = base.FakeUI(None)
        self.wizard(ui, base.FakeSystem()).step_streaming()
        self.assertNotIn("ALREADY PAIRED", ui.text())
        self.assertNotIn("KEEP THE PC", ui.text())

    def test_the_list_of_paired_pcs_fits_the_screen_and_is_clean_text(self):
        system = base.FakeSystem(hosts=["PC-" + "X" * 100, "ODD\x1b[31mNAME"])
        ui = base.FakeUI(None)
        self.wizard(ui, system).step_streaming()
        shown = [line for line in ui.screens[0]["lines"] if line.startswith("ALREADY PAIRED")]
        self.assertEqual(len(shown), 1)
        self.assertLessEqual(len(shown[0]), MAX_COLUMNS)
        self.assertNotIn("\x1b", shown[0])


class PcNotFoundHelpTest(base.WizardTestCase):
    """An asleep or unreachable gaming PC gets a short checklist, with the buttons to press."""

    def failed_screen(self, *answers, texts=()):
        self.texts = list(texts)
        ui = base.FakeUI(*answers, "CONTINUE WITHOUT")
        self.assertEqual(self.wizard(ui, base.FakeSystem()).step_streaming(), "failed")
        return next(screen for screen in ui.screens if screen["title"] == "NOT PAIRED YET")

    def test_the_checklist_names_the_three_usual_causes(self):
        screen = self.failed_screen("FIND MY GAMING PC", "OPEN MOONLIGHT")
        text = " ".join(screen["lines"]).upper()
        self.assertRegex(text, r"ON AND AWAKE|WAKE")
        self.assertIn("SUNSHINE", text)
        self.assertIn("SAME NETWORK", text)
        self.assertIn("PIN", text)

    def test_the_pin_route_also_asks_to_check_the_address(self):
        screen = self.failed_screen("PAIR WITH A PIN", "START PAIRING", texts=["192.168.1.20"])
        self.assertIn("192.168.1.20", " ".join(screen["lines"]))

    def test_the_checklist_names_the_controller_buttons(self):
        text = " ".join(self.failed_screen("FIND MY GAMING PC", "OPEN MOONLIGHT")["lines"])
        self.assertIn("PRESS A", text)
        self.assertIn("B", text.split("PRESS A")[-1])

    def test_the_choices_stay_try_again_and_continue_without_pairing(self):
        screen = self.failed_screen("FIND MY GAMING PC", "OPEN MOONLIGHT")
        self.assertEqual(screen["choices"], ["TRY AGAIN", "CONTINUE WITHOUT PAIRING"])

    def test_every_line_fits_76_columns_even_with_a_long_address(self):
        long_host = "gaming-pc." + "x" * 60 + ".example.com"
        for answers, texts in ((("FIND MY GAMING PC", "OPEN MOONLIGHT"), ()),
                               (("PAIR WITH A PIN", "START PAIRING"), (long_host,))):
            with self.subTest(route=answers[0]):
                screen = self.failed_screen(*answers, texts=texts)
                for line in screen["lines"]:
                    self.assertLessEqual(len(line), MAX_COLUMNS, line)


class LauncherPairingTest(unittest.TestCase):
    """The hidden pairing app must not raise the generic "FAILED TO START" dialog."""

    @classmethod
    def setUpClass(cls):
        path = pathlib.Path(__file__).with_name("couchliteos-launcher.py")
        spec = importlib.util.spec_from_file_location("launcher_for_pairing", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def launcher(self):
        with mock.patch.object(self.module, "network_summary", return_value="OFFLINE"):
            launcher = self.module.Launcher(Screen([-1] * 50))
        launcher.show_launch_failure = mock.Mock()
        return launcher

    def pairing_app(self):
        return self.module.apps.Application(
            id="moonlight-pair", name="MOONLIGHT PAIRING", kind="command", command="/bin/true",
            status_id="moonlight-pair", visible=False,
        )

    def launch(self, launcher, *, status, quiet, clock):
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)

            def request_launch(*_arguments):
                # The launcher clears the status first; the started unit then writes its own.
                if status:
                    (run / "moonlight-pair-status").write_text(status + "\n")

            options = {"quiet": True} if quiet else {}
            with mock.patch.object(self.module, "RUN", run), \
                    mock.patch.object(self.module.apps, "atomic_write", side_effect=request_launch), \
                    mock.patch.object(self.module.time, "monotonic", side_effect=clock):
                return launcher.launch_app(self.pairing_app(), **options)

    QUICK_EXIT = "failed: exited before the application became ready (status 1)"

    def test_a_quick_exit_is_reported_quietly_when_asked(self):
        launcher = self.launcher()
        self.assertFalse(self.launch(launcher, status=self.QUICK_EXIT, quiet=True, clock=itertools.count(0, 1.0).__next__))
        launcher.show_launch_failure.assert_not_called()
        self.assertIn("FAILED TO START", launcher.status)

    def test_a_timeout_is_reported_quietly_when_asked(self):
        launcher = self.launcher()
        clock = iter([0.0, 100.0, 100.0, 100.0])
        self.assertFalse(self.launch(launcher, status="", quiet=True, clock=lambda: next(clock)))
        launcher.show_launch_failure.assert_not_called()
        self.assertIn("TIMED OUT", launcher.status)

    def test_other_applications_still_show_the_dialog(self):
        launcher = self.launcher()
        self.assertFalse(self.launch(launcher, status=self.QUICK_EXIT, quiet=False, clock=itertools.count(0, 1.0).__next__))
        launcher.show_launch_failure.assert_called_once()
        launcher = self.launcher()
        clock = iter([0.0, 100.0, 100.0, 100.0])
        self.assertFalse(self.launch(launcher, status="", quiet=False, clock=lambda: next(clock)))
        launcher.show_launch_failure.assert_called_once()

    def test_launch_and_wait_passes_quiet_on_only_when_asked(self):
        launcher = self.launcher()
        launcher.app_by_id = mock.Mock(return_value=self.pairing_app())
        launcher.launch_app = mock.Mock(return_value=False)
        self.assertFalse(launcher.launch_and_wait("moonlight-pair", quiet=True))
        launcher.launch_app.assert_called_once_with(self.pairing_app(), quiet=True, wake=False)
        launcher.launch_app.reset_mock()
        launcher.launch_and_wait("moonlight-pair")
        launcher.launch_app.assert_called_once_with(self.pairing_app(), wake=False)

    def test_moonlight_pairing_asks_for_a_quiet_launch(self):
        launcher = self.launcher()
        launcher.launch_and_wait = mock.Mock(return_value=False)
        with mock.patch.object(self.module.apps, "write_user_application"), \
                mock.patch.object(self.module.apps, "delete_user_application"):
            self.assertFalse(launcher.pair_moonlight("192.168.1.20", "0427"))
        self.assertIs(launcher.launch_and_wait.call_args.kwargs["quiet"], True)


if __name__ == "__main__":
    unittest.main()
