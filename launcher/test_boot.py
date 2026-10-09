import testenv  # noqa: F401  (first: scratch run and state directories)
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

import couchliteos_boot as boot
import couchliteos_motion as motion

try:
    import gi

    gi.require_version("Gtk", "4.0")
    import couchliteos_gtk_boot as gtk_boot
except (ImportError, ValueError):
    gtk_boot = None

ROOT = pathlib.Path(__file__).resolve().parent.parent
THEME = ROOT / "overlay/usr/share/plymouth/themes/couchliteos"
SIZES = {"logo.png": (500, 250), "halo.png": (900, 420), "ribbon-1.png": (1920, 300),
         "ribbon-2.png": (1920, 300), "dot.png": (28, 28)}


def named(sprites, name):
    return [sprite for sprite in sprites if sprite.name == name]


class TimingTest(unittest.TestCase):
    def test_a_ribbon_drifts_one_screen_width_and_wraps(self):
        self.assertEqual(boot.drift(0, 0), 0)
        self.assertAlmostEqual(boot.drift(boot.RIBBON_SECONDS[0] / 2, 0), 0.5)
        self.assertAlmostEqual(boot.drift(boot.RIBBON_SECONDS[0] * 3.25, 0), 0.25)
        self.assertAlmostEqual(boot.drift(boot.RIBBON_SECONDS[1] / 4, 1), 0.25)

    def test_the_halo_breathes_between_low_and_full(self):
        values = [boot.breath(t / 20) for t in range(200)]
        self.assertAlmostEqual(boot.breath(0), boot.HALO_LOW)
        self.assertAlmostEqual(boot.breath(boot.HALO_SECONDS / 2), 1.0)
        self.assertGreaterEqual(min(values), boot.HALO_LOW - 1e-9)
        self.assertLessEqual(max(values), 1.0 + 1e-9)

    def test_the_dots_pulse_left_to_right(self):
        self.assertAlmostEqual(boot.dot(0, 0), 1.0)
        self.assertLess(boot.dot(0, 1), boot.dot(0, 0))
        self.assertAlmostEqual(boot.dot(boot.DOT_STAGGER, 1), 1.0)
        self.assertAlmostEqual(boot.dot(boot.DOT_SECONDS / 2, 0), boot.DOT_LOW)
        for t in range(100):
            self.assertTrue(boot.DOT_LOW - 1e-9 <= boot.dot(t / 37, t % boot.DOTS) <= 1 + 1e-9)

    def test_only_full_motion_moves_and_off_does_not_fade(self):
        self.assertTrue(boot.moving(motion.FULL))
        self.assertFalse(boot.moving(motion.REDUCED))
        self.assertFalse(boot.moving(motion.OFF))
        self.assertAlmostEqual(boot.fade_seconds(motion.FULL), boot.FADE_MS / 1000)
        self.assertLess(boot.fade_seconds(motion.REDUCED), boot.fade_seconds(motion.FULL))
        self.assertEqual(boot.fade_seconds(motion.OFF), 0)

    def test_the_tv_takes_its_time_from_plymouths_start_on_the_boot_clock(self):
        # Plymouth counts frames from about SPLASH_START: the TV's picture is at the same point.
        self.assertAlmostEqual(boot.splash_seconds(boot.SPLASH_START + 7.5), 7.5)
        self.assertEqual(boot.splash_seconds(boot.SPLASH_START - 1), 0.0)  # never before time 0
        with mock.patch.object(boot, "boot_clock", return_value=None):
            self.assertEqual(boot.splash_seconds(), 0.0)  # no clock: from its own first frame, as before
        with mock.patch.object(boot, "boot_clock", return_value=boot.SPLASH_START + 3):
            self.assertAlmostEqual(boot.splash_seconds(), 3.0)

    def test_the_boot_clock_is_clock_boottime_else_proc_uptime(self):
        with mock.patch.object(boot.time, "CLOCK_BOOTTIME", 7, create=True), \
                mock.patch.object(boot.time, "clock_gettime", return_value=12.5, create=True) as clock:
            self.assertEqual(boot.boot_clock(), 12.5)
            clock.assert_called_once_with(7)
        with mock.patch.object(boot.time, "CLOCK_BOOTTIME", 7, create=True), \
                mock.patch.object(boot.time, "clock_gettime", side_effect=OSError, create=True), \
                mock.patch.object(boot.pathlib.Path, "read_text", return_value="34.25 120.00\n"):
            self.assertEqual(boot.boot_clock(), 34.25)
        with mock.patch.object(boot.time, "CLOCK_BOOTTIME", 7, create=True), \
                mock.patch.object(boot.time, "clock_gettime", side_effect=OSError, create=True), \
                mock.patch.object(boot.pathlib.Path, "read_text", side_effect=OSError):
            self.assertIsNone(boot.boot_clock())

    def test_colours(self):
        self.assertEqual(boot.rgb("ff0000"), (1.0, 0.0, 0.0))
        self.assertAlmostEqual(boot.rgb(boot.TOP)[2], 0x51 / 255)


