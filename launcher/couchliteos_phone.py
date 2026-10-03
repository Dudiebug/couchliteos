"""TYPE ON PHONE: a one-time web form on the home network that fills one text field.

The launcher starts a `Session` on the box's own LAN address and an ephemeral port and
shows its URL as a QR code. `GET /t/<token>` serves one small form; one `POST` to the same
path hands its value to the launcher, which closes the session. Everything else is 404.
Only devices on the box's own subnets may use it (never the Tailscale range), the token is
random and compared in constant time, bodies are capped before they are read, sockets time
out quickly, and the whole session expires after five minutes. The user still confirms the
text in the field, so nothing is saved by the phone alone.
"""

from __future__ import annotations

import hmac
import html
import http.server
import ipaddress
import json
import secrets
import socket
import subprocess
import threading
import time
import urllib.parse
from collections.abc import Callable, Sequence

MAX_CHARS = 1024  # the longest value the phone may send, whatever the field allows
# A form-encoded value is at most 12 bytes a character (4 UTF-8 bytes, each %XX), plus "value=".
MAX_BODY = MAX_CHARS * 12 + 64
EXPIRY_SECONDS = 300.0
SOCKET_TIMEOUT = 3.0  # a slow or silent client loses its connection after this
MAX_CONNECTIONS = 8  # at once; more are dropped unanswered
TAILSCALE_NETWORKS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
Network = ipaddress.IPv4Network | ipaddress.IPv6Network

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CouchLiteOS</title>
<style>
body{{font-family:sans-serif;margin:24px;background:#111;color:#eee}}
input,button{{font-size:20px;width:100%;box-sizing:border-box;padding:12px;margin-top:12px}}
</style></head><body>
<h1>{title}</h1>
{body}
</body></html>
"""
FORM = """<form method="post" action="{action}" autocomplete="off">
<label for="value">{prompt}</label>
<input id="value" name="value" type="{kind}" maxlength="{limit}" autofocus
 autocapitalize="off" autocorrect="off" spellcheck="false" value="">
<button type="submit">SEND TO TV</button>
</form>"""


def lan_interfaces(runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> list[ipaddress.IPv4Interface]:
    """IPv4 addresses with global scope from `ip -j addr`, without loopback and Tailscale."""
    try:
        result = runner(["ip", "-j", "addr"], capture_output=True, text=True, timeout=5, check=False)
        links = json.loads(result.stdout) if result.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    found = []
    for link in links if isinstance(links, list) else []:
        if not isinstance(link, dict) or str(link.get("ifname", "")).startswith(("lo", "tailscale")):
            continue
        for info in link.get("addr_info") or []:
            if not isinstance(info, dict) or info.get("family") != "inet" or info.get("scope") != "global":
                continue
            try:
                interface = ipaddress.IPv4Interface(f"{info['local']}/{info['prefixlen']}")
            except (KeyError, ValueError):
                continue
            if not interface.ip.is_loopback and not in_tailscale(interface.ip):
                found.append(interface)
    return found


def in_tailscale(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(address in network for network in TAILSCALE_NETWORKS if network.version == address.version)


def allowed(client: str, networks: Sequence[Network]) -> bool:
    """True when `client` is on one of the box's own subnets and not a Tailscale address."""
    try:
        address = ipaddress.ip_address(client.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if in_tailscale(address):
        return False
    return any(address in network for network in networks if network.version == address.version)


def qr_lines(url: str, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> list[str]:
    """The URL as a QR code in half-block characters, or [] when qrencode is missing or fails."""
    try:
        result = runner(["qrencode", "-t", "UTF8", "-m", "1", url], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True  # server_close() waits for every request thread
    session: "Session"

    def __init__(self, *args, **kwargs) -> None:
        self._connections: set[socket.socket] = set()
        self._connections_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def verify_request(self, request, client_address) -> bool:
        with self._connections_lock:
            if len(self._connections) >= MAX_CONNECTIONS:
                return False
            self._connections.add(request)
        return True

    def shutdown_request(self, request) -> None:
        with self._connections_lock:
            self._connections.discard(request)
        super().shutdown_request(request)

    def handle_error(self, request, client_address) -> None:
        pass  # a client that hung up or was cut off by close(); nothing to report

    def drop_connections(self) -> None:
        """Cut every open connection, so a client sending slowly cannot hold close() up."""
        with self._connections_lock:
            open_now = list(self._connections)
        for connection in open_now:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class _Handler(http.server.BaseHTTPRequestHandler):
    server: _Server
    timeout = SOCKET_TIMEOUT
    server_version = "CouchLiteOS"
    sys_version = ""

    def log_message(self, *_args) -> None:  # the URL holds the token; keep it out of the journal
        pass

    def _send(self, status: int, title: str, body: str) -> None:
        page = PAGE.format(title=html.escape(title), body=body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(page)
        self.close_connection = True

    def _not_found(self) -> None:
        self._send(404, "NOT FOUND", "")

    def _open(self) -> bool:
        """The request is from the home network, for this session's path, while it is still open."""
        session = self.server.session
        path = urllib.parse.urlsplit(self.path).path
        return (
            allowed(self.client_address[0], session.networks)
            and session.accepting()
            and hmac.compare_digest(path.encode("utf-8", "replace"), session.path.encode("ascii"))
        )

    def _form(self, status: int = 200, note: str = "") -> None:
        session = self.server.session
        form = FORM.format(
            action=html.escape(session.path), prompt=html.escape(session.prompt),
            kind="password" if session.masked else "text", limit=session.limit,
        )
        self._send(status, session.title, (f"<p>{html.escape(note)}</p>" if note else "") + form)

    def do_GET(self) -> None:
        if not self._open():
            return self._not_found()
        self._form()

    def do_POST(self) -> None:
        if not self._open():
            return self._not_found()
        length = self.headers.get("Content-Length", "")
        kind = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if self.headers.get("Transfer-Encoding") or not length.isdigit():
            return self._form(411, "SEND AGAIN")
        if int(length) > MAX_BODY:  # refused before a byte of the body is read
            return self._form(413, "TOO LONG")
        if kind != "application/x-www-form-urlencoded":
            return self._form(415, "SEND AGAIN")
        body = self.rfile.read(int(length))
        try:
            fields = urllib.parse.parse_qsl(
                body.decode("ascii"), keep_blank_values=True, strict_parsing=True, max_num_fields=1,
                encoding="utf-8", errors="strict",
            )
        except (UnicodeError, ValueError):
            return self._form(400, "SEND AGAIN")
        session = self.server.session
        if len(fields) != 1 or fields[0][0] != "value":
            return self._form(400, "SEND AGAIN")
        value = fields[0][1]
        if len(value) > session.limit:
            return self._form(413, f"TOO LONG: AT MOST {session.limit} CHARACTERS")
        if not all(character.isprintable() for character in value):
            return self._form(400, "LETTERS, NUMBERS AND SYMBOLS ONLY")
        if not session._deliver(value):
            return self._not_found()
        self._send(200, "SENT", "<p>SENT. CHECK THE TV AND PRESS A / CROSS OR ENTER TO KEEP IT.</p>")

    def do_HEAD(self) -> None:
        self._not_found()

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD


class Session:
    """One phone form for one text field. Always `close()` it (or use it as a context manager)."""

    def __init__(
        self,
        bind_ip: str,
        clock: Callable[[], float] = time.monotonic,
        token: str | None = None,
        *,
        networks: Sequence[Network] | None = None,
        title: str = "",
        prompt: str = "",
        limit: int = MAX_CHARS,
        masked: bool = False,
        expiry: float = EXPIRY_SECONDS,
    ) -> None:
        self.clock = clock
        self.token = token or secrets.token_urlsafe(16)
        self.path = f"/t/{self.token}"
        address = ipaddress.ip_address(bind_ip)
        # Requests are taken from the box's own subnets (all of them, from `ip -j addr`, by default).
        self.networks = list(networks) if networks is not None else [i.network for i in lan_interfaces()]
        self.title = title or "COUCHLITEOS"
        self.prompt = prompt or "TEXT"
        self.limit = max(0, min(limit, MAX_CHARS))
        self.masked = masked
        self.deadline = clock() + expiry
        self._lock = threading.Lock()
        self._value: str | None = None
        self._taken = False
        self._closed = False
        self._server = _Server((bind_ip, 0), _Handler)
        self._server.session = self
        self.port = self._server.server_address[1]
        host = f"[{bind_ip}]" if address.version == 6 else bind_ip
        self.url = f"http://{host}:{self.port}{self.path}"
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.2}, name="couchliteos-phone", daemon=True
        )
        self._thread.start()

    def expired(self) -> bool:
        return self.clock() >= self.deadline

    def accepting(self) -> bool:
        with self._lock:
            return not self._closed and self._value is None and not self.expired()

    def _deliver(self, value: str) -> bool:
        with self._lock:  # single use: the first accepted value wins
            if self._closed or self._value is not None or self.expired():
                return False
            self._value = value
            return True

    def poll(self) -> str | None:
        """The value the phone sent, once; the session closes as soon as it has been taken."""
        with self._lock:
            value, taken = self._value, self._taken
            if value is not None:
                self._taken = True
        if value is None or taken:
            if self.expired():
                self.close()
            return None
        self.close()
        return value

    def close(self) -> None:
        with self._lock:
            if self._closed and not self._thread.is_alive():
                return
            self._closed = True
        self._server.shutdown()
        self._server.drop_connections()
        self._server.server_close()
        self._thread.join()

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
