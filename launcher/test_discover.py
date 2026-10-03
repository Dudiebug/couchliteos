"""FIND GAMING PCS: the mDNS query, the response parser, the search, and the wizard screens.

The packets are built by hand (a small DNS writer below, plus one packet spelled out byte by byte, laid
out the way Avahi and Bonjour answer a legacy unicast PTR question). Only the socket is faked, never the
parser: the search tests feed packets through `Search` and the screen tests use a real `Search`.
Shares the wizard fakes in test_setup.py (only `base.<name>` is used, so its tests are not collected twice).
"""

import errno
import pathlib
import random
import socket
import struct
import tempfile
import unittest
from unittest import mock

import couchliteos_discover as discover
import couchliteos_setup as setup
import couchliteos_stream as stream
import test_setup as base
import test_stream as base_stream

SERVICE = ("_nvstream", "_tcp", "local")
ANSWER, AUTHORITY, ADDITIONAL = 1, 2, 3


# --------------------------------------------------------------- packets

class Dns:
    """Writes a DNS response, with RFC 1035 name compression the way responders do it."""

    def __init__(self, query_id=0x1A2B, flags=0x8400, compress=True):
        self.query_id, self.flags, self.compress = query_id, flags, compress
        self.body = bytearray()
        self.seen = {}
        self.counts = [0, 0, 0, 0]

    def name(self, labels, at):
        out = bytearray()
        for index in range(len(labels)):
            key = tuple(label.lower() for label in labels[index:])
            if self.compress and key in self.seen:
                return bytes(out) + struct.pack("!H", 0xC000 | self.seen[key])
            if at + len(out) < 0x4000:
                self.seen.setdefault(key, at + len(out))
            raw = labels[index].encode("utf-8") if isinstance(labels[index], str) else labels[index]
            out += bytes([len(raw)]) + raw
        return bytes(out) + b"\0"

    def question(self, labels=SERVICE, rtype=12):
        self.body += self.name(labels, 12 + len(self.body)) + struct.pack("!HH", rtype, 1)
        self.counts[0] += 1
        return self

    def record(self, section, owner, rtype, rdata, flush=False, ttl=120):
        """`rdata` is bytes, or a function of the offset its data will start at (for names inside it)."""
        at = 12 + len(self.body)
        owner_bytes = self.name(owner, at)
        data = rdata(at + len(owner_bytes) + 10) if callable(rdata) else rdata
        self.body += owner_bytes + struct.pack("!HHIH", rtype, 0x8001 if flush else 1, ttl, len(data)) + data
        self.counts[section] += 1
        return self

    def ptr(self, section, owner, target):
        return self.record(section, owner, 12, lambda at: self.name(target, at))

    def srv(self, section, owner, target, port=47989):
        return self.record(
            section, owner, 33, lambda at: struct.pack("!3H", 0, 0, port) + self.name(target, at + 6), flush=True
        )

    def txt(self, section, owner, *strings):
        data = b"".join(bytes([len(item)]) + item for item in strings) or b"\0"
        return self.record(section, owner, 16, data, flush=True)

    def a(self, section, owner, address):
        return self.record(section, owner, 1, socket.inet_aton(address), flush=True)

    def aaaa(self, section, owner, address="fe80::1"):
        return self.record(section, owner, 28, socket.inet_pton(socket.AF_INET6, address), flush=True)

    def pack(self):
        return struct.pack("!6H", self.query_id, self.flags, *self.counts) + bytes(self.body)


def reply(name="DESKTOP-ABC", address="192.168.1.20", port=47989, host=None, **options):
    """What Avahi sends back to a legacy unicast query: the question repeated, PTR as the answer,
    SRV, TXT and A as additional records."""
    host = host or (name.lower() + ".local").split(".")
    instance = (name, *SERVICE)
    return (
        Dns(**options).question()
        .ptr(ANSWER, SERVICE, instance)
        .srv(ADDITIONAL, instance, tuple(host), port)
        .txt(ADDITIONAL, instance)
        .a(ADDITIONAL, tuple(host), address)
        .aaaa(ADDITIONAL, tuple(host))
        .pack()
    )


# One response spelled out byte by byte, so a mistake shared by the writer above and the parser cannot hide.
# Offsets: question name at 12 ("local" at 27), PTR answer at 38 (its target "DESKTOP-ABC._nvstream..." at 50),
# SRV at 64 (its target "desktop-abc.local" at 82), TXT at 96, A at 109.
HAND_MADE = bytes.fromhex(
    "1a2b 8400 0001 0001 0000 0003"  # id, flags (response, authoritative), 1 question, 1 answer, 3 additional
    "095f6e7673747265616d 045f746370 056c6f63616c 00 000c 0001"  # _nvstream._tcp.local PTR IN
    "c00c 000c 0001 00000078 000e 0b4445534b544f502d414243 c00c"  # answer: PTR to DESKTOP-ABC._nvstream._tcp.local
    "c032 0021 8001 00000078 0014 0000 0000 bb75 0b6465736b746f702d616263 c01b"  # SRV, port 47989, desktop-abc.local
    "c032 0010 8001 00001194 0001 00"  # TXT, one empty string
    "c052 0001 8001 00000078 0004 c0a80114"  # A 192.168.1.20 for desktop-abc.local
)


class QueryPacketTest(unittest.TestCase):
    def test_the_question_is_one_ptr_query_for_the_nvstream_service(self):
        packet = discover.build_query(0x1234)
        self.assertEqual(
            packet,
            bytes.fromhex("1234 0000 0001 0000 0000 0000")  # id, no flags, one question, nothing else
            + b"\x09_nvstream\x04_tcp\x05local\x00"
            + bytes.fromhex("000c 0001"),  # PTR, class IN
        )

    def test_it_is_a_standard_query_without_the_unicast_response_bit(self):
        _id, flags, questions, answers, authority, additional = struct.unpack("!6H", discover.build_query(7)[:12])
        self.assertEqual((flags, questions, answers, authority, additional), (0, 1, 0, 0, 0))
        self.assertEqual(discover.build_query(7)[-2:], b"\x00\x01")  # class IN, the top bit (QU) is clear

    def test_the_id_is_sixteen_bits(self):
        self.assertEqual(discover.build_query(0x1FFFF)[:2], b"\xff\xff")

    def test_a_label_must_be_one_to_sixty_three_bytes(self):
        for labels in (("",), ("x" * 64,)):
            with self.assertRaises(ValueError):
                discover.encode_name(labels)


