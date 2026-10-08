import functools
import http.server
import os
import tempfile
import threading
import unittest
from unittest import mock

from tests import preseed_ready, qemu_iso_boot


class QemuIsoBootTests(unittest.TestCase):
    def test_types_release_preseed_url_and_serial_console(self):
        text = (
            " auto=true priority=critical "
            "preseed/url=http://10.0.2.2:8000/installer-preseed.cfg "
            "console=ttyS0,115200n8 DEBIAN_FRONTEND=text"
        )

        keys = qemu_iso_boot.keys_for_text(text)

        self.assertEqual(keys[0], "spc")
        self.assertIn("shift-s", keys)
        self.assertIn("shift-minus", keys)
        self.assertEqual(qemu_iso_boot.text_for_keys(keys), text)

    def test_selects_installer_from_real_iso_menu(self):
        commands = qemu_iso_boot.install_commands(
            "/tmp/menu.ppm", "/tmp/editor.ppm", "/tmp/installer.ppm", " auto=true"
        )

        names = [command for command, _delay in commands]
        self.assertEqual(names.count("sendkey down"), 5)
        self.assertGreaterEqual(commands[names.index("sendkey e")][1], 2.0)
        self.assertGreaterEqual(commands[names.index("sendkey e") + 1][1], 0.5)
        self.assertGreaterEqual(commands[names.index("sendkey e") + 2][1], 0.5)
        self.assertEqual(
            names[names.index("sendkey e") + 1 : names.index("sendkey end") + 1],
            ["sendkey down", "sendkey down", "sendkey end"],
        )
        self.assertIn("screendump /tmp/menu.ppm", names)
        self.assertIn("screendump /tmp/editor.ppm", names)
        self.assertIn("screendump /tmp/installer.ppm", names)
        self.assertLess(names.index("sendkey e"), names.index("sendkey ctrl-x"))
        self.assertLess(
            names.index("sendkey ctrl-x"), names.index("screendump /tmp/installer.ppm")
        )
        self.assertNotIn("-kernel", " ".join(names))
        self.assertNotIn("-initrd", " ".join(names))


class MonitorAddressTests(unittest.TestCase):
    def test_tcp_monitor_address(self):
        import socket

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        try:
            port = listener.getsockname()[1]
            with qemu_iso_boot.connect_monitor(f"tcp:127.0.0.1:{port}", timeout=5) as client:
                self.assertEqual(client.getpeername()[1], port)
        finally:
            listener.close()


class PreseedReadyTests(unittest.TestCase):
    """A stale server on the port must fail the run in seconds, not hang the installer."""

    def folder(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)  # runs after the server below has shut down
        return folder.name

    def serve(self, directory):
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
        handler.log_message = lambda *args: None
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/installer-preseed.cfg"
        patcher = mock.patch.object(preseed_ready, "URL", url)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def preseed(directory, text):
        path = os.path.join(directory, "installer-preseed.cfg")
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def test_this_runs_preseed_is_ready(self):
        served = self.folder()
        path = self.preseed(served, "d-i a string b\n")
        self.serve(served)
        self.assertEqual(preseed_ready.main(["x", path, "2"]), 0)

    def test_an_old_server_with_other_files_fails(self):
        old, new = self.folder(), self.folder()
        path = self.preseed(new, "d-i a string b\n")
        self.serve(old)  # the deleted tree: 404
        self.assertEqual(preseed_ready.main(["x", path, "0.5"]), 1)
        self.preseed(old, "d-i a string old\n")  # another run's preseed
        self.assertEqual(preseed_ready.main(["x", path, "0.5"]), 1)

    def test_nothing_listening_fails(self):
        path = self.preseed(self.folder(), "x\n")
        with mock.patch.object(preseed_ready, "URL", "http://127.0.0.1:9/installer-preseed.cfg"):
            self.assertEqual(preseed_ready.main(["x", path, "0.5"]), 1)


if __name__ == "__main__":
    unittest.main()
