import testenv  # noqa: F401  (first: scratch run and state directories)
import pathlib
import tempfile
import unittest

import couchliteos_frontapp as frontapp
from couchliteos_frontapp import ERROR, LAUNCH, RETURN, Plan, Seen


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


HOME = Seen()
GAME = Seen(frozenset({"moonlight"}))
OVER_GAME = Seen(frozenset({"moonlight"}), launcher_focus=True)  # Home pressed: the TV over the stream


class FrontTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.front = frontapp.Front(self.clock)

    def after(self, seconds, seen):
        self.clock.now += seconds
        return self.front.update(seen)

    def started(self):
        """A start that worked: the launch sound, black, the ready mark, then the game in front."""
        self.assertEqual(self.front.start(), Plan(sound=LAUNCH))
        self.assertEqual(self.after(frontapp.BLACK_AFTER, HOME), Plan(rest=True))
        self.clock.now += 4
        self.front.start_done()
        self.assertEqual(self.front.update(GAME), Plan())
        self.assertEqual(self.after(frontapp.REST_AFTER, GAME), Plan(black=True))
        self.assertTrue(self.front.slow)


class StartTest(FrontTestCase):
    def test_a_start_plays_the_launch_sound_and_rests_once_the_screen_is_black(self):
        self.assertEqual(self.front.start(), Plan(sound=LAUNCH))
        self.assertTrue(self.front.awake.is_set())
        self.assertEqual(self.after(frontapp.BLACK_AFTER / 2, HOME), Plan())  # still fading to black
        self.assertEqual(self.after(frontapp.BLACK_AFTER / 2, HOME), Plan(rest=True))
        self.assertFalse(self.front.awake.is_set())
        self.assertEqual(self.after(1, HOME), Plan())  # once

    def test_the_game_in_front_turns_the_loading_screen_black_and_the_tick_slow(self):
        self.started()
        self.assertEqual(self.after(30, GAME), Plan())
        self.assertFalse(self.front.awake.is_set())

    def test_the_spinner_keeps_going_while_the_application_has_not_settled(self):
        self.front.start()
        self.after(1, HOME)
        self.front.start_done()
        self.after(0.1, GAME)
        self.assertFalse(self.front.slow)  # the loading screen still turns until REST_AFTER


class EndTest(FrontTestCase):
    def test_the_game_ending_wakes_the_tv_reveals_it_and_plays_the_return_sound(self):
        self.started()
        plan = self.after(60, HOME)
        self.assertEqual(plan, Plan(wake=True, reveal=True, sound=RETURN, music_after=frontapp.RETURN_MUSIC))
        self.assertTrue(self.front.awake.is_set())
        self.assertEqual(self.after(1, HOME), Plan())  # once

    def test_home_over_the_game_wakes_it_first_and_plays_the_return_sound_once(self):
        self.started()
        self.clock.now += 10
        self.assertEqual(self.front.returning(),
                         Plan(wake=True, reveal=True, sound=RETURN, music_after=frontapp.RETURN_MUSIC))
        self.assertEqual(self.after(0.2, OVER_GAME), Plan())

    def test_a_quick_look_home_and_back_costs_nothing(self):
        self.started()
        self.after(5, OVER_GAME)
        self.assertEqual(self.after(1, GAME), Plan())  # back to the game: no rest yet
        self.assertEqual(self.after(0.5, OVER_GAME), Plan(sound=RETURN, music_after=frontapp.RETURN_MUSIC))
        self.assertTrue(self.front.awake.is_set())

    def test_back_to_a_running_game_rests_after_a_while_with_a_black_screen(self):
        self.started()
        self.after(5, OVER_GAME)
        self.after(1, GAME)
        self.assertEqual(self.after(frontapp.REST_AFTER, GAME), Plan(rest=True, black=True))

    def test_closing_a_background_application_from_the_tv_plays_the_return_sound(self):
        self.started()
        self.after(5, OVER_GAME)
        self.assertEqual(self.after(3, HOME), Plan(sound=RETURN, music_after=frontapp.RETURN_MUSIC))


