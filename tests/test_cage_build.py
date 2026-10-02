"""Cage rebuilt with wlr-foreign-toplevel-management, and the controller-mouse module, reach the image."""

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

    def test_the_downloaded_tarball_matches_the_pin_and_takes_the_patch(self):
        if not TARBALL.exists():
            self.skipTest("run make fetch-apps to download the Cage release")
        self.assertEqual(hashlib.sha256(TARBALL.read_bytes()).hexdigest(), hook_value("TARBALL_SHA256"))
        if shutil.which("patch") is None:
            self.skipTest("patch is not installed")
        with tempfile.TemporaryDirectory() as directory:
            with tarfile.open(TARBALL) as archive:
                archive.extractall(directory, filter="data")
            result = subprocess.run(
                ["patch", "-p1", "--forward", "--dry-run", "-i", str(PATCH)],
                cwd=pathlib.Path(directory) / "cage-0.2.0", capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_only_debians_cage_0_2_0_is_replaced(self):
        self.assertIn("dpkg-query -W -f '${Version}' cage) == 0.2.0-*", text(HOOK))

    def test_the_build_is_checked_for_the_foreign_toplevel_manager(self):
        self.assertIn('patch -p1 --forward < "$PATCH"', text(HOOK))
        self.assertIn("nm -D --undefined-only", text(HOOK))
        self.assertIn("grep -q wlr_foreign_toplevel_manager_v1_create", text(HOOK))
        # Cage 0.2.0 has no xwayland option; the hook checks meson's summary instead.
        self.assertNotIn("-Dxwayland", text(HOOK))
        self.assertIn("xwayland *: *true", text(HOOK))
        self.assertIn('> "$work/symbols"', text(HOOK), "nm writes a file: grep -q must not SIGPIPE it")

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
        for package in ("meson", "ninja-build", "gcc", "libwlroots-0.18-dev", "wayland-protocols"):
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
        lines = text(PATCH).splitlines()
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
            self.assertEqual((old, new), (0, 0), f"hunk {hunks} ending near line {index}")
        self.assertGreater(hunks, 10)


class ImageContentsTest(unittest.TestCase):
    def test_configure_copies_the_cage_sources_where_the_hook_reads_them(self):
        configure = text(CONFIGURE)
        self.assertEqual(hook_value("SRC"), "/usr/src/couchliteos-cage")
        self.assertIn('install -D -m 0644 "$ROOT/build/downloads/cage-0.2.0.tar.gz" '
                      '"$CHROOT/usr/src/couchliteos-cage/cage-0.2.0.tar.gz"', configure)
        self.assertRegex(configure, r'install -D -m 0644 "\$ROOT/config/cage/cage-0\.2\.0-foreign-toplevel\.patch" '
                                    r'\\\n\s+"\$CHROOT/usr/src/couchliteos-cage/cage-0\.2\.0-foreign-toplevel\.patch"')

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
