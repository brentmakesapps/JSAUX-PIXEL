#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from protocols.usb.dy_frame import (  # noqa: E402
    build_gif_end,
    build_screen_power,
    build_standard_frame,
    encode_gif_upload,
    parse_frame,
)
from protocols.usb.transport import (  # noqa: E402
    PortInfo,
    TransportError,
    UsbSerialTransport,
    list_candidate_ports,
)


class FakePort:
    def __init__(self, device: str, vid: int, pid: int, description: str = "") -> None:
        self.device = device
        self.vid = vid
        self.pid = pid
        self.description = description


class FakeSerial:
    """Minimal serial stand-in: scripted replies after each host write."""

    def __init__(self, replies: list[bytes] | None = None, **kwargs) -> None:
        del kwargs
        self.writes: list[bytes] = []
        self._replies = list(replies or [])
        self._inbox = bytearray()
        self.timeout = 1.0
        self.write_timeout = 1.0
        self.closed = False
        self.reset_calls = 0

    def reset_input_buffer(self) -> None:
        self.reset_calls += 1
        self._inbox.clear()

    def write(self, data: bytes) -> int:
        payload = bytes(data)
        self.writes.append(payload)
        if self._replies:
            self._inbox.extend(self._replies.pop(0))
        return len(payload)

    def read(self, size: int = 1) -> bytes:
        if not self._inbox:
            return b""
        take = bytes(self._inbox[:size])
        del self._inbox[:size]
        return take

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def _ack(frame: bytes, status: int = 0x01) -> bytes:
    parsed = parse_frame(frame)
    return build_standard_frame(parsed.transport_type, parsed.command, bytes([status]))


