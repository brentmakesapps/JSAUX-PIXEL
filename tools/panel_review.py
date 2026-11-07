#!/usr/bin/env python3
"""Look at every installed animation on the real panel, one at a time.

The rotation decides what the panel is showing, so a camera pointed at it tells
you about whichever animation happens to be up. This tool stops the service,
uploads one GIF, films it, and repeats - so each filmstrip is that animation and
nothing else. The service is back up on the way out, the same way the probe does
it.

    sg dialout -c '.venv/bin/python tools/panel_review.py'       # everything installed
    sg dialout -c '.venv/bin/python tools/panel_review.py gears cube-field'

Each animation gets a filmstrip of twelve filmed frames, 0.2s apart, cropped to
the matrix and labelled with the time. That span (2.4s) is longer than most of
the loops, so a strip either shows a whole cycle or shows whether the movement is
continuous. Strips are also stacked into labelled pages of four.

Why filmstrips and not the motion estimator: the panel's glass reflects the
room, and the period a camera reads off that can be the reflection or a harmonic
of the frame rate. A strip of frames in order cannot be fooled that way - either
the shape advances in every frame or it does not.
"""

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT, ROOT / "decky-plugin", ROOT / "tools"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import panel_cam as cam  # noqa: E402
import panel_probe as probe  # noqa: E402
from backend import gif_screen  # noqa: E402

FILM_S = 3.0
TILES = 12
EVERY = 6            # filmed frames between tiles: 6/30fps = 0.2s
PER_ROW = 4          # tiles per row of a strip
PER_PAGE = 4         # strips per page


def options(film_s):
    """The shape `panel_probe.Probe` expects, with nothing but the port set."""
    return argparse.Namespace(port=probe.PORT, baud=probe.BAUD, timeout=3.0,
                               samples=5, size=12, chunks=[498], watch=film_s,
                               poll=0.0, no_camera=False, movie_fps=30.0,
                               min_period=0.15, no_pause=False, recover=True,
                               per_test=900.0, report="")


def strip(directory, out):
    """Twelve filmed frames, cropped to the matrix, in reading order."""
    from PIL import Image, ImageDraw

    frames = sorted(directory.glob("frame-*.png"))
    picks = frames[::EVERY][:TILES]
    if not picks:
        return None
    x, y, wide, tall = (int(part) for part in cam.calibration().split(","))
    tiles = []
    for path in picks:
        tiles.append(Image.open(str(path)).convert("RGB")
                      .crop((x, y, x + wide, y + tall)))
    rows = (len(tiles) + PER_ROW - 1) // PER_ROW
    page = Image.new("RGB", (wide * PER_ROW, (tall + 16) * rows), (8, 8, 8))
    write = ImageDraw.Draw(page)
    for index, tile in enumerate(tiles):
        row, column = divmod(index, PER_ROW)
        page.paste(tile, (column * wide, row * (tall + 16)))
        write.text((column * wide + 4, row * (tall + 16) + tall + 2),
                   "t=%+.2fs" % (index * EVERY / 30.0), fill=(200, 200, 200))
    page.save(str(out))
    return out


def pages(out, names):
    """Strips stacked four to a page, named, so the set fits on a screen."""
    from PIL import Image, ImageDraw

    count = 0
    for start in range(0, len(names), PER_PAGE):
        group = names[start:start + PER_PAGE]
        tiles = [Image.open(str(out / (name + ".png"))) for name in group
                 if (out / (name + ".png")).is_file()]
        if not tiles:
            continue
        wide = max(tile.width for tile in tiles)
        page = Image.new("RGB", (wide, sum(t.height for t in tiles)
                                 + 18 * len(tiles)), (0, 0, 0))
        write = ImageDraw.Draw(page)
        offset = 0
        for name, tile in zip(group, tiles):
            write.text((6, offset + 4), name, fill=(255, 255, 0))
            page.paste(tile, (0, offset + 18))
            offset += tile.height + 18
        count += 1
        page.save(str(out / ("page%d.png" % count)))
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("names", nargs="*",
                        help="which GIFs in the gif directory (default: all)")
    parser.add_argument("--gif-dir", default="",
                        help="default: the directory the panel plays")
    parser.add_argument("--out", default="/tmp/panel-review")
    parser.add_argument("--film", type=float, default=FILM_S,
                        help="seconds to film each animation (default %(default)s)")
    arguments = parser.parse_args()

    directory = Path(arguments.gif_dir).expanduser() if arguments.gif_dir \
        else gif_screen.gif_dir()
    wanted = set(arguments.names)
    gifs = [path for path in sorted(directory.glob("*.gif"))
            if not wanted or path.stem in wanted]
    if not gifs:
        print("no GIFs in", directory)
        return 1

    out = Path(arguments.out)
    out.mkdir(parents=True, exist_ok=True)
    bus = probe.Probe(options(arguments.film))
    bus.test_preflight()
    bus.pause_service()
    if not bus.connect():
        bus.restore_service()
        return 2
    filmed = []
    try:
        for gif in gifs:
            blob = gif.read_bytes()
            started = time.monotonic()
            seconds = bus.timed_upload(blob)
            if seconds is None:
                print("%-16s upload failed: %s" % (gif.stem, bus.last_error),
                      flush=True)
                continue
            upload = time.monotonic() - started
            time.sleep(0.6)            # the change reaches the screen in ~0.46s
            movie = out / gif.stem
            cam.movie(cam.DEVICE, movie, arguments.film, 30.0, "", "")
            strip(movie, out / (gif.stem + ".png"))
            filmed.append(gif.stem)
            print("%-16s %6d bytes  %.2fs upload  %s.png"
                  % (gif.stem, len(blob), upload, out / gif.stem), flush=True)
    finally:
        bus.restore_service()
    print("pages: %d, strips in %s" % (pages(out, filmed), out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