class FailureTest(FrontTestCase):
    def test_a_failed_start_plays_the_error_sound_never_the_return_one(self):
        self.front.start()
        self.after(1, HOME)
        self.clock.now += 3
        self.assertEqual(self.front.failed(), Plan(wake=True, reveal=True, sound=ERROR))
        self.assertTrue(self.front.awake.is_set())
        self.assertEqual(self.after(1, Seen(launcher_focus=True)), Plan())

    def test_a_refusal_before_any_start_only_plays_the_error_sound(self):
        self.assertEqual(self.front.failed(), Plan(sound=ERROR))

    def test_an_application_that_quits_at_once_plays_no_return_sound(self):
        self.front.start()
        self.after(0.3, HOME)
        self.front.start_done()  # "exited:" before its ready mark
        self.assertEqual(self.after(0.2, HOME), Plan(reveal=True))

    def test_one_that_ran_a_while_before_it_quit_plays_the_return_sound(self):
        self.front.start()
        self.after(1, HOME)
        self.clock.now += 3
        self.front.start_done()
        self.assertEqual(self.after(0.1, HOME), Plan(wake=True, reveal=True, sound=RETURN,
                                                     music_after=frontapp.RETURN_MUSIC))

    def test_a_ready_mark_that_goes_at_once_plays_no_return_sound(self):
        self.front.start()
        self.after(0.3, GAME)
        self.front.start_done()
        self.assertEqual(self.after(0.5, HOME), Plan(reveal=True))


class BlankAndSleepTest(FrontTestCase):
    def test_the_blank_screen_rests_at_once_and_a_key_wakes_it_silently(self):
        self.assertEqual(self.after(1, Seen(blank=True)), Plan(rest=True))
        self.assertFalse(self.front.slow)  # the tick goes on: the idle timer still counts to sleep
        self.assertEqual(self.after(300, Seen(blank=True)), Plan())
        self.assertEqual(self.after(1, HOME), Plan(wake=True))

    def test_sleep_rests_and_the_resume_wakes(self):
        self.assertEqual(self.after(1, Seen(blank=True, asleep=True)), Plan(rest=True))
        self.assertEqual(self.front.returning(), Plan(wake=True))

    def test_returning_during_a_start_waits_for_it(self):
        self.front.start()
        self.after(1, HOME)
        self.assertEqual(self.front.returning(), Plan())
        self.assertTrue(self.front.resting)


class StreamTest(FrontTestCase):
    def test_a_stream_that_ends_comes_back_like_any_application(self):
        self.started()
        self.after(600, GAME)
        self.assertFalse(self.front.awake.is_set())
        plan = self.after(5, HOME)
        self.assertTrue(plan.wake and plan.reveal)
        self.assertEqual(plan.sound, RETURN)

    def test_a_second_stream_right_after_the_first_rests_again(self):
        self.started()
        self.after(60, HOME)
        self.clock.now += 10
        self.started()


class LookTest(unittest.TestCase):
    def test_ready_marks_and_launcher_focus(self):
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            self.assertEqual(frontapp.look(run), Seen())
            for name in ("launcher-ready", "moonlight-ready", "moonlight-status", "home.request"):
                (run / name).touch()
            self.assertEqual(frontapp.look(run, blank=True), Seen(frozenset({"moonlight"}), blank=True))
            self.assertTrue(frontapp.look(run).app_in_front)
            (run / "launcher-focus").touch()
            self.assertFalse(frontapp.look(run).app_in_front)
            self.assertEqual(frontapp.look(run / "gone"), Seen())

    def test_the_watched_names(self):
        for name in ("moonlight-ready", "launcher-focus", "home.request", "resumed"):
            self.assertTrue(frontapp.watched(name), name)
        for name in ("launcher-ready", "moonlight-status", "wave-starting", "suspend"):
            self.assertFalse(frontapp.watched(name), name)


if __name__ == "__main__":
    unittest.main()
