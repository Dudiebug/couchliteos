import configparser
import importlib.machinery
import importlib.util
import pathlib
import tempfile
import unittest
from unittest import mock


PATH = pathlib.Path(__file__).parents[1] / "scripts" / "couchliteos-host-address"


def load_module():
    loader = importlib.machinery.SourceFileLoader("host_address", str(PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class AddressSelectionTest(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.temp = tempfile.NamedTemporaryFile(mode="w", delete=False)
        config = configparser.ConfigParser()
        config["tailscale"] = {"host_address_mode": "auto"}
        config["host:gaming-pc"] = {
            "lan_address": "192.0.2.10",
            "tailscale_hostname": "gaming.example.ts.net",
            "tailscale_ip": "100.64.0.10",
            "address_mode": "auto",
        }
        config.write(self.temp)
        self.temp.close()
        self.module.CONFIG = self.temp.name

    def tearDown(self):
        pathlib.Path(self.temp.name).unlink(missing_ok=True)

    def test_auto_prefers_reachable_lan(self):
        with mock.patch.object(self.module, "tailscale_running", return_value=True), \
             mock.patch.object(self.module, "reachable", side_effect=lambda value: value == "192.0.2.10"):
            self.assertEqual(self.module.resolve("gaming-pc"), ("192.0.2.10", "lan", True))

    def test_auto_falls_back_from_magicdns_to_tailscale_ip(self):
        with mock.patch.object(self.module, "tailscale_running", return_value=True), \
             mock.patch.object(self.module, "reachable", side_effect=lambda value: value == "100.64.0.10"):
            self.assertEqual(self.module.resolve("gaming-pc"), ("100.64.0.10", "tailscale-ip", True))

    def test_tailscale_offline_does_not_block_lan_launch(self):
        with mock.patch.object(self.module, "tailscale_running", return_value=False), \
             mock.patch.object(self.module, "reachable", return_value=False):
            self.assertEqual(self.module.resolve("gaming-pc"), ("192.0.2.10", "lan", False))


if __name__ == "__main__":
    unittest.main()
