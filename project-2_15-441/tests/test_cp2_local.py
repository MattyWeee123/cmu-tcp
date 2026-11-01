#!/usr/bin/env python3
"""Packet-level CP2 tests: independent UDP peer plus real bidirectional transfers."""
import csv
import os
import select
import time
import unittest
from test_cp1_local import (
    CheckpointOne,
    Peer,
    Relay,
    ACK,
    FIN,
    SYN,
    MSS,
    MASK,
    RTO,
    add,
)

INITIAL = int(os.environ.get("CP2_TEST_INITIAL_WINDOW", str(2 * MSS)))
THRESHOLD = int(os.environ.get("CP2_TEST_SSTHRESH", str(8 * MSS)))
BUFFER = int(os.environ.get("CP2_TEST_BUFFER", str(8 * MSS + 31)))


class CheckpointTwo(unittest.TestCase):
    grading_overrides = dict(
        WINDOW_INITIAL_WINDOW_SIZE=INITIAL,
        WINDOW_INITIAL_SSTHRESH=THRESHOLD,
        MAX_NETWORK_BUFFER=BUFFER,
    )
    setUpClass = classmethod(CheckpointOne.setUpClass.__func__)
    setUp = CheckpointOne.setUp
    send = CheckpointOne.send
    receive = CheckpointOne.receive
    quiet = CheckpointOne.quiet
    listener_handshake = CheckpointOne.listener_handshake
    initiator_handshake = CheckpointOne.initiator_handshake
    collect_bytes = CheckpointOne.collect_bytes
    transfer = CheckpointOne.transfer

    def rows(self, peer):
        with peer.trace.open() as source:
            return list(csv.DictReader(source))

    def test_real_receiver_triggers_fast_retransmit(self):
        payload = bytes(range(256)) * 512
        listener = Peer(self, receive_length=len(payload), hold=False)
        seen = 0
        dropped = None
        dropped_at = recovered_at = None

        def drop_one(packet, from_receiver):
            nonlocal seen, dropped, dropped_at, recovered_at
            if from_receiver or not packet["data"]:
                return False
            seen += 1
            if seen == 16:
                dropped = packet["seq"]
                dropped_at = time.monotonic()
                return True
            if packet["seq"] == dropped and recovered_at is None:
                recovered_at = time.monotonic()
            return False

        relay = Relay(listener.port, False, drop=drop_one)
        self.addCleanup(relay.close)
        initiator = Peer(
            self, "initiator", relay.port, outgoing=payload, hold=False
        )
        for peer in (initiator, listener):
            peer.process.wait(timeout=20)
            self.assertEqual(peer.process.returncode, 0, peer.errors())
        self.assertEqual(listener.output.read_bytes(), payload)
        self.assertIsNotNone(recovered_at)
        self.assertLess(recovered_at - dropped_at, RTO * 0.8)
        self.assertTrue(
            any(
                row["event"] == "fast_retransmit"
                for row in self.rows(initiator)
            )
        )

    def test_initial_window_and_slow_start(self):
        peer = Peer(self, "initiator", self.port, outgoing=b"x" * (BUFFER * 2))
        seq, ack = self.initiator_handshake(peer)
        first = self.collect_bytes(INITIAL)
        self.quiet()
        released = len(first[0]["data"])
        self.send(peer, seq=seq, ack=add(ack, released), flags=ACK)
        fresh = self.collect_bytes(released + min(released, MSS))
        self.assertEqual(fresh[0]["seq"], add(ack, INITIAL))
        self.quiet()
        row = self.rows(peer)[-1]
        self.assertAlmostEqual(
            float(row["cwnd"]), INITIAL + min(released, MSS)
        )
        self.assertEqual(
            row["state"],
            (
                "slow_start"
                if INITIAL + min(released, MSS) < THRESHOLD
                else "congestion_avoidance"
            ),
        )

    def test_fast_retransmit_and_recovery(self):
        payload = b"a" * INITIAL
        peer = Peer(self, "initiator", self.port, outgoing=payload)
        seq, ack = self.initiator_handshake(peer)
        first = self.collect_bytes(INITIAL)
        for _ in range(2):
            self.send(peer, seq=seq, ack=ack, flags=ACK)
        self.quiet(RTO / 8)
        started = time.monotonic()
        self.send(peer, seq=seq, ack=ack, flags=ACK)
        retransmitted = self.receive(timeout=RTO * 0.8)
        self.assertLess(time.monotonic() - started, RTO * 0.8)
        self.assertEqual(
            (retransmitted["seq"], retransmitted["data"]),
            (first[0]["seq"], first[0]["data"]),
        )
        row = self.rows(peer)[-1]
        self.assertEqual(row["state"], "fast_recovery")
        self.assertAlmostEqual(float(row["ssthresh"]), INITIAL / 2)
        self.assertAlmostEqual(float(row["cwnd"]), INITIAL / 2 + 3 * MSS)
        self.send(peer, seq=seq, ack=ack, flags=ACK)
        self.quiet(RTO / 8)
        self.assertAlmostEqual(
            float(self.rows(peer)[-1]["cwnd"]), INITIAL / 2 + 4 * MSS
        )
        self.send(peer, seq=seq, ack=add(ack, INITIAL), flags=ACK)
        self.quiet(RTO / 8)
        row = self.rows(peer)[-1]
        self.assertEqual(row["state"], "congestion_avoidance")
        self.assertAlmostEqual(float(row["cwnd"]), INITIAL / 2)
        peer.finish()

    def test_timeout_returns_to_slow_start(self):
        peer = Peer(self, "initiator", self.port, outgoing=b"t" * INITIAL)
        seq, ack = self.initiator_handshake(peer)
        initial = self.collect_bytes(INITIAL)
        retry = self.receive()
        self.assertEqual(retry, initial[0])
        row = self.rows(peer)[-1]
        self.assertEqual(
            (row["event"], row["state"]), ("timeout", "slow_start")
        )
        self.assertAlmostEqual(float(row["cwnd"]), MSS)
        self.assertAlmostEqual(float(row["ssthresh"]), INITIAL / 2)
        self.quiet(RTO / 8)
        self.send(peer, seq=seq, ack=add(ack, INITIAL), flags=ACK)
        peer.finish()

    def test_congestion_avoidance_increases_linearly(self):
        peer = Peer(self, "initiator", self.port, outgoing=b"c" * (BUFFER * 3))
        seq, ack = self.initiator_handshake(peer)
        total = 0
        for _ in range(30):
            response = self.receive()
            self.assertTrue(response["data"])
            self.assertEqual(response["seq"], add(ack, total))
            total += len(response["data"])
            self.send(peer, seq=seq, ack=add(ack, total), flags=ACK)
            time.sleep(0.002)
            rows = [r for r in self.rows(peer) if r["event"] == "new_ack"]
            if len(rows) > 1 and rows[-2]["state"] == "congestion_avoidance":
                previous = float(rows[-2]["cwnd"])
                current = float(rows[-1]["cwnd"])
                self.assertAlmostEqual(
                    current, previous + MSS * MSS / previous, delta=0.002
                )
                self.assertLess(current - previous, MSS)
                return
        self.fail("Never entered congestion avoidance")

    def test_advertised_window_limits_and_reopens_sender(self):
        peer = Peer(self, "initiator", self.port, outgoing=b"w" * MSS)
        syn = self.receive()
        self.send(
            peer, seq=100, ack=add(syn["seq"], 1), flags=SYN | ACK, window=7
        )
        self.assertEqual(self.receive()["flags"], ACK)
        initial = self.collect_bytes(7)
        self.assertEqual(initial[0]["data"], b"w" * 7)
        self.quiet()
        ack = add(initial[0]["seq"], 7)
        self.send(peer, seq=101, ack=ack, flags=ACK, window=0)
        self.quiet()
        probe = self.receive()
        self.assertEqual((probe["seq"], len(probe["data"])), (add(ack, -1), 1))
        self.assertFalse(any(r["event"] == "timeout" for r in self.rows(peer)))
        self.send(peer, seq=101, ack=ack, flags=ACK, window=11)
        fresh = self.collect_bytes(11)
        self.assertEqual((fresh[0]["seq"], fresh[0]["data"]), (ack, b"w" * 11))
        self.quiet()

    def test_changed_windows_do_not_trigger_fast_retransmit(self):
        peer = Peer(self, "initiator", self.port, outgoing=b"x")
        seq, ack = self.initiator_handshake(peer)
        self.receive()
        for size in (100, 90, 80, 70):
            self.send(peer, seq=seq, ack=ack, flags=ACK, window=size)
        self.quiet(RTO / 4)
        self.assertFalse(
            any(r["event"] == "fast_retransmit" for r in self.rows(peer))
        )
        self.send(peer, seq=seq, ack=add(ack, 1), flags=ACK)
        peer.finish()

    def test_receive_buffer_window_and_read_reopening(self):
        peer = Peer(self, receive_length=BUFFER, read_gate=True)
        seq, ack = self.listener_handshake(peer)
        sent = 0
        while sent < BUFFER:
            length = min(MSS, BUFFER - sent)
            self.send(
                peer,
                seq=add(seq, sent),
                ack=ack,
                flags=ACK,
                data=b"b" * length,
            )
            sent += length
            response = self.receive()
            self.assertEqual(response["ack"], add(seq, sent))
            self.assertEqual(response["window"], min(65535, BUFFER - sent))
        self.send(
            peer, seq=add(seq, sent), ack=ack, flags=ACK, data=b"overflow"
        )
        response = self.receive()
        self.assertEqual(
            (response["ack"], response["window"]), (add(seq, BUFFER), 0)
        )
        peer.request_close()  # Opens the application's read gate.
        response = self.receive()
        self.assertGreater(response["window"], 0)
        self.assertEqual(peer.line(), "TRANSFER_DONE")
        self.assertEqual(peer.output.read_bytes(), b"b" * BUFFER)
        peer.finish()

    def test_out_of_order_duplicates_and_overlap_are_bounded(self):
        peer = Peer(self, receive_length=6, read_gate=True)
        seq, ack = self.listener_handshake(peer, seq=MASK - 3)
        for _ in range(2):
            self.send(peer, seq=add(seq, 2), ack=ack, flags=ACK, data=b"cdef")
            response = self.receive()
            self.assertEqual(
                (response["ack"], response["window"]),
                (seq, min(65535, BUFFER)),
            )
        self.send(peer, seq=seq, ack=ack, flags=ACK, data=b"abc")
        response = self.receive()
        self.assertEqual(
            (response["ack"], response["window"]),
            (add(seq, 6), min(65535, BUFFER - 6)),
        )
        peer.request_close()
        self.assertEqual(peer.line(), "TRANSFER_DONE")
        self.assertEqual(peer.output.read_bytes(), b"abcdef")
        peer.finish()

    def test_zero_window_with_no_outstanding_data_recovers(self):
        peer = Peer(self, "initiator", self.port, outgoing=b"z")
        syn = self.receive()
        self.send(
            peer, seq=100, ack=add(syn["seq"], 1), flags=SYN | ACK, window=0
        )
        final = self.receive()
        self.quiet()
        probe = self.receive()
        self.assertEqual(probe["seq"], add(final["seq"], -1))
        self.send(peer, seq=101, ack=final["seq"], flags=ACK, window=1)
        data = self.receive()
        self.assertEqual((data["seq"], data["data"]), (final["seq"], b"z"))
        self.send(peer, seq=101, ack=add(data["seq"], 1), flags=ACK)
        peer.finish()


