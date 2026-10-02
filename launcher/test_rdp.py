import dataclasses
import hashlib
import os
import pathlib
import shutil
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import unittest

import couchliteos_apps as apps
import couchliteos_rdp as rdp


SYSTEM = pathlib.Path(__file__).parents[1] / "config" / "apps.d"
FINGERPRINT = "ab" * 32
SECRET = "Fake-Password-9f2c!"


def connection(**changes):
    base = rdp.Connection(
        id="rdp-work-pc", name="Work PC", host="192.168.50.20", username="alice",
        certificate=FINGERPRINT,
    )
    return dataclasses.replace(base, **changes)


class ValidationTest(unittest.TestCase):
    def test_hosts(self):
        self.assertEqual(rdp.validate_host(" Desktop.Example.LAN. "), "desktop.example.lan")
        self.assertEqual(rdp.validate_host("10.0.0.5"), "10.0.0.5")
        for value in ("", "::1", "fe80::1", "bad host", "-bad.example", "1.2.3", "a/b", "x" * 254, "host;id"):
            with self.subTest(value=value), self.assertRaises(rdp.RdpError):
                rdp.validate_host(value)

    def test_ports_usernames_domains_and_resolutions(self):
        self.assertEqual(rdp.validate_port("3389"), 3389)
        for value in ("0", "65536", "abc", ""):
            with self.assertRaises(rdp.RdpError):
                rdp.validate_port(value)
        self.assertEqual(rdp.validate_username(r"CORP\alice"), r"CORP\alice")
        for value in ("", "alice\nbob", "a\x00b"):
            with self.assertRaises(rdp.RdpError):
                rdp.validate_username(value)
        self.assertEqual(rdp.validate_domain(""), "")
        for value in ("my domain", "a/b", "a@b"):
            with self.assertRaises(rdp.RdpError):
                rdp.validate_domain(value)
        self.assertEqual(rdp.validate_resolution("NATIVE"), "native")
        self.assertEqual(rdp.validate_resolution("fit"), "fit")
        self.assertEqual(rdp.validate_resolution("2560x1440"), "2560x1440")
        for value in ("100x100", "9000x9000", "1920*1080", "custom"):
            with self.assertRaises(rdp.RdpError):
                rdp.validate_resolution(value)

    def test_fingerprints_and_passwords(self):
        colon = ":".join(["AB"] * 32)
        self.assertEqual(rdp.normalize_fingerprint(colon), FINGERPRINT)
        with self.assertRaises(rdp.RdpError):
            rdp.normalize_fingerprint("abcd")
        self.assertEqual(rdp.validate_password(""), "")
        for value in ("line\nbreak", "nul\x00", "x" * (rdp.MAX_PASSWORD + 1)):
            with self.assertRaises(rdp.RdpError):
                rdp.validate_password(value)
        self.assertEqual(len(rdp.fingerprint_lines(FINGERPRINT)), 4)

    def test_connection_ids_avoid_applications_and_existing_connections(self):
        self.assertEqual(rdp.connection_id("Work PC!", set()), "rdp-work-pc")
        self.assertEqual(rdp.connection_id("Work PC", {"rdp-work-pc"}), "rdp-work-pc-2")
        self.assertTrue(apps.ID_RE.fullmatch(rdp.connection_id("x" * 80, set())))


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.path = self.root / "rdp" / "connections.ini"

    def tearDown(self):
        self.temporary.cleanup()

    def test_round_trip_is_private_atomic_and_never_contains_a_password(self):
        saved = connection(domain="CORP", resolution="2560x1440", fullscreen=False, audio=False,
                           clipboard=False, save_password=True)
        rdp.upsert_connection(saved, self.path)
        rdp.upsert_connection(connection(id="rdp-lab", name="Lab", host="lab.example", certificate=""), self.path)
        loaded, errors = rdp.load_connections(self.path)
        self.assertEqual(errors, ())
        self.assertEqual(loaded[0], saved)
        self.assertEqual([item.id for item in loaded], ["rdp-work-pc", "rdp-lab"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)
        self.assertFalse(any(line.startswith("password") for line in self.path.read_text().splitlines()))
        self.assertEqual(list(self.path.parent.glob(".connections.ini.*")), [])

    def test_edit_and_delete(self):
        rdp.upsert_connection(connection(), self.path)
        rdp.upsert_connection(connection(port=3390), self.path)
        self.assertEqual(rdp.get_connection("rdp-work-pc", self.path).port, 3390)
        rdp.remove_connection("rdp-work-pc", self.path)
        self.assertIsNone(rdp.get_connection("rdp-work-pc", self.path))

    def test_invalid_sections_and_password_fields_are_isolated(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            rdp.serialize([connection()])
            + "[rdp-bad]\nname = Bad\nhost = ::1\nusername = x\n\n"
            + "[rdp-leak]\nname = Leak\nhost = a.example\nusername = x\npassword = oops\n"
        )
        loaded, errors = rdp.load_connections(self.path)
        self.assertEqual([item.id for item in loaded], ["rdp-work-pc"])
        self.assertEqual(len(errors), 2)
        self.assertTrue(any("unsupported field password" in error for error in errors))

    def test_unreadable_store_is_not_overwritten(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("not ini")
        with self.assertRaises(rdp.RdpError):
            rdp.upsert_connection(connection(), self.path)
        self.assertEqual(self.path.read_text(), "not ini")

    def bulky(self, index):
        # Within every per-field character limit, but two bytes per character on disk.
        return connection(
            id=f"rdp-bulky-{index:02d}", name="\u00e9" * 64, host="h" * 63 + ".example",
            username="\u00e9" * 256, domain="\u00e9" * 255,
        )

    def test_store_larger_than_the_loader_accepts_is_refused_not_saved(self):
        many = [self.bulky(index) for index in range(rdp.MAX_CONNECTIONS)]
        self.assertGreater(len(rdp.serialize(many).encode("utf-8")), rdp.MAX_FILE)
        with self.assertRaisesRegex(rdp.RdpError, "too large"):
            rdp.write_connections(many, self.path)
        self.assertFalse(self.path.exists())

    def test_adding_past_the_size_limit_keeps_the_saved_connections_loadable(self):
        for index in range(rdp.MAX_CONNECTIONS):
            try:
                rdp.upsert_connection(self.bulky(index), self.path)
            except rdp.RdpError as error:
                self.assertIn("too large", str(error))
                break
        else:
            self.fail("a store larger than the loader limit was saved")
        loaded, errors = rdp.load_connections(self.path)
        self.assertEqual(errors, ())
        self.assertEqual(len(loaded), index)
        self.assertGreater(index, 0)

    def test_symlinked_store_is_refused(self):
        self.path.parent.mkdir(parents=True)
        target = self.root / "elsewhere.ini"
        target.write_text(rdp.serialize([connection()]))
        self.path.symlink_to(target)
        loaded, errors = rdp.load_connections(self.path)
        self.assertEqual(loaded, ())
        self.assertEqual(len(errors), 1)


class ArgumentTest(unittest.TestCase):
    def test_password_is_never_an_argument_and_certificate_is_pinned(self):
        vector = rdp.freerdp_arguments(connection(domain="CORP"), "2560x1440")
        self.assertEqual(vector[0], "/usr/bin/sdl-freerdp3")
        self.assertIn("/from-stdin:force", vector)
        self.assertIn(f"/cert:deny,fingerprint:sha256:{FINGERPRINT}", vector)
        self.assertIn("/sec:rdp:off", vector)
        self.assertIn("/d:CORP", vector)
        self.assertFalse(any(item.startswith(("/p:", "/password")) for item in vector))
        self.assertNotIn(SECRET, " ".join(vector))

    def test_resolution_fullscreen_audio_and_clipboard(self):
        native = rdp.freerdp_arguments(connection(), "2560x1440")
        self.assertIn("/size:2560x1440", native)
        self.assertIn("/f", native)
        self.assertIn("/sound:sys:pulse", native)
        self.assertIn("/clipboard:files-to:off", native)
        fit = rdp.freerdp_arguments(connection(resolution="fit", fullscreen=False, audio=False, clipboard=False))
        self.assertIn("+dynamic-resolution", fit)
        self.assertNotIn("/f", fit)
        self.assertIn("/audio-mode:1", fit)
        self.assertIn("-clipboard", fit)
        custom = rdp.freerdp_arguments(connection(resolution="1920x1080"), "2560x1440")
        self.assertIn("/size:1920x1080", custom)
        self.assertIn("/smart-sizing", custom)
        self.assertNotIn("/size:2560x1440", custom)

    def test_unverified_certificate_cannot_start(self):
        with self.assertRaisesRegex(rdp.RdpError, "certificate"):
            rdp.freerdp_arguments(connection(certificate=""))

    def test_client_environment_uses_wayland_only(self):
        environment = rdp.client_environment({"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0", "HOME": "/h"})
        self.assertNotIn("DISPLAY", environment)
        self.assertEqual(environment["SDL_VIDEO_DRIVER"], "wayland")
        self.assertEqual(environment["SDL_APP_ID"], rdp.WAYLAND_APP_ID)
        self.assertEqual(environment["WAYLAND_DISPLAY"], "wayland-0")
        # Through libdecor (no plugin installed) sdl-freerdp3 connects but never maps a window.
        self.assertEqual(environment["SDL_VIDEO_WAYLAND_ALLOW_LIBDECOR"], "0")


class NegotiationTest(unittest.TestCase):
    def test_request_bytes(self):
        request = rdp.negotiation_request()
        self.assertEqual(len(request), 19)
        self.assertEqual(request[:4], b"\x03\x00\x00\x13")
        self.assertEqual(request[5], 0xE0)
        self.assertEqual(struct.unpack("<I", request[-4:])[0], 0x0B)

    def test_response_parsing(self):
        header = b"\x03\x00\x00\x13"
        tls = bytes((14, 0xD0, 0, 0, 0, 0, 0)) + struct.pack("<BBHI", 2, 0, 8, 1)
        self.assertEqual(rdp.parse_negotiation_response(header, tls), 1)
        legacy = bytes((6, 0xD0, 0, 0, 0, 0, 0))
        with self.assertRaisesRegex(rdp.RdpError, "legacy"):
            rdp.parse_negotiation_response(b"\x03\x00\x00\x0b", legacy)
        refused = bytes((14, 0xD0, 0, 0, 0, 0, 0)) + struct.pack("<BBHI", 3, 0, 8, 5)
        with self.assertRaisesRegex(rdp.RdpError, "rejected"):
            rdp.parse_negotiation_response(header, refused)


@unittest.skipUnless(shutil.which("openssl"), "openssl is required to create a test certificate")
class ProbeTest(unittest.TestCase):
    def test_probe_returns_the_presented_certificate_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            key, cert = pathlib.Path(directory, "key.pem"), pathlib.Path(directory, "cert.pem")
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                 "-subj", "/CN=rdp-test", "-keyout", str(key), "-out", str(cert)],
                check=True, capture_output=True,
            )
            expected = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)

            def serve():
                client, _address = listener.accept()
                with client:
                    client.recv(19)
                    client.sendall(b"\x03\x00\x00\x13" + bytes((14, 0xD0, 0, 0, 0, 0, 0))
                                   + struct.pack("<BBHI", 2, 0, 8, 1))
                    try:
                        with context.wrap_socket(client, server_side=True):
                            pass
                    except (ssl.SSLError, OSError):
                        pass

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            try:
                found = rdp.probe_certificate("127.0.0.1", listener.getsockname()[1], timeout=5)
            finally:
                thread.join(5)
                listener.close()
        self.assertEqual(found, expected)


class HandoffAndSecretTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_handoff_is_private_bound_to_a_connection_and_symlinks_are_refused(self):
        handoff = self.root / "rdp-session.secret"
        rdp.write_handoff("rdp-work-pc", SECRET, handoff)
        self.assertEqual(handoff.stat().st_mode & 0o777, 0o600)
        self.assertEqual(rdp.read_handoff(handoff), ("rdp-work-pc", SECRET))
        handoff.write_text(SECRET)
        with self.assertRaisesRegex(rdp.RdpError, "connection id|invalid connection id"):
            rdp.read_handoff(handoff)
        handoff.unlink()
        (self.root / "target").write_text("rdp-work-pc\n" + SECRET)
        handoff.symlink_to(self.root / "target")
        with self.assertRaises(OSError):
            rdp.read_handoff(handoff)

    def test_wrong_owner_is_refused(self):
        handoff = self.root / "rdp-session.secret"
        rdp.write_handoff("rdp-work-pc", SECRET, handoff)
        with self.assertRaisesRegex(rdp.RdpError, "owner"):
            rdp.read_handoff(handoff, owner_uid=os.getuid() + 1)

    def test_secret_requests_are_validated(self):
        request = self.root / "rdp-secret.request"
        request_id = rdp.submit_secret_request("set", "rdp-work-pc", SECRET, request)
        self.assertEqual(request.stat().st_mode & 0o777, 0o600)
        payload = rdp.parse_secret_request(request.read_text())
        self.assertEqual((payload["request_id"], payload["op"], payload["password"]), (request_id, "set", SECRET))
        rdp.submit_secret_request("stage", "rdp-work-pc", "", request)
        self.assertNotIn("password", rdp.parse_secret_request(request.read_text()))
        for text in (
            '{"request_id": "x", "op": "set", "id": "rdp-a", "password": "p"}',
            '{"request_id": "%s", "op": "read", "id": "rdp-a"}' % ("a" * 24),
            '{"request_id": "%s", "op": "set", "id": "../etc", "password": "p"}' % ("a" * 24),
            '{"request_id": "%s", "op": "delete", "id": "rdp-a", "password": "p"}' % ("a" * 24),
            '{"request_id": "%s", "op": "stage", "id": "rdp-a", "password": "p"}' % ("a" * 24),
            '{"request_id": "%s", "op": "set", "id": "rdp-a", "password": "a\\nb"}' % ("a" * 24),
        ):
            with self.subTest(text=text), self.assertRaises(rdp.RdpError):
                rdp.parse_secret_request(text)

    def test_secret_store_is_private_and_bound_to_the_server(self):
        store = rdp.SecretStore(self.root, owner_uid=os.getuid())
        store.store(connection(), SECRET)
        directory = self.root / rdp.SECRETS_NAME
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory / "rdp-work-pc").stat().st_mode & 0o777, 0o600)
        self.assertEqual(store.password_for(connection()), SECRET)
        self.assertEqual(store.password_for(connection(name="Renamed", resolution="fit")), SECRET)
        for changed in (connection(host="10.9.9.9"), connection(port=3390), connection(username="mallory"),
                        connection(domain="EVIL")):
            with self.subTest(changed=changed), self.assertRaisesRegex(rdp.RdpError, "different server"):
                store.password_for(changed)
        self.assertEqual(sorted(path.name for path in directory.iterdir()), ["rdp-work-pc"])
        store.delete("rdp-work-pc")
        self.assertIsNone(store.read("rdp-work-pc"))
        self.assertIsNone(store.password_for(connection()))
        with self.assertRaises(rdp.RdpError):
            store.store(connection(id="../escape"), SECRET)

    def test_secret_store_refuses_a_symlinked_directory(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (self.root / rdp.SECRETS_NAME).symlink_to(elsewhere)
        with self.assertRaises(OSError):
            rdp.SecretStore(self.root, owner_uid=os.getuid()).store(connection(), SECRET)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_stage_handoff_replaces_a_planted_symlink_without_following_it(self):
        target = self.root / "victim"
        target.write_text("unchanged")
        (self.root / rdp.HANDOFF.name).symlink_to(target)
        rdp.stage_handoff("rdp-work-pc", SECRET, self.root)
        self.assertEqual(target.read_text(), "unchanged")
        self.assertFalse((self.root / rdp.HANDOFF.name).is_symlink())
        self.assertEqual(rdp.read_handoff(self.root / rdp.HANDOFF.name), ("rdp-work-pc", SECRET))

    def test_summary_omits_usernames_and_passwords(self):
        summary = rdp.sanitized_summary((connection(domain="CORP", save_password=True),))
        self.assertIn("password=saved", summary)
        self.assertIn("certificate=pinned", summary)
        self.assertNotIn("alice", summary)
        self.assertNotIn("CORP", summary)


class ButtonPersistenceTest(unittest.TestCase):
    """A pinned connection is an ordinary user manifest plus apps-state order."""

    def test_label_position_and_shortcut_survive_a_fresh_load(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            user, state, store = root / "apps.d", root / "apps-state.ini", root / "rdp" / "connections.ini"
            rdp.upsert_connection(connection(), store)
            button = apps.Application(
                id="rdp-work-pc", name="OFFICE", kind="rdp", connection="rdp-work-pc",
                status_id="rdp-work-pc", order=70, shortcut="lb",
            )
            apps.write_user_application(button, system_dir=SYSTEM, user_dir=user)
            loaded = apps.load_applications(SYSTEM, user, state).applications
            apps.write_state(
                [dataclasses.replace(item, order=5) if item.id == "rdp-work-pc" else item for item in loaded], state
            )
            # A reboot is a fresh read of the same persistent files.
            result = apps.load_applications(SYSTEM, user, state)
            self.assertEqual(result.errors, ())
            first = [item for item in result.applications if item.visible][0]
            self.assertEqual(
                (first.id, first.name, first.kind, first.connection, first.shortcut, first.order),
                ("rdp-work-pc", "OFFICE", "rdp", "rdp-work-pc", "lb", 5),
            )
            self.assertEqual(rdp.get_connection(first.connection, store).host, "192.168.50.20")

    def test_rdp_manifest_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "app.ini"
            for text, message in (
                ("[app]\nid = rdp-a\nname = A\nkind = rdp\nconnection = rdp-b\n", "connection"),
                ("[app]\nid = rdp-a\nname = A\nkind = rdp\nconnection = rdp-a\ncommand = /bin/sh\n", "command"),
                ("[app]\nid = rdp-a\nname = A\nkind = rdp\nconnection = rdp-a\nshortcut = start\n", "shortcut"),
                ("[app]\nid = demo\nname = A\nkind = command\ncommand = /bin/true\nconnection = rdp-a\n", "connection"),
            ):
                path.write_text(text)
                with self.subTest(text=text), self.assertRaisesRegex(apps.ManifestError, message):
                    apps.read_manifest(path)
            path.write_text("[app]\nid = rdp-a\nname = A\nkind = rdp\nconnection = rdp-a\nterminal = true\n")
            self.assertFalse(apps.read_manifest(path).terminal)


if __name__ == "__main__":
    unittest.main()
