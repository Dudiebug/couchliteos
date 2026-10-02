"""couchliteos-nvidia-firmware: the opt-in NVIDIA video decoder firmware helper.

No network and no NVIDIA bytes: the driver archive is a small makeself-shaped
stand-in, and the pinned hashes are swapped for the stand-in blobs' hashes.
"""

import hashlib
import importlib.machinery
import importlib.util
import io
import json
import lzma
import pathlib
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "couchliteos-nvidia-firmware"
EXTRACTOR = ROOT / "third_party" / "envytools" / "extract_firmware.py"
SERVICE = ROOT / "services" / "couchliteos-nvidia-firmware.service"

loader = importlib.machinery.SourceFileLoader("nvidia_firmware", str(HELPER))
spec = importlib.util.spec_from_loader(loader.name, loader)
nvfw = importlib.util.module_from_spec(spec)
loader.exec_module(nvfw)

FAKE_BLOBS = {name: f"{name} firmware bytes".encode() for name in nvfw.BLOBS}
FAKE_HASHES = {name: hashlib.sha256(data).hexdigest() for name, data in FAKE_BLOBS.items()}


def makeself(members: dict[str, bytes]) -> bytes:
    header = b"".join(b"# header line %d\n" % line for line in range(1, nvfw.PAYLOAD_LINE))
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return header + lzma.compress(tar_bytes.getvalue())


class HelperTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.temporary.name)
        self.base = base
        self.run_dir = base / "run"
        self.store = base / "store"
        self.sysfs = base / "sys"
        self.hardware_env = base / "hardware.env"
        self.run_dir.mkdir()
        parameter = self.sysfs / "module/firmware_class/parameters/path"
        parameter.parent.mkdir(parents=True)
        parameter.write_text("\n")
        self.hardware_env.write_text("COUCHLITEOS_GPU_DRIVER=nouveau\nCOUCHLITEOS_GPU_ID=10de:0fe9\n")
        patches = {
            "RUN": self.run_dir, "REQUEST": self.run_dir / "nvidia-firmware.request",
            "STATUS": self.run_dir / "nvidia-firmware.status", "STORE": self.store, "SYSFS": self.sysfs,
            "FIRMWARE_PATH_PARAMETER": parameter, "HARDWARE_ENV": self.hardware_env, "BLOBS": FAKE_HASHES, "CONFIG": base / "config.ini",
        }
        for name, value in patches.items():
            patcher = mock.patch.object(nvfw, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.temporary.cleanup()

    def blobs_in(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        for name, data in FAKE_BLOBS.items():
            (directory / name).write_bytes(data)
        return directory

    def status(self):
        return json.loads((self.run_dir / "nvidia-firmware.status").read_text())

    def request(self, action):
        (self.run_dir / "nvidia-firmware.request").write_text(action + "\n")

    def test_only_the_three_decoder_blobs_are_pinned(self):
        self.assertEqual(set(nvfw.LINKS.values()), {"nve0_bsp", "nve0_vp", "nvc0_ppp"})
        source = HELPER.read_text()
        for name in ("nve0_bsp", "nve0_vp", "nvc0_ppp"):
            self.assertRegex(source, rf'"{name}": "[0-9a-f]{{64}}"')
        self.assertNotRegex(source, r'"[a-z0-9_]*fuc4(09|1a)')  # never PGRAPH firmware
        self.assertEqual(nvfw.URL, "https://download.nvidia.com/XFree86/Linux-x86/325.15/NVIDIA-Linux-x86-325.15.run")
        self.assertRegex(nvfw.DRIVER_SHA256, r"^[0-9a-f]{64}$")

    def test_vendored_extractor_is_unmodified_and_keeps_its_license(self):
        digest = hashlib.sha256(EXTRACTOR.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        self.assertEqual(digest, "b684b37b69a78f58fb3a6cf73684698a65927e701adedbae736ddbb0d1768ea7")
        self.assertIn("Permission is hereby granted, free of charge", EXTRACTOR.read_text())

    def test_unpack_writes_only_the_two_driver_files(self):
        run_file = self.base / "driver.run"
        run_file.write_bytes(makeself({
            "./kernel/nv-kernel.o": b"kernel", f"./libnvcuvid.so.{nvfw.VERSION}": b"cuvid",
            "./nvidia-installer": b"no", "./../escape": b"no",
        }))
        work = self.base / "work"
        nvfw.unpack(run_file, work)
        files = sorted(str(path.relative_to(work)).replace("\\", "/") for path in work.rglob("*") if path.is_file())
        self.assertEqual(files, [f"{nvfw.DRIVER}/kernel/nv-kernel.o", f"{nvfw.DRIVER}/libnvcuvid.so.{nvfw.VERSION}"])
        self.assertEqual((work / nvfw.DRIVER / "kernel/nv-kernel.o").read_bytes(), b"kernel")

    def test_unpack_reports_a_missing_member(self):
        run_file = self.base / "driver.run"
        run_file.write_bytes(makeself({"./kernel/nv-kernel.o": b"kernel"}))
        with self.assertRaisesRegex(nvfw.FirmwareError, "MISSING LIBNVCUVID"):
            nvfw.unpack(run_file, self.base / "work")

    def test_unpack_rejects_a_truncated_archive(self):
        run_file = self.base / "driver.run"
        run_file.write_bytes(b"#!/bin/sh\nshort\n")
        with self.assertRaises(nvfw.FirmwareError):
            nvfw.unpack(run_file, self.base / "work")

    def test_install_keeps_the_blobs_and_the_names_nouveau_asks_for(self):
        nvfw.install(self.blobs_in(self.base / "out"))
        nouveau = self.store / "nouveau"
        self.assertEqual((nouveau / "nve7_fuc084").read_bytes(), FAKE_BLOBS["nve0_bsp"])
        self.assertEqual((nouveau / "nv108_fuc085").read_bytes(), FAKE_BLOBS["nve0_vp"])
        self.assertEqual((nouveau / "nve4_fuc086").read_bytes(), FAKE_BLOBS["nvc0_ppp"])
        self.assertEqual(len(list(nouveau.iterdir())), 3 + 3 * len(nvfw.VP5_CHIPS))
        self.assertTrue(nvfw.installed())
        nvfw.install(self.blobs_in(self.base / "out"))  # a second install replaces the first
        self.assertEqual(sorted(path.name for path in self.store.iterdir()), ["nouveau"])

    def test_a_blob_with_the_wrong_hash_is_refused(self):
        out = self.blobs_in(self.base / "out")
        (out / "nve0_vp").write_bytes(b"tampered")
        with self.assertRaisesRegex(nvfw.FirmwareError, "NVE0_VP"):
            nvfw.check_blobs(out)

    def test_vainfo_output_parsing(self):
        self.assertTrue(nvfw.h264_decode_listed("      VAProfileH264High               :\tVAEntrypointVLD\n"))
        self.assertFalse(nvfw.h264_decode_listed("      VAProfileMPEG2Main              :\tVAEntrypointVLD\n"))
        self.assertFalse(nvfw.h264_decode_listed("vaInitialize failed with error code -1 (unknown libva error)\n"))

    def test_install_request_downloads_extracts_and_verifies(self):
        def fake_download(path, progress):
            path.write_bytes(b"driver")

        with mock.patch.object(nvfw, "download", side_effect=fake_download), \
                mock.patch.object(nvfw, "unpack") as unpack, \
                mock.patch.object(nvfw, "extract", side_effect=lambda work: self.blobs_in(work)), \
                mock.patch.object(nvfw, "decoder_works", return_value=True):
            self.request("install")
            self.assertEqual(nvfw.serve(), 0)
        unpack.assert_called_once()
        self.assertEqual(self.status()["state"], "done")
        self.assertIn("READY", self.status()["message"])
        self.assertFalse((self.run_dir / "nvidia-firmware.request").exists())
        self.assertEqual(nvfw.FIRMWARE_PATH_PARAMETER.read_bytes(), str(self.store).encode())  # no newline
        self.assertEqual(nvfw.verified_ids(), {"10de:0fe9"})
        self.assertEqual(nvfw.boot("10de:0fe9"), 0)
        self.assertEqual(nvfw.boot("10de:1234"), 1)  # another GPU is not vouched for

    def test_a_verified_decoder_turns_a_forced_software_decoder_back_to_auto(self):
        config = self.base / "config.ini"
        config.write_text("[moonlight]\ncodec = H.264\ndecoder = software\n[display]\ndecoder = software\n")
        nvfw.install(self.blobs_in(self.base / "out"))
        with mock.patch.object(nvfw, "decoder_works", return_value=True):
            self.request("install")
            self.assertEqual(nvfw.serve(), 0)
        self.assertEqual(config.read_text(), "[moonlight]\ncodec = H.264\ndecoder = auto\n[display]\ndecoder = software\n")

    def test_an_edit_lost_to_a_settings_save_is_redone_once_at_boot(self):
        config = self.base / "config.ini"
        config.write_text("[moonlight]\ndecoder = software\n")
        nvfw.install(self.blobs_in(self.base / "out"))
        with mock.patch.object(nvfw, "decoder_works", return_value=True):
            self.request("install")
            self.assertEqual(nvfw.serve(), 0)
        config.write_text("[moonlight]\ndecoder = software\n")  # the launcher replaced the file meanwhile
        self.assertEqual(nvfw.boot("10de:0fe9"), 0)
        self.assertEqual(config.read_text(), "[moonlight]\ndecoder = auto\n")
        config.write_text("[moonlight]\ndecoder = software\n")  # later the user picks software on purpose
        self.assertEqual(nvfw.boot("10de:0fe9"), 0)
        self.assertEqual(config.read_text(), "[moonlight]\ndecoder = software\n")

    def test_an_unexpected_error_still_ends_the_wait(self):
        with mock.patch.object(nvfw, "download", side_effect=RuntimeError("boom")):
            self.request("install")
            self.assertEqual(nvfw.serve(), 1)
        self.assertEqual(self.status(), {"state": "failed", "message": "FAILED: BOOM"})

    def test_a_retest_does_not_download_again(self):
        nvfw.install(self.blobs_in(self.base / "out"))
        with mock.patch.object(nvfw, "download") as download, \
                mock.patch.object(nvfw, "decoder_works", return_value=False):
            self.request("install")
            self.assertEqual(nvfw.serve(), 0)
        download.assert_not_called()
        self.assertIn("DID NOT START YET", self.status()["message"])
        self.assertEqual(nvfw.boot("10de:0fe9"), 1)  # saved but unverified: software decoding stays

    def test_a_pc_without_nouveau_is_refused(self):
        self.hardware_env.write_text("COUCHLITEOS_GPU_DRIVER=i915\n")
        with mock.patch.object(nvfw, "download") as download:
            self.request("install")
            self.assertEqual(nvfw.serve(), 1)
        download.assert_not_called()
        self.assertEqual(self.status()["state"], "failed")

    def test_a_failed_download_is_reported(self):
        with mock.patch.object(nvfw, "download", side_effect=nvfw.FirmwareError("DOWNLOAD FAILED: NO NETWORK")):
            self.request("install")
            self.assertEqual(nvfw.serve(), 1)
        self.assertEqual(self.status(), {"state": "failed", "message": "DOWNLOAD FAILED: NO NETWORK"})
        self.assertFalse(self.store.exists())

    def test_remove_request(self):
        nvfw.install(self.blobs_in(self.base / "out"))
        nvfw.mark_verified("10de:0fe9")
        self.request("remove")
        self.assertEqual(nvfw.serve(), 0)
        self.assertFalse(self.store.exists())
        self.assertEqual(nvfw.boot("10de:0fe9"), 1)

    def test_unknown_request(self):
        self.request("format-disk")
        self.assertEqual(nvfw.serve(), 1)
        self.assertEqual(self.status()["message"], "UNKNOWN REQUEST")

    def test_boot_without_firmware_leaves_the_kernel_alone(self):
        self.assertEqual(nvfw.boot("10de:0fe9"), 1)
        self.assertEqual(nvfw.FIRMWARE_PATH_PARAMETER.read_text(), "\n")


class DownloadTest(unittest.TestCase):
    def fake_response(self, data, url=None):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = url or nvfw.URL
        response.headers = {"Content-Length": str(len(data))}
        response.read.side_effect = io.BytesIO(data).read
        return response

    def test_wrong_bytes_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(nvfw.urllib.request, "urlopen", return_value=self.fake_response(b"not nvidia")):
            with self.assertRaisesRegex(nvfw.FirmwareError, "CHECKSUM"):
                nvfw.download(pathlib.Path(temporary) / "driver.run")

    def test_pinned_bytes_are_accepted(self):
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(nvfw, "DRIVER_SHA256", hashlib.sha256(b"driver").hexdigest()), \
                mock.patch.object(nvfw.urllib.request, "urlopen", return_value=self.fake_response(b"driver")):
            nvfw.download(pathlib.Path(temporary) / "driver.run")

    def test_a_dropped_connection_is_a_download_failure(self):
        response = self.fake_response(b"driver")
        response.read.side_effect = nvfw.http.client.IncompleteRead(b"dri", 3)
        with tempfile.TemporaryDirectory() as temporary,                 mock.patch.object(nvfw.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(nvfw.FirmwareError, "DOWNLOAD FAILED"):
                nvfw.download(pathlib.Path(temporary) / "driver.run")

    def test_a_redirect_off_https_is_refused(self):
        response = self.fake_response(b"driver", url="http://example.invalid/driver.run")
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(nvfw.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(nvfw.FirmwareError, "HTTPS"):
                nvfw.download(pathlib.Path(temporary) / "driver.run")


class ServiceTest(unittest.TestCase):
    def test_service_is_sandboxed_and_started_by_its_path_unit(self):
        text = SERVICE.read_text()
        self.assertIn("ExecStart=/usr/libexec/couchliteos-nvidia-firmware\n", text)
        self.assertIn("NoNewPrivileges=yes", text)
        self.assertIn("ProtectSystem=strict", text)
        self.assertRegex(text, r"(?m)^CapabilityBoundingSet=CAP_DAC_OVERRIDE$")
        self.assertRegex(text, r"(?m)^ReadWritePaths=/run/couchliteos /var/lib/couchliteos/firmware ")
        self.assertIn("OnFailure=couchliteos-path-rearm@nvidia-firmware.service", text)
        path = (ROOT / "services" / "couchliteos-nvidia-firmware.path").read_text()
        self.assertIn("PathExists=/run/couchliteos/nvidia-firmware.request", path)

    def test_usage(self):
        result = subprocess.run([sys.executable, str(HELPER), "--bogus"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 64)


if __name__ == "__main__":
    unittest.main()
