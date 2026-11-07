#!/usr/bin/env python3
"""Preview or push the local-AI stats screen on the panel.

    python3 tools/ai_agent_screen.py --png preview.png      # look at it, no hardware
    python3 tools/ai_agent_screen.py --push                 # one frame over USB
    python3 tools/ai_agent_screen.py --push --live          # keep it fresh
    python3 tools/ai_agent_screen.py --demo --png demo.png  # fake numbers
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "decky-plugin"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from backend import ai_agent_screen as screen  # noqa: E402
from backend import local_ai_stats as stats_mod  # noqa: E402

# Pushes that fail before the panel is power-cycled back to life. One, because a
# failed push already means the panel is not answering, and the retry ladder it
# would otherwise climb costs three times what the power cycle does.
RECOVER_AFTER_FAILURES = 1
# One upload carries the motion the panel plays between uploads. frames x
# delay_cs has to stay ahead of the upload cadence, otherwise the panel loops
# the same GIF and the activity mark visibly rewinds - the bug that made the
# head jump back once a second.
# One upload carries the motion the panel plays between uploads: 16 frames, one
# cell a step, which is a whole turn of the activity mark (16 cells) at 16 steps a
# second. Frames are patched rather than resent, so that costs ~2.5KB, not 33KB.
DEFAULT_FRAMES = 16
# 4cs (25 steps a second) is as fast as the tip plays: the probe filmed 4cs and
# 6cs turning in exactly the asked time, but 2cs came out 1.46x slow, so the tip
# floors the delay near 3cs. Going faster than this buys nothing.
DEFAULT_DELAY_CS = 4

DEMO_STATS = {
    "gateway": {"model": "qwen3.8-flash-next-iq3_s-256k", "max_context": 262144, "reachable": True},
    "agent": {"read_tokens": 90000, "cache_read_tokens": 4200, "generated_tokens": 630,
              "sessions_active": 2, "error": None},
    "display": {"running": True, "read_percent": 36.0, "gen_percent": 7.7,
                "tokens_read_raw": 94200, "tokens_generated_raw": 630, "tok_per_s": 7.5,
                "sessions_active": 2},
}


def write_png(path: Path, pixels: list, width: int, height: int) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for color in pixels[y * width : (y + 1) * width]:
            rows.extend(color[:3])
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )


def recover_panel(args) -> bool:
    """Power-cycle the panel out of band, then let the next tick reconnect.

    A run that is interrupted mid-upload leaves the tip waiting for the rest of
    that blob, and from then on it answers nothing at all.
    """
    from protocols.usb.transport import UsbSerialTransport

    try:
        bus = UsbSerialTransport(port=args.port or None, baudrate=args.baudrate,
                                  reply_timeout_s=2.0)
    except Exception as exc:  # noqa: BLE001 - nothing to recover if we cannot open
        print(f"recovery skipped: {exc}", file=sys.stderr)
        return False
    try:
        return bus.recover_link()
    finally:
        bus.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai_agent_screen", description=__doc__)
    parser.add_argument("--png", default="", help="write a PNG preview of the frame")
    parser.add_argument("--gif", default="", help="write the GIF the panel plays")
    parser.add_argument("--push", action="store_true", help="upload the frame over USB")
    parser.add_argument("--live", action="store_true", help="with --push, refresh continuously")
    parser.add_argument("--demo", action="store_true", help="use fake numbers, no network or DB")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="seconds between stat polls")
    parser.add_argument("--tick", type=float, default=0.0,
                        help="--live upload cadence; 0 = just before each upload's "
                             "animation finishes (frames x delay), so the panel never "
                             "loops a stale GIF")
    parser.add_argument("--port", default="", help="serial device path (default: auto-detect)")
    parser.add_argument("--baudrate", type=int, default=1_000_000)
    parser.add_argument("--power", action="store_true",
                        help="kept for compatibility: a power-on frame is always sent, "
                             "and it is what tells us the panel is deaf")
    parser.add_argument("--frames", type=int, default=DEFAULT_FRAMES,
                        help="frames per upload: the panel animates these itself, so the "
                             "spinner turns between uploads (keep frames x delay a little "
                             "longer than the upload cadence)")
    parser.add_argument("--delay", type=int, default=DEFAULT_DELAY_CS,
                        help="per-frame delay in centiseconds")
    parser.add_argument("--spinner-step", type=int, default=0,
                        help="cells the activity mark moves per frame "
                             f"(0 = JSAUX_AI_AGENT_SPINNER_STEP or the default {screen.SPINNER_STEP}; "
                             "16 cells = one turn)")
    parser.add_argument("--for", type=float, default=0.0, dest="run_seconds",
                        help="--live: stop cleanly after this many seconds. Killing a run "
                             "mid-upload leaves the panel holding a half-finished blob and "
                             "it stops answering until it is power-cycled")
    args = parser.parse_args(argv)

    stats = DEMO_STATS if args.demo else stats_mod.collect()
    if not args.demo:
        gateway, agent = stats["gateway"], stats["agent"]
        print(
            f"model={gateway.get('model') or '-'} loaded={gateway.get('loaded')} "
            f"busy={agent.get('busy')} read={stats['display']['tokens_read']} "
            f"gen={stats['display']['tokens_generated']} read%={stats['display']['read_percent']} "
            f"gen%={stats['display']['gen_percent']} tok/s={stats['display']['tok_per_s']}"
        )

    cells = screen.spinner_cells_for(args.frames, args.spinner_step or None)
    # Each upload carries a whole turn of the mark, so the panel looping it is
    # invisible - and that loop keeps phase with this clock, so the next upload
    # has to be built to start on the cell the clock has reached, or the mark
    # jumps sideways the moment it lands.
    turn = screen.turn_seconds(args.frames, args.delay)
    # The turn keeps playing on the panel until the numbers change, so uploads
    # are paced by the stats poll, not by the animation.
    tick = args.tick if args.tick > 0 else max(0.2, args.interval)
    # An upload is not on the panel the instant it is built: the wire takes time.
    wire_seconds = screen.upload_seconds(2500)
    phase = 0
    started_at = time.monotonic()
    last_gif: bytes | None = None
    stats_at = time.monotonic()
    if args.live:
        print(f"upload every {tick}s, {args.frames} frames of {args.delay}cs = one "
              f"turn in {turn:.2f}s at {screen.RING_CELLS / turn:.0f} steps a second")
    # The link is held open across frames: reconnecting every tick churns the
    # tip's DTR line, and it stops answering.
    bus = None
    failures = 0
    pushed = False
    one_shot_tries = 0
    try:
        while True:
            pixels = screen.new_frame()
            screen.render_stats(pixels, stats, phase, step=cells)
            if args.png and not pushed and not one_shot_tries:
                write_png(Path(args.png), pixels, screen.MATRIX_WIDTH, screen.MATRIX_HEIGHT)
                print(f"wrote {args.png}")
            if args.gif and not pushed and not one_shot_tries:
                Path(args.gif).write_bytes(
                    screen.render_stats_gif(
                        stats, phase, delay_cs=args.delay, frames=args.frames, step=cells
                    )
                )
                print(f"wrote {args.gif}")
            if not args.push:
                return 0

            from protocols.usb.transport import TransportError, UsbSerialTransport

            # Built to start on the cell the panel's own loop will be on when
            # this upload lands, so the replacement is seamless.
            cell = screen.playback_cells(time.monotonic() - started_at + wire_seconds, turn)
            gif = screen.render_stats_gif(
                stats, 0, delay_cs=args.delay, frames=args.frames, step=cells,
                start_cell=cell,
            )
            if gif == last_gif:
                # Re-uploading content the device already holds is the one path it
                # always answers "rejected" on, so do not send it.
                print("frame unchanged, nothing to push")
            else:
                if bus is None:
                    # The power frame doubles as a probe: if the panel will not
                    # answer that, it is still holding a half-finished upload
                    # from an earlier run and nothing will land until its
                    # screen is cycled.
                    try:
                        bus = UsbSerialTransport(port=args.port or None,
                                                  baudrate=args.baudrate)
                        bus.set_power(True)
                    except (TransportError, OSError) as exc:
                        print(f"panel not answering ({exc}), power-cycling it",
                              file=sys.stderr)
                        if bus is not None:
                            try:
                                bus.close()
                            except Exception:  # noqa: BLE001 - the link is already broken
                                pass
                        bus = None
                        recover_panel(args)
                if bus is None:
                    print("link remade, uploading on the next pass", file=sys.stderr)
                else:
                    try:
                        reply = bus.upload_gif(gif)
                        print(f"pushed gif bytes={len(gif)} status=0x{reply.payload[:1].hex() or '??'}")
                        last_gif = gif
                        pushed = True
                        failures = 0
                        # The next build knows how long this one spent on the wire.
                        wire_seconds = screen.upload_seconds(len(gif))
                    except (TransportError, OSError) as exc:
                        # A finalize can land out of step now and then; the next
                        # tick re-sends, so a single rejection should not end the
                        # run - but one deaf reply already means the panel is
                        # holding a half-finished upload, so cycle it.
                        print(f"error: {exc}", file=sys.stderr)
                        failures += 1
                        try:
                            bus.close()
                        except Exception:  # noqa: BLE001 - the link is already broken
                            pass
                        bus = None
                        if args.live and failures >= RECOVER_AFTER_FAILURES:
                            print(f"{failures} push(es) failed, power-cycling the panel",
                                  file=sys.stderr)
                            recover_panel(args)
                            failures = 0
            if not args.live:
                # One-shot: if the link had to be recovered the frame never
                # went out, so try once more instead of reporting nothing.
                if pushed or one_shot_tries >= 1:
                    return 0
                one_shot_tries += 1
                continue
            if args.run_seconds and time.monotonic() - started_at >= args.run_seconds:
                # A clean stop: a run killed mid-upload leaves the panel holding
                # a half-finished blob and it stops answering until it is cycled.
                print(f"stopped after {args.run_seconds:g}s")
                return 0
            phase += 1
            if not args.demo and time.monotonic() - stats_at >= args.interval:
                stats = stats_mod.collect()
                stats_at = time.monotonic()
            time.sleep(max(0.05, tick))
    finally:
        if bus is not None:
            bus.close()


if __name__ == "__main__":
    raise SystemExit(main())
