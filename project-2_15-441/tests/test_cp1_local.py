#!/usr/bin/env python3
"""Black-box CP1 tests using real UDP sockets; no root, VMs, or packages needed.

Builds into a temporary directory. Timeout/window overrides affect only a copy
of grading.h, never the submitted header. Override CP1_TEST_TIMEOUT (ms),
CP1_TEST_WINDOW (bytes), or CP1_TEST_SANITIZE=1 for additional configurations.
"""

import os
from pathlib import Path
import random
import re
import select
import shlex
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
HEADER = struct.Struct("!IHHIIHHBHH")
SYN, ACK = 8, 4
MASK = (1 << 32) - 1
RTO = int(os.environ.get("CP1_TEST_TIMEOUT", "200")) / 1000
MAX_LEN = int(os.environ.get("CP1_TEST_MAX_LEN", "1400"))
MSS = MAX_LEN - HEADER.size
WINDOW = int(os.environ.get("CP1_TEST_WINDOW", str(MSS * 32)))


def add(seq, length):
    return (seq + length) & MASK


def packet(src, dst, seq=0, ack=0, flags=0, data=b"", **overrides):
    values = dict(
        identifier=15441,
        src=src,
        dst=dst,
        seq=seq,
        ack=ack,
        hlen=25,
        plen=25 + len(data),
        flags=flags,
        window=1,
        ext=0,
    )
    values.update(overrides)
    return HEADER.pack(*values.values()) + data


def decode(data):
    fields = HEADER.unpack(data[:25])
    keys = (
        "identifier",
        "src",
        "dst",
        "seq",
        "ack",
        "hlen",
        "plen",
        "flags",
        "window",
        "ext",
    )
    result = dict(zip(keys, fields))
    if result["identifier"] != 15441 or result["plen"] != len(data):
        raise AssertionError("Invalid outbound header")
    if result["hlen"] != 25 or len(data) > MAX_LEN:
        raise AssertionError("Invalid outbound length")
    result["data"] = data[result["hlen"] :]
    return result