class ParseResponseTest(unittest.TestCase):
    def test_ptr_srv_txt_and_a_make_one_pc(self):
        self.assertEqual(discover.parse_response(reply()), [("DESKTOP-ABC", "192.168.1.20", 47989)])

    def test_a_packet_laid_out_by_hand_parses_the_same(self):
        self.assertEqual(discover.parse_response(HAND_MADE), [("DESKTOP-ABC", "192.168.1.20", 47989)])

    def test_the_port_comes_from_the_srv_record(self):
        found = discover.parse_response(reply(port=48000))
        self.assertEqual(found, [("DESKTOP-ABC", "192.168.1.20", 48000)])
        self.assertEqual(found[0].target, "192.168.1.20:48000")

    def test_the_default_port_is_not_part_of_the_pairing_target(self):
        self.assertEqual(discover.parse_response(reply())[0].target, "192.168.1.20")

    def test_it_works_without_name_compression_and_without_the_question(self):
        packet = reply(compress=False)
        self.assertEqual(discover.parse_response(packet), [("DESKTOP-ABC", "192.168.1.20", 47989)])
        instance = ("DESKTOP-ABC", *SERVICE)
        multicast = Dns(flags=0x8400).ptr(ANSWER, SERVICE, instance).srv(ADDITIONAL, instance, ("pc", "local")) \
            .a(ADDITIONAL, ("pc", "local"), "10.0.0.7").pack()
        self.assertEqual(discover.parse_response(multicast), [("DESKTOP-ABC", "10.0.0.7", 47989)])

    def test_records_in_the_answer_section_are_read_too(self):
        instance = ("Den PC", *SERVICE)
        packet = Dns().ptr(ANSWER, SERVICE, instance).srv(ANSWER, instance, ("den", "local")) \
            .a(ANSWER, ("den", "local"), "192.168.0.9").pack()
        self.assertEqual(discover.parse_response(packet), [("Den PC", "192.168.0.9", 47989)])

    def test_names_are_matched_without_regard_to_case(self):
        instance = ("Den", "_NVSTREAM", "_Tcp", "LOCAL")
        packet = Dns(compress=False).ptr(ANSWER, ("_nvStream", "_tcp", "Local"), instance) \
            .srv(ADDITIONAL, instance, ("Den-PC", "local")).a(ADDITIONAL, ("DEN-pc", "LOCAL"), "192.168.0.9").pack()
        self.assertEqual(discover.parse_response(packet), [("Den", "192.168.0.9", 47989)])

    def test_an_instance_name_may_hold_spaces_dots_and_non_ascii(self):
        for name in ("Living Room PC (Sunshine)", "my.gaming.pc", "Büro-PC", "日本"):
            with self.subTest(name=name):
                self.assertEqual(discover.parse_response(reply(name))[0].name, name)

    def test_a_label_that_is_not_utf8_is_replaced_not_fatal(self):
        found = discover.parse_response(reply(b"PC\xff\xfe", host=("pc", "local")))
        self.assertEqual(found[0].name, "PC��")

    def test_a_response_without_an_a_record_uses_the_address_it_came_from(self):
        instance = ("PC", *SERVICE)
        packet = Dns().question().ptr(ANSWER, SERVICE, instance).srv(ADDITIONAL, instance, ("pc", "local"), 47989).pack()
        self.assertEqual(discover.parse_response(packet, "192.168.1.33"), [("PC", "192.168.1.33", 47989)])
        self.assertEqual(discover.parse_response(packet), [])
        self.assertEqual(discover.parse_response(packet, "0.0.0.0"), [])

    def test_a_ptr_alone_is_a_pc_on_the_default_port(self):
        packet = Dns().ptr(ANSWER, SERVICE, ("PC", *SERVICE)).pack()
        self.assertEqual(discover.parse_response(packet, "192.168.1.33"), [("PC", "192.168.1.33", 47989)])

    def test_an_address_a_pc_cannot_have_is_never_offered(self):
        for address in ("0.0.0.0", "127.0.0.1", "224.0.0.251", "255.255.255.255"):
            with self.subTest(address=address):
                self.assertEqual(discover.parse_response(reply(address=address)), [])

    def test_a_link_local_address_is_fine(self):
        self.assertEqual(discover.parse_response(reply(address="169.254.8.8"))[0].address, "169.254.8.8")

    def test_a_pc_with_two_addresses_is_listed_at_both(self):
        instance = ("PC", *SERVICE)
        packet = Dns().ptr(ANSWER, SERVICE, instance).srv(ADDITIONAL, instance, ("pc", "local")) \
            .a(ADDITIONAL, ("pc", "local"), "192.168.1.20").a(ADDITIONAL, ("pc", "local"), "192.168.1.21").pack()
        self.assertEqual(
            [item.address for item in discover.parse_response(packet)], ["192.168.1.20", "192.168.1.21"]
        )

    def several_addresses(self, *addresses, source=""):
        instance = ("PC", *SERVICE)
        packet = Dns().ptr(ANSWER, SERVICE, instance).srv(ADDITIONAL, instance, ("pc", "local"))
        for address in addresses:
            packet.a(ADDITIONAL, ("pc", "local"), address)
        return [item.address for item in discover.parse_response(packet.pack(), source)]

    def test_a_pc_is_listed_at_the_address_it_answered_from_only(self):
        # A Windows PC announces every adapter: the LAN, Hyper-V/WSL, a VPN and a 169.254 fallback.
        addresses = ("172.20.0.1", "192.168.1.20", "10.8.0.2", "169.254.8.8")
        self.assertEqual(self.several_addresses(*addresses, source="192.168.1.20"), ["192.168.1.20"])

    def test_link_local_addresses_are_dropped_when_it_cannot_be_told_which_one_answered(self):
        self.assertEqual(self.several_addresses("169.254.8.8", "192.168.1.20"), ["192.168.1.20"])
        self.assertEqual(self.several_addresses("169.254.8.8", "192.168.1.20", source="192.168.1.99"), ["192.168.1.20"])

    def test_a_pc_that_only_has_a_link_local_address_is_still_listed(self):
        self.assertEqual(self.several_addresses("169.254.8.8", "169.254.9.9"), ["169.254.8.8", "169.254.9.9"])

    def test_another_service_is_ignored(self):
        other = ("_http", "_tcp", "local")
        packet = Dns().ptr(ANSWER, other, ("Printer", *other)).srv(ADDITIONAL, ("Printer", *other), ("p", "local")) \
            .a(ADDITIONAL, ("p", "local"), "192.168.1.9").pack()
        self.assertEqual(discover.parse_response(packet, "192.168.1.9"), [])

    def test_a_ptr_that_points_at_another_service_is_ignored(self):
        packet = Dns().ptr(ANSWER, SERVICE, ("Printer", "_http", "_tcp", "local")).pack()
        self.assertEqual(discover.parse_response(packet, "192.168.1.9"), [])

    def test_a_query_is_not_a_response(self):
        self.assertEqual(discover.parse_response(reply(flags=0x0000)), [])
        self.assertEqual(discover.parse_response(discover.build_query(1), "192.168.1.9"), [])

    def test_an_error_reply_or_a_different_opcode_is_ignored(self):
        self.assertEqual(discover.parse_response(reply(flags=0x8403)), [])  # NXDOMAIN
        self.assertEqual(discover.parse_response(reply(flags=0x8C00)), [])  # opcode 1

    def test_the_cache_flush_bit_does_not_hide_a_record(self):
        # reply() sets it on SRV, TXT and A: the hand-made packet and the written one both rely on it.
        self.assertEqual(len(discover.parse_response(reply())), 1)

    def test_records_of_another_class_are_ignored(self):
        packet = bytearray(HAND_MADE)
        packet[packet.index(bytes.fromhex("c052 0001 8001")) + 5] = 0x03  # the A record becomes class CH
        self.assertEqual(discover.parse_response(bytes(packet), ""), [])

    def test_the_txt_record_is_read_and_a_damaged_one_costs_only_itself(self):
        instance = ("PC", *SERVICE)
        broken = (
            Dns().question().ptr(ANSWER, SERVICE, instance).txt(ADDITIONAL, instance, b"txtvers=1", b"x")
            .record(ADDITIONAL, instance, 16, b"\x09abc")  # the length byte promises more than the record holds
            .srv(ADDITIONAL, instance, ("pc", "local")).a(ADDITIONAL, ("pc", "local"), "192.168.1.20").pack()
        )
        self.assertEqual(discover.parse_response(broken), [("PC", "192.168.1.20", 47989)])
        records = discover.parse_records(broken)
        self.assertEqual([r.data for r in records if r.rtype == discover.TYPE_TXT], [(b"txtvers=1", b"x")])

    def test_an_a_record_of_the_wrong_size_costs_only_itself(self):
        instance = ("PC", *SERVICE)
        packet = (
            Dns().ptr(ANSWER, SERVICE, instance).srv(ADDITIONAL, instance, ("pc", "local"))
            .record(ADDITIONAL, ("pc", "local"), 1, b"\x01\x02\x03", flush=True)
            .a(ADDITIONAL, ("pc", "local"), "192.168.1.20").pack()
        )
        self.assertEqual(discover.parse_response(packet), [("PC", "192.168.1.20", 47989)])

    def test_a_short_srv_record_is_skipped_and_the_port_falls_back(self):
        instance = ("PC", *SERVICE)
        packet = Dns().ptr(ANSWER, SERVICE, instance).record(ADDITIONAL, instance, 33, b"\x00\x00\x00") \
            .pack()
        self.assertEqual(discover.parse_response(packet, "192.168.1.20"), [("PC", "192.168.1.20", 47989)])

    def test_a_port_of_zero_falls_back_to_the_default(self):
        self.assertEqual(discover.parse_response(reply(port=0))[0].port, 47989)


