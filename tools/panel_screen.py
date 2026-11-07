#!/usr/bin/env python3
"""Run the panel: the scheduler, over USB.

This is the process that owns the port. It asks the bus what should be showing
once a tick, uploads when the answer is new bytes, and leaves the panel looping
what it already has the rest of the time (docs/panel-limits.md is why it works
that way: a loop costs nothing, an upload costs a round trip per 498 bytes).

    python3 tools/panel_screen.py                     # run it (what the service runs)
    python3 tools/panel_screen.py --once --gif u.gif  # one upload, saved to look at
    python3 tools/panel_screen.py --list              # screens, and what each can say
    python3 tools/panel_screen.py --only calendar     # one screen, no rotation

The port lock means running this beside the service is refused rather than
silently interleaved, and one failed push triggers ``recover_link`` rather than
a retry ladder.
"""

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT, ROOT / "decky-plugin"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

from backend.ai_agent_source import AiAgentSource  # noqa: E402
from backend.calendar_source import CalendarSource  # noqa: E402
from backend.gif_screen import GifSource  # noqa: E402
from backend.notifications_source import NotificationsSource  # noqa: E402
from backend.panel_screens import LOCK, PanelBus, ViewLock, view_file  # noqa: E402
from protocols.usb.transport import TransportError, UsbSerialTransport  # noqa: E402

PORT = "/dev/ttyUSB0"
BAUD = 1_000_000
# The bus re-reads its sources this often; the panel is only written when that
# produces something new.
TICK_S = 1.0


def build_sources(only: str, frames: int, delay_cs: int) -> list:
    wanted = [AiAgentSource(frames=frames, delay_cs=delay_cs), NotificationsSource(),
                CalendarSource(), GifSource()]
    if only:
        wanted = [item for item in wanted if item.name == only]
    return wanted


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="panel_screen",
                                     description="Run the panel's content scheduler.")
    parser.add_argument("--port", default=PORT)
    parser.add_argument("--baud", type=int, default=BAUD)
    parser.add_argument("--interval", type=float, default=TICK_S)
    parser.add_argument("--only", default="", help="show one screen, no rotation")
    parser.add_argument("--view", default="",
                        help="the view file to obey (default: the panel's own)")
    parser.add_argument("--frames", type=int, default=16)
    parser.add_argument("--delay", type=int, default=4)
    parser.add_argument("--once", action="store_true",
                        help="one tick, then stop (for checking a screen)")
    parser.add_argument("--gif", default="", help="write the upload here instead of uploading")
    parser.add_argument("--list", dest="listing", action="store_true")
    args = parser.parse_args(argv)

    def log(message) -> None:
        print(message, flush=True)

    sources = build_sources(args.only, args.frames, args.delay)
    if not sources:
        log("no such screen: " + args.only)
        return 2
    if args.listing:
        for source in sources:
            source.refresh(time.monotonic())
            now = time.monotonic()
            ready = "can show" if source.available(now) else "nothing to say"
            log("%-10s priority %-3s %-14s %s" % (source.name, source.priority,
                                                    ready, source.describe(now)))
        return 0

    bus = PanelBus(sources, log=log)
    # The view file is re-read every tick, so `panel_view.py lock gif` changes
    # what is showing without restarting this process.
    view = ViewLock(view_file(args.view))
    view.reload()
    bus.set_view(view)
    if view.mode == LOCK:
        log("view: " + view.summary())
    if args.gif:
        result = bus.tick()
        if not result.get("gif"):
            log("nothing to upload (%s)" % result.get("reason"))
            return 1
        Path(args.gif).write_bytes(result["gif"])
        log("wrote %s (%d bytes, screen %s)" % (args.gif, len(result["gif"]),
                                                  result["screen"]))
        return 0

    failures = 0
    shown = None
    try:
        with UsbSerialTransport(port=args.port, baudrate=args.baud,
                                  reply_timeout_s=3.0) as transport:
            transport.set_power(True)
            while True:
                started = time.monotonic()
                result = bus.tick(started)
                screen_name = result.get("screen")
                gif = result.get("gif")
                if screen_name != shown:
                    shown = screen_name
                    log("screen: %s (%s)" % (screen_name, result.get("reason")))
                if gif:
                    try:
                        transport.upload_gif(gif)
                        failures = 0
                        log("pushed %s bytes=%d" % (screen_name, len(gif)))
                    except TransportError as exc:
                        failures += 1
                        log("push failed: %s" % exc)
                        if failures >= 1:
                            log("recovering the link")
                            transport.recover_link()
                            failures = 0
                if args.once:
                    return 0
                spent = time.monotonic() - started
                time.sleep(max(0.05, args.interval - spent))
    except TransportError as exc:
        log("cannot take the port: %s" % exc)
        return 2
    except KeyboardInterrupt:
        log("stopped")
        return 0
if __name__ == "__main__":
    raise SystemExit(main())