class Peer:
    def __init__(
        self,
        owner,
        role="listener",
        port=0,
        outgoing=b"",
        receive_length=0,
        hold=True,
        initial_sequence=None,
    ):
        self.directory = tempfile.TemporaryDirectory(prefix="cmu-peer-")
        owner.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name)
        input_path = base / "input.bin"
        input_path.write_bytes(outgoing)
        self.output = base / "output.bin"
        self.stderr = open(base / "stderr.txt", "w+")
        environment = os.environ.copy()
        if initial_sequence is not None:
            environment["CP1_TEST_INITIAL_SEQUENCE"] = str(initial_sequence)
        else:
            environment.pop("CP1_TEST_INITIAL_SEQUENCE", None)
        self.process = subprocess.Popen(
            [
                str(owner.binary),
                role,
                str(port),
                str(input_path),
                str(self.output),
                str(receive_length),
                "hold" if hold else "auto",
                "check",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            bufsize=0,
            env=environment,
        )
        owner.addCleanup(self.cleanup)
        ready = self.line(5)
        if not ready.startswith("READY "):
            raise AssertionError(
                "Peer failed to start: " + ready + self.errors()
            )
        self.port = int(ready.split()[1])

    def line(self, timeout=5):
        if not select.select([self.process.stdout], [], [], timeout)[0]:
            raise AssertionError("Peer timed out: " + self.errors())
        return self.process.stdout.readline().decode().strip()

    def errors(self):
        self.stderr.flush()
        self.stderr.seek(0)
        return self.stderr.read()

    def finish(self, timeout=None):
        if self.process.poll() is None:
            self.process.stdin.write(b"\n")
            self.process.stdin.flush()
        self.process.wait(timeout=timeout or max(5, 5 * RTO))
        if self.process.returncode != 0:
            raise AssertionError(
                f"Peer exit {self.process.returncode}: " + self.errors()
            )
        if (
            "runtime error:" in self.errors()
            or "ERROR: AddressSanitizer" in self.errors()
        ):
            raise AssertionError(self.errors())

    def cleanup(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()
        self.stderr.close()


class Relay:
    """A seeded bidirectional loss/duplication/reordering link between two peers."""

    def __init__(self, destination, lossy):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.destination = ("127.0.0.1", destination)
        self.client = None
        self.lossy = lossy
        self.running = True
        self.random = random.Random(15441)
        self.dropped = self.duplicated = self.reordered = 0
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        pending = []
        while self.running:
            now = time.monotonic()
            for due, data, target in pending[:]:
                if due <= now:
                    self.sock.sendto(data, target)
                    pending.remove((due, data, target))
            if not select.select([self.sock], [], [], 0.002)[0]:
                continue
            data, source = self.sock.recvfrom(65535)
            header = decode(data)
            if source == self.destination:
                target = self.client
            else:
                self.client = source
                target = self.destination
            if target is None:
                continue
            # A UDP relay/NAT forwards the payload unchanged, including embedded
            # CMU-TCP ports. Routing uses the actual UDP endpoint addresses.
            if self.lossy and self.random.random() < 0.07:
                self.dropped += 1
                continue
            if self.lossy and header["data"] and self.random.random() < 0.06:
                pending.append((now + 0.015, data, target))
                self.reordered += 1
                continue
            self.sock.sendto(data, target)
            if self.lossy and self.random.random() < 0.04:
                self.sock.sendto(data, target)
                self.duplicated += 1

    def close(self):
        self.running = False
        self.thread.join(timeout=2)
        self.sock.close()


class CheckpointOne(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="cmu-cp1-build-")
        cls.addClassCleanup(cls.build.cleanup)
        build = Path(cls.build.name)
        shutil.copytree(ROOT / "inc", build / "inc")
        grading = build / "inc/grading.h"
        text = grading.read_text()
        text = re.sub(
            r"#define DEFAULT_TIMEOUT .*",
            f"#define DEFAULT_TIMEOUT {int(RTO * 1000)}",
            text,
        )
        text = re.sub(
            r"#define CP1_WINDOW_SIZE .*",
            f"#define CP1_WINDOW_SIZE {WINDOW}",
            text,
        )
        text = re.sub(
            r"#define MAX_LEN .*", f"#define MAX_LEN {MAX_LEN}", text
        )
        grading.write_text(text)
        cls.binary = build / "peer"
        flags = [
            "-std=c11",
            "-D_POSIX_C_SOURCE=200809L",
            "-Dread=cp1_test_read",
            "-pthread",
            "-g",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
        ]
        if os.environ.get("CP1_TEST_SANITIZE") == "1":
            flags += [
                "-fsanitize=address,undefined",
                "-fno-omit-frame-pointer",
            ]
        subprocess.run(
            shlex.split(os.environ.get("CC", "cc"))
            + flags
            + [
                "-I" + str(build / "inc"),
                str(ROOT / "tests/cp1_peer.c"),
                str(ROOT / "src/backend.c"),
                str(ROOT / "src/cmu_tcp.c"),
                str(ROOT / "src/cmu_packet.c"),
                str(ROOT / "tests/cp1_test_random.c"),
                "-o",
                str(cls.binary),
            ],
            check=True,
        )

    def setUp(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.addCleanup(self.sock.close)

    def send(self, peer, **fields):
        values = dict(src=self.port, dst=peer.port)
        values.update(fields)
        self.sock.sendto(packet(**values), ("127.0.0.1", peer.port))

    def receive(self, timeout=None):
        self.sock.settimeout(timeout or max(2, RTO * 2))
        data, source = self.sock.recvfrom(65535)
        result = decode(data)
        self.assertEqual(result["src"], source[1])
        self.assertEqual(result["dst"], self.port)
        if result["data"]:
            self.assertEqual(result["flags"], ACK)
        return result

    def quiet(self, duration=None):
        self.assertFalse(
            select.select([self.sock], [], [], duration or min(0.04, RTO / 4))[
                0
            ],
            "Unexpected response",
        )

    def listener_handshake(self, peer, seq=12345):
        self.send(peer, seq=seq, flags=SYN)
        response = self.receive()
        self.assertEqual(response["flags"], SYN | ACK)
        self.assertEqual(response["ack"], add(seq, 1))
        self.assertEqual(response["data"], b"")
        self.send(
            peer, seq=add(seq, 1), ack=add(response["seq"], 1), flags=ACK
        )
        return add(seq, 1), add(response["seq"], 1)

    def initiator_handshake(self, peer, seq=54321):
        syn = self.receive()
        self.assertEqual(syn["flags"], SYN)
        self.assertEqual(syn["data"], b"")
        self.send(peer, seq=seq, ack=add(syn["seq"], 1), flags=SYN | ACK)
        final = self.receive()
        self.assertEqual(
            (final["flags"], final["seq"], final["ack"]),
            (ACK, add(syn["seq"], 1), add(seq, 1)),
        )
        return add(seq, 1), add(syn["seq"], 1)

    def collect_bytes(self, count):
        segments = []
        total = 0
        while total < count:
            response = self.receive()
            if response["data"]:
                segments.append(response)
                total += len(response["data"])
        self.assertEqual(total, count)
        return segments

    def test_initiator_retransmits_same_syn(self):
        peer = Peer(self, "initiator", self.port, outgoing=b"x")
        first = self.receive()
        started = time.monotonic()
        second = self.receive()
        self.assertEqual(first, second)
        self.assertGreaterEqual(time.monotonic() - started, RTO * 0.7)
        self.assertEqual(first["flags"], SYN)
        self.send(peer, seq=100, ack=add(second["seq"], 1), flags=SYN | ACK)
        final = self.receive()
        self.assertEqual(
            (final["flags"], final["seq"], final["ack"], final["data"]),
            (ACK, add(first["seq"], 1), 101, b""),
        )
        data = self.receive()
        self.assertEqual(
            (data["seq"], data["ack"], data["data"]),
            (final["seq"], 101, b"x"),
        )
        self.send(peer, seq=101, ack=add(data["seq"], 1), flags=ACK)
        peer.finish()

    def test_data_retransmission_piggybacks_latest_ack(self):
        peer = Peer(
            self, "initiator", self.port, outgoing=b"x", receive_length=2
        )
        seq, ack = self.initiator_handshake(peer, seq=302)
        first = self.receive()
        self.assertEqual((first["ack"], first["data"]), (303, b"x"))
        # ACK marks a significant acknowledgment number, not an empty payload.
        # Receive data in both formats while our outgoing byte remains unacked.
        for offset, flags in enumerate((0, ACK)):
            self.send(
                peer, seq=add(seq, offset), ack=ack, flags=flags, data=b"y"
            )
            response = self.receive()
            self.assertEqual(
                (response["flags"], response["ack"], response["data"]),
                (ACK, add(seq, offset + 1), b""),
            )
        retransmitted = self.receive()
        self.assertEqual(
            (
                retransmitted["seq"],
                retransmitted["ack"],
                retransmitted["data"],
            ),
            (ack, 305, b"x"),
        )
        self.send(peer, seq=305, ack=add(ack, 1), flags=ACK)
        self.assertEqual(peer.line(), "TRANSFER_DONE")
        peer.finish()
        self.assertEqual(peer.output.read_bytes(), b"yy")

    def test_initiator_receives_data_with_relay_header_ports(self):
        lengths = (1, min(1000, MSS), MSS)
        peer = Peer(
            self, "initiator", self.port, receive_length=2 * sum(lengths)
        )
        seq, ack = self.initiator_handshake(peer, seq=302)
        expected = bytearray()
        for flags in (0, ACK):
            for length in lengths:
                payload = b"z" * length
                self.send(
                    peer,
                    src=(self.port + 1) % 65536,
                    dst=(peer.port + 1) % 65536,
                    seq=seq,
                    ack=ack,
                    flags=flags,
                    data=payload,
                )
                seq = add(seq, length)
                response = self.receive()
                self.assertEqual(
                    (response["flags"], response["ack"], response["data"]),
                    (ACK, seq, b""),
                )
                expected.extend(payload)
        self.assertEqual(peer.line(), "TRANSFER_DONE")
        peer.finish()
        self.assertEqual(peer.output.read_bytes(), bytes(expected))

    def test_handshake_and_data_through_payload_preserving_udp_relay(self):
        peer = Peer(self, receive_length=1)
        relay = Relay(peer.port, False)
        self.addCleanup(relay.close)
        target = ("127.0.0.1", relay.port)
        self.sock.sendto(
            packet(self.port, peer.port, seq=100, flags=SYN), target
        )
        self.sock.settimeout(max(2, RTO * 2))
        response = decode(self.sock.recvfrom(65535)[0])
        self.assertEqual(
            (response["flags"], response["ack"]), (SYN | ACK, 101)
        )
        self.sock.sendto(
            packet(
                self.port,
                peer.port,
                seq=101,
                ack=add(response["seq"], 1),
                flags=ACK,
                data=b"x",
            ),
            target,
        )
        response = decode(self.sock.recvfrom(65535)[0])
        self.assertEqual((response["flags"], response["ack"]), (ACK, 102))
        self.assertEqual(peer.line(), "TRANSFER_DONE")
        self.assertEqual(peer.output.read_bytes(), b"x")
        peer.finish()

    def test_listener_retransmits_same_syn_ack(self):
        peer = Peer(self)
        self.send(peer, seq=99, flags=SYN)
        first = self.receive()
        second = self.receive()
        self.assertEqual(first, second)
        self.assertEqual((first["flags"], first["ack"]), (SYN | ACK, 100))

    def test_listener_rejects_malformed_packets_then_accepts_syn(self):
        peer = Peer(self)
        mutations = [
            dict(identifier=1),
            dict(hlen=24),
            dict(plen=26),
            dict(ext=1),
            dict(flags=ACK),
            dict(flags=SYN | ACK),
            dict(flags=SYN | 1),
            dict(data=b"x"),
        ]
        for fields in mutations:
            with self.subTest(fields=fields):
                values = dict(seq=99, flags=SYN)
                values.update(fields)
                self.send(peer, **values)
                self.quiet()
        self.sock.sendto(b"short", ("127.0.0.1", peer.port))
        self.sock.sendto(
            packet(self.port, peer.port, seq=99, flags=SYN) + b"x" * 1500,
            ("127.0.0.1", peer.port),
        )
        self.quiet()
        self.listener_handshake(peer)

    def test_initiator_rejects_bad_syn_ack(self):
        peer = Peer(self, "initiator", self.port, receive_length=3)
        syn = self.receive()
        wrong = [
            dict(ack=syn["seq"]),
            dict(flags=SYN),
            dict(flags=ACK),
            dict(identifier=99),
            dict(hlen=24),
            dict(plen=24),
            dict(data=b"x"),
        ]
        for fields in wrong:
            values = dict(seq=100, ack=add(syn["seq"], 1), flags=SYN | ACK)
            values.update(fields)
            self.send(peer, **values)
        self.quiet()
        self.send(peer, seq=100, ack=add(syn["seq"], 1), flags=SYN | ACK)
        final = self.receive()
        self.assertEqual((final["flags"], final["ack"]), (ACK, 101))
        self.send(peer, seq=101, ack=final["seq"], flags=ACK, data=b"abc")
        self.assertEqual(self.receive()["ack"], 104)
        self.assertEqual(peer.line(), "TRANSFER_DONE")
        self.assertEqual(peer.output.read_bytes(), b"abc")
        peer.finish()

    def test_listener_rejects_invalid_final_ack(self):
        peer = Peer(self, receive_length=4)
        self.send(peer, seq=100, flags=SYN)
        syn_ack = self.receive()
        correct = add(syn_ack["seq"], 1)
        self.send(peer, seq=101, ack=add(correct, 1), flags=ACK, data=b"bad!")
        self.send(peer, seq=102, ack=correct, flags=ACK, data=b"bad!")
        self.send(peer, seq=101, ack=correct, flags=0, data=b"bad!")
        self.quiet()
        self.send(peer, seq=101, ack=correct, flags=ACK, data=b"good")
        self.assertEqual(self.receive()["ack"], 105)
        self.assertEqual(peer.line(), "TRANSFER_DONE")
        self.assertEqual(peer.output.read_bytes(), b"good")
        peer.finish()

    def test_duplicate_handshake_packets(self):
        peer = Peer(self, "initiator", self.port)
        syn = self.receive()
        for _ in range(2):
            self.send(peer, seq=100, ack=add(syn["seq"], 1), flags=SYN | ACK)
            final = self.receive()
            self.assertEqual(
                (final["flags"], final["seq"], final["ack"]),
                (ACK, add(syn["seq"], 1), 101),
            )
        peer.finish()

    def test_listener_duplicate_syn(self):
        peer = Peer(self)
        self.send(peer, seq=100, flags=SYN)
        first = self.receive()
        self.send(peer, seq=100, flags=SYN)
        self.assertEqual(self.receive(), first)
        self.send(peer, seq=101, ack=add(first["seq"], 1), flags=ACK)
        self.quiet()
        peer.finish()

    def test_wrong_peer_cannot_hijack_connection(self):
        peer = Peer(self, receive_length=4)
        seq, ack = self.listener_handshake(peer)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as other:
            other.bind(("127.0.0.1", 0))
            other.sendto(
                packet(
                    other.getsockname()[1], peer.port, seq, ack, ACK, b"evil"
                ),
                ("127.0.0.1", peer.port),
            )
            self.assertFalse(
                select.select([other], [], [], min(0.04, RTO / 4))[0]
            )
        self.send(peer, seq=seq, ack=ack, flags=ACK, data=b"good")
        self.assertEqual(self.receive()["ack"], add(seq, 4))
        self.assertEqual(peer.line(), "TRANSFER_DONE")
        self.assertEqual(peer.output.read_bytes(), b"good")
        peer.finish()

    def test_receive_wraparound_duplicates_and_out_of_order(self):
        data = b"abcdefghijklmnop"
        peer = Peer(self, receive_length=len(data))
        seq, ack = self.listener_handshake(peer, MASK - 5)
        self.send(peer, seq=add(seq, 8), ack=ack, flags=ACK, data=data[8:])
        self.assertEqual(self.receive()["ack"], seq)
        for _ in range(2):
            self.send(peer, seq=seq, ack=ack, flags=ACK, data=data[:8])
            self.assertEqual(self.receive()["ack"], add(seq, 8))
        self.send(peer, seq=add(seq, 8), ack=ack, flags=0, data=data[8:])
        self.assertEqual(self.receive()["ack"], add(seq, 16))
        self.assertEqual(peer.line(), "TRANSFER_DONE")
        self.assertEqual(peer.output.read_bytes(), data)
        peer.finish()

    def test_random_initial_sequences(self):
        for role in ("listener", "initiator"):
            numbers = []
            for _ in range(5):
                peer = Peer(
                    self, role, self.port if role == "initiator" else 0
                )
                if role == "listener":
                    self.send(peer, seq=100, flags=SYN)
                response = self.receive()
                numbers.append(response["seq"])
                peer.finish()
            self.assertEqual(len(set(numbers)), len(numbers))
            self.assertNotIn(0, numbers)

    def test_window_limit_and_slide_on_partial_ack(self):
        outgoing = bytes((i % 251 for i in range(WINDOW * 2 + 17)))
        peer = Peer(self, "initiator", self.port, outgoing=outgoing)
        seq, ack = self.initiator_handshake(peer)
        initial = self.collect_bytes(WINDOW)
        self.assertEqual(initial[0]["seq"], ack)
        self.assertEqual(
            b"".join(p["data"] for p in initial), outgoing[:WINDOW]
        )
        self.quiet()
        released = max(1, min(MSS // 2, WINDOW))
        self.send(peer, seq=seq, ack=add(ack, released), flags=ACK)
        fresh = self.collect_bytes(released)
        self.assertEqual(fresh[0]["seq"], add(ack, WINDOW))
        self.assertEqual(
            b"".join(p["data"] for p in fresh),
            outgoing[WINDOW : WINDOW + released],
        )
        self.quiet()

    def test_go_back_n_and_ack_validation(self):
        outgoing = bytes((i % 251 for i in range(WINDOW)))
        peer = Peer(self, "initiator", self.port, outgoing=outgoing)
        seq, ack = self.initiator_handshake(peer)
        initial = self.collect_bytes(WINDOW)
        # Neither an ACK for unsent bytes nor an old ACK can release the window.
        self.send(peer, seq=seq, ack=add(ack, WINDOW + 1), flags=ACK)
        self.send(peer, seq=seq, ack=add(ack, -1), flags=ACK)
        self.send(peer, seq=seq, ack=ack, flags=ACK)
        retransmitted = self.collect_bytes(WINDOW)
        self.assertEqual(
            [(p["seq"], p["data"]) for p in retransmitted],
            [(p["seq"], p["data"]) for p in initial],
        )
        self.send(peer, seq=seq, ack=add(ack, WINDOW), flags=ACK)
        peer.finish()

    def test_timeout_resends_only_unacknowledged_suffix(self):
        outgoing = bytes((i % 251 for i in range(WINDOW)))
        peer = Peer(self, "initiator", self.port, outgoing=outgoing)
        seq, ack = self.initiator_handshake(peer)
        self.collect_bytes(WINDOW)
        consumed = max(1, WINDOW // 3)
        self.send(peer, seq=seq, ack=add(ack, consumed), flags=ACK)
        suffix = (
            self.collect_bytes(WINDOW - consumed) if consumed < WINDOW else []
        )
        self.assertEqual(
            b"".join(p["data"] for p in suffix), outgoing[consumed:]
        )
        if suffix:
            self.assertEqual(suffix[0]["seq"], add(ack, consumed))
        self.send(peer, seq=seq, ack=add(ack, WINDOW), flags=ACK)
        peer.finish()

    def test_lost_final_data_ack_during_close(self):
        peer = Peer(self, receive_length=3, hold=False)
        seq, ack = self.listener_handshake(peer)
        self.send(peer, seq=seq, ack=ack, flags=ACK, data=b"end")
        self.assertEqual(self.receive()["ack"], add(seq, 3))
        time.sleep(RTO * 1.1)
        self.send(peer, seq=seq, ack=ack, flags=ACK, data=b"end")
        self.assertEqual(self.receive()["ack"], add(seq, 3))
        peer.process.wait(timeout=max(5, RTO * 4))
        self.assertEqual(peer.process.returncode, 0, peer.errors())
        self.assertEqual(peer.output.read_bytes(), b"end")

    def test_sender_sequence_wraparound(self):
        outgoing = bytes((i % 251 for i in range(WINDOW + 73)))
        initial_sequence = MASK - min(100, WINDOW)
        peer = Peer(
            self,
            "initiator",
            self.port,
            outgoing=outgoing,
            initial_sequence=initial_sequence,
        )
        seq, ack = self.initiator_handshake(peer)
        self.assertEqual(ack, add(initial_sequence, 1))
        received = bytearray()
        while len(received) < len(outgoing):
            response = self.receive()
            self.assertEqual(response["seq"], add(ack, len(received)))
            received.extend(response["data"])
            self.send(peer, seq=seq, ack=add(ack, len(received)), flags=ACK)
        self.assertEqual(bytes(received), outgoing)
        peer.finish()

    def test_duplicate_acks_do_not_postpone_timeout(self):
        peer = Peer(self, "initiator", self.port, outgoing=b"x")
        seq, ack = self.initiator_handshake(peer)
        first = self.receive()
        deadline = time.monotonic() + RTO * 1.7
        retransmitted = None
        while time.monotonic() < deadline:
            self.send(peer, seq=seq, ack=ack, flags=ACK)
            if select.select([self.sock], [], [], RTO / 8)[0]:
                retransmitted = self.receive()
                break
        self.assertIsNotNone(
            retransmitted, "Duplicate ACKs suppressed retransmission"
        )
        self.assertEqual(retransmitted["seq"], first["seq"])
        self.assertEqual(retransmitted["data"], b"x")
        self.send(peer, seq=seq, ack=add(ack, 1), flags=ACK)
        peer.finish()

    def test_no_application_data_before_handshake(self):
        peer = Peer(self, "initiator", self.port, outgoing=b"pending")
        self.assertEqual(self.receive()["flags"], SYN)
        self.quiet()
        self.assertEqual(self.receive()["flags"], SYN)

    def test_initiator_can_start_before_listener(self):
        port = self.port
        self.sock.close()
        initiator = Peer(self, "initiator", port, outgoing=b"late", hold=False)
        time.sleep(RTO * 1.2)
        listener = Peer(self, port=port, receive_length=4, hold=False)
        for peer in (initiator, listener):
            peer.process.wait(timeout=max(5, RTO * 6))
            self.assertEqual(peer.process.returncode, 0, peer.errors())
        self.assertEqual(listener.output.read_bytes(), b"late")

    def test_handshake_with_initial_sequence_at_uint32_max(self):
        peer = Peer(self, initial_sequence=MASK, receive_length=1)
        seq, ack = self.listener_handshake(peer, MASK)
        self.assertEqual((seq, ack), (0, 0))
        self.send(peer, seq=seq, ack=ack, flags=ACK, data=b"x")
        response = self.receive()
        self.assertEqual((response["seq"], response["ack"]), (0, 1))
        self.assertEqual(peer.line(), "TRANSFER_DONE")
        peer.finish()

    def transfer(self, lossy):
        size = max(
            16000,
            WINDOW * 3 + 131,
            int(os.environ.get("CP1_TEST_TRANSFER_BYTES", "0")),
        )
        left = random.Random(11).randbytes(size)
        right = random.Random(22).randbytes(size + 79)
        listener = Peer(
            self, outgoing=left, receive_length=len(right), hold=False
        )
        relay = Relay(listener.port, lossy)
        self.addCleanup(relay.close)
        initiator = Peer(
            self,
            "initiator",
            relay.port,
            outgoing=right,
            receive_length=len(left),
            hold=False,
        )
        timeout = max(20, RTO * 150)
        for peer, expected in ((listener, right), (initiator, left)):
            peer.process.wait(timeout=timeout)
            self.assertEqual(peer.process.returncode, 0, peer.errors())
            self.assertEqual(peer.output.read_bytes(), expected)
            self.assertNotIn("runtime error:", peer.errors())
            self.assertNotIn("ERROR: AddressSanitizer", peer.errors())
        if lossy:
            self.assertGreater(relay.dropped, 0)
            self.assertGreater(relay.duplicated, 0)
            self.assertGreater(relay.reordered, 0)

    def test_bidirectional_lossless_transfer(self):
        self.transfer(False)

    def test_bidirectional_lossy_transfer(self):
        self.transfer(True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