# Reuse handshake, peer-isolation, data integrity, and teardown regressions.
for name in (
    "test_initiator_retransmits_same_syn",
    "test_data_retransmission_piggybacks_latest_ack",
    "test_initiator_receives_data_with_relay_header_ports",
    "test_handshake_and_data_through_payload_preserving_udp_relay",
    "test_listener_retransmits_same_syn_ack",
    "test_listener_rejects_malformed_packets_then_accepts_syn",
    "test_initiator_rejects_bad_syn_ack",
    "test_listener_rejects_invalid_final_ack",
    "test_duplicate_handshake_packets",
    "test_listener_duplicate_syn",
    "test_wrong_peer_cannot_hijack_connection",
    "test_random_initial_sequences",
    "test_lost_final_data_ack_during_close",
    "test_no_application_data_before_handshake",
    "test_initiator_can_start_before_listener",
    "test_handshake_with_initial_sequence_at_uint32_max",
    "test_initiator_close_waits_for_handshake",
    "test_listener_close_waits_for_final_handshake_ack",
    "test_fin_retransmission_and_receive_after_own_fin_ack",
    "test_combined_fin_ack_with_and_without_payload",
    "test_peer_fin_delivers_eof_and_allows_response",
    "test_simultaneous_fin_and_sequence_wraparound",
    "test_bidirectional_lossless_transfer",
    "test_bidirectional_lossy_transfer",
):
    setattr(CheckpointTwo, name, getattr(CheckpointOne, name))

if __name__ == "__main__":
    unittest.main(defaultTest="CheckpointTwo", verbosity=2)
