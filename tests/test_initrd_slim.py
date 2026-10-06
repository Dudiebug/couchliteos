"""overlay/etc/initramfs/post-update.d/couchliteos-slim: nouveau leaves a finished initrd."""
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "overlay/etc/initramfs/post-update.d/couchliteos-slim"
VERSION = "6.12.111+deb13-amd64"
TOOLS = ("cpio", "zstd", "python3")


def cpio(directory: pathlib.Path) -> bytes:
    names = subprocess.run(["find", ".", "-mindepth", "1", "-print0"], cwd=directory, check=True,
                           stdout=subprocess.PIPE).stdout
    return subprocess.run(["cpio", "-o", "-H", "newc", "-R", "0:0", "--null", "--quiet"], cwd=directory,
                          input=names, check=True, stdout=subprocess.PIPE).stdout


def listing(data: bytes) -> list[str]:
    return subprocess.run(["cpio", "-t", "--quiet"], input=data, check=True,
                          stdout=subprocess.PIPE).stdout.decode().split()


@unittest.skipUnless(all(shutil.which(tool) for tool in TOOLS), "needs cpio, zstd and python3")
class InitrdSlimTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        early = self.tmp / "early-tree"
        (early / "kernel/x86/microcode").mkdir(parents=True)
        (early / "kernel/x86/microcode/GenuineIntel.bin").write_bytes(b"microcode")
        self.early = cpio(early)
        main = self.tmp / "main-tree"
        modules = main / "usr/lib/modules" / VERSION / "kernel"
        for path in ("drivers/gpu/drm/nouveau/nouveau.ko.xz", "fs/ext4/ext4.ko.xz"):
            (modules / path).parent.mkdir(parents=True, exist_ok=True)
            (modules / path).write_bytes(b"ko")
        (main / "usr/lib/firmware/nvidia/ad102/gsp").mkdir(parents=True)
        (main / "usr/lib/firmware/nvidia/ad102/gsp/gsp.bin").write_bytes(b"fw" * 1000)
        (main / "usr/lib/firmware/i915").mkdir(parents=True)
        (main / "usr/lib/firmware/i915/dmc.bin").write_bytes(b"fw")
        os.symlink("usr/lib", main / "lib")
        (main / "init").write_text("#!/bin/sh\n")
        compressed = subprocess.run(["zstd", "-q", "-c"], input=cpio(main), check=True,
                                    stdout=subprocess.PIPE).stdout
        self.initrd = self.tmp / f"initrd.img-{VERSION}"
        self.initrd.write_bytes(self.early + compressed)
        self.initrd.chmod(0o600)
        # depmod records that it ran against the unpacked tree.
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "depmod").write_text(
            '#!/bin/sh\n[ "$1 $2 $4" = "-a -b ' + VERSION + '" ] || exit 2\n'
            'echo ok > "$3/usr/lib/modules/$4/modules.dep"\n')
        (bin_dir / "depmod").chmod(0o755)
        self.env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")

    def run_script(self, initrd: pathlib.Path):
        return subprocess.run(["bash", str(SCRIPT), VERSION, str(initrd)], env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def main_listing(self) -> list[str]:
        data = self.initrd.read_bytes()
        self.assertTrue(data.startswith(self.early), "the microcode archive stays byte for byte")
        main = subprocess.run(["zstd", "-dcq"], input=data[len(self.early):], check=True,
                              stdout=subprocess.PIPE).stdout
        return listing(main)

    def test_nouveau_and_its_firmware_leave_the_initrd_and_the_rest_stays(self):
        result = self.run_script(self.initrd)
        self.assertEqual(result.returncode, 0, result.stderr)
        names = self.main_listing()
        self.assertFalse([name for name in names if "nouveau" in name or "firmware/nvidia" in name], names)
        for kept in (f"usr/lib/modules/{VERSION}/kernel/fs/ext4/ext4.ko.xz", "usr/lib/firmware/i915/dmc.bin",
                     "lib", "init", f"usr/lib/modules/{VERSION}/modules.dep"):
            self.assertIn(kept, names)
        self.assertEqual(self.initrd.stat().st_mode & 0o777, 0o600)

    def test_a_second_run_changes_nothing(self):
        self.run_script(self.initrd)
        first = self.initrd.read_bytes()
        result = self.run_script(self.initrd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.initrd.read_bytes(), first)

    def test_an_initrd_that_is_not_zstd_is_left_as_it_is(self):
        gzip_initrd = self.tmp / "initrd.gz"
        gzip_initrd.write_bytes(self.early + b"\x1f\x8b\x08\x00rest")
        result = self.run_script(gzip_initrd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(gzip_initrd.read_bytes(), self.early + b"\x1f\x8b\x08\x00rest")

    def test_a_missing_initrd_is_not_an_error(self):
        self.assertEqual(self.run_script(self.tmp / "absent").returncode, 0)


if __name__ == "__main__":
    unittest.main()
