import testenv  # noqa: F401  (first: scratch run and state directories)
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
from xml.etree import ElementTree

import couchliteos_apps as apps
import couchliteos_icons as icons
import couchliteos_xmb as xmb


def app(app_id, kind="request", **fields):
    return apps.Application(id=app_id, name=app_id.upper(), kind=kind, **fields)


def write(path, text="<svg/>"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def desktop(share, name, icon, exec_line="/usr/bin/true"):
    return write(share / "applications" / f"{name}.desktop",
                 f"[Desktop Entry]\nType=Application\nName={name}\nExec={exec_line} %u\nIcon={icon}\n")


class IconsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.usr = root / "usr/share"
        self.flatpak = root / "flatpak/exports/share"
        self.shares = (self.usr, self.flatpak)
        self.appimages = root / "opt/apps"
        self.appimages.mkdir(parents=True)
        self.bundle = root / "bundle"
        write(self.bundle / f"{icons.FALLBACK}.svg")
        write(self.bundle / "gear.svg")

    def official(self, application):
        return icons.official(application, self.shares, self.appimages)


class OfficialTest(IconsTestCase):
    def test_a_package_icon_largest_and_svg_first(self):
        desktop(self.usr, "firefox-esr", "firefox-esr", "/usr/lib/firefox-esr/firefox-esr")
        write(self.usr / "icons/hicolor/48x48/apps/firefox-esr.png")
        big = write(self.usr / "icons/hicolor/128x128/apps/firefox-esr.png")
        self.assertEqual(self.official(app("firefox", binary="/usr/bin/firefox-esr")), big)
        svg = write(self.usr / "icons/hicolor/scalable/apps/firefox-esr.svg")
        self.assertEqual(self.official(app("firefox", binary="/usr/bin/firefox-esr")), svg)

    def test_found_by_the_program_it_runs(self):
        desktop(self.usr, "com.google.Chrome", "google-chrome", "/usr/bin/google-chrome-stable")
        icon = write(self.usr / "icons/hicolor/256x256/apps/google-chrome.png")
        chrome = app("chrome", kind="command", command="/usr/bin/google-chrome-stable", arguments="--no-first-run")
        self.assertEqual(self.official(chrome), icon)

    def test_a_flatpak_export(self):
        desktop(self.flatpak, "com.valvesoftware.Steam", "com.valvesoftware.Steam")
        icon = write(self.flatpak / "icons/hicolor/scalable/apps/com.valvesoftware.Steam.svg")
        self.assertEqual(self.official(app("steam", flatpak="com.valvesoftware.Steam")), icon)

    def test_an_unpacked_appimage(self):
        folder = self.appimages / "moonlight"
        write(folder / "com.moonlight_stream.Moonlight.desktop",
              "[Desktop Entry]\nName=Moonlight\nExec=moonlight\nIcon=moonlight\n")
        beside = write(folder / "moonlight.svg")
        self.assertEqual(self.official(app("moonlight", request="start-moonlight")), beside)
        desktop(folder / "usr/share", "com.moonlight_stream.Moonlight", "moonlight")
        themed = write(folder / "usr/share/icons/hicolor/scalable/apps/moonlight.svg")
        self.assertEqual(self.official(app("moonlight", request="start-moonlight")), themed)

    def test_a_web_shortcut_does_not_take_its_browsers_icon(self):
        desktop(self.usr, "firefox-esr", "firefox-esr", "/usr/bin/firefox-esr")
        write(self.usr / "icons/hicolor/scalable/apps/firefox-esr.svg")
        netflix = app("netflix", kind="command", command="/usr/bin/firefox-esr",
                      arguments="--kiosk https://www.netflix.com/browse")
        self.assertIsNone(self.official(netflix))

    def test_a_shell_has_no_official_icon(self):
        desktop(self.usr, "bash-thing", "utilities-terminal", "/bin/bash")
        write(self.usr / "icons/hicolor/scalable/apps/utilities-terminal.svg")
        self.assertIsNone(self.official(app("terminal", kind="command", command="/bin/bash")))

    def test_nothing_installed(self):
        self.assertIsNone(self.official(app("tailscale", request="start-tailscale")))


class SafetyTest(IconsTestCase):
    def test_only_svg_and_png_inside_the_icon_folders(self):
        outside = write(pathlib.Path(self.tmp.name) / "elsewhere/evil.svg")
        desktop(self.usr, "a", str(outside))
        self.assertIsNone(self.official(app("a")))
        bmp = write(self.usr / "icons/hicolor/48x48/apps/b.bmp")
        desktop(self.usr, "b", str(bmp))
        self.assertIsNone(self.official(app("b")))
        absolute = write(self.usr / "pixmaps/c.png")
        desktop(self.usr, "c", str(absolute))
        self.assertEqual(self.official(app("c")), absolute)

    def test_odd_icon_names_are_refused(self):
        for name in ("../../etc/passwd", "a b", "", "-x"):
            self.assertIsNone(icons.resolve(name, self.shares), name)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks")
    def test_a_link_out_of_the_icon_folders_is_refused(self):
        target = write(pathlib.Path(self.tmp.name) / "secret/key.png")
        link = self.usr / "icons/hicolor/scalable/apps/d.png"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("no symlinks here")
        self.assertIsNone(icons.resolve("d", self.shares))

    def test_huge_files_are_refused(self):
        write(self.usr / "icons/hicolor/scalable/apps/big.svg", "x" * (icons.MAX_BYTES + 1))
        self.assertIsNone(icons.resolve("big", self.shares))
        write(self.usr / "applications/big.desktop", "[Desktop Entry]\n" + "#" * icons.MAX_DESKTOP_BYTES)
        self.assertEqual(icons.desktop_entry(self.usr / "applications/big.desktop"), {})

    def test_a_broken_desktop_file_is_ignored(self):
        path = write(self.usr / "applications/broken.desktop", "Icon=x\n[[[")
        self.assertEqual(icons.desktop_entry(path), {})

    def test_the_exec_line_skips_env(self):
        self.assertEqual(icons._exec_program({"Exec": "env FOO=1 /usr/bin/thing --x"}), "thing")
        self.assertEqual(icons._exec_program({}), "")


class ItemTest(IconsTestCase):
    def icons(self):
        return icons.Icons(self.shares, self.bundle, self.appimages)

    def test_bundled_by_name_else_the_fallback(self):
        self.assertEqual(icons.bundled("gear", self.bundle), self.bundle / "gear.svg")
        self.assertEqual(icons.bundled("missing", self.bundle), self.bundle / f"{icons.FALLBACK}.svg")
        self.assertEqual(icons.bundled("../gear", self.bundle), self.bundle / f"{icons.FALLBACK}.svg")

    def test_official_first_then_bundled(self):
        desktop(self.usr, "firefox-esr", "firefox-esr", "/usr/bin/firefox-esr")
        icon = write(self.usr / "icons/hicolor/scalable/apps/firefox-esr.svg")
        lookup = self.icons()
        self.assertEqual(lookup.for_item("globe", app("firefox", binary="/usr/bin/firefox-esr")), (icon, True))
        self.assertEqual(lookup.for_item("gear"), (self.bundle / "gear.svg", False))
        self.assertEqual(lookup.for_item("gear", app("tailscale")), (self.bundle / "gear.svg", False))

    def test_a_manifest_icon_wins(self):
        desktop(self.usr, "firefox-esr", "firefox-esr", "/usr/bin/firefox-esr")
        write(self.usr / "icons/hicolor/scalable/apps/firefox-esr.svg")
        chosen = app("firefox", binary="/usr/bin/firefox-esr", icon="gear")
        self.assertEqual(self.icons().for_item("gear", chosen), (self.bundle / "gear.svg", False))

    def test_looked_up_once_until_forgotten(self):
        lookup = self.icons()
        firefox = app("firefox", binary="/usr/bin/firefox-esr")
        self.assertFalse(lookup.for_item("globe", firefox)[1])
        desktop(self.usr, "firefox-esr", "firefox-esr", "/usr/bin/firefox-esr")
        write(self.usr / "icons/hicolor/scalable/apps/firefox-esr.svg")
        self.assertFalse(lookup.for_item("globe", firefox)[1])
        lookup.forget()
        self.assertTrue(lookup.for_item("globe", firefox)[1])


# Shapes plus the silver, gloss and shadow of tools/make-icons.py; no text, images or scripts.
SVG_TAGS = {"svg", "g", "path", "circle", "ellipse", "rect", "defs", "linearGradient", "stop", "clipPath",
            "filter", "feGaussianBlur"}


class BundleTest(unittest.TestCase):
    """The icons this project draws itself (overlay/usr/share/couchliteos/icons)."""

    def used_names(self):
        source = (pathlib.Path(__file__).parent / "couchliteos_xmb.py").read_text()
        names = set(re.findall(r'icon="([a-z0-9-]+)"', source))
        names |= set(re.findall(r'return "([a-z0-9-]+)"', source.split("def icon_of", 1)[1].split("\ndef ", 1)[0]))
        for table in (xmb.ICONS, xmb.APP_ICONS, xmb.ENTRY_ICONS, xmb.POWER_ICONS):
            names |= set(table.values())
        names |= set(xmb.WEB_SERVICES.values())
        return names | {icons.FALLBACK}

    def test_drawn_by_the_generator(self):
        tool = pathlib.Path(__file__).resolve().parent.parent / "tools" / "make-icons.py"
        result = subprocess.run([sys.executable, str(tool), "--check"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_every_icon_the_xmb_uses_is_there(self):
        for name in sorted(self.used_names()):
            self.assertTrue((icons.REPO_ICONS / f"{name}.svg").is_file(), name)
            self.assertEqual(icons.bundled(name, icons.REPO_ICONS).name, f"{name}.svg")

    def test_only_small_plain_svg(self):
        for path in sorted(icons.REPO_ICONS.iterdir()):
            self.assertEqual(path.suffix, ".svg", path.name)
            text = path.read_text()
            self.assertLess(len(text), 8192, path.name)
            root = ElementTree.fromstring(text)
            self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg", path.name)
            self.assertEqual(root.get("viewBox"), "0 0 24 24", path.name)
            for element in root.iter():
                self.assertIn(element.tag.split("}")[1], SVG_TAGS, path.name)
                self.assertFalse([key for key in element.keys() if "href" in key or key.startswith("on")], path.name)
                for value in element.attrib.values():
                    for target in re.findall(r"url\(([^)]*)\)", value):
                        self.assertRegex(target, r"^#[a-z]+$", path.name)
            self.assertNotIn("://", text.replace('xmlns="http://www.w3.org/2000/svg"', ""), path.name)

    def test_nothing_unused(self):
        unused = {path.stem for path in icons.REPO_ICONS.glob("*.svg")} - self.used_names()
        self.assertEqual(unused, set())


if __name__ == "__main__":
    unittest.main()
