#!/usr/bin/env python3
"""Post something to the panel from anywhere on this box.

    python3 tools/panel_notify.py "Build failed" "3 tests failed in decky"
    python3 tools/panel_notify.py --source ci --ttl 90 "Deploy done"
    python3 tools/panel_notify.py --list        # what is waiting
    python3 tools/panel_notify.py --clear       # forget what is waiting

The spool directory is the seam: the panel bus reads it, so anything that can
write a file - a shell hook, a CI callback, a D-Bus listener - can put a card on
the panel without knowing anything about the panel. Files are named so that the
newest one sorts last, and each carries its own expiry.

Where the spool lives: ``JSAUX_PIXEL_NOTIFY_DIR`` or
``~/.local/share/jsaux-pixel/notify``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "decky-plugin"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from backend import notifications_source as notify  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="panel_notify",
                                     description="Put a card on the panel.")
    parser.add_argument("title", nargs="?")
    parser.add_argument("body", nargs="?", default="")
    parser.add_argument("--source", default="local",
                        help="who is speaking, shown on the card")
    parser.add_argument("--ttl", type=float, default=notify.DEFAULT_TTL_S,
                        help="seconds the card is allowed to interrupt for")
    parser.add_argument("--dir", default="", help="spool directory override")
    parser.add_argument("--list", action="store_true", dest="listing")
    parser.add_argument("--clear", action="store_true")
    args = parser.parse_args(argv)
    directory = notify.spool_dir(args.dir)
    if args.clear:
        removed = notify.clear(directory)
        print(f"removed {removed} pending notification(s) from {directory}")
        return 0
    if args.listing:
        items = notify.read_pending(directory, time.time())
        print(f"{len(items)} pending in {directory}")
        for item in items:
            left = item.expires_at - time.time()
            print(f"  {item.posted_at:.0f} {item.source}: {item.title}"
                  f"{f' | {item.body}' if item.body else ''} ({left:.0f}s left)")
        return 0
    if not args.title:
        parser.error("a title is needed (or --list / --clear)")
    path = notify.enqueue(directory, title=args.title, body=args.body,
                           source=args.source, ttl=args.ttl)
    print(f"queued {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
