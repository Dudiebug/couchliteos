import testenv  # noqa: F401  (first: scratch run and state directories)
"""TYPE ON PHONE: the one-time form server and the launcher's text field."""

import http.client
import ipaddress
import json
import pathlib
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import couchliteos_phone as phone
from test_ux2 import Clock, Screen, load_launcher

LOOPBACK = [ipaddress.ip_network("127.0.0.0/8")]


def request(session, method="GET", path=None, body=None, headers=None):
    """(status, page text) for one request to the session's loopback server."""
    connection = http.client.HTTPConnection("127.0.0.1", session.port, timeout=5)
    try:
        connection.request(method, path or session.path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read().decode("utf-8")
    finally:
        connection.close()


def post(session, value, path=None):
    body = urllib.parse.urlencode({"value": value})
    return request(session, "POST", path, body, {"Content-Type": "application/x-www-form-urlencoded"})


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.threads = threading.active_count()
        self.clock = Clock()

    def session(self, **kwargs):
        kwargs.setdefault("networks", LOOPBACK)
        session = phone.Session("127.0.0.1", self.clock, **kwargs)
        self.addCleanup(session.close)
        return session

    def assertClosed(self, session):
        self.assertFalse(session._thread.is_alive())
        self.assertEqual(session._server.socket.fileno(), -1)
        self.assertEqual(threading.active_count(), self.threads)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", session.port), timeout=1).close()

    def test_the_token_is_long_and_random_and_in_the_url(self):
        first, second = self.session(), self.session()
        self.assertNotEqual(first.token, second.token)
        self.assertGreaterEqual(len(first.token), 21)  # 16 random bytes
        self.assertEqual(first.url, f"http://127.0.0.1:{first.port}/t/{first.token}")

    def test_get_serves_the_form_with_the_prompt_escaped(self):
        session = self.session(title="<b>T</b>", prompt='NAME "&" <script>', limit=32)
        status, page = request(session)
        self.assertEqual(status, 200)
        self.assertIn('maxlength="32"', page)
        self.assertIn('type="text"', page)
        self.assertNotIn("<script>", page)
        self.assertIn("NAME &quot;&amp;&quot; &lt;script&gt;", page)
        self.assertIn("&lt;b&gt;T&lt;/b&gt;", page)

    def test_a_masked_field_asks_for_a_password_input(self):
        self.assertIn('type="password"', request(self.session(masked=True))[1])

    def test_single_use_then_poll_closes_the_server(self):
        session = self.session()
        self.assertIsNone(session.poll())
        self.assertEqual(post(session, "Hello Wörld 123")[0], 200)
        self.assertEqual(post(session, "second")[0], 404)  # the first value wins
        self.assertEqual(request(session)[0], 404)
        self.assertEqual(session.poll(), "Hello Wörld 123")
        self.assertIsNone(session.poll())
        self.assertClosed(session)

    def test_wrong_token_and_other_paths_are_404(self):
        session = self.session()
        for path in ("/", "/t/", "/t/wrong", session.path[:-1], session.path + "x", "/x" + session.path):
            self.assertEqual(request(session, path=path)[0], 404, path)
            self.assertEqual(post(session, "x", path=path)[0], 404, path)
        self.assertEqual(request(session, "PUT")[0], 404)
        self.assertEqual(request(session, "DELETE")[0], 404)
        self.assertIsNone(session.poll())

    def test_expiry_with_a_fake_clock(self):
        session = self.session()
        self.assertEqual(request(session)[0], 200)
        self.clock.now += phone.EXPIRY_SECONDS
        self.assertTrue(session.expired())
        self.assertEqual(request(session)[0], 404)
        self.assertEqual(post(session, "late")[0], 404)
        self.assertIsNone(session.poll())  # an expired session closes itself
        self.assertClosed(session)

    def test_requests_from_outside_the_home_subnets_are_refused(self):
        session = self.session(networks=[ipaddress.ip_network("192.168.1.0/24")])
        self.assertEqual(request(session)[0], 404)
        self.assertEqual(post(session, "x")[0], 404)
        self.assertIsNone(session.poll())

    def test_allowed_addresses(self):
        home = [ipaddress.ip_network("192.168.1.0/24"), ipaddress.ip_network("100.64.0.0/10")]
        self.assertTrue(phone.allowed("192.168.1.40", home))
        self.assertTrue(phone.allowed("::ffff:192.168.1.40", home))
        self.assertFalse(phone.allowed("192.168.2.40", home))
        self.assertFalse(phone.allowed("100.100.1.2", home))  # Tailscale, even if a subnet covers it
        self.assertFalse(phone.allowed("fd7a:115c:a1e0::1", [ipaddress.ip_network("fd7a::/16")]))
        self.assertFalse(phone.allowed("not an address", home))

    def test_oversize_bodies_are_refused_before_they_are_read(self):
        session = self.session(limit=8)
        self.assertEqual(post(session, "123456789")[0], 413)  # longer than the field
        connection = http.client.HTTPConnection("127.0.0.1", session.port, timeout=5)
        connection.putrequest("POST", session.path)
        connection.putheader("Content-Type", "application/x-www-form-urlencoded")
        connection.putheader("Content-Length", str(phone.MAX_BODY + 1))
        connection.endheaders()  # no body is sent: an answer proves nothing waited for it
        self.assertEqual(connection.getresponse().status, 413)
        connection.close()
        self.assertEqual(post(session, "1234")[0], 200)  # the session stays open after a refused send
        self.assertEqual(session.poll(), "1234")

    def test_the_value_limit_is_at_most_1024_characters(self):
        session = self.session(limit=5000)
        self.assertEqual(session.limit, phone.MAX_CHARS)
        self.assertEqual(post(session, "x" * 1025)[0], 413)
        self.assertEqual(post(session, "é" * 1024)[0], 200)
        self.assertEqual(session.poll(), "é" * 1024)

    def test_malformed_posts_are_refused(self):
        session = self.session()
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        self.assertEqual(request(session, "POST", body="value=x", headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(request(session, "POST", body="other=x", headers=form)[0], 400)
        self.assertEqual(request(session, "POST", body="value=a&value=b", headers=form)[0], 400)
        self.assertEqual(request(session, "POST", body="value=%FF", headers=form)[0], 400)
        self.assertEqual(post(session, "line\nbreak")[0], 400)
        self.assertIsNone(session.poll())

    def raw(self, session, data):
        """The status line's code for raw request bytes (None when closed unanswered)."""
        with socket.create_connection(("127.0.0.1", session.port), timeout=5) as client:
            client.sendall(data)
            answer = client.recv(64)
        return int(answer.split()[1]) if answer else None

    def held(self, session, data):
        """A connection that sent part of a request and is waiting; closed at the end of the test."""
        client = socket.create_connection(("127.0.0.1", session.port), timeout=5)
        self.addCleanup(client.close)
        client.sendall(data)
        return client

    def test_chunked_or_unnumbered_bodies_are_refused_with_411(self):
        session = self.session()
        head = b"POST " + session.path.encode() + b" HTTP/1.1\r\nContent-Type: application/x-www-form-urlencoded\r\n"
        self.assertEqual(self.raw(session, head + b"Transfer-Encoding: chunked\r\n\r\n7\r\nvalue=x\r\n0\r\n\r\n"), 411)
        self.assertEqual(self.raw(session, head + b"Transfer-Encoding: chunked\r\nContent-Length: 7\r\n\r\nvalue=x"), 411)
        self.assertEqual(self.raw(session, head + b"Content-Length: seven\r\n\r\nvalue=x"), 411)
        self.assertEqual(self.raw(session, head + b"Content-Length: -7\r\n\r\nvalue=x"), 411)
        self.assertEqual(self.raw(session, head + b"\r\nvalue=x"), 411)  # no length at all
        self.assertIsNone(session.poll())
        self.assertEqual(post(session, "ok")[0], 200)  # still open: nothing was delivered
        self.assertEqual(session.poll(), "ok")

    def test_more_than_max_connections_at_once_are_dropped_unanswered(self):
        session = self.session()
        with mock.patch.object(phone._Handler, "timeout", 30):  # the held ones must not time out first
            waiting = [self.held(session, b"GET " + session.path.encode() + b" HTTP/1.1\r\n")
                       for _ in range(phone.MAX_CONNECTIONS)]
            time.sleep(0.5)  # every one accepted and counted
            self.assertIsNone(self.raw(session, b"GET " + session.path.encode() + b" HTTP/1.0\r\n\r\n"))
            waiting[0].close()  # one fewer: the next request is answered again
            for _ in range(50):
                time.sleep(0.1)
                if self.raw(session, b"GET " + session.path.encode() + b" HTTP/1.0\r\n\r\n") == 200:
                    break
            else:
                self.fail("no request was answered after a held connection closed")

    def test_two_posts_at_once_only_one_value_is_kept(self):
        session = self.session()
        with mock.patch.object(phone._Handler, "timeout", 30):
            clients = []
            for value in ("first", "other"):
                body = f"value={value}".encode()
                head = (b"POST " + session.path.encode() + b" HTTP/1.0\r\nContent-Type: application/x-www-form-urlencoded\r\n"
                        + b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n")
                clients.append((self.held(session, head + body[:-1]), body[-1:]))
            time.sleep(0.3)  # both handlers are reading their bodies: both passed the open check
            for client, last in clients:
                client.sendall(last)
            statuses = sorted(int(client.recv(64).split()[1]) for client, _last in clients)
        self.assertEqual(statuses, [200, 404])
        self.assertIn(session.poll(), ("first", "other"))
        self.assertIsNone(session.poll())

    def test_close_cuts_a_client_that_sends_slowly(self):
        session = self.session()
        client = socket.create_connection(("127.0.0.1", session.port), timeout=5)
        self.addCleanup(client.close)
        client.sendall(b"POST " + session.path.encode() + b" HTTP/1.0\r\nContent-Length: 100\r\n")
        time.sleep(0.3)  # the server thread is now waiting for the rest of the headers
        started = time.monotonic()
        session.close()
        self.assertLess(time.monotonic() - started, phone.SOCKET_TIMEOUT)
        self.assertClosed(session)

    def test_close_without_any_request_and_twice(self):
        session = self.session()
        session.close()
        session.close()
        self.assertClosed(session)

    def test_the_context_manager_closes_on_an_exception(self):
        with self.assertRaises(ValueError):
            with phone.Session("127.0.0.1", self.clock, networks=LOOPBACK) as session:
                raise ValueError
        self.assertClosed(session)


class HelpersTest(unittest.TestCase):
    IP_ADDR = [
        {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8, "scope": "host"}]},
        {"ifname": "enp1s0", "addr_info": [
            {"family": "inet", "local": "192.168.1.20", "prefixlen": 24, "scope": "global"},
            {"family": "inet6", "local": "fe80::1", "prefixlen": 64, "scope": "link"},
        ]},
        {"ifname": "tailscale0", "addr_info": [{"family": "inet", "local": "100.101.1.2", "prefixlen": 32, "scope": "global"}]},
        {"ifname": "wlan0", "addr_info": [
            {"family": "inet", "local": "10.0.0.5", "prefixlen": 16, "scope": "global"},
            {"family": "inet", "local": "100.90.0.1", "prefixlen": 10, "scope": "global"},
        ]},
    ]

    def test_lan_interfaces_skips_loopback_link_local_and_tailscale(self):
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(self.IP_ADDR), ""))
        found = phone.lan_interfaces(runner)
        self.assertEqual([str(interface) for interface in found], ["192.168.1.20/24", "10.0.0.5/16"])
        self.assertEqual(runner.call_args.args[0], ["ip", "-j", "addr"])

    def test_lan_interfaces_is_empty_when_ip_fails(self):
        self.assertEqual(phone.lan_interfaces(mock.Mock(side_effect=OSError)), [])
        self.assertEqual(phone.lan_interfaces(mock.Mock(return_value=subprocess.CompletedProcess([], 0, "nope", ""))), [])
        self.assertEqual(phone.lan_interfaces(mock.Mock(return_value=subprocess.CompletedProcess([], 1, "[]", ""))), [])

    def test_qr_lines_runs_qrencode_without_a_shell(self):
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "█▀█\n█▄█\n", ""))
        self.assertEqual(phone.qr_lines("http://x/t/a;b", runner), ["█▀█", "█▄█"])
        self.assertEqual(runner.call_args.args[0], ["qrencode", "-t", "UTF8", "-m", "1", "http://x/t/a;b"])
        self.assertEqual(phone.qr_lines("u", mock.Mock(side_effect=FileNotFoundError)), [])
        self.assertEqual(phone.qr_lines("u", mock.Mock(return_value=subprocess.CompletedProcess([], 1, "", ""))), [])


