import testenv  # noqa: F401  (first: scratch run and state directories)
import math
import unittest

import couchliteos_loading as loading
import couchliteos_motion as motion


def brightest(dots) -> int:
    return max(range(len(dots)), key=lambda index: dots[index].alpha)


class RingTest(unittest.TestCase):
    def test_the_dots_sit_on_a_circle_clockwise_from_the_top(self):
        dots = loading.ring(0.0, motion.FULL)
        self.assertEqual(len(dots), loading.DOTS)
        for dot in dots:
            self.assertAlmostEqual(math.hypot(dot.x, dot.y), 1.0, places=6)
        self.assertAlmostEqual(dots[0].y, -1.0)  # the top
        self.assertAlmostEqual(dots[loading.DOTS // 4].x, 1.0)  # a quarter on: the right

    def test_full_motion_the_head_goes_round_with_a_fading_tail(self):
        step = loading.TURN_SECONDS / loading.DOTS
        heads = [brightest(loading.ring(n * step, motion.FULL)) for n in range(loading.DOTS + 1)]
        self.assertEqual(heads, [*range(loading.DOTS), 0])
        dots = loading.ring(0.0, motion.FULL)
        tail = [dots[-n].alpha for n in range(1, 5)]  # the dots just behind the head, going back
        self.assertEqual(tail, sorted(tail, reverse=True))
        self.assertLess(tail[0], dots[0].alpha)
        self.assertGreater(dots[0].radius, dots[loading.DOTS // 2].radius)  # the head is bigger

    def test_no_dot_flicks_on_between_two_frames(self):
        frames = [loading.ring(n / 60, motion.FULL) for n in range(int(loading.TURN_SECONDS * 60) + 2)]
        for before, after in zip(frames, frames[1:]):
            for a, b in zip(before, after):
                self.assertLess(abs(a.alpha - b.alpha), 0.25)

    def test_every_dot_stays_visible_and_within_its_brightness(self):
        for level in motion.LEVELS:
            for n in range(50):
                for dot in loading.ring(n * 0.137, level):
                    self.assertGreaterEqual(dot.alpha, loading.DIM - 1e-9)
                    self.assertLessEqual(dot.alpha, 1.0)
                    self.assertGreater(dot.radius, 0)

    def test_reduced_motion_nothing_goes_round_the_ring_breathes(self):
        for n in range(20):
            dots = loading.ring(n * 0.31, motion.REDUCED)
            self.assertEqual(len({dot.alpha for dot in dots}), 1)  # every dot alike: no head
            self.assertEqual(len({dot.radius for dot in dots}), 1)
        low = loading.ring(0.0, motion.REDUCED)[0].alpha
        high = loading.ring(loading.BREATHE_SECONDS / 2, motion.REDUCED)[0].alpha
        self.assertGreater(high, low + 0.4)

    def test_motion_off_the_head_steps_twice_a_second_and_nothing_eases(self):
        self.assertEqual(loading.ring(0.0, motion.OFF), loading.ring(loading.STEP_SECONDS * 0.9, motion.OFF))
        self.assertEqual(brightest(loading.ring(loading.STEP_SECONDS, motion.OFF)), 1)
        self.assertEqual(loading.frame(0.1, motion.OFF), loading.frame(0.4, motion.OFF))
        self.assertNotEqual(loading.frame(0.1, motion.OFF), loading.frame(0.6, motion.OFF))
        self.assertNotEqual(loading.frame(0.1, motion.FULL), loading.frame(0.11, motion.FULL))

    def test_the_ring_fades_in_except_with_motion_off(self):
        self.assertEqual(loading.appear(0.0, motion.OFF), 1.0)
        for level in (motion.FULL, motion.REDUCED):
            self.assertEqual(loading.appear(0.0, level), 0.0)
            self.assertEqual(loading.appear(1.0, level), 1.0)
        self.assertLess(loading.appear(0.05, motion.REDUCED), 1.0)


if __name__ == "__main__":
    unittest.main()
