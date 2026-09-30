import importlib.machinery
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "launcher"))
import moonlightos_rdp as rdp  # noqa: E402

LOADER = importlib.machinery.SourceFileLoader("moonlightos_rdp_secret", str(ROOT / "scripts" / "moonlightos-rdp-secret"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
helper = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(helper)

SECRET = "Fake-Saved-Password-5150"


class SecretHelperTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.temporary.name)
        self.run = base / "run"
        self.data = base / "data"
        self.run.mkdir(mode=0o700)
        self.data.mkdir(mode=0o750)
        self.connections = self.data / "rdp" / "connections.ini"
        self.store = rdp.SecretStore(self.data, owner_uid=os.getuid())
        self.connection = rdp.Connection(
            id="rdp-work-pc", name="Work PC", host="192.168.50.20", username="alice",
            save_password=True, certificate="cd" * 32,
        )
        rdp.upsert_connection(self.connection, self.connections)
        self.owner = (os.getuid(), os.getgid())

    def tearDown(self):
        self.temporary.cleanup()

    def request(self, operation, password=""):
        return rdp.submit_secret_request(operation, "rdp-work-pc", password, self.run / "rdp-secret.request")

    def handle(self, owner=None):
        return helper.handle_request(self.run, self.store, self.connections, owner or self.owner)

    def status(self):
        return json.loads((self.run / "rdp-secret.status").read_text())

    def test_shebang_isolates_python_from_the_callers_environment(self):
        first = (ROOT / "scripts" / "moonlightos-rdp-secret").read_text().splitlines()[0]
        self.assertEqual(first, "#!/usr/bin/python3 -I")

    def test_save_stage_and_delete(self):
        request_id = self.request("set", SECRET)
        self.assertEqual(self.handle(), 0)
        self.assertFalse((self.run / "rdp-secret.request").exists())
        self.assertEqual(self.store.password_for(self.connection), SECRET)
        status = self.status()
        self.assertEqual((status["request_id"], status["state"]), (request_id, "success"))
        self.assertEqual((self.run / "rdp-secret.status").stat().st_mode & 0o777, 0o644)
        self.assertNotIn(SECRET, (self.run / "rdp-secret.status").read_text())

        self.request("stage")
        self.assertEqual(self.handle(), 0)
        handoff = self.run / "rdp-session.secret"
        self.assertEqual(handoff.stat().st_mode & 0o777, 0o600)
        self.assertEqual(rdp.read_handoff(handoff), ("rdp-work-pc", SECRET))

        self.request("delete")
        self.assertEqual(self.handle(), 0)
        self.assertIsNone(self.store.read("rdp-work-pc"))

    def test_stage_refuses_a_connection_redirected_to_another_server(self):
        self.request("set", SECRET)
        self.handle()
        rdp.upsert_connection(rdp.dataclasses.replace(self.connection, host="203.0.113.9"), self.connections)
        self.request("stage")
        self.assertEqual(self.handle(), 1)
        self.assertIn("different server", self.status()["message"])
        self.assertFalse((self.run / "rdp-session.secret").exists())

    def test_stage_without_a_saved_password_fails_cleanly(self):
        self.request("stage")
        self.assertEqual(self.handle(), 1)
        self.assertEqual(self.status()["state"], "failed")
        self.assertFalse((self.run / "rdp-session.secret").exists())

    def test_unknown_connection_and_malformed_requests_fail_without_echoing_secrets(self):
        rdp.remove_connection("rdp-work-pc", self.connections)
        self.request("set", SECRET)
        self.assertEqual(self.handle(), 1)
        self.assertEqual(self.status()["state"], "failed")
        self.assertIsNone(self.store.read("rdp-work-pc"))
        (self.run / "rdp-secret.request").write_text('{"op": "set", "password": "%s"}' % SECRET)
        self.assertEqual(self.handle(), 1)
        self.assertNotIn(SECRET, (self.run / "rdp-secret.status").read_text())
        self.assertFalse((self.run / "rdp-secret.request").exists())

    def test_request_from_another_owner_is_refused(self):
        self.request("set", SECRET)
        self.assertEqual(self.handle((os.getuid() + 1, os.getgid())), 1)
        self.assertIsNone(self.store.read("rdp-work-pc"))

    def test_only_the_request_subcommand_exists(self):
        self.assertEqual(helper.main(["stage"]), 64)


if __name__ == "__main__":
    unittest.main()
