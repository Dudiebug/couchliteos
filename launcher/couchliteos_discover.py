"""Find gaming PCs on the local network: Sunshine and GeForce Experience announce `_nvstream._tcp.local`.

One mDNS question is sent as an RFC 6762 section 6.7 "legacy unicast" query. A query whose source port is
not 5353 is answered by unicast, straight to that port, so this joins no multicast group, binds no
well-known port, and needs no daemon (no avahi). Standard library only: `socket` and `struct`.

`Search` is the live search, one short `poll()` at a time so a screen can keep drawing and watching the
B button; `find_pcs()` runs one to the end. `parse_response()` is the pure part: bytes in, PCs out, and
whatever arrives (it comes from the LAN) can only make it return less, never raise.
"""

from __future__ import annotations

import ipaddress
import random
import socket
import struct
import subprocess
import time
from collections.abc import Callable, Iterable
from typing import NamedTuple

MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
SERVICE = ("_nvstream", "_tcp", "local")
DEFAULT_PORT = 47989  # Sunshine / GameStream HTTP port; the SRV record carries the real one

LISTEN_SECONDS = 3.0  # how long replies are collected
RESEND_SECONDS = 1.0  # Wi-Fi drops multicast now and then, so the question is repeated (RFC 6762 section 5.2)
SLICE_SECONDS = 0.1  # one poll() blocks this long at most
MAX_PACKET = 9000  # RFC 6762 section 17: a packet may use the interface MTU, and a jumbo frame is 9000
MAX_RECORDS = 200  # more than a real response carries; bounds the work a hostile packet can cause
MAX_NAME = 255  # RFC 1035: the longest name
MAX_POINTERS = 16  # a name of a few labels needs one or two jumps
HEADER_SIZE = 12
VIRTUAL_INTERFACES = ("lo", "tailscale", "docker", "virbr", "veth", "br-")  # mDNS does not cross them

TYPE_A, TYPE_PTR, TYPE_TXT, TYPE_SRV = 1, 12, 16, 33
CLASS_IN = 1
NO_NETWORK = "NO NETWORK"


class DiscoveryError(Exception):
    """The search could not start; the message is meant for the user."""


class FoundPC(NamedTuple):
    name: str
    address: str
    port: int = DEFAULT_PORT

    @property
    def target(self) -> str:
        """What `moonlight pair` is given: the address, plus the port only when it is not the default."""
        return self.address if self.port == DEFAULT_PORT else f"{self.address}:{self.port}"


# ---------------------------------------------------------------- the question


def encode_name(labels: tuple[str, ...]) -> bytes:
    out = b""
    for label in labels:
        raw = label.encode("utf-8")
        if not 0 < len(raw) < 64:
            raise ValueError("a DNS label is 1 to 63 bytes")
        out += bytes([len(raw)]) + raw
    return out + b"\0"


def build_query(query_id: int = 0, service: tuple[str, ...] = SERVICE) -> bytes:
    """A DNS message with no flags and one question: PTR (every instance of `service`), class IN.

    The question has no "unicast response" bit: the source port is what asks for a unicast reply."""
    header = struct.pack("!6H", query_id & 0xFFFF, 0, 1, 0, 0, 0)
    return header + encode_name(service) + struct.pack("!2H", TYPE_PTR, CLASS_IN)


# ---------------------------------------------------------------- the answer


class _Malformed(Exception):
    """A packet that does not follow RFC 1035; never leaves this module."""


class _Record(NamedTuple):
    name: tuple[str, ...]
    rtype: int
    data: object  # PTR: name labels; SRV: (port, target labels); A: dotted address; TXT: tuple of bytes


def read_name(data: bytes, offset: int) -> tuple[tuple[str, ...], int]:
    """The labels of the name at `offset`, and the offset just after it in the record.

    A name may end in a compression pointer to an earlier name. Each target is followed once, so a
    pointer loop (or a pointer at itself) is refused instead of spinning."""
    labels: list[str] = []
    after: int | None = None
    visited: set[int] = set()
    length_so_far = 0
    position = offset
    while True:
        if position >= len(data):
            raise _Malformed("a name runs past the end of the packet")
        length = data[position]
        if length == 0:
            return tuple(labels), position + 1 if after is None else after
        if length & 0xC0 == 0xC0:
            if position + 1 >= len(data):
                raise _Malformed("a pointer is cut off")
            target = (length & 0x3F) << 8 | data[position + 1]
            if after is None:
                after = position + 2
            if target < HEADER_SIZE or target in visited or len(visited) >= MAX_POINTERS:
                raise _Malformed("a compression pointer loops or points into the header")
            visited.add(target)
            position = target
        elif length & 0xC0:
            raise _Malformed("unsupported label type")
        else:
            start = position + 1
            if start + length > len(data):
                raise _Malformed("a label runs past the end of the packet")
            length_so_far += length + 1
            if length_so_far > MAX_NAME:
                raise _Malformed("a name is too long")
            labels.append(data[start:start + length].decode("utf-8", "replace"))
            position = start + length