class PcOrderTest(unittest.TestCase):
    def test_the_same_pc_twice_is_listed_once_and_the_list_is_sorted_by_name(self):
        packets = [
            reply("zeta", "192.168.1.30"), reply("Alpha", "192.168.1.20"), reply("zeta", "192.168.1.30"),
            reply("beta", "192.168.1.25"), reply("Alpha", "192.168.1.20"),
        ]
        found = discover.tidy([pc for packet in packets for pc in discover.parse_response(packet)])
        self.assertEqual([pc.name for pc in found], ["Alpha", "beta", "zeta"])

    def test_the_same_name_at_two_addresses_stays_two_entries_in_address_order(self):
        found = discover.tidy(
            discover.parse_response(reply("PC", "192.168.1.9")) + discover.parse_response(reply("PC", "192.168.1.10"))
        )
        self.assertEqual([pc.address for pc in found], ["192.168.1.9", "192.168.1.10"])

    def test_several_pcs_in_one_packet_are_all_found(self):
        dns = Dns().question().ptr(ANSWER, SERVICE, ("Den", *SERVICE)).ptr(ANSWER, SERVICE, ("Attic", *SERVICE))
        for name, address in (("Den", "192.168.1.10"), ("Attic", "192.168.1.11")):
            dns.srv(ADDITIONAL, (name, *SERVICE), (name.lower(), "local")).a(ADDITIONAL, (name.lower(), "local"), address)
        found = discover.tidy(discover.parse_response(dns.pack()))
        self.assertEqual(found, [("Attic", "192.168.1.11", 47989), ("Den", "192.168.1.10", 47989)])


class MalformedPacketTest(unittest.TestCase):
    def test_nothing_useful_in_junk(self):
        for data in (b"", b"\x00", b"\x00" * 11, b"\xff" * 12, b"\x84\x00" * 40, bytes(range(256))):
            with self.subTest(length=len(data)):
                self.assertEqual(discover.parse_response(data, "192.168.1.9"), [])

    def test_every_truncation_of_a_good_packet_is_survived_and_never_invents_a_pc(self):
        packet = reply()
        for length in range(len(packet)):
            with self.subTest(length=length):
                found = discover.parse_response(packet[:length], "192.168.1.20")
                self.assertTrue(set(found) <= {("DESKTOP-ABC", "192.168.1.20", 47989)}, found)

    def test_a_truncated_packet_keeps_the_records_read_before_the_cut(self):
        cut = HAND_MADE.index(bytes.fromhex("c052 0001"))  # everything but the A record
        self.assertEqual(discover.parse_response(HAND_MADE[:cut], "192.168.1.77"), [("DESKTOP-ABC", "192.168.1.77", 47989)])

    def test_the_header_may_promise_more_records_than_there_are(self):
        packet = bytearray(reply())
        packet[6:12] = struct.pack("!3H", 60000, 60000, 60000)
        self.assertEqual(discover.parse_response(bytes(packet), "192.168.1.9"), [])
        packet[4:6] = struct.pack("!H", 60000)
        self.assertEqual(discover.parse_response(bytes(packet), "192.168.1.9"), [])
        packet[4:12] = struct.pack("!4H", 1, 3, 0, 0)  # plausible counts, but the data is not
        self.assertIsInstance(discover.parse_response(bytes(packet)), list)

    def test_a_label_that_runs_past_the_packet(self):
        packet = struct.pack("!6H", 1, 0x8400, 0, 1, 0, 0) + b"\x3fshort"
        self.assertEqual(discover.parse_response(packet, "192.168.1.9"), [])

    def test_a_reserved_label_type(self):
        packet = struct.pack("!6H", 1, 0x8400, 0, 1, 0, 0) + b"\x40abc\x00" + struct.pack("!HHIH", 12, 1, 1, 0)
        self.assertEqual(discover.parse_response(packet, "192.168.1.9"), [])

    def test_a_name_longer_than_255_bytes(self):
        long_name = b"".join(b"\x3f" + b"a" * 63 for _ in range(5)) + b"\x00"
        packet = struct.pack("!6H", 1, 0x8400, 0, 1, 0, 0) + long_name + struct.pack("!HHIH", 12, 1, 1, 0)
        self.assertEqual(discover.parse_response(packet, "192.168.1.9"), [])

    def test_a_record_whose_length_runs_past_the_packet(self):
        packet = bytearray(HAND_MADE)
        at = packet.index(bytes.fromhex("c052 0001 8001 00000078")) + 10
        packet[at:at + 2] = struct.pack("!H", 400)  # the A record claims 400 bytes
        self.assertEqual(discover.parse_response(bytes(packet), ""), [])
        self.assertEqual(discover.parse_response(bytes(packet), "192.168.1.9"), [("DESKTOP-ABC", "192.168.1.9", 47989)])

    def test_random_bytes_never_raise(self):
        rng = random.Random(20260930)
        for _ in range(3000):
            body = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 200)))
            header = struct.pack("!6H", rng.randrange(65536), 0x8400, rng.randrange(3), rng.randrange(4), rng.randrange(2), rng.randrange(5))
            result = discover.parse_response(header + body, "192.168.1.9")
            self.assertIsInstance(result, list)

    def test_a_flipped_byte_anywhere_in_a_good_packet_never_raises(self):
        rng = random.Random(7)
        for packet in (reply(), HAND_MADE):
            for position in range(len(packet)):
                for _ in range(8):
                    damaged = bytearray(packet)
                    damaged[position] = rng.randrange(256)
                    self.assertIsInstance(discover.parse_response(bytes(damaged), "192.168.1.9"), list)


