"""Cage rebuilt with wlr-foreign-toplevel-management, the docked keyboard and the mouse speed, and the
controller-mouse module, reach the image."""

import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
HOOK = ROOT / "config/live-build/hooks/live/0050-cage.hook.chroot"
PATCH = ROOT / "config/cage/cage-0.2.0-foreign-toplevel.patch"
OSK_PATCH = ROOT / "config/cage/cage-0.2.0-osk-panel.patch"
SPEED_PATCH = ROOT / "config/cage/cage-0.2.0-pointer-speed.patch"
PATCHES = (PATCH, OSK_PATCH, SPEED_PATCH)  # applied in this order
CONFIGURE = ROOT / "build/configure.sh"
SOURCES = ROOT / "build/sources.lock"
TARBALL = ROOT / "build/downloads/cage-0.2.0.tar.gz"


def text(path):
    return path.read_text(encoding="utf-8")


def hook_value(name):
    match = re.search(rf"^{name}=(.*)$", text(HOOK), re.MULTILINE)
    return match.group(1) if match else None


class CageHookTest(unittest.TestCase):
    def test_hook_is_executable(self):
        # live-build skips hooks that are not executable; git records the mode the build sees.
        try:
            listing = subprocess.run(
                ["git", "-C", str(ROOT), "ls-files", "-s", "--", str(HOOK.relative_to(ROOT))],
                capture_output=True, text=True, check=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError):
            listing = ""
        if listing:
            self.assertTrue(listing.startswith("100755 "), listing)
        else:  # a source tree without git metadata
            self.assertTrue(os.access(HOOK, os.X_OK))

    def test_hook_is_valid_strict_bash(self):
        self.assertTrue(text(HOOK).startswith("#!/bin/bash\n"))
        self.assertIn("set -Eeuo pipefail", text(HOOK))
        if shutil.which("bash") is None:
            self.skipTest("bash is not installed")
        result = subprocess.run(["bash", "-n", str(HOOK)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hook_runs_before_the_main_couchliteos_hook(self):
        hooks = sorted(path.name for path in HOOK.parent.glob("*.hook.chroot"))
        self.assertLess(hooks.index(HOOK.name), hooks.index("0100-couchliteos.hook.chroot"))

    def test_the_release_tarball_is_pinned_by_sha256(self):
        self.assertEqual(hook_value("TARBALL"), "$SRC/cage-0.2.0.tar.gz")
        self.assertRegex(hook_value("TARBALL_SHA256") or "", r"^[0-9a-f]{64}$")
        self.assertIn('sha256sum --check --quiet', text(HOOK))
        # The check aborts the hook rather than building unverified sources.
        self.assertRegex(text(HOOK), r"sha256sum --check --quiet - \|\| \{[^}]*exit 1")

    def test_the_downloaded_tarball_matches_the_pin_and_takes_the_patches(self):
        if not TARBALL.exists():
            self.skipTest("run make fetch-apps to download the Cage release")
        self.assertEqual(hashlib.sha256(TARBALL.read_bytes()).hexdigest(), hook_value("TARBALL_SHA256"))
        if shutil.which("patch") is None:
            self.skipTest("patch is not installed")
        with tempfile.TemporaryDirectory() as directory:
            with tarfile.open(TARBALL) as archive:
                archive.extractall(directory, filter="data")
            for patch in PATCHES:  # each applies on top of the ones before it, without fuzz
                result = subprocess.run(
                    ["patch", "-p1", "--forward", "--fuzz=0", "-i", str(patch)],
                    cwd=pathlib.Path(directory) / "cage-0.2.0", capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("offset", result.stdout, patch.name)

    def test_the_hook_applies_every_patch_in_order(self):
        names = re.findall(r'^  "\$SRC/(cage-0\.2\.0-[a-z-]+\.patch)"$', text(HOOK), re.MULTILINE)
        self.assertEqual(names, [patch.name for patch in PATCHES])
        self.assertIn('for source in "$TARBALL" "${PATCHES[@]}"; do', text(HOOK))
        self.assertEqual(sorted(path.name for path in PATCH.parent.glob("*.patch")), sorted(names))

    def test_only_debians_cage_0_2_0_is_replaced(self):
        self.assertIn("dpkg-query -W -f '${Version}' cage) == 0.2.0-*", text(HOOK))

    def test_the_build_is_checked_for_the_foreign_toplevel_manager(self):
        self.assertIn('for patch in "${PATCHES[@]}"; do\n    patch -p1 --forward < "$patch"\n  done', text(HOOK))
        self.assertIn("nm -D --undefined-only", text(HOOK))
        self.assertIn("grep -q wlr_foreign_toplevel_manager_v1_create", text(HOOK))
        # Cage 0.2.0 has no xwayland option; the hook checks meson's summary instead.
        self.assertNotIn("-Dxwayland", text(HOOK))
        self.assertIn("xwayland *: *true", text(HOOK))
        self.assertIn('> "$work/symbols"', text(HOOK), "nm writes a file: grep -q must not SIGPIPE it")
        self.assertIn('grep -q libinput_device_config_accel_set_speed "$work/symbols"', text(HOOK))
        self.assertIn('grep -aq couchliteos-osk "$work/cage-0.2.0/build/cage"', text(HOOK))
        # The launcher's SIGHUP guard looks for this path in /proc/<pid>/exe.
        self.assertIn('grep -aq /var/lib/couchliteos/mouse-speed "$work/cage-0.2.0/build/cage"', text(HOOK))

    def test_debians_binary_is_diverted_once_not_overwritten(self):
        hook = text(HOOK)
        self.assertIn("dpkg-divert --truename /usr/bin/cage", hook)
        self.assertIn("dpkg-divert --local --rename --divert /usr/bin/cage.debian --add /usr/bin/cage", hook)
        divert = hook.index("--add /usr/bin/cage")
        install = hook.index('install -m 0755 "$work/cage-0.2.0/build/cage" /usr/bin/cage')
        self.assertLess(divert, install, "a later cage upgrade must not overwrite the rebuilt binary")

    def test_build_dependencies_are_purged_and_sources_removed(self):
        hook = text(HOOK)
        deps = re.search(r"BUILD_DEPS=\(([^)]*)\)", hook)
        self.assertIsNotNone(deps)
        for package in ("meson", "ninja-build", "gcc", "libwlroots-0.18-dev", "wayland-protocols", "libinput-dev"):
            self.assertIn(package, deps.group(1).split())
        # Only what the build pulled in is removed: the before/after package lists.
        before = hook.index('sort > "$before"')
        installed = hook.index('apt-get install --yes --no-install-recommends "${BUILD_DEPS[@]}"')
        after = hook.index('sort > "$after"')
        self.assertLess(before, installed)
        self.assertLess(installed, after)
        self.assertIn('added=$(comm -13 "$before" "$after")', hook)
        self.assertIn("apt-get purge --yes $added", hook)
        self.assertIn('rm -rf -- "$SRC"', hook)
        self.assertIn("trap 'rm -rf -- \"$before\" \"$after\" \"$work\"' EXIT", hook)
        self.assertIn("/usr/bin/cage -v", hook.strip().splitlines()[-1])


class CagePatchTest(unittest.TestCase):
    def files(self):
        return re.findall(r"^\+\+\+ b/(\S+)", text(PATCH), re.MULTILINE)

    def test_patch_touches_the_cage_sources_with_p1_paths(self):
        self.assertEqual(self.files(), ["cage.c", "server.h", "view.c", "view.h", "xdg_shell.c", "xwayland.c"])
        self.assertEqual(len(re.findall(r"^--- a/", text(PATCH), re.MULTILINE)), len(self.files()))

    def test_patch_creates_the_manager_and_one_handle_per_view(self):
        patch = text(PATCH)
        self.assertIn("+#include <wlr/types/wlr_foreign_toplevel_management_v1.h>", patch)
        self.assertIn("+\tserver.foreign_toplevel_manager = wlr_foreign_toplevel_manager_v1_create(", patch)
        self.assertIn("+\tstruct wlr_foreign_toplevel_manager_v1 *foreign_toplevel_manager;", patch)
        self.assertIn("wlr_foreign_toplevel_handle_v1_create(view->server->foreign_toplevel_manager)", patch)
        self.assertIn("wlr_foreign_toplevel_handle_v1_destroy(view->foreign_toplevel_handle)", patch)

    def test_activate_raises_and_focuses_and_close_reaches_both_shells(self):
        patch = text(PATCH)
        self.assertIn("handle_surface_request_activate", patch)
        # Raised views move to the front so closing the keyboard refocuses them.
        self.assertIn("+\twl_list_insert(&view->server->views, &view->link);", patch)
        self.assertIn("+\tseat_set_focus(view->server->seat, view);", patch)
        self.assertIn("wlr_xdg_toplevel_send_close", patch)
        self.assertIn("wlr_xwayland_surface_close", patch)
        self.assertEqual(patch.count("+\t.close = close,"), 2)
        # wlrctl matches on title and app_id: both shells report them.
        self.assertEqual(patch.count("wlr_foreign_toplevel_handle_v1_set_title("), 2)
        self.assertEqual(patch.count("wlr_foreign_toplevel_handle_v1_set_app_id("), 2)

    def test_handle_calls_are_guarded_for_unmapped_views(self):
        patch = text(PATCH)
        self.assertIn("+\tif (view->foreign_toplevel_handle)\n+\t\twlr_foreign_toplevel_handle_v1_set_activated", patch)
        self.assertIn("+\tif (xdg_shell_view->xdg_toplevel->title)", patch)
        self.assertIn("+\tif (xwayland_view->xwayland_surface->class)", patch)

    def test_listeners_added_on_map_are_removed_on_unmap(self):
        patch = text(PATCH)
        for listener in ("request_activate", "request_close"):
            self.assertIn(f"wl_signal_add(&view->foreign_toplevel_handle->events.{listener}, &view->{listener});",
                          patch)
            self.assertIn(f"+\t\twl_list_remove(&view->{listener}.link);", patch)

    def test_hunk_line_counts_match_their_headers(self):
        for patch, least in ((PATCH, 11), (OSK_PATCH, 5), (SPEED_PATCH, 6)):
            with self.subTest(patch=patch.name):
                self.assertGreaterEqual(self.count_hunks(patch), least)

    def count_hunks(self, patch):
        lines = text(patch).splitlines()
        index = 0
        hunks = 0
        while index < len(lines):
            header = re.match(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@", lines[index])
            index += 1
            if not header:
                continue
            hunks += 1
            old, new = int(header.group(1) or 1), int(header.group(2) or 1)
            while (old or new) and index < len(lines):
                line = lines[index]
                if line.startswith("-"):
                    old -= 1
                elif line.startswith("+"):
                    new -= 1
                elif line.startswith(" ") or line == "":
                    old -= 1
                    new -= 1
                else:
                    break
                index += 1
            self.assertEqual((old, new), (0, 0), f"{patch.name}: hunk {hunks} ending near line {index}")
        return hunks


class OskPanelPatchTest(unittest.TestCase):
    def test_patch_touches_only_the_view_code(self):
        self.assertEqual(re.findall(r"^\+\+\+ b/(\S+)", text(OSK_PATCH), re.MULTILINE), ["view.c", "view.h", "xdg_shell.c"])

    def test_the_keyboard_app_id_is_docked_at_the_bottom_forty_percent(self):
        patch = text(OSK_PATCH)
        self.assertIn('+#define CAGE_OSK_APP_ID "couchliteos-osk"', patch)
        self.assertIn("+#define CAGE_OSK_PANEL_SHARE 0.40", patch)
        self.assertIn("+#define CAGE_OSK_PANEL_MIN_HEIGHT 240", patch)
        self.assertIn("+\treturn app_id && strcmp(app_id, CAGE_OSK_APP_ID) == 0;", patch)
        self.assertIn("+\tint height = (int) (layout_box->height * CAGE_OSK_PANEL_SHARE);", patch)
        self.assertIn("+\tview->ly = layout_box->y + layout_box->height - height;", patch)
        self.assertIn("+\tview->impl->maximize(view, layout_box->width, height);", patch)
        self.assertIn("+\tif (view_is_osk(view)) {\n+\t\tview_dock_osk(view, &layout_box);", patch)

    def test_the_keyboard_stays_on_top_with_the_focus_and_ignores_fullscreen(self):
        patch = text(OSK_PATCH)
        self.assertIn("+\t\t\twlr_scene_node_raise_to_top(&view->scene_tree->node);", patch)
        # Mapping or raising another view keeps the keyboard above it and focused.
        self.assertEqual(patch.count("+\tstruct cg_view *osk = view_raise_osk(view->server);"), 2)
        self.assertEqual(patch.count("+\tif (osk && osk != view)\n+\t\treturn;\n \tseat_set_focus(view->server->seat, view);"), 2)
        self.assertIn("+\tif (view_is_osk(&xdg_shell_view->view)) {", patch)

    def test_foot_sizes_its_font_for_the_same_panel(self):
        foot = text(ROOT / "launcher/couchliteos_foot.py")
        self.assertIn('OSK_APP_ID = "couchliteos-osk"', foot)
        self.assertIn("OSK_SHARE = 0.40", foot)
        self.assertIn("OSK_MIN_HEIGHT = 240", foot)
        self.assertIn("--app-id=couchliteos-osk", text(ROOT / "scripts/couchliteos-osk-session"))


class PointerSpeedPatchTest(unittest.TestCase):
    def test_patch_touches_the_seat_signals_and_build(self):
        self.assertEqual(
            re.findall(r"^\+\+\+ b/(\S+)", text(SPEED_PATCH), re.MULTILINE), ["cage.c", "meson.build", "seat.c", "seat.h"]
        )
        self.assertIn("+libinput       = dependency('libinput')", text(SPEED_PATCH))
        self.assertIn("+    libinput,", text(SPEED_PATCH))

    def test_the_speed_file_is_read_clamped_and_applied_through_libinput(self):
        patch = text(SPEED_PATCH)
        self.assertIn('+#define CAGE_POINTER_SPEED_PATH "/var/lib/couchliteos/mouse-speed"', patch)
        self.assertIn("+\t\t\t*speed = value < -1.0 ? -1.0 : value > 1.0 ? 1.0 : value;", patch)
        self.assertIn("isfinite(value)", patch)
        self.assertIn("+\tif (!wlr_input_device_is_libinput(device))", patch)
        self.assertIn("+\tif (!libinput_device_config_accel_is_available(handle))", patch)
        self.assertIn("+\t\tspeed = libinput_device_config_accel_get_default_speed(handle);", patch)
        self.assertIn("libinput_device_config_accel_set_speed(handle, speed)", patch)

    def test_new_pointers_and_sighup_apply_it(self):
        patch = text(SPEED_PATCH)
        self.assertIn("+\tpointer_apply_speed(wlr_pointer, have_speed, speed);\n }", patch)
        self.assertIn("+\twl_list_for_each (pointer, &seat->pointers, link) {", patch)
        self.assertIn(
            "+\tstruct wl_event_source *sighup_source = wl_event_loop_add_signal(event_loop, SIGHUP, handle_signal, &server);",
            patch,
        )
        self.assertIn("+\tcase SIGHUP:", patch)
        self.assertIn("+\t\tif (server->seat)\n+\t\t\tseat_apply_pointer_speed(server->seat);", patch)
        self.assertIn("+\twl_event_source_remove(sighup_source);", patch)


class ImageContentsTest(unittest.TestCase):
    def test_configure_copies_the_cage_sources_where_the_hook_reads_them(self):
        configure = text(CONFIGURE)
        self.assertEqual(hook_value("SRC"), "/usr/src/couchliteos-cage")
        self.assertIn('install -D -m 0644 "$ROOT/build/downloads/cage-0.2.0.tar.gz" '
                      '"$CHROOT/usr/src/couchliteos-cage/cage-0.2.0.tar.gz"', configure)
        for patch in PATCHES:
            name = re.escape(patch.name)
            self.assertRegex(configure, rf'install -D -m 0644 "\$ROOT/config/cage/{name}" '
                                        rf'\\\n\s+"\$CHROOT/usr/src/couchliteos-cage/{name}"')

    def test_configure_installs_the_pointer_module_next_to_the_launcher(self):
        configure = text(CONFIGURE)
        self.assertIn('install -D -m 0644 "$ROOT/launcher/couchliteos_pointer.py" '
                      '"$CHROOT/usr/libexec/couchliteos_pointer.py"', configure)
        self.assertIn("/usr/libexec/couchliteos-launcher", configure)

    def test_sources_lock_fetches_the_cage_release(self):
        rows = [line.split("|") for line in text(SOURCES).splitlines() if line and not line.startswith("#")]
        cage = [row for row in rows if row[0] == "cage"]
        self.assertEqual(cage, [[
            "cage", "0.2.0", "https://github.com/cage-kiosk/cage/releases/download/v0.2.0/cage-0.2.0.tar.gz",
            "cage-0.2.0.tar.gz",
        ]])
        self.assertTrue(all(len(row) == 4 for row in rows))

    def test_cage_and_wlrctl_are_installed_from_debian(self):
        packages = set()
        for path in (ROOT / "config/live-build/package-lists").glob("*.list.chroot"):
            packages.update(line.strip() for line in text(path).splitlines() if line.strip() and not line.startswith("#"))
        self.assertIn("cage", packages)
        self.assertIn("wlrctl", packages)


if __name__ == "__main__":
    unittest.main()