def _rdata(data: bytes, rtype: int, start: int, end: int) -> object | None:
    """The decoded data of a record we use, None for any other type. Raises _Malformed when damaged."""
    if rtype == TYPE_A:
        if end - start != 4:
            raise _Malformed("an A record is four bytes")
        return str(ipaddress.IPv4Address(data[start:end]))
    if rtype == TYPE_PTR:
        return read_name(data, start)[0]
    if rtype == TYPE_SRV:
        if end - start < 7:
            raise _Malformed("an SRV record is cut off")
        port = struct.unpack_from("!H", data, start + 4)[0]
        return port, read_name(data, start + 6)[0]
    if rtype == TYPE_TXT:
        strings: list[bytes] = []
        position = start
        while position < end:
            size = data[position]
            if position + 1 + size > end:
                raise _Malformed("a TXT string runs past its record")
            strings.append(data[position + 1:position + 1 + size])
            position += 1 + size
        return tuple(strings)
    return None


def parse_records(data: bytes) -> list[_Record]:
    """The A, PTR, SRV and TXT records of a DNS response, from every section.

    Records read before damage in the packet (a cut-off tail, a bad name) are kept; one record with
    bad data is skipped on its own, because its length says where the next one starts."""
    if len(data) < HEADER_SIZE:
        return []
    _query_id, flags, questions, answers, authority, additional = struct.unpack_from("!6H", data)
    if not flags & 0x8000 or flags & 0x780F:  # not a response, or an opcode/rcode other than 0
        return []
    total = answers + authority + additional
    if questions > MAX_RECORDS or total > MAX_RECORDS:
        return []
    records: list[_Record] = []
    offset = HEADER_SIZE
    try:
        for _ in range(questions):
            offset = read_name(data, offset)[1] + 4  # type and class
            if offset > len(data):
                raise _Malformed("a question is cut off")
        for _ in range(total):
            name, offset = read_name(data, offset)
            if offset + 10 > len(data):
                raise _Malformed("a record header is cut off")
            rtype, rclass, _ttl, length = struct.unpack_from("!HHIH", data, offset)
            start, offset = offset + 10, offset + 10 + length
            if offset > len(data):
                raise _Malformed("a record runs past the end of the packet")
            if rclass & 0x7FFF != CLASS_IN:  # the top bit is mDNS's cache-flush flag
                continue
            try:
                value = _rdata(data, rtype, start, offset)
            except (_Malformed, ValueError, struct.error):
                continue
            if value is not None:
                records.append(_Record(name, rtype, value))
    except _Malformed:
        pass
    return records


