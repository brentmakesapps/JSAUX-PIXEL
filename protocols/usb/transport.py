"""Host USB-serial transport for DY frames (open / write / reply / GIF session)."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

try:  # advisory locking of the link; absent on platforms without flock
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

from .dy_frame import (
    DEFAULT_BAUDRATE,
    HEADER,
    EncodedTransfer,
    ParsedFrame,
    build_brightness,
    build_builtin_animation,
    build_gif_end,
    build_screen_power,
    encode_gif_upload,
    is_known_usb_ids,
    parse_frame,
)

WRITE_TIMEOUT_S = 1.0
REPLY_TIMEOUT_S = 1.0
INTER_PACKET_DELAY_S = 0.012
PACKET_RETRIES = 3
SESSION_RETRIES = 2
READ_CHUNK = 256
# The screen needs a moment between power-off and power-on to let go of a
# transfer it is still holding; shorter holds have not cleared it.
POWER_CYCLE_HOLD_S = 3.0


class TransportError(RuntimeError):
    """Raised when the serial link or device rejects a transfer."""


@dataclass(frozen=True)
class PortInfo:
    device: str
    vid: int | None
    pid: int | None
    description: str = ""


def require_pyserial():
    try:
        import serial  # type: ignore
        from serial.tools import list_ports  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised via CLI message
        raise ImportError(
            "pyserial is required for USB serial I/O. Install with: pip install pyserial"
        ) from exc
    return serial, list_ports


def list_candidate_ports(list_ports_fn=None) -> list[PortInfo]:
    """Return serial ports whose USB VID/PID match the published allowlist."""
    if list_ports_fn is None:
        _, list_ports = require_pyserial()
        list_ports_fn = list_ports.comports
    found: list[PortInfo] = []
    for port in list_ports_fn():
        vid = getattr(port, "vid", None)
        pid = getattr(port, "pid", None)
        if vid is None or pid is None:
            continue
        if not is_known_usb_ids(int(vid), int(pid)):
            continue
        found.append(
            PortInfo(
                device=str(port.device),
                vid=int(vid),
                pid=int(pid),
                description=str(getattr(port, "description", "") or ""),
            )
        )
    return found


def _read_frame(
    serial_obj,
    *,
    timeout_s: float,
    expect_transport: int | None,
    expect_command: int | None,
) -> ParsedFrame:
    deadline = time.monotonic() + timeout_s
    buf = bytearray()
    while time.monotonic() < deadline:
        waiting = max(0.0, deadline - time.monotonic())
        serial_obj.timeout = min(0.05, waiting) if waiting else 0
        chunk = serial_obj.read(READ_CHUNK)
        if chunk:
            buf.extend(chunk)
            frame = _try_extract_frame(buf, expect_transport, expect_command)
            if frame is not None:
                return frame
        else:
            time.sleep(0.005)
    raise TransportError("reply timeout")


def _try_extract_frame(
    buf: bytearray,
    expect_transport: int | None,
    expect_command: int | None,
) -> ParsedFrame | None:
    while True:
        start = buf.find(HEADER)
        if start < 0:
            buf.clear()
            return None
        if start > 0:
            del buf[:start]
        if len(buf) < 8:
            return None
        payload_length = int.from_bytes(buf[4:6], "big")
        total = 8 + payload_length
        if len(buf) < total:
            return None
        raw = bytes(buf[:total])
        del buf[:total]
        try:
            parsed = parse_frame(raw)
        except ValueError:
            continue
        if expect_transport is not None and parsed.transport_type != expect_transport:
            continue
        if expect_command is not None and parsed.command != expect_command:
            continue
        return parsed


class UsbSerialTransport:
    """Open a serial port and exchange DY frames with timeout / retry policy."""

    def __init__(
        self,
        port: str | None = None,
        *,
        baudrate: int = DEFAULT_BAUDRATE,
        write_timeout_s: float = WRITE_TIMEOUT_S,
        reply_timeout_s: float = REPLY_TIMEOUT_S,
        inter_packet_delay_s: float = INTER_PACKET_DELAY_S,
        packet_retries: int = PACKET_RETRIES,
        session_retries: int = SESSION_RETRIES,
        serial_factory: Callable[..., object] | None = None,
        auto_open: bool = True,
    ) -> None:
        self.baudrate = int(baudrate)
        self.write_timeout_s = float(write_timeout_s)
        self.reply_timeout_s = float(reply_timeout_s)
        self.inter_packet_delay_s = float(inter_packet_delay_s)
        self.packet_retries = max(1, int(packet_retries))
        self.session_retries = max(1, int(session_retries))
        self._serial_factory = serial_factory
        self._serial = None
        self._lock_handle = None
        self.port = port
        if auto_open:
            self.open(port)

    def _acquire_link_lock(self, device: str) -> None:
        """One process at a time on the panel.

        Two writers on one serial link is how the panel ends up deaf: their
        frames interleave, the tip is left holding half a blob, and after that
        it answers nothing until its screen is cycled.
        """
        if fcntl is None or self._lock_handle is not None:
            return
        handle = Path("/tmp", f"jsaux-pixel-{Path(device).name}.lock").open("a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = handle.read().strip() or "unknown"
            handle.close()
            raise TransportError(
                f"another process (pid {holder}) is driving the panel on {device}"
            )
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._lock_handle = handle

    def _release_link_lock(self) -> None:
        if self._lock_handle is None:
            return
        try:
            fcntl.flock(self._lock_handle, fcntl.LOCK_UN)
        except OSError:
            pass
        self._lock_handle.close()
        self._lock_handle = None

    def open(self, port: str | None = None) -> None:
        if self._serial is not None:
            return
        device = port or self.port
        if not device:
            candidates = list_candidate_ports()
            if not candidates:
                raise TransportError("no allowlisted USB serial device found")
            device = candidates[0].device
        self.port = device
        self._acquire_link_lock(device)
        if self._serial_factory is not None:
            self._serial = self._serial_factory(
                port=device,
                baudrate=self.baudrate,
                timeout=self.reply_timeout_s,
                write_timeout=self.write_timeout_s,
            )
        else:
            serial, _ = require_pyserial()
            self._serial = serial.Serial(
                port=device,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.reply_timeout_s,
                write_timeout=self.write_timeout_s,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )

    def close(self) -> None:
        serial_obj = self._serial
        self._serial = None
        self._release_link_lock()
        if serial_obj is not None:
            close = getattr(serial_obj, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> "UsbSerialTransport":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def exchange(
        self,
        frame: bytes,
        *,
        expect_transport: int | None = None,
        expect_command: int | None = None,
        reply_timeout_s: float | None = None,
    ) -> ParsedFrame:
        if self._serial is None:
            raise TransportError("serial port is not open")
        if expect_transport is None and len(frame) >= 3:
            expect_transport = frame[2]
        if expect_command is None and len(frame) >= 4:
            expect_command = frame[3]
        timeout = self.reply_timeout_s if reply_timeout_s is None else float(reply_timeout_s)
        last_error: Exception | None = None
        for attempt in range(self.packet_retries):
            try:
                if attempt:
                    time.sleep(self.inter_packet_delay_s)
                self._write(frame)
                time.sleep(self.inter_packet_delay_s)
                return _read_frame(
                    self._serial,
                    timeout_s=timeout,
                    expect_transport=expect_transport,
                    expect_command=expect_command,
                )
            except (TransportError, OSError, ValueError) as exc:
                last_error = exc
        raise TransportError(f"exchange failed after retries: {last_error}")

    def set_power(self, enabled: bool) -> ParsedFrame:
        return self.exchange(build_screen_power(enabled))

    def set_brightness(self, brightness: int) -> ParsedFrame:
        return self.exchange(build_brightness(brightness))

    def play_animation(self, animation_id: int) -> ParsedFrame:
        return self.exchange(build_builtin_animation(animation_id))

    def recover_link(self, *, hold_s: float = POWER_CYCLE_HOLD_S) -> bool:
        """Bring the tip back after a half-finished upload.

        An interrupted run (Ctrl-C, a dropped frame) leaves the tip waiting for
        the rest of that blob, and from then on it answers nothing at all: every
        upload reads as "reply timeout" and only a re-plug seems to fix it.
        Closing the dangling session and power-cycling the screen clears it -
        measured 30/30 frames landing afterwards, 0/10 before.

        Returns whether the screen answered the power-on.
        """
        try:
            self.exchange(build_gif_end(), reply_timeout_s=min(2.0, self.reply_timeout_s))
        except (TransportError, OSError, ValueError):
            # The tip usually does not answer this; the power cycle is what works.
            pass
        hold = max(0.5, float(hold_s))
        awake = False
        for enabled in (False, True):
            try:
                self.set_power(enabled)
                awake = enabled
            except (TransportError, OSError, ValueError):
                awake = False
            time.sleep(hold)
        return awake

    def upload_gif(self, gif_data: bytes, *, chunk_size: int | None = None) -> ParsedFrame:
        if not gif_data:
            raise TransportError("gif payload is empty")
        kwargs = {} if chunk_size is None else {"chunk_size": int(chunk_size)}
        transfer = encode_gif_upload(gif_data, **kwargs)
        last_error: Exception | None = None
        for _ in range(self.session_retries):
            try:
                return self._upload_gif_once(transfer)
            except TransportError as exc:
                last_error = exc
                time.sleep(self.inter_packet_delay_s)
        raise TransportError(f"gif upload failed after session retries: {last_error}")

    def _upload_gif_once(self, transfer: EncodedTransfer) -> ParsedFrame:
        begin = self.exchange(transfer.preamble)
        status = begin.payload[0] if begin.payload else 0xFF
        if status == 0x00:
            raise TransportError("device rejected GIF begin")
        # 0x02 means "I already hold this exact blob, skip the rest".
        already_held = status == 0x02
        if not already_held:
            for chunk in transfer.chunks:
                reply = self.exchange(chunk)
                chunk_status = reply.payload[0] if reply.payload else 0xFF
                if chunk_status == 0x00:
                    raise TransportError("device rejected GIF chunk")
        # The device checksums the whole blob at finalize, which takes longer
        # than a per-packet reply. Timing out here triggers a session retry,
        # the device then answers "same content", and the retry ends in a
        # finalize rejection — so give this reply real headroom.
        finish_timeout = min(30.0, max(self.reply_timeout_s, 2.0 + len(transfer.chunks) * 0.25))
        finish = self.exchange(transfer.finish, reply_timeout_s=finish_timeout)
        finish_status = finish.payload[0] if finish.payload else 0xFF
        if finish_status == 0x00:
            if already_held:
                # The device had this content already, so it skipped the
                # chunks and refused to re-commit it — the frame is on screen,
                # which is what the upload was for.
                return finish
            raise TransportError("device rejected GIF finalize")
        return finish

    def _drain_input(self) -> None:
        """Drop stale RX bytes so a previous reply cannot match the next command."""
        serial_obj = self._serial
        if serial_obj is None:
            return
        reset = getattr(serial_obj, "reset_input_buffer", None)
        if callable(reset):
            try:
                reset()
                return
            except OSError:
                pass
        # Fallback for test doubles / odd backends without reset_input_buffer.
        waiting = getattr(serial_obj, "in_waiting", 0)
        try:
            pending = int(waiting() if callable(waiting) else waiting)
        except (TypeError, ValueError, OSError):
            pending = 0
        if pending > 0:
            try:
                serial_obj.read(pending)
            except OSError:
                return

    def _write(self, frame: bytes) -> None:
        self._drain_input()
        written = self._serial.write(frame)
        flush = getattr(self._serial, "flush", None)
        if callable(flush):
            flush()
        if written is not None and int(written) != len(frame):
            raise TransportError(f"short write: {written}/{len(frame)}")


def iter_ports_as_dicts(ports: Iterable[PortInfo]) -> list[dict]:
    return [
        {
            "device": p.device,
            "vid": None if p.vid is None else f"0x{p.vid:04X}",
            "pid": None if p.pid is None else f"0x{p.pid:04X}",
            "description": p.description,
        }
        for p in ports
    ]
