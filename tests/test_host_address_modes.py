"""couchliteos-host-address: address modes, Tailscale state, and the command line."""

import configparser
import contextlib
import importlib.machinery
import importlib.util
import io
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "couchliteos-host-address"


def load_module():
    loader = importlib.machinery.SourceFileLoader("host_address_modes", str(PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


LAN = "192.0.2.10"
MAGIC = "gaming.example.ts.net"
TS_IP = "100.64.0.10"


class ResolveModesTest(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config_path = pathlib.Path(self.directory.name) / "config.ini"
        self.module.CONFIG = str(self.config_path)

    def write_config(self, host=None, global_mode=None, profile="gaming-pc"):
        config = configparser.ConfigParser()
        if global_mode is not None:
            config["tailscale"] = {"host_address_mode": global_mode}
        if host is not None:
            config[f"host:{profile}"] = host
        with self.config_path.open("w") as stream:
            config.write(stream)

    def resolve(self, profile="gaming-pc", tailscale_only=False, online=True, reachable=()):
        with mock.patch.object(self.module, "tailscale_running", return_value=online), \
             mock.patch.object(self.module, "reachable", side_effect=lambda value: value in reachable) as probe:
            result = self.module.resolve(profile, tailscale_only)
        return result, [call.args[0] for call in probe.call_args_list]

    def full_host(self, mode=None):
        host = {"lan_address": LAN, "tailscale_hostname": MAGIC, "tailscale_ip": TS_IP}
        if mode is not None:
            host["address_mode"] = mode
        return host

    def test_lan_mode_never_tries_tailscale_even_when_lan_is_down(self):
        self.write_config(self.full_host("lan"))
        result, probed = self.resolve(reachable={MAGIC, TS_IP})
        self.assertEqual(result, (LAN, "lan", False))
        self.assertEqual(probed, [LAN])

    def test_tailscale_mode_skips_the_lan_address(self):
        self.write_config(self.full_host("tailscale"))
        result, probed = self.resolve(reachable={LAN, TS_IP})
        self.assertEqual(result, (TS_IP, "tailscale-ip", True))
        self.assertNotIn(LAN, probed)

    def test_tailscale_mode_with_tailscale_offline_has_no_address(self):
        self.write_config(self.full_host("tailscale"))
        result, probed = self.resolve(online=False, reachable={LAN, MAGIC, TS_IP})
        self.assertEqual(result, ("", "unconfigured", False))
        self.assertEqual(probed, [])

    def test_tailscale_only_flag_overrides_a_lan_mode(self):
        self.write_config(self.full_host("lan"))
        result, _probed = self.resolve(tailscale_only=True, reachable={LAN, MAGIC})
        self.assertEqual(result, (MAGIC, "tailscale-magicdns", True))

    def test_unreachable_tailscale_falls_back_to_magicdns_name_unprobed(self):
        self.write_config(self.full_host("tailscale"))
        result, _probed = self.resolve(reachable=set())
        self.assertEqual(result, (MAGIC, "tailscale-magicdns", False))

    def test_host_mode_overrides_the_global_mode(self):
        self.write_config(self.full_host("lan"), global_mode="tailscale")
        result, _probed = self.resolve(reachable={LAN, MAGIC})
        self.assertEqual(result, (LAN, "lan", True))

    def test_global_mode_applies_when_the_host_has_none(self):
        self.write_config(self.full_host(), global_mode="tailscale")
        result, _probed = self.resolve(reachable={LAN, MAGIC})
        self.assertEqual(result, (MAGIC, "tailscale-magicdns", True))

    def test_unknown_mode_is_treated_as_auto(self):
        self.write_config(self.full_host("carrier-pigeon"))
        result, probed = self.resolve(reachable={MAGIC})
        self.assertEqual(result, (MAGIC, "tailscale-magicdns", True))
        self.assertEqual(probed, [LAN, MAGIC])

    def test_mode_value_is_trimmed(self):
        self.write_config(self.full_host("  lan  "))
        result, _probed = self.resolve(reachable={MAGIC})
        self.assertEqual(result, (LAN, "lan", False))

    def test_empty_lan_address_falls_through_to_tailscale(self):
        host = self.full_host()
        host["lan_address"] = "   "
        self.write_config(host)
        result, probed = self.resolve(reachable={TS_IP})
        self.assertEqual(result, (TS_IP, "tailscale-ip", True))
        self.assertNotIn("", probed)

    def test_missing_profile_is_unconfigured(self):
        self.write_config(self.full_host(), profile="other-pc")
        self.assertEqual(self.resolve()[0], ("", "unconfigured", False))

    def test_missing_config_file_is_unconfigured(self):
        self.assertFalse(self.config_path.exists())
        self.assertEqual(self.resolve()[0], ("", "unconfigured", False))

    def test_profiles_are_independent(self):
        config = configparser.ConfigParser()
        config["host:living-room"] = {"lan_address": "192.0.2.20"}
        config["host:gaming-pc"] = {"lan_address": LAN}
        with self.config_path.open("w") as stream:
            config.write(stream)
        result, _probed = self.resolve("living-room", reachable={"192.0.2.20"})
        self.assertEqual(result, ("192.0.2.20", "lan", True))


class TailscaleStateTest(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def completed(self, returncode=0, stdout=""):
        return subprocess.CompletedProcess(["tailscale"], returncode, stdout, "")

    def test_running_backend(self):
        with mock.patch.object(self.module, "run", return_value=self.completed(stdout='{"BackendState": "Running"}')):
            self.assertTrue(self.module.tailscale_running())

    def test_other_backend_states_are_not_running(self):
        for state in ("NeedsLogin", "Stopped", "Starting", ""):
            with mock.patch.object(self.module, "run", return_value=self.completed(stdout=f'{{"BackendState": "{state}"}}')):
                self.assertFalse(self.module.tailscale_running(), state)

    def test_failed_command_is_not_running(self):
        with mock.patch.object(self.module, "run", return_value=self.completed(1, '{"BackendState": "Running"}')):
            self.assertFalse(self.module.tailscale_running())

    def test_invalid_json_is_not_running(self):
        with mock.patch.object(self.module, "run", return_value=self.completed(stdout="not json")):
            self.assertFalse(self.module.tailscale_running())

    def test_run_reports_a_missing_or_hung_command_as_status_127(self):
        for error in (FileNotFoundError("tailscale"), subprocess.TimeoutExpired(["tailscale"], 3)):
            with mock.patch.object(self.module.subprocess, "run", side_effect=error):
                result = self.module.run(["tailscale", "status"])
            self.assertEqual(result.returncode, 127)
            self.assertEqual(result.stdout, "")

    def test_run_never_raises_on_failure_and_uses_a_timeout(self):
        with mock.patch.object(self.module.subprocess, "run", return_value=self.completed(3)) as run:
            self.assertEqual(self.module.run(["ping"], 2).returncode, 3)
        self.assertEqual(run.call_args.kwargs["timeout"], 2)
        self.assertFalse(run.call_args.kwargs["check"])

    def test_reachable_pings_once_and_never_pings_an_empty_address(self):
        with mock.patch.object(self.module, "run", return_value=self.completed(0)) as run:
            self.assertTrue(self.module.reachable(LAN))
            self.assertFalse(self.module.reachable(""))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], ["ping", "-c", "1", "-W", "1", LAN])

    def test_unreachable_when_ping_fails(self):
        with mock.patch.object(self.module, "run", return_value=self.completed(1)):
            self.assertFalse(self.module.reachable(LAN))


class CommandLineTest(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def main(self, argv, result):
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["couchliteos-host-address", *argv]), \
             mock.patch.object(self.module, "resolve", return_value=result) as resolve, \
             contextlib.redirect_stdout(output):
            code = self.module.main()
        return code, output.getvalue(), resolve

    def test_quiet_prints_only_the_address(self):
        code, output, resolve = self.main(["--quiet"], (LAN, "lan", True))
        self.assertEqual((code, output), (0, LAN + "\n"))
        resolve.assert_called_once_with("gaming-pc", False)

    def test_verbose_output_names_the_source_and_reachability(self):
        code, output, _ = self.main(["living-room"], (TS_IP, "tailscale-ip", False))
        self.assertEqual(code, 0)
        self.assertEqual(output, f"living-room: {TS_IP} (tailscale-ip, not currently reachable)\n")

    def test_reachable_wording(self):
        _code, output, _ = self.main([], (LAN, "lan", True))
        self.assertEqual(output, f"gaming-pc: {LAN} (lan, reachable)\n")

    def test_tailscale_only_flag_is_passed_through(self):
        _code, _output, resolve = self.main(["--tailscale-only", "pc2"], (MAGIC, "tailscale-magicdns", True))
        resolve.assert_called_once_with("pc2", True)

    def test_no_address_exits_2(self):
        code, output, _ = self.main([], ("", "unconfigured", False))
        self.assertEqual(code, 2)
        self.assertEqual(output, "gaming-pc: no address configured (unconfigured, not currently reachable)\n")

    def test_quiet_with_no_address_prints_an_empty_line_and_exits_2(self):
        code, output, _ = self.main(["--quiet"], ("", "unconfigured", False))
        self.assertEqual((code, output), (2, "\n"))


if __name__ == "__main__":
    unittest.main()