def _folded(labels: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(label.casefold() for label in labels)


def _is_instance(labels: tuple[str, ...]) -> bool:
    """`<instance>._nvstream._tcp.local`."""
    return len(labels) > len(SERVICE) and _folded(labels[-len(SERVICE):]) == _folded(SERVICE)


def usable_address(text: str) -> bool:
    """A unicast IPv4 address a PC can really have; refuses 0.0.0.0, loopback, multicast, broadcast."""
    try:
        address = ipaddress.IPv4Address(text)
    except ValueError:
        return False
    return not (address.is_unspecified or address.is_loopback or address.is_multicast or address.is_reserved)


def parse_response(data: bytes, source: str = "") -> list[FoundPC]:
    """The gaming PCs one mDNS response describes. Never raises."""
    return [pc for pc, _from_srv in _parse(data, source)]


def _parse(data: bytes, source: str = "") -> list[tuple[FoundPC, bool]]:
    """parse_response, each PC with whether its port came from an SRV record (False: a PTR alone).

    The SRV record gives the port and the name of the host, whose A record gives the address. A
    response without a usable A record falls back to `source`, the address it came from: a legacy
    unicast reply comes from the PC itself."""
    instances: dict[tuple[str, ...], tuple[str, ...]] = {}
    services: dict[tuple[str, ...], tuple[int, tuple[str, ...]]] = {}
    addresses: dict[tuple[str, ...], list[str]] = {}
    for record in parse_records(data):
        if record.rtype == TYPE_PTR and _folded(record.name) == _folded(SERVICE):
            if _is_instance(record.data):  # type: ignore[arg-type]
                instances.setdefault(_folded(record.data), record.data)  # type: ignore[arg-type]
        elif record.rtype == TYPE_SRV:
            if _is_instance(record.name):
                instances.setdefault(_folded(record.name), record.name)
                services.setdefault(_folded(record.name), record.data)  # type: ignore[arg-type]
        elif record.rtype == TYPE_A and usable_address(record.data):  # type: ignore[arg-type]
            addresses.setdefault(_folded(record.name), []).append(record.data)  # type: ignore[arg-type]
    found: list[tuple[FoundPC, bool]] = []
    for key, labels in instances.items():
        port, host = services.get(key, (DEFAULT_PORT, ()))
        port = port if 0 < port < 65536 else DEFAULT_PORT
        candidates = addresses.get(_folded(host), [])
        if source in candidates:  # a PC announces every adapter; the one that answered is on this LAN
            candidates = [source]
        else:  # otherwise a 169.254 fallback is never the one to use, unless it is all there is
            candidates = [item for item in candidates if not ipaddress.IPv4Address(item).is_link_local] or candidates
        candidates = candidates or ([source] if usable_address(source) else [])
        name = ".".join(labels[:-len(SERVICE)]).strip()
        found.extend((FoundPC(name or address, address, port), key in services) for address in candidates)
    return found


def _sort_key(pc: FoundPC) -> tuple[str, str, int, int]:
    return pc.name.casefold(), pc.name, int(ipaddress.IPv4Address(pc.address)), pc.port


def tidy(found: Iterable[FoundPC]) -> list[FoundPC]:
    """Each PC once, sorted by name (then address)."""
    return sorted(set(found), key=_sort_key)


# ---------------------------------------------------------------- the search


def interface_addresses(output: str) -> list[str]:
    """IPv4 addresses of the interfaces that can reach the LAN, from `ip -brief -4 address show up`.

    Loopback, VPN and container interfaces are left out: multicast does not cross them."""
    found: list[str] = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 3 or fields[0].startswith(VIRTUAL_INTERFACES):
            continue
        for item in fields[2:]:
            try:
                interface = ipaddress.ip_interface(item)
            except ValueError:
                continue
            address = str(interface.ip)
            if interface.version == 4 and usable_address(address) and address not in found:
                found.append(address)
    return found


def local_addresses(run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> list[str]:
    try:
        output = run(
            ["ip", "-brief", "-4", "address", "show", "up"],
            text=True, capture_output=True, check=False, timeout=3,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return interface_addresses(output or "")


class Search:
    """One question, repeated, and the replies to it, collected a slice at a time.

    Creating it opens the socket and sends the first question; it raises DiscoveryError when the
    network cannot be used at all. Call `poll()` until it returns True, read `results()`, and
    `close()` (polling to the end closes it too)."""

    def __init__(
        self,
        *,
        listen: float = LISTEN_SECONDS,
        addresses: Callable[[], list[str]] = local_addresses,
        socket_factory: Callable[..., socket.socket] = socket.socket,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.clock = clock
        self.query = build_query(random.randrange(1, 0x10000))
        self.interfaces = addresses() or [""]  # "" = whatever interface the default route uses
        self.found: set[FoundPC] = set()
        self.from_srv: set[FoundPC] = set()  # found with their SRV record: the real port
        self.finished = False
        try:
            self.sock = socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
        except OSError as error:
            raise DiscoveryError(f"COULD NOT OPEN A NETWORK CONNECTION: {error.strerror or error}".upper()) from error
        try:
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)  # RFC 6762 section 11
            self.sock.bind(("", 0))  # a random port, never 5353: that is what makes this a legacy unicast query
        except OSError as error:
            self.sock.close()
            raise DiscoveryError(f"COULD NOT OPEN A NETWORK CONNECTION: {error.strerror or error}".upper()) from error
        start = clock()
        self.deadline = start + listen
        self.next_send = start + RESEND_SECONDS
        if not self.send():
            self.close()
            raise DiscoveryError(NO_NETWORK)

    def send(self) -> int:
        """Ask on every interface; returns on how many the datagram could be sent."""
        sent = 0
        for address in self.interfaces:
            try:
                if address:
                    self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
                self.sock.sendto(self.query, (MDNS_GROUP, MDNS_PORT))
                sent += 1
            except OSError:
                continue
        return sent

    def poll(self, seconds: float = SLICE_SECONDS) -> bool:
        """Collect replies for up to `seconds`; True when the listening time is over."""
        slice_end = self.clock() + seconds
        while not self.finished:
            now = self.clock()
            if now >= self.deadline:
                self.close()
            elif now >= self.next_send:
                self.send()
                self.next_send = now + RESEND_SECONDS
            elif now >= slice_end:
                break
            else:
                self.receive(min(slice_end, self.next_send, self.deadline) - now)
        return self.finished

    def receive(self, timeout: float) -> None:
        try:
            self.sock.settimeout(timeout)
            data, sender = self.sock.recvfrom(MAX_PACKET)
        except socket.timeout:
            return
        except OSError:
            self.close()  # the socket is gone; what was collected so far is the result
            return
        for pc, from_srv in _parse(data, str(sender[0])):
            self.found.add(pc)
            if from_srv:
                self.from_srv.add(pc)

    def results(self) -> list[FoundPC]:
        """A PC first heard of by a PTR alone (listed on the default port) is dropped once its SRV
        record, in a later packet, has named it with its real port."""
        named = {pc.name.casefold() for pc in self.from_srv}
        return tidy(pc for pc in self.found if pc in self.from_srv or pc.name.casefold() not in named)

    def close(self) -> None:
        self.finished = True
        try:
            self.sock.close()
        except OSError:
            pass


def find_pcs(**options: object) -> list[FoundPC]:
    """Search to the end: the PCs found, sorted. Raises DiscoveryError when the network is unusable."""
    search = Search(**options)  # type: ignore[arg-type]
    try:
        while not search.poll():
            pass
        return search.results()
    finally:
        search.close()
