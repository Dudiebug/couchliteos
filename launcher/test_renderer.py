import testenv  # noqa: F401  (first: scratch run and state directories)
import pathlib
import tempfile
import unittest

import couchliteos_renderer as renderer

DEBIAN_ICDS = frozenset({"intel_icd", "intel_hasvk_icd", "radeon_icd", "nouveau_icd", "lvp_icd",
                         "virtio_icd", "asahi_icd"})  # mesa-vulkan-drivers installs them all


class ChooseTest(unittest.TestCase):
    def test_nouveau_takes_opengl_even_with_the_nvk_file_installed(self):
        # The iMac 2013 (GT 755M): the only Vulkan device is llvmpipe.
        self.assertEqual(renderer.choose(("nouveau",), DEBIAN_ICDS)[0], "ngl")

    def test_i915_and_old_radeon_take_opengl(self):
        for driver in ("i915", "radeon"):
            self.assertEqual(renderer.choose((driver,), DEBIAN_ICDS)[0], "ngl", driver)

    def test_a_card_with_a_vulkan_driver_keeps_gtks_choice(self):
        self.assertIsNone(renderer.choose(("amdgpu",), DEBIAN_ICDS)[0])
        self.assertIsNone(renderer.choose(("xe",), DEBIAN_ICDS)[0])
        self.assertIsNone(renderer.choose(("nvidia",), DEBIAN_ICDS | {"nvidia_icd"})[0])
        self.assertIsNone(renderer.choose(("i915", "nvidia"), frozenset({"nvidia_icd"}))[0])

    def test_amdgpu_without_its_icd_takes_opengl(self):
        self.assertEqual(renderer.choose(("amdgpu",), frozenset({"lvp_icd"}))[0], "ngl")
        self.assertEqual(renderer.choose(("nvidia",), frozenset())[0], "ngl")

    def test_no_card_or_no_hardware_gl_keeps_gtks_choice(self):
        self.assertIsNone(renderer.choose((), DEBIAN_ICDS)[0])
        self.assertIsNone(renderer.choose(("bochs-drm",), DEBIAN_ICDS)[0])
        self.assertIsNone(renderer.choose(("virtio_gpu",), DEBIAN_ICDS)[0])  # QEMU: unchanged


class SysfsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.drm, self.icd, self.run = self.root / "drm", self.root / "icd.d", self.root / "run"
        for directory in (self.drm, self.icd, self.run):
            directory.mkdir()

    def card(self, name, driver):
        device = self.drm / name / "device"
        device.mkdir(parents=True)
        (device / "uevent").write_text(f"DRIVER={driver}\nPCI_CLASS=30000\n")

    def test_drivers_come_from_the_cards_not_their_outputs(self):
        self.card("card0", "nouveau")
        self.card("card0-HDMI-A-1", "ignored")
        self.card("card1", "i915")
        (self.drm / "renderD128").mkdir()
        self.assertEqual(renderer.drm_drivers(self.drm), ("nouveau", "i915"))
        self.assertEqual(renderer.drm_drivers(self.root / "missing"), ())

    def test_icd_names_drop_the_architecture(self):
        (self.icd / "radeon_icd.x86_64.json").write_text("{}")
        (self.icd / "nvidia_icd.json").write_text("{}")
        (self.icd / "README").write_text("")
        self.assertEqual(renderer.icd_names((self.icd, self.root / "missing")),
                         frozenset({"radeon_icd", "nvidia_icd"}))

    def test_prepare_sets_opengl_and_records_why(self):
        self.card("card0", "nouveau")
        (self.icd / "lvp_icd.x86_64.json").write_text("{}")
        environ = {}
        self.assertEqual(renderer.prepare(environ, self.run, self.drm, (self.icd,)), "ngl")
        self.assertEqual(environ, {"GSK_RENDERER": "ngl"})
        lines = (self.run / "renderer").read_text().splitlines()
        self.assertEqual(lines[0], "ngl")
        self.assertIn("nouveau", lines[1])

    def test_prepare_never_changes_a_renderer_set_before(self):
        self.card("card0", "nouveau")
        environ = {"GSK_RENDERER": "cairo"}
        self.assertEqual(renderer.prepare(environ, self.run, self.drm, (self.icd,)), "cairo")
        self.assertEqual(environ, {"GSK_RENDERER": "cairo"})
        self.assertEqual((self.run / "renderer").read_text().splitlines()[0], "cairo")

    def test_prepare_keeps_gtks_choice_with_a_vulkan_gpu(self):
        self.card("card0", "amdgpu")
        (self.icd / "radeon_icd.x86_64.json").write_text("{}")
        environ = {}
        self.assertEqual(renderer.prepare(environ, self.run, self.drm, (self.icd,)), "")
        self.assertEqual(environ, {})
        self.assertEqual((self.run / "renderer").read_text().splitlines()[0], "default")

    def test_an_unwritable_run_dir_is_not_an_error(self):
        environ = {}
        renderer.prepare(environ, self.root / "missing" / "run", self.drm, (self.icd,))
        self.assertEqual(environ, {})


if __name__ == "__main__":
    unittest.main()