class CompressionLoopTest(unittest.TestCase):
    HEADER = struct.pack("!6H", 1, 0x8400, 0, 1, 0, 0)

    def test_a_pointer_to_itself(self):
        packet = self.HEADER + b"\xc0\x0c" + struct.pack("!HHIH", 12, 1, 1, 0)
        self.assertEqual(discover.parse_response(packet, "192.168.1.9"), [])

    def test_two_pointers_that_point_at_each_other(self):
        packet = self.HEADER + b"\xc0\x0e\xc0\x0c" + struct.pack("!HHIH", 12, 1, 1, 0)
        self.assertEqual(discover.parse_response(packet, "192.168.1.9"), [])

    def test_a_label_then_a_pointer_back_to_its_own_start(self):
        packet = self.HEADER + b"\x03abc\xc0\x0c" + struct.pack("!HHIH", 12, 1, 1, 0)
        self.assertEqual(discover.parse_response(packet, "192.168.1.9"), [])

    def test_a_pointer_into_the_header_or_past_the_end(self):
        for target in (0, 11, 0x3FFF):
            with self.subTest(target=target):
                packet = self.HEADER + struct.pack("!H", 0xC000 | target) + struct.pack("!HHIH", 12, 1, 1, 0)
                self.assertEqual(discover.parse_response(packet, "192.168.1.9"), [])

    def test_a_long_chain_of_pointers_is_given_up_on(self):
        chain = b"".join(struct.pack("!H", 0xC000 | (12 + 2 * (index + 1))) for index in range(40)) + b"\x00"
        packet = self.HEADER + chain + struct.pack("!HHIH", 12, 1, 1, 0)
        self.assertEqual(discover.parse_response(packet, "192.168.1.9"), [])

    def test_a_ptr_whose_target_loops_costs_only_that_record(self):
        instance = ("PC", *SERVICE)
        dns = Dns().question().ptr(ANSWER, SERVICE, instance)
        dns.record(ANSWER, SERVICE, 12, lambda at: struct.pack("!H", 0xC000 | at))  # its data points at itself
        dns.srv(ADDITIONAL, instance, ("pc", "local")).a(ADDITIONAL, ("pc", "local"), "192.168.1.20")
        self.assertEqual(discover.parse_response(dns.pack()), [("PC", "192.168.1.20", 47989)])

    def test_the_visited_pointers_are_per_name(self):
        # Two names that share the same suffix pointer are not a loop.
        self.assertEqual(len(discover.parse_response(reply())), 1)
        self.assertEqual(len(discover.parse_response(HAND_MADE)), 1)


# --------------------------------------------------------------- the socket boundary

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeSocket:
    """A UDP socket on a scripted network. `replies` are (seconds after the search started, packet,
    (sender address, port)); time only moves when the search waits for a packet."""

    def __init__(self, clock, replies=(), fail_send=(), fail_all_sends=False, bind_error=None, recv_error=None):
        self.clock = clock
        self.start = clock.now
        self.replies = sorted(replies, key=lambda item: item[0])
        self.fail_send, self.fail_all_sends, self.bind_error, self.recv_error = fail_send, fail_all_sends, bind_error, recv_error
        self.options, self.sent, self.bound, self.closed, self.timeout, self.interface = [], [], None, False, None, None

    def setsockopt(self, level, name, value):
        self.options.append((level, name, value))
        if (level, name) == (socket.IPPROTO_IP, socket.IP_MULTICAST_IF):
            self.interface = socket.inet_ntoa(value)

    def bind(self, address):
        if self.bind_error:
            raise self.bind_error
        self.bound = address

    def settimeout(self, value):
        self.timeout = value

    def sendto(self, data, address):
        if self.fail_all_sends or self.interface in self.fail_send:
            raise OSError(errno.ENETUNREACH, "Network is unreachable")
        self.sent.append((round(self.clock.now - self.start, 6), data, address, self.interface))

    def recvfrom(self, size):
        if self.recv_error and self.clock.now - self.start >= self.recv_error[0]:
            raise self.recv_error[1]
        end = self.clock.now + self.timeout
        if self.replies and self.start + self.replies[0][0] <= end:
            when, data, sender = self.replies.pop(0)
            self.clock.now = max(self.clock.now, self.start + when)
            return data, sender
        self.clock.now = end
        raise socket.timeout("timed out")

    def close(self):
        self.closed = True


def make_search(replies=(), interfaces=("192.168.1.50",), listen=3.0, **faults):
    """(a Search on a fake network, its socket)."""
    clock = Clock()
    sock = FakeSocket(clock, replies, **faults)
    search = discover.Search(
        listen=listen, addresses=lambda: list(interfaces), socket_factory=lambda *args: sock, clock=clock
    )
    return search, sock


def from_pc(seconds, address="192.168.1.20", **options):
    return seconds, reply(address=address, **options), (address, 5353)


class QuerySendingTest(unittest.TestCase):
    def test_it_asks_the_mdns_group_from_a_random_port_not_5353(self):
        search, sock = make_search()
        self.assertEqual(sock.bound, ("", 0))
        self.assertEqual(len(sock.sent), 1)
        _when, data, address, interface = sock.sent[0]
        self.assertEqual(address, ("224.0.0.251", 5353))
        self.assertEqual(interface, "192.168.1.50")
        self.assertEqual(data[2:], discover.build_query(0)[2:])
        self.assertNotEqual(data[:2], b"\x00\x00")  # a legacy resolver uses a real id
        self.assertIn((socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255), sock.options)
        search.close()

    def test_it_never_joins_the_multicast_group(self):
        search, sock = make_search()
        search.close()
        self.assertEqual([name for _level, name, _value in sock.options if name == socket.IP_ADD_MEMBERSHIP], [])

    def test_the_question_is_repeated_every_second_until_the_time_is_up(self):
        search, sock = make_search()
        while not search.poll():
            pass
        self.assertEqual([when for when, *_rest in sock.sent], [0, 1.0, 2.0])
        self.assertTrue(sock.closed)

    def test_it_listens_for_about_three_seconds(self):
        search, sock = make_search()
        clock = search.clock
        while not search.poll():
            pass
        self.assertAlmostEqual(clock.now - sock.start, discover.LISTEN_SECONDS, places=6)
        self.assertTrue(2 <= discover.LISTEN_SECONDS <= 3)

    def test_every_interface_gets_the_question_so_wired_and_wifi_both_hear_it(self):
        search, sock = make_search(interfaces=("192.168.1.50", "10.0.0.8"))
        search.close()
        self.assertEqual([entry[3] for entry in sock.sent], ["192.168.1.50", "10.0.0.8"])

    def test_one_interface_that_cannot_send_does_not_stop_the_others(self):
        search, sock = make_search(interfaces=("192.168.1.50", "10.0.0.8"), fail_send=("192.168.1.50",))
        search.close()
        self.assertEqual([entry[3] for entry in sock.sent], ["10.0.0.8"])

    def test_without_interface_data_the_default_route_is_used(self):
        search, sock = make_search(interfaces=())
        search.close()
        self.assertEqual([entry[3] for entry in sock.sent], [None])
        self.assertFalse([name for _l, name, _v in sock.options if name == socket.IP_MULTICAST_IF])


class SearchResultTest(unittest.TestCase):
    def run_search(self, replies, **kwargs):
        search, sock = make_search(replies, **kwargs)
        try:
            while not search.poll():
                pass
            return search.results(), sock
        finally:
            search.close()

    def test_replies_are_parsed_deduplicated_and_sorted(self):
        replies = [
            from_pc(0.2, "192.168.1.30", name="zeta"), from_pc(0.3, "192.168.1.20", name="Alpha"),
            from_pc(1.1, "192.168.1.30", name="zeta"),  # the answer to the repeated question
            from_pc(1.2, "192.168.1.20", name="Alpha"),
            (1.5, b"garbage", ("192.168.1.99", 5353)),
            from_pc(2.5, "192.168.1.25", name="beta"),
        ]
        found, sock = self.run_search(replies)
        self.assertEqual(
            found, [("Alpha", "192.168.1.20", 47989), ("beta", "192.168.1.25", 47989), ("zeta", "192.168.1.30", 47989)]
        )
        self.assertTrue(sock.closed)

    def test_a_reply_after_the_listening_time_is_not_waited_for(self):
        found, _sock = self.run_search([from_pc(0.5, name="Early"), from_pc(3.5, "192.168.1.21", name="Late")])
        self.assertEqual([pc.name for pc in found], ["Early"])

    def test_the_sender_fills_in_for_a_missing_address_record(self):
        instance = ("PC", *SERVICE)
        packet = Dns().ptr(ANSWER, SERVICE, instance).srv(ADDITIONAL, instance, ("pc", "local")).pack()
        found, _sock = self.run_search([(0.1, packet, ("192.168.1.44", 5353))])
        self.assertEqual(found, [("PC", "192.168.1.44", 47989)])

    def test_nothing_answering_is_an_empty_list_not_an_error(self):
        found, sock = self.run_search([])
        self.assertEqual(found, [])
        self.assertTrue(sock.closed)

    def test_find_pcs_runs_a_search_to_the_end(self):
        clock = Clock()
        sock = FakeSocket(clock, [from_pc(0.4)])
        found = discover.find_pcs(addresses=lambda: ["192.168.1.50"], socket_factory=lambda *args: sock, clock=clock)
        self.assertEqual(found, [("DESKTOP-ABC", "192.168.1.20", 47989)])
        self.assertTrue(sock.closed)

    def test_results_grow_as_polling_goes_on(self):
        search, _sock = make_search([from_pc(0.25)])
        self.assertFalse(search.poll(0.1))
        self.assertEqual(search.results(), [])
        self.assertFalse(search.poll(0.5))
        self.assertEqual(len(search.results()), 1)
        search.close()

    def test_a_poll_after_the_end_does_nothing_and_close_can_be_repeated(self):
        search, sock = make_search()
        while not search.poll():
            pass
        sent = len(sock.sent)
        self.assertTrue(search.poll())
        search.close()
        search.close()
        self.assertEqual(len(sock.sent), sent)

    def test_a_socket_that_dies_while_listening_ends_the_search_with_what_it_has(self):
        search, sock = make_search(
            [from_pc(0.1, name="Got")], recv_error=(0.5, OSError(errno.EBADF, "Bad file descriptor"))
        )
        while not search.poll():
            pass
        self.assertEqual([pc.name for pc in search.results()], ["Got"])
        self.assertTrue(sock.closed)