class Keys(Screen):
    """Each key is a str, an int, None (no key before the poll timeout) or a callable run at that read."""

    def get_wch(self):
        if not self.keys:
            raise RuntimeError("stop")
        key = self.keys.pop(0)
        if callable(key):
            key = key()
        if key is None:
            raise launcher_module.curses.error("no input")
        return key


launcher_module = load_launcher()


class TextFieldTest(unittest.TestCase):
    """The received text fills the field, and the user still confirms it."""

    def setUp(self):
        self.module = launcher_module
        self.clock = Clock()
        self.sessions = []
        real = phone.Session
        module = self.module

        def make(_ip, clock, **kwargs):
            # The launcher picks the LAN address; the test serves on loopback instead.
            kwargs["networks"] = LOOPBACK
            session = real("127.0.0.1", self.clock, **kwargs)
            self.sessions.append((clock, kwargs, session))
            return session

        lan = [ipaddress.ip_interface("192.168.1.20/24")]
        for patch in (
            mock.patch.object(module.phone, "Session", side_effect=make),
            mock.patch.object(module.phone, "lan_interfaces", return_value=lan),
            mock.patch.object(module.phone, "qr_lines", return_value=["QR1", "QR2"]),
            mock.patch.object(module.curses, "curs_set"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.threads = threading.active_count()

    def settings(self, keys):
        with mock.patch.object(self.module, "network_summary", return_value="OFFLINE"):
            launcher = self.module.Launcher(Screen())
        screen = Keys(keys)
        return self.module.ApplicationsSettings(screen, launcher), screen

    def send(self, value):
        def key():
            self.assertEqual(post(self.sessions[-1][2], value)[0], 200)
            return None
        return key

    def test_text_from_the_phone_fills_the_field_and_enter_confirms_it(self):
        f2 = self.module.curses.KEY_F2
        settings, screen = self.settings(["a", f2, None, self.send("Typed On Phone"), "!", "\n"])
        self.assertEqual(settings.text_input("ADD WEB APPLICATION", "NAME", 32, initial="old"), "Typed On Phone!")
        _clock, kwargs, session = self.sessions[0]
        self.assertEqual((kwargs["title"], kwargs["prompt"], kwargs["limit"], kwargs["masked"]),
                         ("ADD WEB APPLICATION", "NAME", 32, False))
        self.assertFalse(session._thread.is_alive())
        self.assertEqual(threading.active_count(), self.threads)

    def test_the_phone_screen_shows_the_url_and_qr_code(self):
        seen = []
        settings, screen = self.settings([
            self.module.curses.KEY_F7, lambda: seen.extend(screen.text()), "\x1b", "\x1b",
        ])
        self.assertIsNone(settings.text_input("T", "P", 10))
        session = self.sessions[0][2]
        self.assertIn(session.url, seen)
        self.assertIn("QR1", seen)
        self.assertIn(self.module.PHONE_HINT, seen)

    def test_b_cancels_and_the_field_keeps_its_text(self):
        settings, screen = self.settings([self.module.curses.KEY_F2, None, "\x1b", "\n"])
        self.assertEqual(settings.text_input("T", "P", 10, initial="kept"), "kept")
        self.assertFalse(self.sessions[0][2]._thread.is_alive())
        self.assertEqual(threading.active_count(), self.threads)

    def test_expiry_returns_to_the_field(self):
        def later():
            self.clock.now += phone.EXPIRY_SECONDS
            return None
        settings, screen = self.settings([self.module.curses.KEY_F2, later, "\n"])
        self.assertEqual(settings.text_input("T", "P", 10, initial="kept"), "kept")
        self.assertFalse(self.sessions[0][2]._thread.is_alive())

    def test_text_longer_than_the_field_is_refused_by_the_phone_form(self):
        f2 = self.module.curses.KEY_F2

        def too_long():
            self.assertEqual(post(self.sessions[-1][2], "123456")[0], 413)
            return None
        settings, screen = self.settings([f2, too_long, self.send("12345"), "\n"])
        self.assertEqual(settings.text_input("T", "P", 5), "12345")

    def test_a_password_field_asks_the_phone_for_a_password(self):
        settings, screen = self.settings([self.module.curses.KEY_F2, "\x1b", "\x1b"])
        settings.text_input("WI-FI PASSWORD", "PASSWORD", 64, masked=True)
        self.assertTrue(self.sessions[0][1]["masked"])

    def test_no_home_network_says_so_and_starts_nothing(self):
        seen = []
        settings, screen = self.settings([self.module.curses.KEY_F2, lambda: seen.extend(screen.text()) or "\n"])
        with mock.patch.object(self.module.phone, "lan_interfaces", return_value=[]):
            self.assertEqual(settings.text_input("T", "P", 10, initial="x"), "x")
        self.assertEqual(self.sessions, [])
        self.assertIn("TYPE ON PHONE NEEDS A HOME NETWORK CONNECTION", seen)

    def test_an_exception_while_waiting_still_closes_the_session(self):
        settings, screen = self.settings([self.module.curses.KEY_F2])  # the next read raises "stop"
        with self.assertRaisesRegex(RuntimeError, "stop"):
            settings.text_input("T", "P", 10)
        self.assertFalse(self.sessions[0][2]._thread.is_alive())
        self.assertEqual(threading.active_count(), self.threads)

    def test_the_field_names_the_phone_keys(self):
        self.assertIn("F2", self.module.PHONE_ROW)
        self.assertIn("SELECT", self.module.PHONE_ROW)
        self.assertLessEqual(len(self.module.PHONE_ROW), 76)


if __name__ == "__main__":
    unittest.main()
