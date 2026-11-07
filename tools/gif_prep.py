#!/usr/bin/env python3
"""Turn an ordinary GIF into one the panel can actually play.

The panel is not a screen a GIF can be pointed at: the link costs 73ms per
498-byte chunk, the tip's GIF buffer is 32 KiB, it plays about 30 fps and no
faster, and a whole 64x54 frame costs ~2,045 bytes (docs/panel-limits.md). So the
fitting happens here, with the budget in hand, and it reports what it gave up.
``backend/gif_screen.py`` only serves files from the gif directory, so anything
that plays has passed through this tool.

    python3 tools/gif_prep.py some.gif              # into the gif directory
    python3 tools/gif_prep.py some.gif --budget 20000 --colors 6
    python3 tools/gif_prep.py --list                # what will play
    python3 tools/gif_prep.py --survey media/gifs   # what a folder would cost
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT, ROOT / "decky-plugin"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

from backend import ai_agent_screen as screen  # noqa: E402
from backend import gif_screen  # noqa: E402

WIDTH = screen.MATRIX_WIDTH
HEIGHT = screen.MATRIX_HEIGHT
# The tip's buffer is 32 KiB; margin means an extra frame still plays instead of
# the whole upload being rejected.
DEFAULT_BUDGET = 24 * 1024
MIN_DELAY_CS = 4  # the measured playback floor, about 25 steps a second


def fit(image, colors):
    """Contain into 64x54 on black, then reduce to a small palette."""
    from PIL import Image

    scale = min(WIDTH / image.width, HEIGHT / image.height)
    size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
    resized = image.convert("RGB").resize(size, Image.LANCZOS)
    canvas = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    canvas.paste(resized, ((WIDTH - size[0]) // 2, (HEIGHT - size[1]) // 2))
    if colors < 256:
        canvas = canvas.quantize(colors=max(2, colors), method=2, kmeans=3)
    palette = canvas.getpalette("RGB") or []
    pixels = []
    for entry in canvas.getdata():
        index = entry if isinstance(entry, int) else entry[0]
        spot = palette[index * 3:index * 3 + 3]
        pixels.append((spot[0], spot[1], spot[2]) if len(spot) == 3 else (0, 0, 0))
    return pixels


def load_frames(path, colors):
    """Whole RGB frames and their delays; partial frames are composited."""
    from PIL import Image, ImageSequence

    frames = []
    delays = []
    base = None
    opened = Image.open(str(path))
    for image in ImageSequence.Iterator(opened):
        duration = max(MIN_DELAY_CS, int(image.info.get("duration", 100)) // 100)
        frame = image.convert("RGBA")
        if base is None or frame.size != base.size:
            base = Image.new("RGBA", frame.size, (0, 0, 0, 255))
        base = Image.alpha_composite(base, frame)
        frames.append(fit(base, colors))
        delays.append(duration)
    return frames, delays


def changed_per_frame(frames, keep):
    if keep < 2:
        return 0.0
    moves = [sum(1 for before, after in zip(frames[step - 1], frames[step])
                  if before != after) for step in range(1, keep)]
    return sum(moves) / len(moves)


def prepare(path, out_dir, budget, colors):
    frames, delays = load_frames(path, colors)
    if not frames:
        raise ValueError("no frames in " + str(path))
    delay = max(MIN_DELAY_CS, min(delays))
    keep = len(frames)
    data = b""
    while keep >= 1:
        data = screen.encode_frames(frames[:keep], delay_cs=delay)
        if len(data) <= budget:
            break
        keep = max(0, int(keep * 0.67))
    out_dir.mkdir(parents=True, exist_ok=True)
    destination = out_dir / path.name
    destination.write_bytes(data)
    return {"source": str(path), "path": str(destination), "bytes": len(data),
            "budget": budget, "frames_source": len(frames), "frames_kept": keep,
            "colors": colors, "delay_cs": delay,
            "changed": round(changed_per_frame(frames, keep), 1)}


def survey(path, budget, colors):
    """What this GIF would cost, and how much of it the budget keeps.

    Nothing is written: a survey is how you find out whether a directory of
    animations is worth converting at all. The answer for a GIF made for a
    computer screen is usually that only a second of it fits.
    """
    frames, delays = load_frames(path, colors)
    if not frames:
        return {"source": str(path), "frames_source": 0, "frames_kept": 0,
                 "bytes": 0, "delay_cs": MIN_DELAY_CS, "loop_s": 0.0, "changed": 0.0}
    delay = max(MIN_DELAY_CS, min(delays))
    low, high, kept, data = 1, len(frames), 0, b""
    while low <= high:
        middle = (low + high) // 2
        blob = screen.encode_frames(frames[:middle], delay_cs=delay)
        if len(blob) <= budget:
            kept, data, low = middle, blob, middle + 1
        else:
            high = middle - 1
    return {"source": str(path), "frames_source": len(frames), "frames_kept": kept,
            "bytes": len(data), "budget": budget, "delay_cs": delay,
            "loop_s": round(kept * delay / 100.0, 2),
            "changed": round(changed_per_frame(frames, max(2, min(kept, 8))), 1),
            "pixels": WIDTH * HEIGHT}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="gif_prep",
                                     description="Fit a GIF to what the panel can play.")
    parser.add_argument("source", nargs="*")
    parser.add_argument("--out", default="")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    parser.add_argument("--colors", type=int, default=8)
    parser.add_argument("--list", dest="listing", action="store_true")
    parser.add_argument("--survey", default="",
                        help="report what a directory of GIFs would cost; write nothing")
    args = parser.parse_args(argv)
    out_dir = gif_screen.gif_dir(args.out)
    if args.survey:
        directory = Path(args.survey).expanduser()
        files = sorted(directory.glob("*.gif")) if directory.is_dir() else []
        if not files:
            print("no GIFs in " + str(directory), file=sys.stderr)
            return 2
        print("%-34s %6s %5s %8s %6s %7s  %s"
              % ("source", "frames", "keep", "bytes", "loop s", "moved", "verdict"))
        for item in files:
            report = survey(item, args.budget, args.colors)
            if report["frames_kept"] >= report["frames_source"]:
                verdict = "fits as is"
            else:
                verdict = "flicker" if report["loop_s"] < 1.5 else "short"
            print("%-34s %6d %5d %8d %6.2f %7.1f  %s"
                  % (item.name, report["frames_source"], report["frames_kept"],
                     report["bytes"], report["loop_s"], report["changed"], verdict))
        return 0
    if args.listing:
        files = sorted(out_dir.glob("*.gif")) if out_dir.is_dir() else []
        print("%d prepared animations in %s" % (len(files), out_dir))
        for item in files:
            print("  %s  %d bytes" % (item.name, item.stat().st_size))
        return 0
    if not args.source:
        parser.error("a source GIF is needed (or --list)")
    for name in args.source:
        path = Path(name).expanduser()
        if not path.is_file():
            print("no such file: " + str(path), file=sys.stderr)
            return 2
        report = prepare(path, out_dir, args.budget, args.colors)
        print("%s -> %s" % (report["source"], report["path"]))
        print("  frames %d -> %d, bytes %d of a %d budget, %d colors, %dcs"
               % (report["frames_source"], report["frames_kept"], report["bytes"],
                   report["budget"], report["colors"], report["delay_cs"]))
        print("  motion: %s pixels a frame change (a whole frame is %d)"
               % (report["changed"], WIDTH * HEIGHT))
        if report["frames_kept"] < report["frames_source"]:
            print("  trimmed %d frames to fit; raise --budget or lower --colors"
                   % (report["frames_source"] - report["frames_kept"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