class TransportTests(unittest.TestCase):
    def test_list_candidate_ports_filters_allowlist(self) -> None:
        ports = list_candidate_ports(
            list_ports_fn=lambda: [
                FakePort("/dev/ttyOK", 0x1A86, 0x7523, "ok"),
                FakePort("/dev/ttyBad", 0x1234, 0x7523, "bad"),
            ]
        )
        self.assertEqual([p.device for p in ports], ["/dev/ttyOK"])
        self.assertIsInstance(ports[0], PortInfo)

    def test_exchange_returns_matching_reply(self) -> None:
        request = build_screen_power(True)
        fake = FakeSerial(replies=[_ack(request, 0x01)])

        def factory(**kwargs):
            return fake

        with UsbSerialTransport(port="/dev/fake", serial_factory=factory) as bus:
            reply = bus.set_power(True)
        self.assertEqual(reply.payload, b"\x01")
        self.assertEqual(len(fake.writes), 1)
        self.assertEqual(fake.reset_calls, 1)
        self.assertTrue(fake.closed)

    def test_drain_discards_stale_bytes_before_write(self) -> None:
        request = build_screen_power(True)
        stale = _ack(request, 0x7E)
        good = _ack(request, 0x01)
        fake = FakeSerial(replies=[good])
        fake._inbox.extend(stale)
        with UsbSerialTransport(
            port="/dev/fake",
            serial_factory=lambda **kwargs: fake,
            inter_packet_delay_s=0.0,
        ) as bus:
            reply = bus.exchange(request)
        self.assertEqual(reply.payload, b"\x01")
        self.assertEqual(fake.reset_calls, 1)

    def test_exchange_retries_after_bad_crc_then_ok(self) -> None:
        request = build_screen_power(False)
        good = _ack(request, 0x00)
        bad = good[:-1] + bytes([(good[-1] ^ 0xFF) & 0xFF])
        fake = FakeSerial(replies=[bad, good])

        with UsbSerialTransport(
            port="/dev/fake",
            serial_factory=lambda **kwargs: fake,
            inter_packet_delay_s=0.0,
            reply_timeout_s=0.05,
        ) as bus:
            reply = bus.exchange(request)
        self.assertEqual(reply.command, 0x01)
        self.assertEqual(len(fake.writes), 2)

    def test_gif_begin_status_skip_chunks(self) -> None:
        gif = b"GIF89a" + b"\x00" * 40
        transfer = encode_gif_upload(gif, chunk_size=16)
        replies = [
            _ack(transfer.preamble, 0x02),
            _ack(transfer.finish, 0x01),
        ]
        fake = FakeSerial(replies=replies)
        with UsbSerialTransport(
            port="/dev/fake",
            serial_factory=lambda **kwargs: fake,
            inter_packet_delay_s=0.0,
        ) as bus:
            finish = bus.upload_gif(gif, chunk_size=16)
        self.assertEqual(finish.command, 0x0B)
        # begin + finish only; chunks skipped
        self.assertEqual(len(fake.writes), 2)

    def test_gif_already_held_makes_a_finalize_rejection_harmless(self) -> None:
        # begin 0x02 = "same content, skip the chunks"; the device then has no
        # reason to accept a re-commit, but the frame is already displayed.
        gif = b"GIF89a" + b"\x00" * 40
        transfer = encode_gif_upload(gif, chunk_size=16)
        fake = FakeSerial(
            replies=[_ack(transfer.preamble, 0x02), _ack(transfer.finish, 0x00)]
        )
        with UsbSerialTransport(
            port="/dev/fake",
            serial_factory=lambda **kwargs: fake,
            inter_packet_delay_s=0.0,
        ) as bus:
            finish = bus.upload_gif(gif, chunk_size=16)
        self.assertEqual(finish.command, 0x0B)
        self.assertEqual(len(fake.writes), 2)

    def test_gif_begin_reject(self) -> None:
        gif = b"GIF89a" + b"\x11" * 8
        transfer = encode_gif_upload(gif, chunk_size=8)
        fake = FakeSerial(replies=[_ack(transfer.preamble, 0x00)] * 3)
        with UsbSerialTransport(
            port="/dev/fake",
            serial_factory=lambda **kwargs: fake,
            inter_packet_delay_s=0.0,
            session_retries=1,
            packet_retries=1,
        ) as bus:
            with self.assertRaises(TransportError):
                bus.upload_gif(gif, chunk_size=8)


    def test_recover_link_cycles_the_screen_power(self) -> None:
        """A stuck tip only starts answering again after a power cycle."""
        gif_end = build_gif_end()
        off = build_screen_power(False)
        on = build_screen_power(True)
        fake = FakeSerial(replies=[_ack(gif_end, 0x01), _ack(off, 0x01), _ack(on, 0x01)])
        with UsbSerialTransport(
            port="/dev/fake",
            serial_factory=lambda **kwargs: fake,
            inter_packet_delay_s=0.0,
            reply_timeout_s=0.05,
        ) as bus:
            awake = bus.recover_link(hold_s=0.05)
        self.assertTrue(awake)
        self.assertEqual(fake.writes, [gif_end, off, on])

    def test_recover_link_survives_a_silent_panel(self) -> None:
        fake = FakeSerial(replies=[])
        with UsbSerialTransport(
            port="/dev/fake",
            serial_factory=lambda **kwargs: fake,
            inter_packet_delay_s=0.0,
            reply_timeout_s=0.02,
            packet_retries=1,
        ) as bus:
            awake = bus.recover_link(hold_s=0.05)
        self.assertFalse(awake)
        # gif-end plus both power frames still went out.
        self.assertEqual(len(fake.writes), 3)

    def test_the_link_allows_only_one_writer(self) -> None:
        """Two drivers on one link is how the panel ends up holding half a frame."""
        port = "/dev/jsaux-lock-probe"
        first = UsbSerialTransport(port=port, serial_factory=lambda **kw: FakeSerial())
        try:
            with self.assertRaises(TransportError) as held:
                UsbSerialTransport(port=port, serial_factory=lambda **kw: FakeSerial())
            self.assertIn("driving the panel", str(held.exception))
        finally:
            first.close()
        with UsbSerialTransport(port=port, serial_factory=lambda **kw: FakeSerial()) as bus:
            self.assertIsNotNone(bus)


if __name__ == "__main__":
    unittest.main()
