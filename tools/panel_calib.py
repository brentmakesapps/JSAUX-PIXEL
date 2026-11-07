#!/usr/bin/env python3
"""Find the panel in the camera frame by lighting up the panel.

Guessing the crop rectangle off a photo is guesswork: the framing changes when
the camera is nudged, and the measurements that follow depend on it. So the
panel says where it is - a frame whose outermost lit pixels are the matrix's own
border, which is trivial to find in the camera image.

    python3 tools/panel_calib.py            # writes the calibration
    python3 tools/panel_calib.py --show      # also keep the marked-up frame

The result lands in ~/.config/opencode/jsaux-panel-calib.json and panel_cam.py
reads it, so every later grab, movie and motion measurement is addressed in LED
coordinates instead of guessed pixels.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "decky-plugin"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from backend import ai_agent_screen as screen  # noqa: E402
from backend import gif_encoder  # noqa: E402
from protocols.usb.transport import TransportError, UsbSerialTransport  # noqa: E402
CAM_TOOLS = ROOT / "tools"
if str(CAM_TOOLS) not in sys.path:
    sys.path.insert(0, str(CAM_TOOLS))
import panel_cam as cam  # noqa: E402

CALIB_FILE = Path.home() / ".config/opencode/jsaux-panel-calib.json"
W, H = screen.MATRIX_WIDTH, screen.MATRIX_HEIGHT
PALETTE = [(0, 0, 0), (255, 255, 255)]


def border_frame() -> list:
    """The matrix outline plus the four corner cells, nothing else lit."""
    indices = [0] * (W * H)
    for x in range(W):
        for y in range(H):
            if x in (0, W - 1) or y in (0, H - 1):
                indices[y * W + x] = 1
    for x, y in ((2, 2), (W - 3, 2), (2, H - 3), (W - 3, H - 3)):
        for dy in range(2):
            for dx in range(2):
                indices[(y + dy) * W + x + dx] = 1
    return indices


def upload_frame(port: str, indices: list) -> bool:
    """Put one frame on the panel. The port lock decides if we may."""
    gif = gif_encoder.encode(W, H, PALETTE, [indices], delay_cs=100)
    try:
        with UsbSerialTransport(port=port, baudrate=1_000_000, reply_timeout_s=3.0) as bus:
            bus.set_power(True)
            bus.upload_gif(gif)
        return True
    except (TransportError, OSError) as exc:
        print(f"could not put the marker on the panel: {exc}", file=sys.stderr)
        return False


def difference_box(before: Path, after: Path, threshold: int) -> tuple | None:
    """Bounding box of what changed between two frames.

    The panel is the only thing in shot that we can change on demand, so the
    difference of a marker frame and a dark frame is the matrix and nothing
    else - no brightness guessing about the desk behind it.
    """
    done = subprocess.run(
        ["magick", str(before), str(after), "-compose", "difference", "-composite",
         "-colorspace", "Gray", "-threshold", f"{threshold}%",
         "-format", "%@", "info:"],
        check=True, capture_output=True, text=True)
    geometry = done.stdout.strip()
    if "+" not in geometry:
        return None
    size, _, offset = geometry.partition("+")
    width, _, height = size.partition("x")
    parts = offset.replace("-", "+").split("+")
    if len(parts) != 2 or not width.isdigit() or not height.isdigit():
        return None
    left, top = (int(value) if value else 0 for value in parts[:2])
    return left, top, int(width), int(height)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="panel_calib",
                                     description="Locate the panel in the camera frame.")
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--device", default=cam.DEVICE)
    parser.add_argument("--threshold", type=int, default=25,
                        help="how much a pixel has to change to count as a lit LED")
    parser.add_argument("--show", action="store_true",
                        help="keep the difference mask for looking at")
    args = parser.parse_args(argv)
    if not upload_frame(args.port, border_frame()):
        return 2
    lit = cam.grab(args.device, Path("/tmp/opencode-calib-lit.png"), "", 1.0, "", 0.0,
                   cam.WARMUP)
    if not upload_frame(args.port, [0] * (W * H)):
        return 2
    dark = cam.grab(args.device, Path("/tmp/opencode-calib-dark.png"), "", 1.0, "", 0.0,
                    cam.WARMUP)
    box = difference_box(lit, dark, args.threshold)
    if box is None:
        print("nothing changed in shot - is the panel awake and in frame?",
              file=sys.stderr)
        return 2
    left, top, width, height = box
    # The lit border is the outermost LEDs, so the box centre-to-centre spans
    # W-1 by H-1 pitches, and (left, top) is the centre of LED 0,0.
    pitch_x = width / float(W - 1)
    pitch_y = height / float(H - 1)
    rect = (max(0, int(round(left - pitch_x / 2))), max(0, int(round(top - pitch_y / 2))),
            int(round(width + pitch_x)), int(round(height + pitch_y)))
    calib = {
        "rect": ",".join(str(value) for value in rect),
        "origin_x": left,
        "origin_y": top,
        "pitch_x": round(pitch_x, 3),
        "pitch_y": round(pitch_y, 3),
        "camera": args.device,
        "threshold": args.threshold,
    }
    CALIB_FILE.parent.mkdir(parents=True, exist_ok=True)
    CALIB_FILE.write_text(json.dumps(calib, indent=2), encoding="utf-8")
    print(json.dumps(calib, indent=2))
    print(f"wrote {CALIB_FILE}")
    if args.show:
        subprocess.run(["magick", str(lit), "-colorspace", "Gray",
                        "-threshold", f"{args.threshold}%", str(lit) + ".lit.png"],
                       check=False)
        print(f"lit mask: {lit}.lit.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
