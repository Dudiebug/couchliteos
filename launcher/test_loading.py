import testenv  # noqa: F401  (first: scratch run and state directories)
import unittest

import couchliteos_loading as loading
import couchliteos_motion as motion


class Clock:
    def __init__(self) -> None:
        self.now = 50.0

    def __call__(self) -> float:
        return self.now


class LoadingTestCase(unittest.TestCase):
    def loading(self, level=motion.FULL):
        self.clock = Clock()
        return loading.Loading(self.clock, level)

    def at(self, screen, ms):
        self.clock.now += ms / 1000
        return screen.frame()


class StartTest(LoadingTestCase):
    def test_hidden_draws_nothing(self):
        frame = self.loading().frame()
        self.assertFalse(frame.drawn)
        self.assertFalse(frame.moving)

    def test_it_fades_to_black_then_the_picture_and_the_name_come_up(self):
        screen = self.loading()
        screen.start()
        first = screen.frame()
        self.assertEqual((first.black, first.content), (0.0, 0.0))
        self.assertTrue(first.moving)
        half = self.at(screen, loading.FADE_MS / 2)
        self.assertAlmostEqual(half.black, 0.5, places=2)
        self.assertEqual(half.content, 0.0)  # nothing on a half-faded home screen
        black = self.at(screen, loading.FADE_MS / 2)
        self.assertEqual((black.black, black.content), (1.0, 0.0))
        up = self.at(screen, loading.CONTENT_MS)
        self.assertEqual((up.black, up.content), (1.0, 1.0))
        self.assertTrue(self.at(screen, 60_000).moving)  # the ring turns until the application is there

    def test_the_ring_turns_once_in_spin_ms_with_a_tail(self):
        screen = self.loading()
        screen.start()
        start = screen.frame().dots
        self.assertEqual(len(start), loading.DOTS)
        self.assertEqual(start[0], 1.0)  # the head at the top
        self.assertLess(start[1], loading.DIM_DOT + 0.01)  # ahead of it: unlit
        self.assertGreater(start[-1], start[-2])  # the tail fades behind it
        quarter = self.at(screen, loading.SPIN_MS / 4)
        self.assertEqual(quarter.dots.index(max(quarter.dots)), loading.DOTS // 4)
        self.assertEqual(self.at(screen, loading.SPIN_MS * 3 / 4).dots, start)

    def test_the_head_moves_smoothly_between_two_dots(self):
        between = loading.ring(2.5)
        self.assertAlmostEqual(between[3], loading.DIM_DOT + (1 - loading.DIM_DOT) * 0.5)  # coming up
        self.assertLess(between[2], 1.0)
        for head in (0.0, 0.3, 5.99, 11.7):
            self.assertTrue(all(loading.DIM_DOT <= dot <= 1.0 for dot in loading.ring(head)))

    def test_the_picture_breathes(self):
        screen = self.loading()
        screen.start()
        self.assertEqual(screen.frame().swell, 1.0)
        top = self.at(screen, loading.BREATHE_MS / 2)
        self.assertAlmostEqual(top.swell, 1 + loading.SWELL)
        self.assertAlmostEqual(self.at(screen, loading.BREATHE_MS / 2).swell, 1.0)


class BlackAndRevealTest(LoadingTestCase):
    def test_black_takes_the_content_away_then_holds_still(self):
        screen = self.loading()
        screen.start()
        self.at(screen, 2000)
        screen.black()
        going = self.at(screen, loading.CONTENT_MS / 2)
        self.assertEqual(going.black, 1.0)
        self.assertAlmostEqual(going.content, 0.5, places=2)
        still = self.at(screen, loading.CONTENT_MS / 2)
        self.assertEqual((still.black, still.content, still.moving), (1.0, 0.0, False))

    def test_plain_black_is_the_same_frame_later_so_the_view_stops_drawing(self):
        # The view stops its tick when a frame that does not move equals the last one drawn: the
        # ring's and the picture's time must not leak into a black that shows neither.
        screen = self.loading()
        screen.start()
        self.at(screen, 2000)
        screen.black()
        held = self.at(screen, loading.CONTENT_MS + 10)
        self.assertFalse(held.moving)
        self.assertEqual(self.at(screen, 370), held)
        self.assertEqual(self.at(screen, 5000), held)

    def test_black_without_a_start_is_plain_black_at_once(self):
        screen = self.loading()
        screen.black()
        frame = screen.frame()
        self.assertEqual((frame.black, frame.content, frame.moving), (1.0, 0.0, False))
        self.assertTrue(screen.shown)

    def test_reveal_fades_the_black_away_and_hides_it(self):
        screen = self.loading()
        screen.start()
        self.at(screen, 3000)
        screen.black()
        self.at(screen, 5000)
        screen.reveal()
        self.assertEqual(screen.frame().black, 1.0)
        half = self.at(screen, loading.REVEAL_MS / 2)
        self.assertAlmostEqual(half.black, 0.5, places=2)
        self.assertTrue(half.moving)
        gone = self.at(screen, loading.REVEAL_MS / 2)
        self.assertFalse(gone.drawn)
        self.assertEqual(screen.phase, loading.HIDDEN)

    def test_a_reveal_while_starting_takes_the_content_away_quickly(self):
        screen = self.loading()
        screen.start()
        self.at(screen, 1000)
        screen.reveal()  # it failed: the failure screen is under the black
        self.assertAlmostEqual(self.at(screen, loading.REVEAL_MS / 2).content, 0.0)

    def test_reveal_when_hidden_does_nothing(self):
        screen = self.loading()
        screen.reveal()
        self.assertEqual(screen.phase, loading.HIDDEN)

    def test_a_start_during_a_reveal_goes_back_to_black_from_there(self):
        screen = self.loading()
        screen.black()
        screen.reveal()
        partway = self.at(screen, loading.REVEAL_MS / 2)
        screen.start()
        self.assertAlmostEqual(screen.frame().black, partway.black)
        self.assertEqual(self.at(screen, loading.FADE_MS).black, 1.0)


class MotionTest(LoadingTestCase):
    def test_reduced_motion_keeps_short_fades_and_a_still_picture(self):
        screen = self.loading(motion.REDUCED)
        screen.start()
        self.assertEqual(self.at(screen, 200).black, 1.0)
        self.assertEqual(screen.frame().swell, 1.0)
        self.assertTrue(screen.frame().moving)  # the ring still says it is working

    def test_motion_off_jumps_and_steps_the_ring(self):
        screen = self.loading(motion.OFF)
        screen.start()
        frame = screen.frame()
        self.assertEqual((frame.black, frame.content), (1.0, 1.0))
        steps = {self.at(screen, 1000 / loading.OFF_STEPS / 3).dots for _ in range(3 * loading.OFF_STEPS - 1)}
        self.assertEqual(len(steps), loading.OFF_STEPS)  # a few positions a second, never between
        screen.reveal()
        self.assertFalse(screen.frame().drawn)


if __name__ == "__main__":
    unittest.main()