class FrameTest(unittest.TestCase):
    def test_back_to_front_ribbons_then_light_then_logo_then_dots(self):
        names = [sprite.name for sprite in boot.frame(1920, 1080, SIZES, 1.0, True)]
        self.assertEqual(names, ["ribbon-1.png"] * 2 + ["ribbon-2.png"] * 2 + ["halo.png", "logo.png"]
                         + ["dot.png"] * boot.DOTS)

    def test_the_logo_is_centred_at_its_height_and_scales_with_the_screen(self):
        for width, height in ((1920, 1080), (1280, 720), (3840, 2160)):
            (logo,) = named(boot.frame(width, height, SIZES, 0, True), "logo.png")
            scale = height / boot.REFERENCE_HEIGHT
            self.assertAlmostEqual(logo.width, 500 * scale)
            self.assertAlmostEqual(logo.x + logo.width / 2, width / 2)
            self.assertAlmostEqual(logo.y + logo.height / 2, height * boot.LOGO_Y)

    def test_a_ribbon_is_two_screen_wide_copies_side_by_side_covering_the_screen(self):
        for t in (0, 3.3, 11.9, 40):
            first, second = named(boot.frame(1280, 800, SIZES, t, True), "ribbon-1.png")
            self.assertEqual(first.width, 1280)
            self.assertAlmostEqual(second.x, first.x + 1280)
            self.assertLessEqual(first.x, 0)
            self.assertGreaterEqual(second.x + second.width, 1280)
            self.assertAlmostEqual(first.height, 300 * 800 / 1080)

    def test_still_frame_rests_with_full_light_and_no_dots(self):
        sprites = boot.frame(1920, 1080, SIZES, 7.0, False)
        self.assertEqual(sprites, boot.frame(1920, 1080, SIZES, 0.0, False))
        self.assertEqual(named(sprites, "dot.png"), [])
        self.assertEqual(named(sprites, "halo.png")[0].opacity, 1.0)
        self.assertAlmostEqual(named(sprites, "ribbon-2.png")[0].x, -boot.STILL_DRIFT[1] * 1920)

    def test_missing_pictures_are_left_out(self):
        self.assertEqual(boot.frame(1920, 1080, {}, 1.0, True), [])
        only_logo = boot.frame(1920, 1080, {"logo.png": (500, 250)}, 1.0, True)
        self.assertEqual([sprite.name for sprite in only_logo], ["logo.png"])

    def test_the_dots_sit_centred_under_the_logo(self):
        dots = named(boot.frame(1920, 1080, SIZES, 0.4, True), "dot.png")
        middle = sum(d.x + d.width / 2 for d in dots) / len(dots)
        self.assertAlmostEqual(middle, 960)
        self.assertTrue(all(d.y + d.height / 2 == 1080 * boot.DOTS_Y for d in dots))


class ThemeTest(unittest.TestCase):
    """The Plymouth theme tools/make-logo.py writes from this module."""

    def test_every_picture_is_there(self):
        for name in boot.FILES:
            self.assertTrue((THEME / name).is_file(), name)

    def test_written_by_the_generator_from_this_module(self):
        tool = ROOT / "tools" / "make-logo.py"
        result = subprocess.run([sys.executable, str(tool), "--check"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_script_has_no_text_or_password_prompt(self):
        script = (THEME / "couchliteos.script").read_text()
        self.assertNotIn("Image.Text", script)  # needs plymouth-label, which is not installed
        self.assertNotIn("SetDisplayPasswordFunction", script)  # Plymouth's own prompt shows instead
        self.assertIn("Plymouth.SetRefreshFunction(refresh_callback);", script)
        self.assertIn(f"ribbon[1].seconds = {boot.RIBBON_SECONDS[1]:g};", script)

    def test_the_theme_points_at_its_files(self):
        theme = (THEME / "couchliteos.plymouth").read_text()
        self.assertIn("ModuleName=script\n", theme)
        self.assertIn(f"ImageDir={boot.ASSETS.as_posix()}\n", theme)
        self.assertIn(f"ScriptFile={boot.ASSETS.as_posix()}/couchliteos.script\n", theme)


@unittest.skipIf(gtk_boot is None, "needs PyGObject with GTK 4")
class PicturesTest(unittest.TestCase):
    """What the TV's loading screen loads; the widget itself runs in tests/tv-headless.sh."""

    def test_every_picture_loads_at_its_size(self):
        textures = gtk_boot.load_pictures(THEME)
        self.assertEqual(sorted(textures), sorted(boot.FILES))
        self.assertEqual(textures["ribbon-1.png"].get_width(), 1920)

    def test_a_missing_folder_leaves_the_gradient_alone(self):
        self.assertEqual(gtk_boot.load_pictures(ROOT / "no-such-folder"), {})

    def test_the_gradient_runs_from_top_to_bottom(self):
        texture = gtk_boot.gradient_texture(64)
        self.assertEqual((texture.get_width(), texture.get_height()), (2, 64))


if __name__ == "__main__":
    unittest.main()
