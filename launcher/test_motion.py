import testenv  # noqa: F401  (first: scratch run and state directories)
import unittest

import couchliteos_motion as motion


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class EasingTest(unittest.TestCase):
    def test_every_easing_starts_at_0_and_ends_at_1(self):
        for ease in (motion.linear, motion.ease_out_cubic, motion.ease_in_out_cubic, motion.ease_out_back):
            self.assertAlmostEqual(ease(0.0), 0.0, places=6, msg=ease.__name__)
            self.assertAlmostEqual(ease(1.0), 1.0, places=6, msg=ease.__name__)

    def test_ease_out_is_ahead_of_linear_halfway(self):
        self.assertGreater(motion.ease_out_cubic(0.5), 0.5)

    def test_back_overshoots_then_settles(self):
        self.assertGreater(max(motion.ease_out_back(t / 100) for t in range(101)), 1.0)


class TweenTest(unittest.TestCase):
    def test_value_moves_from_start_to_end_over_the_duration(self):
        tween = motion.Tween(0, 100, t0=10, duration=0.25, ease=motion.linear)
        self.assertEqual(tween.value(10), 0)
        self.assertEqual(tween.value(10.125), 50)
        self.assertEqual(tween.value(10.25), 100)
        self.assertEqual(tween.value(99), 100)
        self.assertFalse(tween.done(10.125))
        self.assertTrue(tween.done(10.25))

    def test_a_dropped_frame_skips_ahead_rather_than_slowing_down(self):
        tween = motion.Tween(0, 100, t0=0, duration=0.2, ease=motion.linear)
        self.assertEqual(tween.value(0.5), 100)

    def test_zero_duration_is_already_at_the_end(self):
        self.assertEqual(motion.Tween(0, 5, 0, 0).value(0), 5)


class AnimatorTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.anim = motion.Animator(self.clock)

    def test_to_moves_and_step_reports_until_settled(self):
        self.anim.set("x", 0)
        self.anim.to("x", 100, motion.CATEGORY_MS, motion.linear)
        self.assertTrue(self.anim.step())
        self.assertFalse(self.anim.idle)
        self.clock.now += 0.11
        self.assertAlmostEqual(self.anim.get("x"), 50, delta=1)
        self.clock.now += 0.2
        self.assertFalse(self.anim.step())
        self.assertTrue(self.anim.idle)
        self.assertEqual(self.anim.get("x"), 100)

    def test_a_retarget_mid_tween_starts_from_the_current_value(self):
        self.anim.set("x", 0)
        self.anim.to("x", 100, 200, motion.linear)
        self.clock.now += 0.1
        self.anim.to("x", 200, 200, motion.linear)
        self.assertAlmostEqual(self.anim.get("x"), 50, delta=1)  # no jump

    def test_a_single_press_after_a_pause_gets_the_full_duration(self):
        self.anim.set("x", 0)
        self.anim.to("x", 1, motion.CATEGORY_MS)
        self.clock.now += 1
        self.anim.to("x", 2, motion.CATEGORY_MS)
        self.assertAlmostEqual(self.anim.tweens["x"].duration, motion.CATEGORY_MS / 1000)

    def test_held_keys_never_queue_up_animation(self):
        # gamepad-nav repeats every 120 ms: each retarget must finish within HELD_MS.
        self.anim.set("x", 0)
        self.anim.to("x", 1, motion.CATEGORY_MS)
        for step in range(2, 10):
            self.clock.now += 0.12
            self.anim.to("x", step, motion.CATEGORY_MS)
            self.assertLessEqual(self.anim.tweens["x"].duration, motion.HELD_MS / 1000)
        self.clock.now += motion.HELD_MS / 1000
        self.anim.step()
        self.assertEqual(self.anim.get("x"), 9)

    def test_the_same_target_again_does_not_restart(self):
        self.anim.set("x", 0)
        self.anim.to("x", 10, 200)
        started = self.anim.tweens["x"]
        self.clock.now += 0.05
        self.anim.to("x", 10, 200)
        self.assertIs(self.anim.tweens["x"], started)

    def test_motion_off_jumps(self):
        self.anim.level = motion.OFF
        self.anim.set("x", 0)
        self.anim.to("x", 10, 200)
        self.assertTrue(self.anim.idle)
        self.assertEqual(self.anim.get("x"), 10)

    def test_reduced_jumps_movement_but_keeps_short_fades(self):
        self.anim.level = motion.REDUCED
        self.anim.set("slide", 0)
        self.anim.to("slide", 300, motion.CATEGORY_MS)
        self.assertEqual(self.anim.get("slide"), 300)
        self.anim.set("alpha", 0)
        self.anim.to("alpha", 1, motion.PAGE_MS, movement=False)
        self.assertFalse(self.anim.idle)
        self.assertLessEqual(self.anim.tweens["alpha"].duration, motion.FADE_MS / 1000)

    def test_an_unknown_value_starts_at_its_target(self):
        self.anim.to("new", 5, 200)
        self.assertEqual(self.anim.get("new"), 5)
        self.assertTrue(self.anim.idle)

    def test_target_is_where_a_value_is_going(self):
        self.anim.set("x", 0)
        self.anim.to("x", 7, 200)
        self.assertEqual(self.anim.target("x"), 7)
        self.assertEqual(self.anim.target("missing", 3), 3)


class LevelTest(unittest.TestCase):
    def test_the_software_renderer_is_at_most_reduced(self):
        self.assertEqual(motion.level_for("full", "cairo"), motion.REDUCED)
        self.assertEqual(motion.level_for("off", "cairo"), motion.OFF)
        self.assertEqual(motion.level_for("full", None), motion.FULL)
        self.assertEqual(motion.level_for(None, "ngl"), motion.FULL)
        self.assertEqual(motion.level_for("silly", ""), motion.FULL)


class FrameMeterTest(unittest.TestCase):
    def run_frames(self, gap):
        meter = motion.FrameMeter()
        now = 0.0
        for _ in range(motion.FrameMeter.SAMPLES + 2):
            meter.frame(now)
            now += gap
        return meter

    def test_60_fps_is_not_slow(self):
        meter = self.run_frames(1 / 60)
        self.assertFalse(meter.slow())
        self.assertAlmostEqual(meter.fps(), 60, delta=1)

    def test_15_fps_is_slow(self):
        self.assertTrue(self.run_frames(1 / 15).slow())

    def test_too_few_frames_never_count_as_slow(self):
        meter = motion.FrameMeter()
        meter.frame(0)
        meter.frame(1 / 10)
        self.assertFalse(meter.slow())

    def test_a_pause_between_animations_is_not_a_slow_frame(self):
        meter = motion.FrameMeter()
        meter.frame(0)
        meter.frame(5)
        self.assertEqual(meter.intervals, [])


if __name__ == "__main__":
    unittest.main()