class NoNetworkTest(unittest.TestCase):
    def test_no_socket_is_no_search(self):
        def refuse(*args):
            raise OSError(errno.EMFILE, "Too many open files")

        with self.assertRaises(discover.DiscoveryError) as caught:
            discover.Search(addresses=lambda: [], socket_factory=refuse)
        self.assertIn("TOO MANY OPEN FILES", str(caught.exception))

    def test_a_socket_that_cannot_bind_is_closed_and_reported(self):
        clock = Clock()
        sock = FakeSocket(clock, bind_error=OSError(errno.EACCES, "Permission denied"))
        with self.assertRaises(discover.DiscoveryError):
            discover.Search(addresses=lambda: [], socket_factory=lambda *a: sock, clock=clock)
        self.assertTrue(sock.closed)

    def test_when_every_send_fails_there_is_no_network(self):
        clock = Clock()
        sock = FakeSocket(clock, fail_all_sends=True)
        with self.assertRaises(discover.DiscoveryError) as caught:
            discover.Search(addresses=lambda: ["192.168.1.50"], socket_factory=lambda *a: sock, clock=clock)
        self.assertEqual(str(caught.exception), "NO NETWORK")
        self.assertTrue(sock.closed)

    def test_find_pcs_reports_the_same(self):
        clock = Clock()
        with self.assertRaises(discover.DiscoveryError):
            discover.find_pcs(
                addresses=lambda: [], socket_factory=lambda *a: FakeSocket(clock, fail_all_sends=True), clock=clock
            )

    def test_a_later_send_that_fails_is_not_an_error(self):
        # The cable is pulled after the first question went out: the answers so far still count.
        class Dying(FakeSocket):
            def sendto(self, data, address):
                if self.sent:
                    raise OSError(errno.ENETUNREACH, "Network is unreachable")
                super().sendto(data, address)

        clock = Clock()
        sock = Dying(clock, [from_pc(0.2)])
        found = discover.find_pcs(addresses=lambda: ["192.168.1.50"], socket_factory=lambda *a: sock, clock=clock)
        self.assertEqual(len(found), 1)


class InterfaceTest(unittest.TestCase):
    IP_BRIEF = (
        "lo               UNKNOWN        127.0.0.1/8\n"
        "enp2s0           UP             192.168.1.50/24\n"
        "wlan0            UP             10.0.0.8/24 169.254.3.4/16\n"
        "tailscale0       UNKNOWN        100.101.102.103/32\n"
        "docker0          DOWN           172.17.0.1/16\n"
        "br-1a2b3c        UP             172.18.0.1/16\n"
        "virbr0           UP             192.168.122.1/24\n"
        "veth12ab         UP             \n"
        "garbage\n"
    )

    def test_only_interfaces_that_can_reach_the_lan_are_asked(self):
        self.assertEqual(discover.interface_addresses(self.IP_BRIEF), ["192.168.1.50", "10.0.0.8", "169.254.3.4"])

    def test_the_same_address_is_listed_once(self):
        self.assertEqual(discover.interface_addresses("a UP 10.0.0.8/24\nb UP 10.0.0.8/24\n"), ["10.0.0.8"])

    def test_nothing_in_nothing_out(self):
        self.assertEqual(discover.interface_addresses(""), [])

    def test_ip_that_fails_to_run_means_the_default_route(self):
        def broken(*args, **kwargs):
            raise OSError("no such file")

        self.assertEqual(discover.local_addresses(broken), [])

    def test_ip_output_is_read_from_the_real_command_shape(self):
        run = mock.Mock(return_value=mock.Mock(stdout=self.IP_BRIEF))
        self.assertEqual(discover.local_addresses(run)[0], "192.168.1.50")
        self.assertEqual(run.call_args.args[0], ["ip", "-brief", "-4", "address", "show", "up"])


# --------------------------------------------------------------- the wizard screens

class SearchUI(base.FakeUI):
    """FakeUI whose wait() polls until the search is over, as the real screen does.
    `cancel` lists, per wait, whether B is pressed during it."""

    def __init__(self, *answers, cancel=()):
        super().__init__(*answers)
        self.cancel = list(cancel)

    def wait(self, title, lines, done, timeout, *, big=None):
        self.screens.append({"title": title, "lines": list(lines), "choices": [], "big": big})
        self.waits.append(title)
        if self.cancel and self.cancel.pop(0):
            return None
        for _ in range(1000):
            if done():
                return True
        return False


class DiscoverSystem(base.FakeSystem):
    """The wizard's system with the network replaced by one fake socket per search."""

    def __init__(self, *searches, **values):
        self.wakeable = []
        self.started = 0
        self.sockets = []
        self.searches = list(searches)
        super().__init__(**values)

    def start_search(self):
        self.started += 1
        replies, faults = self.searches.pop(0)
        clock = Clock()
        sock = FakeSocket(clock, replies, **faults)
        self.sockets.append(sock)
        return discover.Search(addresses=lambda: ["192.168.1.50"], socket_factory=lambda *a: sock, clock=clock)

    def wakeable_pcs(self):
        return self.wakeable


NOTHING = ([], {})
DESKTOP = ([from_pc(0.2)], {})
GAMING_PC = stream.Host(name="Gaming-PC", mac=bytes.fromhex("1c1b0d8dbfe9"), local="192.168.1.50")
DEN_PC = stream.Host(name="Den-PC", mac=bytes.fromhex("1c1b0d8dbfea"), local="192.168.1.51")


