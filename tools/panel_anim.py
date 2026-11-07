#!/usr/bin/env python3
"""Draw the panel animations, look at them, and put them where the panel plays them.

backend/panel_anim.py holds the drawing; this is the part that decides whether
a drawing is affordable. The test is the measured one (docs/panel-limits.md):
the tip refuses a GIF begin at 32,768 bytes, a chunk takes 73ms, and the panel
plays about 30 fps, so an animation is judged by its bytes, its loop length and
how many cells move per frame.

    tools/panel_anim.py --list               what is available and what it costs
    tools/panel_anim.py --png dot-wave       a contact sheet to look at
    tools/panel_anim.py --install dot-wave   into the gif directory the panel plays
    tools/panel_anim.py --install-all

The install writes are what ``backend/gif_screen.py`` serves, so anything that
lands there has already passed the budget here.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT, ROOT / "decky-plugin"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

from backend import ai_agent_screen as screen  # noqa: E402
from backend import gif_screen, panel_anim  # noqa: E402
from backend.panel_draw import changed_cells  # noqa: E402

# The tip's measured GIF buffer. Files at or over this are refused at GIF begin.
HARD_LIMIT = 32768
# Leave room for the rest of a rotation: one animation should not eat the buffer.
DEFAULT_BUDGET = 24576
CHUNK = 498
CHUNK_S = 0.073


def measure(name, total=None):
    """Frames, cost and loop length for one animation."""
    frames = panel_anim.frames(name, total)
    delay = panel_anim.delay_cs(name)
    moves = [changed_cells(before, after)
             for before, after in zip(frames, frames[1:] + frames[:1])]
    data = screen.encode_frames(frames, delay_cs=delay)
    plain = screen.encode_frames(frames, delay_cs=delay, compress=False)
    saved = 100.0 * (1.0 - len(data) / float(max(1, len(plain))))
    return {
        "name": name,
        "source": panel_anim.sources(name),
        "frames": len(frames),
        "delay_cs": delay,
        "loop_s": round(len(frames) * delay / 100.0, 2),
        "bytes": len(data),
        "literal_bytes": len(plain),
        "saved_pct": round(saved, 1),
        "moved_median": sorted(moves)[len(moves) // 2],
        "upload_s": round(-(-len(data) // CHUNK) * CHUNK_S, 2),
    }


def sheet(name, total, out):
    """A contact sheet: a few frames of the animation, big enough to read."""
    from PIL import Image

    frames = panel_anim.frames(name, total)
    picks = [index * len(frames) // 6 for index in range(6)]
    wide = screen.MATRIX_WIDTH * 2
    tall = screen.MATRIX_HEIGHT * 2
    page = Image.new("RGB", (wide * len(picks) + 2 * len(picks), tall), (24, 24, 24))
    for slot, index in enumerate(picks):
        image = Image.new("RGB", (screen.MATRIX_WIDTH, screen.MATRIX_HEIGHT))
        image.putdata(frames[index])
        page.paste(image.resize((wide, tall), 0), (slot * (wide + 2), 0))
    page.save(str(out))
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(prog="panel_anim",
                                     description="Panel-scale animation replicas.")
    parser.add_argument("names", nargs="*")
    parser.add_argument("--list", dest="listing", action="store_true")
    parser.add_argument("--png", default="", help="write a contact sheet here")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--install-all", action="store_true")
    parser.add_argument("--frames", type=int, default=0, help="override the frame count")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    parser.add_argument("--out", default="", help="gif directory override")
    args = parser.parse_args(argv)

    wanted = args.names or [item[0] for item in panel_anim.ANIMATIONS]
    if args.listing or not (args.png or args.install or args.install_all):
        print("%-14s %-38s %6s %7s %8s %9s %8s %8s %s"
              % ("name", "replica of", "frames", "loop s", "moved", "bytes",
                 "literal", "saved%", "upload"))
        for name in wanted:
            item = measure(name, args.frames)
            flag = "" if item["bytes"] < args.budget else " OVER"
            print("%-14s %-38s %6d %7.2f %8d %9d %8d %8.1f %5.2f%s"
                  % (item["name"], item["source"], item["frames"], item["loop_s"],
                     item["moved_median"], item["bytes"], item["literal_bytes"],
                     item["saved_pct"], item["upload_s"], flag))
        return 0

    destination = gif_screen.gif_dir(args.out)
    for name in wanted:
        if args.png:
            path = Path(args.png).expanduser()
            if len(wanted) > 1:
                path = Path(str(path.with_suffix("")) + "-" + name + ".png")
            print("wrote " + str(sheet(name, args.frames, path)))
        if args.install or args.install_all:
            item = measure(name, args.frames)
            if item["bytes"] >= HARD_LIMIT:
                print("%s: %d bytes is over the tip's %d buffer - not installed"
                      % (name, item["bytes"], HARD_LIMIT))
                continue
            data = screen.encode_frames(panel_anim.frames(name, args.frames),
                                        delay_cs=panel_anim.delay_cs(name))
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / (name + ".gif")
            target.write_bytes(data)
            print("%s -> %s  %d bytes, %d frames, %.2f s loop, %.2f s to upload"
                  % (name, target, len(data), item["frames"], item["loop_s"],
                     item["upload_s"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
