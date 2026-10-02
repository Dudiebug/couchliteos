"""The optional cache that lets builds share the bootstrap root and package downloads."""

import pathlib
import re
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]

LB_CACHE = ROOT / "build/lb-cache.sh"

# A stand-in for the mirror: the Date line changes on every call (as it does on every
# archive push), the file hashes change only with FAKE_RELEASE.
FAKE_MIRROR = r'''
lb_cache_fetch() {
  printf 'Origin: Debian\nDate: %s\nSHA256:\n %s 1234 main/binary-amd64/Packages.xz\n' \
    "$(date +%N)" "$(printf '%s' "${FAKE_RELEASE:-one}" | sha256sum | cut -d' ' -f1)"
}
'''

# What `lb build` leaves in cache/bootstrap after the bootstrap stage.
MAKE_ROOT = r'''
make_root() {
  mkdir -p cache/bootstrap/etc
  printf 'BUILD_ID=x\n' > cache/bootstrap/etc/os-release
  chmod 640 cache/bootstrap/etc/os-release
  ln -s usr/bin cache/bootstrap/bin
}
'''


def lb_cache(script, *, cwd, env):
    """Run shell `script` with build/lb-cache.sh sourced, the mirror replaced by a stub."""
    program = f'set -Eeuo pipefail\nsource "{LB_CACHE}"\n{FAKE_MIRROR}{MAKE_ROOT}{script}\n'
    return subprocess.run(["bash", "-c", program], cwd=cwd, env=env, capture_output=True, text=True)


class SharedBuildCacheTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = pathlib.Path(self.directory.name)
        self.work, self.shared = base / "work", base / "shared"
        (self.work / "config").mkdir(parents=True)
        (self.work / "config/common").write_text('LB_SYSTEM="live"\nDEBOOTSTRAP_OPTIONS="--include=ca-certificates"\n')
        self.write_bootstrap_config("http://deb.debian.org/debian/")
        self.env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "SOURCE_DATE_EPOCH": "1000", "HOME": self.directory.name}

    def write_bootstrap_config(self, mirror):
        (self.work / "config/bootstrap").write_text(
            f'LB_PARENT_MIRROR_BOOTSTRAP="{mirror}"\nLB_PARENT_DISTRIBUTION_CHROOT="trixie"\n')

    def run_script(self, script, **extra_env):
        return lb_cache(script, cwd=self.work, env=dict(self.env, **extra_env))

    def key(self, **extra_env):
        result = self.run_script("lb_cache_key", **extra_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_key_is_stable_and_follows_every_input(self):
        first = self.key()
        self.assertRegex(first, r"^[0-9a-f]{32}$")
        self.assertEqual(self.key(), first, "the mirror's Date line must not matter")
        # Tarball builds take SOURCE_DATE_EPOCH from the clock; it only sets BUILD_ID,
        # which every restore rewrites, so it must not make every build a miss.
        self.assertEqual(self.key(SOURCE_DATE_EPOCH="2000"), first)
        self.assertNotEqual(self.key(FAKE_RELEASE="two"), first)
        self.write_bootstrap_config("http://other.example/debian/")
        self.assertNotEqual(self.key(), first)

    def test_key_follows_config_common(self):
        first = self.key()
        (self.work / "config/common").write_text('LB_SYSTEM="live"\nDEBOOTSTRAP_OPTIONS="--include=nano"\n')
        self.assertNotEqual(self.key(), first)

    def test_key_ignores_how_packages_are_fetched(self):
        first = self.key()
        with (self.work / "config/common").open("a") as common:
            common.write('LB_CACHE_PACKAGES="false"\nLB_APT_HTTP_PROXY="http://127.0.0.1:3142"\n')
        self.assertEqual(self.key(), first)

    def test_no_key_when_the_package_indices_cannot_be_read(self):
        for broken in ("lb_cache_fetch() { return 22; }", "lb_cache_fetch() { :; }",
                       "lb_cache_fetch() { echo 'Date: now'; }"):
            result = self.run_script(f"{broken}\nlb_cache_key")
            self.assertNotEqual(result.returncode, 0, broken)
            self.assertEqual(result.stdout, "", broken)

    def test_prepare_links_the_package_caches_and_reports_a_miss(self):
        result = self.run_script(f'lb_cache_prepare "{self.shared}"; printf "%s" "$LB_CACHE_KEY"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"^[0-9a-f]{32}$")
        for name in ("packages.bootstrap", "packages.chroot", "packages.binary"):
            link = self.work / "cache" / name
            self.assertTrue(link.is_symlink(), name)
            self.assertEqual(link.resolve(), (self.shared / name).resolve())
        self.assertFalse((self.work / "cache/bootstrap").exists())

    def test_prepare_keeps_an_existing_package_cache_and_never_deletes_cached_debs(self):
        (self.work / "cache/packages.chroot").mkdir(parents=True)
        (self.work / "cache/packages.chroot/keep.deb").write_text("x")
        # apt stamps a download with the server file's date, so file age says nothing about use.
        old = self.shared / "packages.chroot/old.deb"
        old.parent.mkdir(parents=True)
        old.write_text("x")
        subprocess.run(["touch", "-d", "400 days ago", str(old)], check=True)
        result = self.run_script(f'lb_cache_prepare "{self.shared}"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.work / "cache/packages.chroot/keep.deb").is_file())
        self.assertFalse((self.work / "cache/packages.chroot").is_symlink())
        self.assertTrue(old.exists())

    def test_prepare_leaves_the_package_cache_links_the_lab_driver_made(self):
        shared_chroot = self.shared / "packages.chroot"
        shared_chroot.mkdir(parents=True)
        (self.work / "cache").mkdir()
        (self.work / "cache/packages.chroot").symlink_to(shared_chroot)
        result = self.run_script(f'lb_cache_prepare "{self.shared}"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.work / "cache/packages.chroot").resolve(), shared_chroot.resolve())

    def test_bootstrap_root_is_saved_then_restored_identically(self):
        script = f'lb_cache_prepare "{self.shared}"\nmake_root\nlb_cache_save_bootstrap "{self.shared}"'
        result = self.run_script(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(list(self.shared.glob("bootstrap-*"))), 1)
        self.assertEqual(list(self.shared.glob(".*")), [], "no temporary directory is left behind")
        # A later build with the same inputs starts from the saved root.
        subprocess.run(["rm", "-rf", str(self.work / "cache")], check=True)
        result = self.run_script(f'lb_cache_prepare "{self.shared}"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("restored", result.stderr)
        root = self.work / "cache/bootstrap"
        # BUILD_ID is this build's (SOURCE_DATE_EPOCH=1000), not the saved root's.
        self.assertEqual((root / "etc/os-release").read_text(), "BUILD_ID=19700101T001640Z\n")
        self.assertEqual((root / "etc/os-release").stat().st_mode & 0o777, 0o640)
        self.assertTrue((root / "bin").is_symlink())
        self.assertFalse((self.work / "cache/bootstrap").is_symlink(), "live-build must get its own copy")

    def test_changed_inputs_do_not_restore_and_a_stale_local_root_is_dropped(self):
        self.run_script(f'lb_cache_prepare "{self.shared}"\nmake_root\nlb_cache_save_bootstrap "{self.shared}"')
        result = self.run_script(f'mkdir -p cache/bootstrap\nlb_cache_prepare "{self.shared}"', FAKE_RELEASE="two")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.work / "cache/bootstrap").exists())
        self.assertIn("bootstrapping normally", result.stderr)

    def test_a_new_source_date_still_restores_with_its_own_build_id(self):
        self.run_script(f'lb_cache_prepare "{self.shared}"\nmake_root\nlb_cache_save_bootstrap "{self.shared}"')
        subprocess.run(["rm", "-rf", str(self.work / "cache")], check=True)
        result = self.run_script(f'lb_cache_prepare "{self.shared}"', SOURCE_DATE_EPOCH="1790000000")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("restored", result.stderr)
        self.assertEqual((self.work / "cache/bootstrap/etc/os-release").read_text(), "BUILD_ID=20260921T141320Z\n")

    def test_unreadable_indices_leave_the_local_root_alone_and_save_nothing(self):
        script = (f'lb_cache_fetch() {{ return 22; }}\nmake_root\nlb_cache_prepare "{self.shared}"\n'
                  f'lb_cache_save_bootstrap "{self.shared}"')
        result = self.run_script(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.work / "cache/packages.chroot").is_symlink())
        self.assertTrue((self.work / "cache/bootstrap/etc/os-release").is_file())
        self.assertEqual(list(self.shared.glob("bootstrap-*")), [])

    def test_a_failed_save_never_fails_the_build(self):
        script = (f'lb_cache_prepare "{self.shared}"\nmake_root\ncp() {{ return 1; }}\n'
                  f'lb_cache_save_bootstrap "{self.shared}"')
        result = self.run_script(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("saving the bootstrap root failed", result.stderr)
        self.assertEqual(list(self.shared.glob("bootstrap-*")), [])
        self.assertEqual(list(self.shared.glob(".*")), [])

    def test_only_the_two_newest_bootstrap_roots_are_kept(self):
        for index, name in enumerate(("bootstrap-a", "bootstrap-b", "bootstrap-c")):
            (self.shared / name).mkdir(parents=True)
            subprocess.run(["touch", "-d", f"2026-01-0{index + 1}", str(self.shared / name)], check=True)
        (self.shared / "bootstrap").mkdir()  # not one of ours: must survive
        result = self.run_script(f'lb_cache_prepare "{self.shared}"\nmake_root\nlb_cache_save_bootstrap "{self.shared}"')
        self.assertEqual(result.returncode, 0, result.stderr)
        kept = sorted(path.name for path in self.shared.glob("bootstrap-*"))
        self.assertEqual(len(kept), 2, kept)
        self.assertIn("bootstrap-c", kept)
        self.assertNotIn("bootstrap-a", kept)
        self.assertNotIn("bootstrap-b", kept)
        self.assertTrue((self.shared / "bootstrap").is_dir())

    def test_relative_cache_directory_is_refused(self):
        result = self.run_script("lb_cache_prepare relative/dir")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("absolute", result.stderr)

    def test_build_script_uses_the_cache_only_when_asked(self):
        build = (ROOT / "build/build.sh").read_text()
        self.assertRegex(build, r'(?m)^source "\$ROOT/build/lb-cache\.sh"$')
        self.assertRegex(build, r'(?m)^if \[\[ -n \$\{COUCHLITEOS_LB_CACHE:-\} \]\]; then\n  lb_cache_prepare "\$COUCHLITEOS_LB_CACHE"\nfi$')
        self.assertRegex(build, r'(?m)^if \[\[ -n \$\{COUCHLITEOS_LB_CACHE:-\} \]\]; then\n'
                                r'  timed chroot lb_cache_chroot_stage "\$COUCHLITEOS_LB_CACHE" \$\(\(release \? 0 : 1\)\)\n'
                                r'else\n  timed chroot lb chroot\nfi$')
        self.assertRegex(build, r'(?m)^if \[\[ -n \$\{COUCHLITEOS_LB_CACHE:-\} \]\]; then\n  lb_cache_save_bootstrap "\$COUCHLITEOS_LB_CACHE"\nfi$')
        self.assertEqual(len(re.findall(r'(?m)^[^#\n]*COUCHLITEOS_LB_CACHE', build)), 6)
        self.assertNotRegex(build, r'(?m)^\s*lb build\b', "the stages run one by one")
        # Order: lb config, prepare, the five stages `lb build` runs, save.
        markers = ("lb config noauto", "lb_cache_prepare", "timed bootstrap lb bootstrap", "timed chroot ",
                   "timed installer lb installer", "timed binary lb_binary", "timed source lb source",
                   "lb_cache_save_bootstrap")
        order = [build.index(marker) for marker in markers]
        self.assertEqual(order, sorted(order))
        subprocess.run(["bash", "-n", str(ROOT / "build/build.sh")], check=True)
        subprocess.run(["bash", "-n", str(LB_CACHE)], check=True)


if __name__ == "__main__":
    unittest.main()
