"""Faster builds: cached-package checks, the reused chroot, version pins, the test marker
and the stage timings (docs/BUILDING.md)."""

import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
LB_CACHE = ROOT / "build/lb-cache.sh"
PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

FAKE_MIRROR = r'''
lb_cache_fetch() {
  printf 'Date: %s\nSHA256:\n %s 1234 main/binary-amd64/Packages.xz\n' \
    "$(date +%N)" "$(printf '%s' "${FAKE_RELEASE:-one}" | sha256sum | cut -d' ' -f1)"
}
'''


def run_bash(script, *, cwd, env=None):
    program = f'set -Eeuo pipefail\nsource "{LB_CACHE}"\n{FAKE_MIRROR}{script}\n'
    environment = {"PATH": PATH, "HOME": str(cwd), "SOURCE_DATE_EPOCH": "1000"}
    environment.update(env or {})
    return subprocess.run(["bash", "-c", program], cwd=cwd, env=environment, capture_output=True, text=True)


class Temporary(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = pathlib.Path(directory.name)


@unittest.skipUnless(shutil.which("dpkg-deb"), "dpkg-deb is not installed")
class CachedPackageCheckTest(Temporary):
    def make_deb(self, directory, name):
        tree = self.base / f"tree-{name}"
        (tree / "DEBIAN").mkdir(parents=True)
        (tree / "usr/share/doc" / name).mkdir(parents=True)
        (tree / "usr/share/doc" / name / "README").write_bytes(os.urandom(64 * 1024))
        (tree / "DEBIAN/control").write_text(
            f"Package: {name}\nVersion: 1.0\nArchitecture: all\nMaintainer: Test <test@example.invalid>\n"
            f"Description: test package\n")
        directory.mkdir(parents=True, exist_ok=True)
        deb = directory / f"{name}_1.0_all.deb"
        subprocess.run(["dpkg-deb", "--root-owner-group", "-Zxz", "-b", str(tree), str(deb)],
                       check=True, capture_output=True)
        return deb

    def test_damaged_packages_are_deleted_and_reported(self):
        chroot, binary = self.base / "packages.chroot", self.base / "packages.binary"
        good = [self.make_deb(chroot, f"good{index}") for index in range(20)]
        good.append(self.make_deb(binary, "goodbinary"))
        truncated = self.make_deb(chroot, "truncated")
        data = truncated.read_bytes()
        truncated.write_bytes(data[: len(data) - 4096])
        garbage = chroot / "garbage_1.0_all.deb"
        garbage.write_bytes(b"!<arch>\nnot really")
        (chroot / "partial").mkdir()  # apt's download directory: not a package
        result = run_bash(f'lb_cache_verify_debs "{chroot}" "{binary}" "{self.base}/missing"', cwd=self.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("deleted 2 damaged cached package(s) of 23", result.stderr)
        self.assertIn(str(truncated), result.stderr)
        self.assertFalse(truncated.exists())
        self.assertFalse(garbage.exists())
        self.assertTrue(all(deb.exists() for deb in good))

    def test_a_clean_cache_is_reported_and_kept(self):
        chroot = self.base / "packages.chroot"
        deb = self.make_deb(chroot, "fine")
        result = run_bash(f'lb_cache_verify_debs "{chroot}"', cwd=self.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("all 1 cached packages unpack cleanly", result.stderr)
        self.assertTrue(deb.exists())

    def test_no_cache_directory_is_fine(self):
        result = run_bash(f'lb_cache_verify_debs "{self.base}/none"', cwd=self.base)
        self.assertEqual(result.returncode, 0, result.stderr)


class ChrootKeyTest(Temporary):
    def setUp(self):
        super().setUp()
        self.work = self.base / "work"
        config = self.work / "config"
        for name in ("package-lists", "archives", "hooks/live", "includes.chroot/etc",
                     "includes.chroot_before_packages/etc/apt"):
            (config / name).mkdir(parents=True)
        (config / "common").write_text('LB_MODE="debian"\nLB_CACHE_PACKAGES="true"\n')
        (config / "bootstrap").write_text(
            'LB_PARENT_MIRROR_BOOTSTRAP="http://deb.debian.org/debian/"\nLB_PARENT_DISTRIBUTION_CHROOT="trixie"\n')
        (config / "chroot").write_text('LB_CHROOT_FILESYSTEM="squashfs"\nLB_CHROOT_SQUASHFS_COMPRESSION_TYPE="zstd"\n')
        (config / "binary").write_text('LB_BINARY_IMAGES="iso-hybrid"\n')
        (config / "package-lists/couchliteos.list.chroot").write_text("cage\ntailscale=1.102.3\n")
        (config / "archives/tailscale.list.chroot").write_text("deb https://pkgs.tailscale.com/stable/debian trixie main\n")
        (config / "hooks/live/0100-x.hook.chroot").write_text("#!/bin/sh\necho one\n")
        (config / "includes.chroot/etc/couchliteos").write_text("one\n")

    def key(self, **env):
        result = run_bash("lb_cache_chroot_key", cwd=self.work, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"^[0-9a-f]{32}\n$")
        return result.stdout

    def changed(self, path, text):
        first = self.key()
        target = self.work / "config" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return first, self.key()

    def test_key_is_stable(self):
        self.assertEqual(self.key(), self.key())
        self.assertEqual(self.key(SOURCE_DATE_EPOCH="2000"), self.key())

    def test_package_inputs_change_the_key(self):
        for path, text in (("package-lists/couchliteos.list.chroot", "cage\ntailscale=1.102.4\n"),
                           ("package-lists/extra.list.chroot", "nano\n"),
                           ("archives/tailscale.list.chroot", "deb https://example.invalid/ trixie main\n"),
                           ("archives/couchliteos-pins.pref.chroot", "Package: x\n"),
                           ("includes.chroot_before_packages/etc/apt/apt.conf.d/10x", "APT::x 1;\n"),
                           ("preseed/x.cfg.chroot", "d-i x string y\n"),
                           ("packages.chroot/x_1_all.deb", "x"),
                           ("chroot", 'LB_CHROOT_FILESYSTEM="ext4"\n'),
                           ("binary", 'LB_BINARY_IMAGES="hdd"\n'),
                           ("common", 'LB_MODE="ubuntu"\n')):
            with self.subTest(path=path):
                first, second = self.changed(path, text)
                self.assertNotEqual(first, second)

    def test_mode_of_a_package_input_changes_the_key(self):
        first = self.key()
        (self.work / "config/archives/tailscale.list.chroot").chmod(0o600)
        self.assertNotEqual(self.key(), first)

    def test_the_mirror_changes_the_key(self):
        self.assertNotEqual(self.key(FAKE_RELEASE="two"), self.key())

    def test_hooks_includes_and_fetch_settings_do_not_change_the_key(self):
        # They run or apply on every build, so the snapshot does not depend on them.
        for path, text in (("hooks/live/0100-x.hook.chroot", "#!/bin/sh\necho two\n"),
                           ("hooks/live/0200-new.hook.chroot", "#!/bin/sh\n"),
                           ("includes.chroot/etc/couchliteos", "two\n"),
                           ("chroot", 'LB_CHROOT_FILESYSTEM="squashfs"\nLB_CHROOT_SQUASHFS_COMPRESSION_TYPE="xz"\n'
                                      'LB_CHROOT_SQUASHFS_COMPRESSION_LEVEL="9"\n'),
                           ("common", 'LB_MODE="debian"\nLB_CACHE_PACKAGES="false"\n'
                                      'LB_APT_HTTP_PROXY="http://127.0.0.1:3142"\n')):
            with self.subTest(path=path):
                first, second = self.changed(path, text)
                self.assertEqual(first, second)

    def test_no_key_without_the_package_indices(self):
        result = run_bash("lb_cache_fetch() { return 22; }\nlb_cache_chroot_key", cwd=self.work)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")


@unittest.skipUnless(shutil.which("zstd"), "zstd is not installed")
class ChrootSnapshotTest(Temporary):
    def setUp(self):
        super().setUp()
        self.work, self.shared = self.base / "work", self.base / "shared"
        root = self.work / "chroot"
        (root / "etc").mkdir(parents=True)
        (root / "var/cache/apt/archives").mkdir(parents=True)
        (root / "etc/os-release").write_text('NAME="Debian"\nBUILD_ID=20200101T000000Z\n')
        (root / "etc/shadow").write_text("root:*:1::::::\n")
        (root / "etc/shadow").chmod(0o640)
        (root / "usr/bin").mkdir(parents=True)
        (root / "bin").symlink_to("usr/bin")
        (root / "var/cache/apt/archives/big_1_amd64.deb").write_text("x" * 1000)
        self.shared.mkdir()

    def test_save_then_restore_gives_the_same_tree_with_this_builds_id(self):
        snapshot = self.shared / "chroot-k1.tar.zst"
        result = run_bash(f'lb_cache_chroot_save "{self.shared}" "{snapshot}"', cwd=self.work)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(snapshot.is_file())
        self.assertEqual(list(self.shared.glob(".tmp-*")), [])
        shutil.rmtree(self.work / "chroot")
        (self.work / "chroot").mkdir()
        (self.work / "chroot/leftover").write_text("from debootstrap\n")
        result = run_bash(f'lb_cache_chroot_restore "{snapshot}"', cwd=self.work, env={"SOURCE_DATE_EPOCH": "2000"})
        self.assertEqual(result.returncode, 0, result.stderr)
        root = self.work / "chroot"
        self.assertFalse((root / "leftover").exists(), "the snapshot replaces chroot/")
        self.assertEqual((root / "etc/os-release").read_text(), 'NAME="Debian"\nBUILD_ID=19700101T003320Z\n')
        self.assertEqual((root / "etc/shadow").stat().st_mode & 0o777, 0o640)
        self.assertTrue((root / "bin").is_symlink())
        self.assertFalse((root / "var/cache/apt/archives/big_1_amd64.deb").exists(), "downloads are not kept")
        self.assertTrue((root / "var/cache/apt/archives").is_dir())
        self.assertFalse((self.work / "chroot.lb-cache").exists())

    def test_an_old_snapshot_is_deleted_not_used(self):
        snapshot = self.shared / "chroot-k1.tar.zst"
        run_bash(f'lb_cache_chroot_save "{self.shared}" "{snapshot}"', cwd=self.work)
        subprocess.run(["touch", "-d", "8 days ago", str(snapshot)], check=True)
        result = run_bash(f'lb_cache_chroot_restore "{snapshot}"', cwd=self.work)
        self.assertEqual(result.returncode, 1)
        self.assertIn("older than 7 days", result.stderr)
        self.assertFalse(snapshot.exists())
        self.assertTrue((self.work / "chroot/var/cache/apt/archives/big_1_amd64.deb").exists(), "chroot/ untouched")

    def test_a_damaged_snapshot_leaves_the_chroot_alone(self):
        snapshot = self.shared / "chroot-k1.tar.zst"
        snapshot.write_bytes(b"not zstd")
        result = run_bash(f'lb_cache_chroot_restore "{snapshot}"', cwd=self.work)
        self.assertEqual(result.returncode, 1)
        self.assertIn("installing packages afresh", result.stderr)
        self.assertTrue((self.work / "chroot/var/cache/apt/archives/big_1_amd64.deb").exists())
        self.assertFalse((self.work / "chroot.lb-cache").exists())

    def test_no_snapshot_is_a_miss(self):
        result = run_bash(f'lb_cache_chroot_restore "{self.shared}/chroot-none.tar.zst"', cwd=self.work)
        self.assertEqual(result.returncode, 1)

    def test_only_the_two_newest_snapshots_are_kept(self):
        for index, name in enumerate(("chroot-a.tar.zst", "chroot-b.tar.zst", "chroot-c.tar.zst")):
            (self.shared / name).write_text("x")
            subprocess.run(["touch", "-d", f"2026-01-0{index + 1}", str(self.shared / name)], check=True)
        result = run_bash(f'lb_cache_chroot_save "{self.shared}" "{self.shared}/chroot-d.tar.zst"', cwd=self.work)
        self.assertEqual(result.returncode, 0, result.stderr)
        kept = sorted(path.name for path in self.shared.glob("chroot-*"))
        self.assertEqual(kept, ["chroot-c.tar.zst", "chroot-d.tar.zst"])

    def test_a_failed_save_never_fails_the_build(self):
        result = run_bash(f'zstd() {{ return 1; }}\nlb_cache_chroot_save "{self.shared}" "{self.shared}/chroot-k.tar.zst"',
                          cwd=self.work)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("saving the chroot snapshot failed", result.stderr)
        self.assertEqual(list(self.shared.iterdir()), [])


# The stage with `lb` and the chroot commands replaced by stubs that log their arguments.
STAGE_STUBS = r'''
lb() { printf 'lb %s\n' "$*" >> calls; }
lb_cache_chroot_cmd() { printf 'in-chroot %s\n' "$*" >> calls; }
lb_cache_chroot_supported() { [[ -z ${UNSUPPORTED:-} ]]; }
lb_cache_chroot_key() { printf 'k1\n'; }
lb_cache_chroot_restore() { printf 'restore %s\n' "$1" >> calls; [[ -n ${HIT:-} ]]; }
lb_cache_chroot_save() { printf 'save %s\n' "$2" >> calls; }
'''

PACKAGE_STEPS = [
    "lb chroot_cache restore",
    "lb chroot_prep install all mode-archives-chroot",
    "lb chroot_linux-image",
    "lb chroot_firmware",
    "lb chroot_preseed",
    "lb chroot_includes_before_packages",
    "lb chroot_package-lists install",
    "lb chroot_install-packages install",
    "in-chroot dpkg-query -W",
    "lb chroot_package-lists live",
    "lb chroot_install-packages live",
]
FINAL_STEPS = [
    "lb chroot_includes_after_packages",
    "lb chroot_hooks",
    "lb chroot_hacks",
    "lb chroot_interactive",
    "in-chroot dpkg-query -W",
    "lb chroot_prep remove all mode-archives-chroot",
    "lb chroot_cache save",
    "in-chroot ls -lR",
]
SNAPSHOT_STEPS = [
    "lb chroot_prep remove all mode-archives-chroot",
    "save /cache/chroot-k1.tar.zst",
    "lb chroot_prep install all mode-archives-chroot",
]


class ChrootStageTest(Temporary):
    def stage(self, reuse, **env):
        result = run_bash(f'{STAGE_STUBS}lb_cache_chroot_stage /cache {reuse}', cwd=self.base, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return (self.base / "calls").read_text().splitlines(), result.stderr

    def test_a_miss_installs_saves_then_runs_includes_and_hooks(self):
        calls, _ = self.stage(1)
        self.assertEqual(calls, ["restore /cache/chroot-k1.tar.zst"] + PACKAGE_STEPS + SNAPSHOT_STEPS + FINAL_STEPS)

    def test_a_hit_restores_and_still_runs_every_step(self):
        calls, _ = self.stage(1, HIT="1")
        self.assertEqual(calls, ["restore /cache/chroot-k1.tar.zst"] + PACKAGE_STEPS + FINAL_STEPS)

    def test_a_release_never_restores(self):
        calls, stderr = self.stage(0, HIT="1")
        self.assertEqual(calls, PACKAGE_STEPS + SNAPSHOT_STEPS + FINAL_STEPS)
        self.assertIn("release build", stderr)

    def test_another_live_build_version_runs_plain_lb_chroot(self):
        calls, _ = self.stage(1, UNSUPPORTED="1", HIT="1")
        self.assertEqual(calls, ["lb chroot"])

    def test_the_steps_are_those_of_the_live_build_this_follows(self):
        script = pathlib.Path("/usr/lib/live/build/chroot")
        source = LB_CACHE.read_text()
        expected = re.search(r"(?m)^LB_CACHE_CHROOT_SCRIPT_MD5=([0-9a-f]{32})$", source).group(1)
        if not script.is_file() or hashlib.md5(script.read_bytes()).hexdigest() != expected:
            self.skipTest("live-build 1:20250505+deb13u1 is not installed")
        steps = []
        for line in script.read_text().splitlines():
            match = re.match(r'\s*lb (chroot_\S+)(.*?)(?: "\$\{@\}")?$', line)
            if match:
                steps.append(f"lb {match.group(1)}{match.group(2)}")
            elif re.match(r'\s*Chroot chroot "', line):
                steps.append("in-chroot " + line.split('"')[1])
        expanded = []
        for step in steps:
            if "${_PASS}" in step:
                continue
            expanded.append(step)
        loop = [step for step in steps if "${_PASS}" in step]
        self.assertEqual(loop, ["lb chroot_package-lists ${_PASS}", "lb chroot_install-packages ${_PASS}"])
        index = expanded.index("lb chroot_includes_before_packages") + 1
        expanded[index:index] = ["lb chroot_package-lists install", "lb chroot_install-packages install",
                                 "lb chroot_package-lists live", "lb chroot_install-packages live"]
        # The dpkg-query after the install pass is inside the loop in live-build.
        calls, _ = self.stage(1, HIT="1")
        self.assertEqual([call for call in calls if call.startswith("lb ")],
                         [step for step in expanded if step.startswith("lb ")])
        self.assertEqual([call for call in calls if call.startswith("in-chroot ")],
                         [step for step in steps if step.startswith("in-chroot ")])


class AptPinsTest(Temporary):
    def test_versioned_entries_become_pins(self):
        lists = self.base / "a.list.chroot"
        lists.write_text("# comment\ncage\n  tailscale=1.102.3  # pinned\nfoo=1:2.0-1+deb13u1\n"
                         "! not a package=1\nbar\n")
        result = subprocess.run([str(ROOT / "build/apt-pins.sh"), str(lists)], capture_output=True, text=True,
                                env={"PATH": PATH})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout,
                         "Package: foo\nPin: version 1:2.0-1+deb13u1\nPin-Priority: 1001\n\n"
                         "Package: tailscale\nPin: version 1.102.3\nPin-Priority: 1001\n\n")

    def test_the_tailscale_pin_matches_the_notices(self):
        result = subprocess.run([str(ROOT / "build/apt-pins.sh"), *map(str, ROOT.glob("config/**/package-lists/*.list.chroot"))],
                                capture_output=True, text=True, env={"PATH": PATH}, check=True)
        version = re.search(r"Package: tailscale\nPin: version (\S+)\n", result.stdout).group(1)
        self.assertIn(version, (ROOT / "THIRD_PARTY_NOTICES.md").read_text())
        self.assertIn(version, (ROOT / "docs/networking/tailscale.md").read_text())

    def test_configure_installs_the_pins_for_the_build_only(self):
        configure = (ROOT / "build/configure.sh").read_text()
        self.assertIn('build/apt-pins.sh" "$WORK"/config/package-lists/*.list.chroot', configure)
        self.assertIn('"$WORK/config/archives/couchliteos-pins.pref.chroot"', configure)
        self.assertIn('"$WORK/config/archives/couchliteos-pins.pref.binary"', configure)
        # A plain .pref would be copied into the image's apt configuration.
        self.assertNotRegex(configure, r"couchliteos-pins\.pref\"")


class TestGateTest(Temporary):
    def setUp(self):
        super().setUp()
        self.tree = self.base / "tree"
        (self.tree / "build").mkdir(parents=True)
        shutil.copy(ROOT / "build/test-gate.sh", self.tree / "build/test-gate.sh")
        (self.tree / "src.sh").write_text("echo one\n")
        (self.tree / "Makefile").write_text("test:\n\techo ran >> ran\n")
        for ignored in ("build/work", "build/out", ".git", "__pycache__"):
            (self.tree / ignored).mkdir(parents=True)

    def gate(self, *args):
        return subprocess.run([str(self.tree / "build/test-gate.sh"), *args], cwd=self.tree,
                              capture_output=True, text=True, env={"PATH": PATH})

    def test_marker_matches_only_the_same_tree(self):
        self.assertNotEqual(self.gate("check").returncode, 0, "no marker yet")
        self.assertEqual(self.gate("mark").returncode, 0)
        self.assertEqual(self.gate("check").returncode, 0)
        # Build output, Git data and Python caches do not count.
        (self.tree / "build/work/x").write_text("x")
        (self.tree / "build/out/x.iso").write_text("x")
        (self.tree / ".git/HEAD").write_text("x")
        (self.tree / "__pycache__/x.pyc").write_text("x")
        self.assertEqual(self.gate("check").returncode, 0)
        for change in (lambda: (self.tree / "src.sh").write_text("echo two\n"),
                       lambda: (self.tree / "new.sh").write_text(""),
                       lambda: (self.tree / "src.sh").chmod(0o755),
                       lambda: (self.tree / "dir").mkdir()):
            with self.subTest():
                self.gate("mark")
                change()
                self.assertNotEqual(self.gate("check").returncode, 0)

    def test_run_skips_only_after_a_pass(self):
        if not shutil.which("make"):
            self.skipTest("make is not installed")
        result = self.gate("run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.tree / "ran").read_text(), "ran\n")
        self.gate("mark")
        result = self.gate("run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skipped", result.stdout)
        self.assertEqual((self.tree / "ran").read_text(), "ran\n")
        (self.tree / "src.sh").write_text("echo three\n")
        self.assertEqual(self.gate("run").returncode, 0)
        self.assertEqual((self.tree / "ran").read_text(), "ran\nran\n")
        self.assertFalse((self.tree / "build/.tests-passed").exists(), "run never writes the marker itself")

    def test_make_test_marks_and_make_build_checks(self):
        makefile = (ROOT / "Makefile").read_text()
        test_recipe = re.search(r"(?ms)^test:\n(.*?)\n\n", makefile).group(1)
        self.assertEqual(test_recipe.splitlines()[-1], "\t./build/test-gate.sh mark")
        self.assertIn("\t./build/timed.sh tests ./build/test-gate.sh run\n", makefile)
        self.assertIn("build/.tests-passed", (ROOT / ".gitignore").read_text())


class TimingsTest(Temporary):
    def timed(self, *args):
        return subprocess.run([str(ROOT / "build/timed.sh"), *args], capture_output=True, text=True,
                              env={"PATH": PATH, "COUCHLITEOS_TIMINGS": str(self.base / "times")})

    def test_summary_totals_the_top_level_stages(self):
        for name, seconds in (("tests", "5"), ("bootstrap", "20"), ("binary", "300"),
                              ("binary/squashfs", "100"), ("source", "1")):
            result = self.timed("--record", name, seconds)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"couchliteos-time: {name} {seconds} ", result.stdout)
        result = self.timed("noop", "true")
        self.assertIn("couchliteos-time: noop 0 (0m00s)", result.stdout)
        summary = self.timed("--summary").stdout
        self.assertRegex(summary, r"\n    squashfs +100s  1m40s\n")
        self.assertRegex(summary, r"\n  total +326s  5m26s\n$")

    def test_a_failing_command_fails_and_records_nothing(self):
        result = self.timed("x", "false")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.base / "times").exists())

    def test_bad_arguments_are_refused(self):
        for args in ((), ("--record", "x"), ("--record", "x", "1.5"), ("x",)):
            with self.subTest(args=args):
                self.assertEqual(self.timed(*args).returncode, 64)


if __name__ == "__main__":
    unittest.main()
