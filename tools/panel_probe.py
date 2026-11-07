#!/usr/bin/env python3
"""Measure what the panel link can actually carry, so the design uses numbers.

Every number in this repository that describes the link came from a probe like
this one, and any of them can rot - a different chunk size, cable or hub, or a
firmware quirk changes the answer. So the measurements live here:

    python3 tools/panel_probe.py preflight             # who owns the port?
    python3 tools/panel_probe.py chunks                # cost per chunk, max chunk
    python3 tools/panel_probe.py ceiling               # biggest GIF it accepts
    python3 tools/panel_probe.py frames                # most frames in one upload
    python3 tools/panel_probe.py framerate             # fastest delay_cs it honours
    python3 tools/panel_probe.py cost                  # bytes per frame by motion
    python3 tools/panel_probe.py rotation              # cost of swapping screens
    python3 tools/panel_probe.py latency               # data change -> on screen
    python3 tools/panel_probe.py sleep --watch 180     # when does it go quiet
    python3 tools/panel_probe.py all --report probe.json

Two rules are enforced, because each of them already cost hours once:

* **one writer** - the probe refuses to measure unless it holds the port lock,
  and names the process holding it when something else does;
* **your panel comes back** - if the probe pauses the panel service it restores
  it on the way out, including after a crash.

Upload time on this link tracks *round trips*, not bytes: a chunk costs about
70ms of exchange while its bytes cost about 4ms. That is why the chunk test is
the first one worth running.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "decky-plugin", ROOT / "tools"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import panel_cam  # noqa: E402
from backend import ai_agent_screen as screen  # noqa: E402
from backend import gif_encoder  # noqa: E402
from backend import local_ai_stats  # noqa: E402
from protocols.usb.dy_frame import (  # noqa: E402
    GIF_CHUNK_DATA_SIZE as GIF_CHUNK_DEFAULT,
    build_builtin_animation,
)
from protocols.usb.transport import TransportError, UsbSerialTransport  # noqa: E402

PORT = "/dev/ttyUSB0"
BAUD = 1_000_000
PANEL_SERVICE = "jsaux-pixel-panel"
# Written while the panel service is paused: if the probe is killed, the next
# one reads this and puts the panel back before measuring anything.
MARKER = Path("/tmp/jsaux-pixel-probe.marker")
SAMPLES = 5
TEST_ORDER = ["chunks", "dedupe", "ceiling", "frames", "framerate", "codec", "cost",
              "rotation", "latency", "sleep", "canvas", "loop", "brightness",
              "animation"]
W, H = screen.MATRIX_WIDTH, screen.MATRIX_HEIGHT
PIXELS = W * H
# Fixtures build index lists straight into their own palette, because the byte
# cost is what is being measured and it depends on the palette size.
PALETTE = [
    (0, 0, 0),        # 0 black
    (235, 235, 235),   # 1 white
    (40, 220, 110),    # 2 green
    (250, 170, 40),    # 3 amber
    (40, 200, 230),    # 4 cyan
    (240, 60, 60),     # 5 red
    (70, 120, 250),    # 6 blue
    (90, 90, 90),      # 7 dim
]


def blank(index: int = 0) -> list:
    return [index] * PIXELS


def noise_frame(seed: int) -> list:
    """A full frame of scattered pixels: nothing to patch, the worst case."""
    out = []
    state = (seed + 1) * 0x5DE0
    for _ in range(PIXELS):
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        out.append((state >> 13) % len(PALETTE))
    return out


def encode(buffers, delay_cs: int = 10, compress: bool = False) -> bytes:
    return gif_encoder.encode(W, H, PALETTE, buffers, delay_cs=max(1, delay_cs),
                               compress=compress)


def walk_gif(frames: int, delay_cs: int, run: int = 4, compress: bool = False) -> bytes:
    """A bright run walking a dim ring: motion, and hardly anything else moves.

    The run has to be clearly brighter than the ring it walks, or the camera
    measurement has nothing to count.
    """
    ring = screen.ring_cells(screen.SPINNER_X, screen.SPINNER_Y, screen.SPINNER_SIZE)
    buffers = []
    for frame in range(max(1, frames)):
        indices = blank(0)
        for cx, cy in ring:
            indices[cy * W + cx] = 7
        for offset in range(run):
            cx, cy = ring[(frame + offset) % len(ring)]
            indices[cy * W + cx] = 1
        buffers.append(indices)
    return encode(buffers, delay_cs, compress=compress)


def noise_gif(frames: int, seed: int = 0) -> bytes:
    return encode([noise_frame(seed * 977 + index) for index in range(max(1, frames))])


def sized_gif(target_bytes: int, seed: int = 0) -> bytes:
    """A GIF as close to ``target_bytes`` as whole noise frames can get.

    ``seed`` makes it a different blob: the tip dedupes by the CRC in GIF begin
    and answers "same content, skip the rest", so a repeated blob is not a real
    transfer and measures nothing.
    """
    one = len(noise_gif(1, seed))
    per = len(noise_gif(2, seed)) - one
    frames = max(2, int((target_bytes - one) / max(1, per)) + 1)
    gif = noise_gif(frames, seed)
    while len(gif) < target_bytes and frames < 400:
        frames += 1
        gif = noise_gif(frames, seed)
    return gif


def stats(samples) -> dict:
    good = [value for value in samples if value is not None]
    row = {"n": len(samples), "failures": len(samples) - len(good)}
    if good:
        row.update(median=round(statistics.median(good), 3),
                    min=round(min(good), 3), max=round(max(good), 3),
                    mean=round(statistics.fmean(good), 3))
    return row


class Probe:
    def __init__(self, args):
        self.args = args
        self.bus = None
        self.results = {}
        self.paused_service = False
        self.last_error = ""
        # A measurement that cannot finish is worse than no measurement: each one
        # reports "out of time" from inside this box instead of running forever.
        self.deadline = float("inf")

    # ---------- plumbing -------------------------------------------------
    def log(self, message: str) -> None:
        print(message, flush=True)

    def expired(self) -> bool:
        return time.monotonic() >= self.deadline

    def holders(self) -> list:
        """Anything else that could be driving the panel."""
        try:
            out = subprocess.run(["ps", "-eo", "pid,args"], check=True,
                                 capture_output=True, text=True).stdout
        except (OSError, subprocess.CalledProcessError):
            return []
        hits = []
        for line in out.splitlines():
            if "panel_probe" in line or "grep" in line:
                continue
            if "ai_agent_screen" in line or "matrix_cli" in line:
                hits.append(line.strip())
        return hits

    def pause_service(self) -> None:
        # A run that was killed cannot restore what it paused, so the marker
        # says "a probe died mid-flight" and the next one puts things back first.
        if MARKER.exists():
            self.log("a previous probe left the panel stopped - starting it back up")
            subprocess.run(["systemctl", "--user", "start", PANEL_SERVICE],
                           check=False, capture_output=True, text=True)
            MARKER.unlink(missing_ok=True)
        if self.args.no_pause:
            self.log("leaving the panel service up (-n): the lock decides who wins")
            return
        try:
            subprocess.run(["systemctl", "--user", "stop", PANEL_SERVICE],
                           check=False, capture_output=True, text=True)
            self.paused_service = True
            MARKER.write_text(str(time.time()), encoding="utf-8")
            self.log(f"paused {PANEL_SERVICE}.service (restored on the way out)")
            time.sleep(0.5)
        except OSError as exc:
            self.log(f"could not pause the service: {exc}")

    def restore_service(self) -> None:
        if not self.paused_service:
            return
        try:
            subprocess.run(["systemctl", "--user", "start", PANEL_SERVICE],
                           check=False, capture_output=True, text=True)
            MARKER.unlink(missing_ok=True)
            self.log(f"restored {PANEL_SERVICE}.service")
        except OSError as exc:
            self.log(f"restore failed ({exc}) - start it by hand: "
                     f"systemctl --user start {PANEL_SERVICE}")

    def connect(self) -> bool:
        """Take the port. The lock is the writer check, so failing says who holds."""
        try:
            self.bus = UsbSerialTransport(port=self.args.port, baudrate=self.args.baud,
                                          reply_timeout_s=self.args.timeout)
            self.bus.open()
        except TransportError as exc:
            self.log(f"cannot take the port: {exc}")
            for line in self.holders():
                self.log(f"  running: {line}")
            return False
        except OSError as exc:
            self.log(f"cannot open {self.args.port}: {exc} "
                     f"(serial needs the dialout group: sg dialout -c '...')")
            return False
        try:
            reply = self.bus.set_power(True)
            self.log(f"panel answered power-on: 0x{bytes(reply.payload[:1]).hex() or '??'}")
        except (TransportError, OSError) as exc:
            self.log(f"panel did not answer a power frame: {exc}")
            if self.args.recover and self.bus.recover_link():
                self.log("recovered; measuring")
            else:
                self.log("recovery failed - nothing here is measurable")
                self.close()
                return False
        return True

    def close(self) -> None:
        if self.bus is not None:
            self.bus.close()
            self.bus = None

    def timed_upload(self, gif: bytes, chunk_size: int | None = None):
        """Seconds to land a blob, or None when it did not land."""
        try:
            started = time.monotonic()
            self.bus.upload_gif(gif, chunk_size=chunk_size)
            return time.monotonic() - started
        except TransportError as exc:
            self.last_error = str(exc)
            return None

    def recover_if_needed(self, failed: bool) -> None:
        if failed and self.args.recover:
            self.log("  recovering the link")
            self.bus.recover_link()
            time.sleep(0.3)

    def watch(self, seconds: float, turn_s: float = 0.0,
               led: str = "58,0,2,2") -> dict:
        """Film the panel while a blob plays and time what the mark did.

        The watched cell is on the activity ring, so the signal repeating there
        is one turn of the mark. Its period against the turn length the upload
        was built for is the tip's real playback rate - the measurement that
        used to need a pair of eyes.
        """
        if self.args.no_camera:
            return {}
        directory = Path("/tmp/opencode-panel-probe-movie")
        try:
            panel_cam.movie(panel_cam.DEVICE, directory, max(0.4, seconds),
                            float(self.args.movie_fps), "", "")
            report = panel_cam.motion(directory, panel_cam.led_cell(led) or "0,0,8,8",
                                      float(self.args.movie_fps),
                                      min_s=self.args.min_period)
        except (OSError, subprocess.CalledProcessError) as exc:
            return {"camera_error": str(exc)}
        if turn_s and report.get("period_s"):
            report["expected_turn_s"] = round(turn_s, 3)
            # 1.0 means it played the frames it was given at the delay asked for;
            # more than that means the tip was slower than it was told.
            report["playback_ratio"] = round(report["period_s"] / turn_s, 2)
        if turn_s and turn_s < self.args.min_period:
            report["resolvable"] = False
            report["resolvable_note"] = (f"a {turn_s:.2f}s turn is below the "
                                         f"{self.args.min_period}s the {self.args.movie_fps:g} "
                                         f"fps camera can resolve")
        return report

    # ---------- measurements --------------------------------------------
    def test_preflight(self) -> None:
        others = self.holders()
        self.log(f"other panel processes: {len(others)}")
        for line in others:
            self.log(f"  {line}")
        self.results["preflight"] = {"other_processes": others,
                                      "port": self.args.port, "baud": self.args.baud}

    def test_chunks(self) -> None:
        """Upload time against chunk size - with a different blob every sample.

        The tip dedupes by the CRC in GIF begin, so uploading the same bytes
        twice measures the reply, not the transfer.
        """
        target = int(self.args.size * 1000)
        rows = []
        for chunk in self.args.chunks:
            samples = []
            for attempt in range(self.args.samples):
                blob = sized_gif(target, seed=attempt + 1)
                samples.append(self.timed_upload(blob, chunk_size=chunk))
            row = {"chunk": chunk, "bytes": len(blob),
                   "round_trips": -(-len(blob) // chunk), **stats(samples)}
            if row["failures"] == self.args.samples:
                row["error"] = self.last_error
            rows.append(row)
            self.log(f"  chunk {chunk:6d} ({row['round_trips']:3d} round trips) -> "
                     f"{row.get('median', '-'):>6}s, failures {row['failures']}")
            self.recover_if_needed(any(value is None for value in samples))
        self.results["chunks"] = {"rows": rows}

    def test_dedupe(self) -> None:
        """Does the tip skip a blob whose CRC it already has?"""
        blob = sized_gif(int(self.args.size * 1000), seed=11)
        first = self.timed_upload(blob)
        repeats = [self.timed_upload(blob) for _ in range(self.args.samples)]
        row = {"bytes": len(blob), "first_seconds": round(first, 3) if first else None,
               **{"repeat_" + key: value for key, value in stats(repeats).items()}}
        self.results["dedupe"] = row
        self.log(f"  {len(blob)} bytes: fresh {row['first_seconds']}s, "
                 f"repeat median {row.get('repeat_median', '-')}s")

    def test_ceiling(self) -> None:
        """How big a GIF the tip will take at all."""
        rows = []
        last_ok, first_bad = 0, 0
        for target in (16, 20, 24, 26, 28, 30, 32, 36, 40):
            if self.expired():
                self.log("  out of time for this test")
                break
            blob = sized_gif(target * 1000)
            seconds = self.timed_upload(blob)
            ok = seconds is not None
            rows.append({"bytes": len(blob), "ok": ok,
                         "seconds": round(seconds, 2) if ok else None,
                         "error": None if ok else self.last_error})
            self.log(f"  {len(blob):6d} bytes -> {'ok' if ok else 'rejected'}")
            if ok:
                last_ok = max(last_ok, len(blob))
            elif not first_bad:
                first_bad = len(blob)
            self.recover_if_needed(not ok)
        if last_ok and first_bad > last_ok:
            low, high = last_ok, first_bad
            tried = set()
            while high - low > 512 and not self.expired():
                blob = sized_gif((low + high) // 2, seed=high)
                if len(blob) in tried:
                    # Frames are ~2KB each, so the size cannot be refined below
                    # that - the boundary is a whole frame wide.
                    self.log(f"  boundary is frame-granular at {len(blob)} bytes")
                    break
                tried.add(len(blob))
                ok = self.timed_upload(blob) is not None
                rows.append({"bisect": True, "bytes": len(blob), "ok": ok})
                self.log(f"  bisect {len(blob):6d} bytes -> {'ok' if ok else 'rejected'}")
                if ok:
                    low = max(low, len(blob))
                else:
                    high = min(high, len(blob))
                self.recover_if_needed(not ok)
            last_ok = low
        self.results["ceiling"] = {"max_bytes": last_ok, "first_rejected": first_bad,
                                   "rows": rows}
        self.log(f"  ceiling: {last_ok} accepted, {first_bad} rejected")

    def test_frames(self) -> None:
        """How many frames one upload may carry."""
        rows = []
        for frames in (32, 64, 128, 256):
            if self.expired():
                self.log("  out of time for this test")
                break
            blob = walk_gif(frames, delay_cs=6)
            seconds = self.timed_upload(blob)
            ok = seconds is not None
            row = {"frames": frames, "bytes": len(blob), "ok": ok,
                   "seconds": round(seconds, 2) if ok else None}
            if ok:
                row.update(self.watch(max(1.5, frames * 0.06 + 0.6), turn_s=0.96))
            rows.append(row)
            self.log(f"  {frames:4d} frames, {len(blob):6d} bytes -> "
                     f"{'ok' if ok else 'rejected'}, turn "
                     f"{row.get('period_s', '-')}s asked {row.get('expected_turn_s', '-')}s")
            self.recover_if_needed(not ok)
        self.results["frames"] = {"needs_eyes": True, "rows": rows}

    def test_framerate(self) -> None:
        """Fastest frame delay the tip actually honours.

        Each blob walks the 16-cell ring twice, so the watched ring cell should
        change twice a turn: if the count comes up short, the tip floored the
        delay rather than playing it.
        """
        rows = []
        for delay in (1, 2, 4, 6, 10):
            if self.expired():
                self.log("  out of time for this test")
                break
            blob = walk_gif(32, delay_cs=delay)
            seconds = self.timed_upload(blob)
            ok = seconds is not None
            play = 32 * delay / 100.0
            row = {"delay_cs": delay, "nominal_fps": round(100 / delay, 1),
                   "plays_s": round(play, 2), "bytes": len(blob), "ok": ok}
            if ok:
                row.update(self.watch(max(1.4, play + 0.6), turn_s=16 * delay / 100.0))
            rows.append(row)
            self.log(f"  delay {delay:2d}cs ({100 / delay:5.1f} fps) for {play:4.1f}s "
                     f"-> {'ok' if ok else 'rejected'}, "
                     f"turn {row.get('period_s', '-')}s asked "
                     f"{row.get('expected_turn_s', '-')}s"
                     f"{', ratio ' + str(row.get('playback_ratio')) if row.get('playback_ratio') else ''}")
            self.recover_if_needed(not ok)
        self.results["framerate"] = {"rows": rows,
                                     "note": "a ratio of 1 means the tip played every "
                                             "frame it was given; below 1 means it "
                                             "floored the delay"}
        if not self.args.no_camera:
            self.log("  ratio 1 = it really played them; below 1 = the delay was floored")

    def test_codec(self) -> None:
        """Does the tip read a compressed LZW stream, and play it at the same rate?

        One ring walk, encoded twice: literal codes, then real LZW. The frames are
        identical, so if the second is accepted and the watched turn matches, what
        an animation costs follows what changes rather than how large the area
        that changes is - which is the difference between a two second loop and a
        five second one.
        """
        rows = []
        for label, kwargs in (("literal", {}), ("compressed", {"compress": True})):
            blob = walk_gif(64, 4, **kwargs)
            row = {"stream": label, "bytes": len(blob),
                   "crc32": zlib.crc32(blob) & 0xFFFFFFFF}
            # The signal period is one trip of the mark round the ring, not the
            # length of the loop: with 16 ring cells and a 4 cs frame the mark
            # repeats every 0.64 s however many frames were uploaded.
            turn = screen.RING_CELLS * 4 / 100.0
            row["expected_mark_period_s"] = round(turn, 3)
            seconds = self.timed_upload(blob)
            row["ok"] = seconds is not None
            row["seconds"] = round(seconds, 2) if seconds is not None else None
            if row["ok"]:
                row.update(self.watch(5.0, turn_s=turn))
            rows.append(row)
            # A period that is a whole number of mark trips is the 30 fps camera
            # sampling the same rate, not slower playback.
            period = row.get("period_s")
            if period:
                multiple = period / turn
                row["mark_trips_per_period"] = round(multiple, 2)
                row["rate_matches"] = abs(multiple - round(multiple)) < 0.15
            self.log(f"  {label:10s} {len(blob):6d} bytes crc={row['crc32']:#010x} -> "
                     f"{'ok' if row['ok'] else 'rejected'}, mark period "
                     f"{row.get('period_s', '-')}s asked "
                     f"{row.get('expected_mark_period_s', '-')}s "
                     f"{'' if row.get('rate_matches') is None else ('same rate' if row['rate_matches'] else 'DIFFERENT RATE')}")
            self.recover_if_needed(not row["ok"])
        self.results["codec"] = {"needs_eyes": True, "rows": rows,
                                 "note": "same frames both ways; a matching turn "
                                         "means the tip decoded the compressed "
                                         "stream correctly. The crc is recorded "
                                         "because the tip ignores a GIF begin it "
                                         "has already seen."}

    def test_cost(self) -> None:
        """Bytes per frame against the area that moves - no hardware needed."""
        rows = []
        for label, painter in (
            ("ring run (4 cells)", self._pattern_run),
            ("one ticker row", self._pattern_row),
            ("16x16 block", lambda indices, frame: self._pattern_block(indices, frame, 16)),
            ("42x42 block", lambda indices, frame: self._pattern_block(indices, frame, 42)),
            ("every pixel", None),
        ):
            if painter is None:
                frames = [noise_frame(index) for index in range(8)]
                whole, first = encode(frames, 6), encode(frames[:1], 6)
                packed, first_packed = encode(frames, 6, compress=True), \
                    encode(frames[:1], 6, compress=True)
            else:
                buffers = []
                for frame in range(8):
                    indices = blank(0)
                    painter(indices, frame)
                    buffers.append(indices)
                whole, first = encode(buffers, 6), encode(buffers[:1], 6)
                packed = encode(buffers, 6, compress=True)
                first_packed = encode(buffers[:1], 6, compress=True)
            per_frame = (len(whole) - len(first)) / 7.0
            packed_frame = (len(packed) - len(first_packed)) / 7.0
            rows.append({"motion": label, "bytes_per_frame": round(per_frame, 1),
                         "first_frame": len(first), "eight_frames": len(whole),
                         "compressed_bytes_per_frame": round(packed_frame, 1),
                         "compressed_eight_frames": len(packed)})
            self.log(f"  {label:20s} {per_frame:8.1f} bytes a frame literal, "
                     f"{packed_frame:8.1f} compressed")
        self.results["cost"] = {"rows": rows,
                                "note": "literal is what everything was measured "
                                        "against first; compressed is what "
                                        "encode_frames sends by default now"}

    def _pattern_run(self, indices, frame) -> None:
        ring = screen.ring_cells(screen.SPINNER_X, screen.SPINNER_Y, screen.SPINNER_SIZE)
        for cx, cy in ring:
            indices[cy * W + cx] = 7
        for offset in range(4):
            cx, cy = ring[(frame + offset) % len(ring)]
            indices[cy * W + cx] = 2

    def _pattern_row(self, indices, frame) -> None:
        row = 8 + (frame % 30)
        for x in range(W):
            indices[row * W + x] = 4

    def _pattern_block(self, indices, frame, size) -> None:
        x0 = (frame % 4) * 2
        y0 = 6 + (frame % 3) * 2
        for y in range(y0, min(H, y0 + size)):
            for x in range(x0, min(W, x0 + size)):
                indices[y * W + x] = 3

    def test_rotation(self) -> None:
        """Cost of swapping one screen for another, which sets the rotation floor."""
        samples = []
        for index in range(max(4, self.args.samples * 2)):
            if self.expired():
                self.log("  out of time for this test")
                break
            # Distinct content every time: the tip refuses a blob it already has.
            samples.append(self.timed_upload(walk_gif(16 + index, delay_cs=6)))
        row = stats(samples)
        floor = round(row.get("max", 0.0) * 2, 2)
        self.results["rotation"] = {**row, "floor_seconds": floor}
        self.log(f"  median {row.get('median')}s, worst {row.get('max')}s, "
                 f"failures {row['failures']}")
        self.log(f"  a safe rotation interval is about {floor}s")

    def test_latency(self) -> None:
        """Provider data changes -> acknowledged on the panel."""
        seen = None
        marks = []
        deadline = time.monotonic() + self.args.watch
        while time.monotonic() < deadline and len(marks) < self.args.samples:
            snapshot = local_ai_stats.collect()
            gif = screen.render_stats_gif(
                snapshot, 0, delay_cs=6, frames=16,
                start_cell=screen.playback_cells(time.monotonic() % 30, 0.96))
            if gif != seen:
                detected = time.monotonic()
                seconds = self.timed_upload(gif)
                if seconds is not None:
                    seen = gif
                    marks.append(time.monotonic() - detected)
                self.recover_if_needed(seconds is None)
            time.sleep(0.4)
        row = stats(marks)
        self.results["latency"] = {**row, "changes_seen": len(marks)}
        self.log(f"  changes seen: {len(marks)}, median change->on-screen "
                 f"{row.get('median', '-')}s")

    def test_sleep(self) -> None:
        """How long an untouched panel keeps answering.

        Any probe can reset the panel's own idle timer, so the silent form
        (``--poll 0``) leaves it completely alone for ``--watch`` seconds and
        only asks afterwards - that is the one worth running.
        """
        rows = []
        started = time.monotonic()
        if self.args.poll <= 0:
            self.log(f"leaving the panel alone for {self.args.watch:.0f}s - watch it "
                     f"and note when it goes dark")
            time.sleep(self.args.watch)
            probes = [time.monotonic() - started]
        else:
            probes = []
            while time.monotonic() - started < self.args.watch:
                time.sleep(self.args.poll)
                probes.append(time.monotonic() - started)
        for at in probes:
            try:
                reply = self.bus.set_power(True)
                rows.append({"t": round(at, 1), "status": bytes(reply.payload[:1]).hex()})
                self.log(f"  {at:6.1f}s -> 0x{bytes(reply.payload[:1]).hex()}")
            except (TransportError, OSError) as exc:
                rows.append({"t": round(at, 1), "error": str(exc)})
                self.log(f"  {at:6.1f}s -> no answer: {exc}")
                break
        self.results["sleep"] = {"rows": rows,
                                 "answered_for_s": rows[-1]["t"] if rows else 0}

    # ---------- what the design assumes, tested --------------------------
    def cell_mean(self, led: str) -> float:
        """Average brightness of an LED rectangle, as the camera sees it."""
        frame = panel_cam.grab(panel_cam.DEVICE, Path("/tmp/opencode-probe-grab.png"),
                                "", 1.0, "", 0.0, panel_cam.WARMUP)
        rect = panel_cam.led_cell(led) or "0,0,8,8"
        x, y, width, height = (int(part) for part in rect.split(","))
        out = subprocess.run(["magick", str(frame), "-crop", f"{width}x{height}+{x}+{y}",
                              "+repage", "-colorspace", "Gray", "-format", "%[mean]",
                              "info:"], check=True, capture_output=True, text=True)
        try:
            return float(out.stdout.strip().rstrip("%"))
        except ValueError:
            return 0.0

    def test_canvas(self) -> None:
        """Does a GIF with a smaller canvas replace the screen or only its corner?

        The renderer draws the whole 64x54 every time. If a small canvas
        composited instead, a notification could be a 16x16 patch - a tenth of
        the bytes - with the stats left where they are.
        """
        field = encode([blank(1)], 20)          # whole screen lit
        if self.timed_upload(field) is None:
            self.results["canvas"] = {"error": self.last_error}
            return
        time.sleep(0.5)
        before = self.cell_mean("24,24,8,8")
        small = gif_encoder.encode(16, 16, PALETTE, [blank(0)], delay_cs=100)
        accepted = self.timed_upload(small) is not None
        time.sleep(0.5)
        after = self.cell_mean("24,24,8,8")
        corner = self.cell_mean("1,1,8,8")
        verdict = "unknown"
        if accepted:
            if after > before * 0.5:
                verdict = "composites: the rest of the screen survived"
            elif after <= before * 0.2:
                verdict = "clears: a small canvas replaces everything"
        self.results["canvas"] = {"accepted": accepted, "lit_before": round(before, 1),
                                   "after_small_upload": round(after, 1),
                                   "small_canvas_area": round(corner, 1),
                                   "verdict": verdict}
        self.log(f"  small canvas accepted: {accepted}; far side {before:.0f} -> "
                 f"{after:.0f}; its own corner {corner:.0f} -> {verdict}")

    def test_loop(self) -> None:
        """Does the panel keep looping the upload, or stop after a while?

        Every animation here assumes the loop is effectively endless; if the tip
        stopped, a screen at rest would freeze with the mark parked.
        """
        blob = walk_gif(16, delay_cs=4)
        if self.timed_upload(blob) is None:
            self.results["loop"] = {"error": self.last_error}
            return
        rows = []
        waited = 0.0
        for step in (2.0, 20.0, 40.0, 60.0):
            if self.expired():
                break
            time.sleep(step)
            waited += step
            report = self.watch(2.0, turn_s=16 * 0.04)
            rows.append({"at_s": round(waited, 1),
                         "period_s": report.get("period_s"),
                         "still_moving": bool(report.get("period_s"))})
            self.log(f"  at {waited:5.1f}s: "
                     f"{'still turning' if report.get('period_s') else 'STOPPED'}")
        self.results["loop"] = {"rows": rows,
                                 "still_looping_at_s": rows[-1]["at_s"] if rows else 0}

    def test_brightness(self) -> None:
        """Does the brightness setting survive a power cycle?"""
        field = encode([blank(1)], 20)
        if self.timed_upload(field) is None:
            self.results["brightness"] = {"error": self.last_error}
            return
        rows = []
        for level in (10, 100):
            try:
                self.bus.set_brightness(level)
                time.sleep(0.8)
                rows.append({"level": level, "mean": round(self.cell_mean("24,24,8,8"), 1)})
            except TransportError as exc:
                rows.append({"level": level, "error": str(exc)})
                self.log(f"  brightness {level}: no answer ({exc})")
        if any("error" in row for row in rows):
            self.results["brightness"] = {"rows": rows, "supported": False}
            return
        self.results["brightness"] = {"rows": rows, "supported": True}

    def test_animation(self) -> None:
        """Does the built-in animation command do anything on this tip?

        It costs one frame of wire time, so an attract mode built on it would be
        free compared with uploading - if the tip answers it at all.
        """
        rows = []
        for animation_id in (0, 1, 2, 3, 5, 8, 16, 20):
            try:
                reply = self.bus.exchange(build_builtin_animation(animation_id))
                status = bytes(reply.payload[:1]).hex() if reply.payload else "?"
                rows.append({"id": animation_id, "status": status})
                self.log(f"  animation {animation_id:2d} -> 0x{status}")
            except TransportError as exc:
                rows.append({"id": animation_id, "error": str(exc)})
                self.log(f"  animation {animation_id:2d} -> no answer")
            time.sleep(0.4)
        self.results["animation"] = {
            "rows": rows,
            "answered": [row["id"] for row in rows if "status" in row]}

    # ---------- runner ---------------------------------------------------
    def run(self, wanted: list) -> int:
        tests = {"chunks": self.test_chunks, "dedupe": self.test_dedupe,
                  "ceiling": self.test_ceiling,
                  "frames": self.test_frames, "framerate": self.test_framerate,
                  "codec": self.test_codec,
                  "cost": self.test_cost, "rotation": self.test_rotation,
                  "latency": self.test_latency, "sleep": self.test_sleep,
                  "canvas": self.test_canvas, "loop": self.test_loop,
                  "brightness": self.test_brightness,
                  "animation": self.test_animation}
        try:
            self.test_preflight()
            hardware = [name for name in wanted if name != "cost"]
            if hardware:
                self.pause_service()
                if not self.connect():
                    return 2
            if "cost" in wanted:
                self.log("--- cost (no hardware) ---")
                tests["cost"]()
            for name in hardware:
                self.log(f"--- {name} ---")
                self.deadline = time.monotonic() + self.args.per_test
                started = time.monotonic()
                tests[name]()
                self.log(f"  took {time.monotonic() - started:.1f}s")
            self.print_constants()
            if self.args.report:
                Path(self.args.report).write_text(json.dumps(self.results, indent=2),
                                                  encoding="utf-8")
                self.log(f"report: {self.args.report}")
        finally:
            self.close()
            self.restore_service()
        return 0

    def print_constants(self) -> None:
        rows = self.results.get("chunks", {}).get("rows", [])
        accepted = [row for row in rows if row["failures"] < self.args.samples]
        biggest = max((row["chunk"] for row in accepted), default=None)
        fastest = min(accepted, key=lambda row: row["median"]) if accepted else None
        frames_rows = self.results.get("frames", {}).get("rows", [])
        frames = max((row["frames"] for row in frames_rows if row["ok"]), default=None)
        self.log("CONSTANTS")
        self.log(f"  CHUNK_SIZE             {biggest or GIF_CHUNK_DEFAULT}"
                 + (f"  (fastest was {fastest['chunk']} at {fastest['median']}s)"
                    if fastest else ""))
        self.log(f"  MAX_GIF_BYTES          {self.results.get('ceiling', {}).get('max_bytes', '-')}")
        self.log(f"  MAX_FRAMES_PER_UPLOAD  {frames or '-'}")
        self.log(f"  ROTATION_FLOOR_S       {self.results.get('rotation', {}).get('floor_seconds', '-')}")
        floor = self.playback_floor()
        self.log(f"  MIN_DELAY_CS           {floor if floor else '-'}"
                 "  (from the filmed turn period)")

    def playback_floor(self):
        """The fastest frame delay the tip played at the rate it was asked for."""
        rows = self.results.get("framerate", {}).get("rows", [])
        honoured = [row["delay_cs"] for row in rows
                    if row.get("playback_ratio") and 0.85 <= row["playback_ratio"] <= 1.15]
        return min(honoured) if honoured else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="panel_probe",
                                     description="Measure the panel link's limits.")
    parser.add_argument("--port", default=PORT)
    parser.add_argument("--baud", type=int, default=BAUD)
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--samples", type=int, default=SAMPLES)
    parser.add_argument("--size", type=int, default=12,
                        help="blob size in KB for the chunk test")
    parser.add_argument("--chunks", default="498,996,2000,4000,8000,16000",
                        help="comma separated chunk sizes to try")
    parser.add_argument("--watch", type=float, default=60.0,
                        help="seconds for the latency and sleep tests")
    parser.add_argument("--poll", type=float, default=0.0,
                        help="seconds between power probes in the sleep test "
                             "(0 = leave it alone until the end)")
    parser.add_argument("--no-camera", action="store_true",
                        help="skip the webcam measurement (the old eyeball way)")
    parser.add_argument("--movie-fps", type=float, default=30.0,
                        help="camera rate for the watch measurements")
    parser.add_argument("--min-period", type=float, default=0.4,
                        help="shortest period the camera can be trusted with "
                             "(a 30 fps movie aliases anything faster)")
    parser.add_argument("--no-pause", action="store_true",
                        help="leave the panel service running (the lock decides)")
    parser.add_argument("--no-recover", action="store_false", dest="recover",
                        help="do not power-cycle the panel after a failure")
    parser.add_argument("--per-test", type=float, default=180.0,
                        help="time box for each measurement; it reports "
                             "'out of time' instead of running on")
    parser.add_argument("--report", default="", help="write the results as JSON here")
    parser.add_argument("tests", nargs="*", choices=TEST_ORDER + ["all", "preflight"],
                        help="which measurements to run")
    args = parser.parse_args(argv)
    args.chunks = [int(value) for value in args.chunks.split(",") if value.strip()]
    wanted = args.tests or ["all"]
    if "all" in wanted:
        wanted = TEST_ORDER
    return Probe(args).run(wanted)


if __name__ == "__main__":
    raise SystemExit(main())