class FindFlowTest(base.WizardTestCase):
    def run_step(self, ui, system, **extra):
        pairing = extra.pop("pairing", True)

        def pair(host, pin):
            self.calls.append(("pair", host, pin))
            if pairing:
                system.hosts.append("DESKTOP-ABC")
            return True

        wizard = self.wizard(ui, system, **{"pair_moonlight": pair, **extra})
        with mock.patch.object(setup, "new_pairing_pin", return_value="0427"):
            return wizard.step_streaming()

    def pairs(self):
        return [call[1:] for call in self.calls if call[0] == "pair"]

    def test_find_gaming_pcs_is_the_first_choice_of_the_streaming_step(self):
        ui = SearchUI(None)
        self.run_step(ui, DiscoverSystem())
        self.assertEqual(ui.screens[0]["choices"][0], "SEARCH THE NETWORK (RECOMMENDED)")
        self.assertIn("FIND MY GAMING PC IN MOONLIGHT", ui.screens[0]["choices"])
        self.assertIn("PAIR WITH A PIN SHOWN HERE", ui.screens[0]["choices"])

    def test_picking_a_found_pc_fills_in_the_address_and_goes_on_to_the_pin(self):
        ui = SearchUI("SEARCH THE NETWORK", "DESKTOP-ABC", "START PAIRING")
        system = DiscoverSystem(DESKTOP)
        self.assertEqual(self.run_step(ui, system), "done")
        self.assertEqual(self.pairs(), [("192.168.1.20", "0427")])
        self.assertEqual([call for call in self.calls if call[0] == "text"], [])  # nothing typed
        self.assertTrue(all(sock.closed for sock in system.sockets))

    def test_the_search_is_announced_and_the_list_names_each_pc(self):
        ui = SearchUI("SEARCH THE NETWORK", "DESKTOP", None, None, None)  # B on the PIN screen, B on the list, B on the streaming choices
        self.run_step(ui, DiscoverSystem(DESKTOP))
        searching = [screen for screen in ui.screens if screen["title"] == "FIND GAMING PCS" and screen["lines"] == ["SEARCHING...  (B CANCELS)"]]
        self.assertTrue(searching)
        listing = next(screen for screen in ui.screens if screen["choices"] and "192.168.1.20" in screen["choices"][0])
        self.assertEqual(listing["choices"][0], "DESKTOP-ABC  192.168.1.20")
        self.assertEqual(listing["choices"][-3:], ["SEARCH AGAIN", "ENTER ADDRESS MANUALLY", "BACK"])
        self.assertIn("FOUND 1 GAMING PC.", listing["lines"][0])

    def test_the_pin_screen_shows_the_name_and_the_web_address_of_the_pc_picked(self):
        ui = SearchUI("SEARCH THE NETWORK", "DESKTOP-ABC", "START PAIRING")
        self.run_step(ui, DiscoverSystem(DESKTOP))
        screen = next(screen for screen in ui.screens if screen["big"] == "0427")
        self.assertEqual(screen["title"], "PAIR WITH DESKTOP-ABC (192.168.1.20)")
        self.assertIn("HTTPS://192.168.1.20:47990", " ".join(screen["lines"]))

    def test_several_pcs_are_listed_by_name(self):
        replies = [from_pc(0.1, "192.168.1.30", name="zeta"), from_pc(0.2, "192.168.1.20", name="Alpha")]
        ui = SearchUI("SEARCH THE NETWORK", "ZETA", "START PAIRING")
        system = DiscoverSystem((replies, {}))
        self.run_step(ui, system)
        listing = next(screen for screen in ui.screens if screen["title"] == "FIND GAMING PCS" and screen["choices"])
        self.assertEqual(listing["choices"][:2], ["ALPHA  192.168.1.20", "ZETA  192.168.1.30"])
        self.assertEqual(self.pairs(), [("192.168.1.30", "0427")])

    def test_a_pc_on_another_port_is_paired_with_that_port(self):
        ui = SearchUI("SEARCH THE NETWORK", "DESKTOP-ABC", "START PAIRING")
        self.run_step(ui, DiscoverSystem(([from_pc(0.2, port=48000)], {})))
        self.assertEqual(self.pairs(), [("192.168.1.20:48000", "0427")])
        screen = next(screen for screen in ui.screens if screen["big"] == "0427")
        self.assertIn("HTTPS://192.168.1.20:48001", " ".join(screen["lines"]))
        self.assertTrue(any("(PORT 48000)" in choice for s in ui.screens for choice in s["choices"]))

    def test_a_pc_that_is_already_paired_says_so_and_can_just_be_used(self):
        system = DiscoverSystem(DESKTOP, hosts=["DESKTOP-ABC"])
        ui = SearchUI("SEARCH THE NETWORK", "DESKTOP-ABC", "USE IT")
        self.assertEqual(self.run_step(ui, system), "done")
        self.assertIn("(ALREADY PAIRED)", ui.text())
        self.assertEqual(self.pairs(), [])

    def test_an_already_paired_pc_can_be_backed_out_of_and_says_how_to_pair_it_again(self):
        system = DiscoverSystem(DESKTOP, hosts=["DESKTOP-ABC"])
        ui = SearchUI("SEARCH THE NETWORK", "DESKTOP-ABC", "BACK", "SKIP")
        self.assertEqual(self.run_step(ui, system), "skipped")
        self.assertIn("DELETE IT IN MOONLIGHT FIRST", ui.text())
        self.assertEqual(self.pairs(), [])

    def test_a_pairing_that_saved_nothing_gets_the_usual_help(self):
        ui = SearchUI("SEARCH THE NETWORK", "DESKTOP-ABC", "START PAIRING", "CONTINUE WITHOUT")
        self.assertEqual(self.run_step(ui, DiscoverSystem(DESKTOP), pairing=False), "failed")
        self.assertIn("THE ADDRESS IS RIGHT: 192.168.1.20", ui.text())

    def test_search_again_asks_the_network_again(self):
        ui = SearchUI("SEARCH THE NETWORK", "SEARCH AGAIN", "DESKTOP-ABC", "START PAIRING")
        system = DiscoverSystem(DESKTOP, DESKTOP)
        self.assertEqual(self.run_step(ui, system), "done")
        self.assertEqual(system.started, 2)

    # nothing found

    def not_found(self, ui):
        return next(screen for screen in ui.screens if screen["lines"][:1] == ["NO GAMING PC ANSWERED. THE USUAL REASONS:"])

    def test_nothing_found_explains_the_usual_reasons_in_plain_words(self):
        ui = SearchUI("SEARCH THE NETWORK", None, "SKIP")
        self.assertEqual(self.run_step(ui, DiscoverSystem(NOTHING)), "skipped")
        screen = self.not_found(ui)
        text = " ".join(screen["lines"])
        self.assertIn("OFF OR ASLEEP", text)
        self.assertIn("SUNSHINE IS NOT RUNNING", text)
        self.assertIn("DIFFERENT NETWORK", text)
        self.assertEqual(screen["choices"], ["SEARCH AGAIN", "ENTER ADDRESS MANUALLY", "BACK"])

    def test_wake_pc_is_offered_only_when_a_paired_pc_has_a_known_address(self):
        woken = []
        wake = lambda host: woken.append(host) or f"{host.label} IS AWAKE."
        ui = SearchUI("SEARCH THE NETWORK", None, "SKIP")
        self.run_step(ui, DiscoverSystem(NOTHING), wake_pc=wake)
        self.assertNotIn("WAKE PC", self.not_found(ui)["choices"])  # none paired: nothing to wake
        ui = SearchUI("SEARCH THE NETWORK", None, "SKIP")
        self.run_step(ui, DiscoverSystem(NOTHING, wakeable=[GAMING_PC]))  # no wake action wired
        self.assertNotIn("WAKE PC", self.not_found(ui)["choices"])
        ui = SearchUI("SEARCH THE NETWORK", None, "SKIP")
        self.run_step(ui, DiscoverSystem(NOTHING, wakeable=[GAMING_PC]), wake_pc=wake)
        self.assertEqual(self.not_found(ui)["choices"][0], "WAKE PC")
        self.assertEqual(woken, [])

    def test_wake_pc_wakes_it_and_then_searches_again(self):
        woken = []
        wake = lambda host: woken.append(host) or f"{host.label} IS AWAKE."
        system = DiscoverSystem(NOTHING, DESKTOP, wakeable=[GAMING_PC])
        ui = SearchUI("SEARCH THE NETWORK", "WAKE PC", "OK", "DESKTOP-ABC", "START PAIRING")
        self.assertEqual(self.run_step(ui, system, wake_pc=wake), "done")
        self.assertEqual(woken, [GAMING_PC])
        self.assertIn("GAMING-PC IS AWAKE.", ui.text())
        self.assertEqual(system.started, 2)

    def test_with_several_wakeable_pcs_it_asks_which(self):
        woken = []
        wake = lambda host: woken.append(host) or "OK DONE"
        system = DiscoverSystem(NOTHING, NOTHING, wakeable=[GAMING_PC, DEN_PC])
        ui = SearchUI("SEARCH THE NETWORK", "WAKE PC", "DEN-PC", "OK", None, "SKIP")
        self.run_step(ui, system, wake_pc=wake)
        self.assertEqual(woken, [DEN_PC])
        self.assertEqual(ui.screens[[s["title"] for s in ui.screens].index("WAKE WHICH PC?")]["choices"], ["GAMING-PC", "DEN-PC", "BACK"])

    def test_backing_out_of_which_pc_wakes_nothing(self):
        woken = []
        system = DiscoverSystem(NOTHING, NOTHING, wakeable=[GAMING_PC, DEN_PC])
        ui = SearchUI("SEARCH THE NETWORK", "WAKE PC", None, None, "SKIP")
        self.run_step(ui, system, wake_pc=lambda host: woken.append(host) or "x")
        self.assertEqual(woken, [])

    def test_backing_out_of_which_pc_returns_to_the_not_found_screen_without_searching(self):
        system = DiscoverSystem(NOTHING, NOTHING, wakeable=[GAMING_PC, DEN_PC])
        ui = SearchUI("SEARCH THE NETWORK", "WAKE PC", None, "BACK", "SKIP")
        self.run_step(ui, system, wake_pc=lambda host: "x")
        self.assertEqual(system.started, 1)
        titles = ui.titles()
        after = ui.screens[titles.index("WAKE WHICH PC?") + 1]
        self.assertEqual(after["lines"][:1], ["NO GAMING PC ANSWERED. THE USUAL REASONS:"])
        self.assertEqual(after["choices"][0], "WAKE PC")

    def test_enter_address_manually_from_nothing_found_goes_to_the_typed_address(self):
        self.texts = ["192.168.1.77"]
        ui = SearchUI("SEARCH THE NETWORK", "ENTER ADDRESS", "START PAIRING")
        self.assertEqual(self.run_step(ui, DiscoverSystem(NOTHING)), "done")
        self.assertEqual(self.pairs(), [("192.168.1.77", "0427")])
        self.assertEqual([call[0] for call in self.calls if call[0] == "text"], ["text"])

    def test_enter_address_manually_is_also_in_the_list_of_found_pcs(self):
        self.texts = ["gaming-pc"]
        ui = SearchUI("SEARCH THE NETWORK", "ENTER ADDRESS", "START PAIRING")
        self.run_step(ui, DiscoverSystem(DESKTOP))
        self.assertEqual(self.pairs(), [("gaming-pc", "0427")])

    def test_a_bad_typed_address_is_refused_after_the_search_too(self):
        self.texts = ["bad host;rm", "gaming-pc"]
        ui = SearchUI("SEARCH THE NETWORK", "ENTER ADDRESS", "OK", "START PAIRING")
        self.run_step(ui, DiscoverSystem(NOTHING))
        self.assertEqual(self.pairs(), [("gaming-pc", "0427")])

    # backing out

    def test_b_on_the_nothing_found_screen_goes_back_to_the_streaming_choices(self):
        ui = SearchUI("SEARCH THE NETWORK", None, "SKIP")
        self.assertEqual(self.run_step(ui, DiscoverSystem(NOTHING)), "skipped")
        self.assertEqual(ui.titles()[-1], "STREAMING PC")
        self.assertEqual(self.pairs(), [])

    def test_the_back_choice_does_the_same(self):
        ui = SearchUI("SEARCH THE NETWORK", "BACK", "SKIP")
        self.assertEqual(self.run_step(ui, DiscoverSystem(NOTHING)), "skipped")
        self.assertEqual(ui.titles()[-1], "STREAMING PC")

    def test_b_on_the_list_of_found_pcs_goes_back(self):
        for answer in (None, "BACK"):
            with self.subTest(answer=answer):
                ui = SearchUI("SEARCH THE NETWORK", answer, "SKIP")
                self.assertEqual(self.run_step(ui, DiscoverSystem(DESKTOP)), "skipped")
                self.assertEqual(self.pairs(), [])

    def test_b_while_searching_cancels_the_search_at_once(self):
        ui = SearchUI("SEARCH THE NETWORK", "SKIP", cancel=[True])
        system = DiscoverSystem(DESKTOP)
        self.assertEqual(self.run_step(ui, system), "skipped")
        self.assertEqual(ui.titles().count("STREAMING PC"), 2)
        self.assertTrue(system.sockets[0].closed)
        self.assertEqual(self.pairs(), [])

    def list_screens(self, ui):
        return [screen for screen in ui.screens if screen["lines"][:1] == ["FOUND 1 GAMING PC. PICK YOURS."]]

    def test_b_on_the_pin_screen_goes_back_to_the_list_of_found_pcs(self):
        # Used to drop to the streaming choices and lose the list; B on the list then goes on back.
        ui = SearchUI("SEARCH THE NETWORK", "DESKTOP-ABC", "BACK", None, "SKIP")
        system = DiscoverSystem(DESKTOP)
        self.assertEqual(self.run_step(ui, system), "skipped")
        self.assertEqual(self.pairs(), [])
        self.assertEqual(len(self.list_screens(ui)), 2)
        self.assertEqual(system.started, 1)  # the same list, not a new search

    def test_another_pc_can_be_picked_after_backing_out_of_a_pin_screen(self):
        ui = SearchUI("SEARCH THE NETWORK", "DESKTOP-ABC", "BACK", "DESKTOP-ABC", "START PAIRING")
        self.assertEqual(self.run_step(ui, DiscoverSystem(DESKTOP)), "done")
        self.assertEqual(self.pairs(), [("192.168.1.20", "0427")])

    def test_b_at_the_typed_address_after_the_search_goes_back(self):
        self.texts = []  # the typing screen is dismissed
        ui = SearchUI("SEARCH THE NETWORK", "ENTER ADDRESS", "SKIP")
        self.assertEqual(self.run_step(ui, DiscoverSystem(NOTHING)), "skipped")

    # no network

    def test_no_network_is_said_plainly_and_offers_manual_entry(self):
        system = DiscoverSystem(([], {"fail_all_sends": True}), wakeable=[GAMING_PC])
        ui = SearchUI("SEARCH THE NETWORK", None, "SKIP")
        self.assertEqual(self.run_step(ui, system, wake_pc=lambda host: "x"), "skipped")
        screen = next(s for s in ui.screens if s["lines"] and s["lines"][0].startswith("NO NETWORK."))
        self.assertIn("SETTINGS > NETWORK", screen["lines"][0])
        self.assertEqual(screen["choices"], ["SEARCH AGAIN", "ENTER ADDRESS MANUALLY", "BACK"])  # no WAKE PC without a network
        self.assertTrue(system.sockets[0].closed)

    def test_a_search_that_cannot_open_a_socket_ends_on_the_same_screen(self):
        class Refusing(DiscoverSystem):
            def start_search(self):
                self.started += 1
                raise discover.DiscoveryError("COULD NOT OPEN A NETWORK CONNECTION: TOO MANY OPEN FILES")

        ui = SearchUI("SEARCH THE NETWORK", "ENTER ADDRESS", "START PAIRING")
        self.texts = ["192.168.1.9"]
        self.assertEqual(self.run_step(ui, Refusing()), "done")
        self.assertIn("TOO MANY OPEN FILES", ui.text())

    def test_retrying_after_no_network_searches_again(self):
        system = DiscoverSystem(([], {"fail_all_sends": True}), DESKTOP)
        ui = SearchUI("SEARCH THE NETWORK", "SEARCH AGAIN", "DESKTOP-ABC", "START PAIRING")
        self.assertEqual(self.run_step(ui, system), "done")

    # text on the screens

    def test_every_line_is_all_caps_and_short(self):
        ui = SearchUI("SEARCH THE NETWORK", None, "SKIP")
        self.run_step(ui, DiscoverSystem(NOTHING, wakeable=[GAMING_PC]), wake_pc=lambda host: "x")
        for screen in ui.screens:
            for line in screen["lines"] + screen["choices"]:
                self.assertEqual(line, line.upper())
        for line in setup.not_found_lines() + setup.not_found_lines("NO NETWORK"):
            self.assertLessEqual(len(line), 80)  # the screen wraps at its own width, but keep them short

    def test_a_hostile_name_is_made_safe_to_draw(self):
        packet = reply("Evil\x1b[31mPC" + "X" * 40)
        ui = SearchUI("SEARCH THE NETWORK", None, "SKIP")
        self.run_step(ui, DiscoverSystem(([(0.1, packet, ("192.168.1.20", 5353))], {})))
        listing = next(s for s in ui.screens if s["choices"] and "192.168.1.20" in s["choices"][0])
        self.assertNotIn("\x1b", listing["choices"][0])
        self.assertLessEqual(len(listing["choices"][0]), 28 + 2 + len("192.168.1.20"))
        self.assertIn("EVIL?[31MPC", listing["choices"][0])


