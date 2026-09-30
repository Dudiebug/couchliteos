import unittest

from tests import qemu_iso_boot


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


if __name__ == "__main__":
    unittest.main()