class LabelTest(unittest.TestCase):
    def test_label_name_then_address(self):
        self.assertEqual(setup.found_label(discover.FoundPC("Den", "10.0.0.5")), "DEN  10.0.0.5")

    def test_label_mentions_a_port_that_is_not_the_default(self):
        self.assertEqual(setup.found_label(discover.FoundPC("Den", "10.0.0.5", 48000)), "DEN  10.0.0.5 (PORT 48000)")

    def test_label_marks_a_pc_already_paired_whatever_its_case(self):
        self.assertEqual(setup.found_label(discover.FoundPC("Den", "10.0.0.5"), ["den"]), "DEN  10.0.0.5  (ALREADY PAIRED)")

    def test_sunshine_web_address_is_the_http_port_plus_one(self):
        self.assertEqual(setup.sunshine_web_address("192.168.1.20"), "192.168.1.20:47990")
        self.assertEqual(setup.sunshine_web_address("pc.lan:48000"), "pc.lan:48001")
        self.assertEqual(setup.sunshine_web_address("pc.lan:47989"), "pc.lan:47990")


class SystemBoundaryTest(unittest.TestCase):
    CONF = (
        "[General]\nwidth=1920\n\n[hosts]\n"
        "1\\hostname=GAMING-PC\n1\\uuid=U1\n1\\localaddress=192.168.1.50\n"
        "1\\mac=1c:1b:0d:8d:bf:e9\n"
        "2\\hostname=NO-ADDRESS\n2\\uuid=U2\n"
        "size=2\n"
    )

    def test_only_paired_pcs_with_a_known_mac_can_be_woken(self):
        with tempfile.TemporaryDirectory() as directory:
            conf = pathlib.Path(directory) / "Moonlight Game Streaming Project" / "Moonlight.conf"
            conf.parent.mkdir()
            conf.write_text(self.CONF, encoding="utf-8")
            hosts = setup.System(config_root=pathlib.Path(directory)).wakeable_pcs()
        self.assertEqual([host.name for host in hosts], ["GAMING-PC"])
        self.assertEqual(hosts[0].mac_text, "1C:1B:0D:8D:BF:E9")

    def test_no_moonlight_settings_means_nothing_to_wake(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(setup.System(config_root=pathlib.Path(directory)).wakeable_pcs(), [])

    def test_the_real_system_starts_a_real_search(self):
        with mock.patch.object(setup.discover, "Search") as search:
            self.assertIs(setup.System.start_search(), search.return_value)


class LauncherWiringTest(base_stream.LauncherTestCase):
    """The launcher gives the wizard what the screens call: WAKE PC, with the words SETTINGS > STREAMING uses."""

    def test_wake_message_says_how_it_went(self):
        launcher = self.launcher()
        launcher.wake_host = mock.Mock(return_value="woke")
        self.assertEqual(launcher.wake_message(self.HOST), "GAMING-PC IS AWAKE.")
        self.assertTrue(launcher.wake_host.call_args.kwargs["force"])

    def test_a_pc_without_a_known_address_is_not_woken(self):
        launcher = self.launcher()
        launcher.wake_host = mock.Mock()
        self.assertEqual(launcher.wake_message(stream.Host(name="PC")), stream.NO_MAC)
        launcher.wake_host.assert_not_called()

    def test_a_wake_failure_is_a_message_not_a_crash(self):
        launcher = self.launcher()
        launcher.wake_host = mock.Mock(side_effect=OSError("Network is unreachable"))
        self.assertEqual(launcher.wake_message(self.HOST), "WAKE FAILED: NETWORK IS UNREACHABLE")

    def test_the_startup_wizard_is_given_wake_pc(self):
        captured = {}

        class FakeWizard:
            def __init__(self, ui, actions, system, **options):
                captured["actions"] = actions

            def run(self, force=False, resume=False):
                pass

        launcher = self.launcher()
        launcher.reload_applications = mock.Mock()
        with mock.patch.object(self.module.setup, "SetupWizard", FakeWizard), mock.patch.object(
            self.module.bluetooth, "BluetoothClient"
        ), mock.patch.object(self.module, "HOME_REQUEST", mock.Mock()):
            launcher.setup_wizard()
        self.assertEqual(captured["actions"]["wake_pc"], launcher.wake_message)

    def test_pair_another_gaming_pc_is_given_wake_pc_and_runs_the_step_with_find_gaming_pcs(self):
        launcher = self.launcher()
        settings = self.module.StreamingSettings(base_stream.Screen(), launcher)
        settings.message = mock.Mock()
        with tempfile.TemporaryDirectory() as directory, self.world(pathlib.Path(directory)), mock.patch.object(
            self.module.setup, "SetupWizard"
        ) as wizard, mock.patch.object(self.module.setup, "CursesUI"), mock.patch.object(self.module.setup, "System"):
            settings.pair_pc()
        self.assertEqual(wizard.call_args.args[1]["wake_pc"], launcher.wake_message)
        wizard.return_value.step_streaming.assert_called_once_with()
        self.assertEqual(settings.rows([self.HOST], stream.StreamSettings())[5], "PAIR ANOTHER GAMING PC")

    def test_the_real_wizard_step_offers_find_gaming_pcs_first(self):
        # pair_pc runs SetupWizard.step_streaming itself, so its first choice is what SETTINGS shows too.
        ui = base.FakeUI(None)
        wizard = setup.SetupWizard(ui, {}, base.FakeSystem(), marker=pathlib.Path("/nonexistent/marker"))
        self.assertEqual(wizard.step_streaming(), "skipped")
        self.assertEqual(ui.screens[0]["choices"][0], "SEARCH THE NETWORK (RECOMMENDED)")


if __name__ == "__main__":
    unittest.main()
