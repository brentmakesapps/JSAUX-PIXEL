#!/usr/bin/env python3
"""Look at the panel through a webcam, so "it looked smooth" becomes a number.

The panel has no readback: the only way to know what it is showing is to look at
it. A webcam makes that repeatable, so the measurements that used to need a human
can report numbers instead - how fast the activity mark really moves, whether
every frame of an upload actually played, whether the screen went dark.

    python3 tools/panel_cam.py list                       # sizes the camera does
    python3 tools/panel_cam.py grab --out panel.png       # one settled frame
    python3 tools/panel_cam.py grab --rect 340,70,300,410 --scale 3
    python3 tools/panel_cam.py movie --seconds 3 --fps 30 --dir /tmp/panel-movie
    python3 tools/panel_cam.py motion --dir /tmp/panel-movie --cell 600,120,40,40

``--rect x,y,w,h`` crops the camera frame to the panel and ``--scale`` enlarges
it so individual LEDs are legible. ``motion`` watches one small rectangle through
a movie and reports how many times its brightness stepped - the panel's real
frame rate, not the delay_cs we asked for.

Framing is the one judgement call: point the camera at the panel, ``grab``, and
read the crop rectangle off that picture. The panel does not move, so it is a
set-once thing.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

DEVICE = "/dev/video0"
# This desk's framing, read off a grabbed frame: the 64x54 matrix sits at this
# rectangle of the 640x480 camera frame - 268x232, so 4.2 camera pixels per LED,
# and the aspect matches 64:54. Framing is physical, so it is a constant: if the
# camera moves, grab without --rect and read the edges off that.
PANEL_RECT = "132,140,268,232"
# The panel can report its own framing (tools/panel_calib.py); when that exists
# it beats the constant above.
CALIB_FILE = Path.home() / ".config/opencode/jsaux-panel-calib.json"
# The camera needs a few frames to settle its auto-exposure; a bright panel
# against a dark desk otherwise arrives with a different exposure each time.
WARMUP = 8


def run(argv) -> str:
    done = subprocess.run(argv, check=True, capture_output=True, text=True)
    return done.stdout


def calibration() -> str:
    """The framing the panel reported for itself, if it has been calibrated."""
    try:
        data = json.loads(CALIB_FILE.read_text(encoding="utf-8"))
        return str(data.get("rect") or PANEL_RECT)
    except (OSError, ValueError):
        return PANEL_RECT


def led_cell(spec: str) -> str:
    """LED coordinates to a pixel rectangle, straight off the calibration.

    Measurements then say "watch the 5x5 mark at LED 58,0" instead of carrying
    hand-counted pixels that break the moment the camera is nudged.
    """
    try:
        data = json.loads(CALIB_FILE.read_text(encoding="utf-8"))
        x, y, width, height = (float(part) for part in spec.split(","))
        origin_x = float(data["origin_x"])
        origin_y = float(data["origin_y"])
        pitch_x = float(data["pitch_x"])
        pitch_y = float(data["pitch_y"])
    except (OSError, ValueError, KeyError):
        return ""
    if not pitch_x or not pitch_y:
        return ""
    return ",".join(str(int(round(value))) for value in (
        origin_x + x * pitch_x, origin_y + y * pitch_y,
        width * pitch_x, height * pitch_y))


def grab(device: str, out: Path, rect: str, scale: float, size: str, fps: float,
         warmup: int) -> Path:
    """One settled frame, cropped and enlarged when asked."""
    argv = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if size:
        argv += ["-video_size", size]
    if fps:
        argv += ["-framerate", str(fps)]
    argv += ["-f", "v4l2", "-i", device, "-frames:v", str(max(1, warmup))]
    if warmup > 1:
        argv += ["-y", str(out.parent / (out.name + ".warm%02d.png"))]
    else:
        argv += ["-y", str(out)]
    run(argv)
    frame = out.parent / (out.name + ".raw.png")
    if warmup > 1:
        warmed = sorted(out.parent.glob(out.name + ".warm*.png"))
        if not warmed:
            raise RuntimeError("the camera returned no frames")
        for stale in warmed[:-1]:
            stale.unlink()
        warmed[-1].replace(frame)
    else:
        frame.replace(out)
        frame = out
    if rect or scale != 1.0:
        argv = ["magick", str(frame)]
        if rect:
            x, y, width, height = (int(part) for part in rect.split(","))
            argv += ["-crop", f"{width}x{height}+{x}+{y}", "+repage"]
        if scale != 1.0:
            argv += ["-filter", "point", "-resize", f"{int(scale * 100)}%"]
        argv += ["-background", "#101010", "-bordercolor", "#333333", "-border", "2"]
        argv += [str(out)]
        run(argv)
        frame.unlink()
    else:
        frame.replace(out)
    return out


def movie(device: str, directory: Path, seconds: float, fps: float, size: str,
          rect: str) -> list:
    directory.mkdir(parents=True, exist_ok=True)
    for old in directory.glob("frame-*.png"):
        old.unlink()
    argv = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if size:
        argv += ["-video_size", size]
    argv += ["-framerate", str(max(1, int(fps))), "-f", "v4l2", "-i", device,
             "-t", str(seconds)]
    if rect:
        x, y, width, height = (int(part) for part in rect.split(","))
        argv += ["-vf", f"crop={width}:{height}:{x}:{y}"]
    argv += ["-y", str(directory / "frame-%04d.png")]
    run(argv)
    return sorted(directory.glob("frame-*.png"))


def motion(directory: Path, cell: str, fps: float, min_s: float = 0.0) -> dict:
    """How many times a cell changed brightness across the movie.

    A GIF playing at the delay asked for produces one step per frame, so the
    step count over the movie's own rate is the panel's real frame rate.
    """
    frames = sorted(directory.glob("frame-*.png"))
    if len(frames) < 2:
        return {"frames": len(frames), "error": "not enough frames"}
    x, y, width, height = (int(part) for part in cell.split(","))
    samples = []
    for frame in frames:
        out = run(["magick", str(frame), "-crop", f"{width}x{height}+{x}+{y}",
                   "+repage", "-colorspace", "Gray", "-format", "%[mean]", "info:"])
        try:
            samples.append(float(out.strip().rstrip("%")))
        except ValueError:
            samples.append(0.0)
    spread = max(samples) - min(samples)
    # A step is a real jump, not sensor noise.
    threshold = max(0.4, spread * 0.18)
    steps = sum(1 for before, after in zip(samples, samples[1:])
                if abs(after - before) >= threshold)
    seconds = max(0.01, len(frames) / max(1.0, fps))
    result = {"frames": len(frames), "mean_min": round(min(samples), 2),
              "mean_max": round(max(samples), 2), "spread": round(spread, 2),
              "steps": steps, "steps_per_second": round(steps / seconds, 2)}
    # Counting steps over-counts a run that fades in and out across several
    # frames, so the honest number is the period of the signal: for a mark
    # walking a ring, one period is one turn.
    period = dominant_period(samples, fps, min_s)
    if period:
        result["period_s"] = round(period, 3)
        result["rate_per_second"] = round(1.0 / period, 2)
    else:
        result["period_note"] = (f"no repeating period at or above {min_s}s: the "
                                  f"movie is too short or the rate is above the "
                                  f"camera's {fps:g} fps")
    return result


def dominant_period(samples, fps: float, min_s: float = 0.0):
    """The repeating period of the sampled brightness, in seconds.

    Autocorrelation, because the thing being watched (a run of lit cells walking
    a ring) fades in and out rather than switching, so zero crossings and step
    counts both lie about it. ``min_s`` matters: sampled at 30 fps, a fast
    animation aliases onto a one-frame period, and that is the camera's limit,
    not the panel's - so the search window is part of the answer.
    """
    if len(samples) < 8 or fps <= 0:
        return None
    mean = sum(samples) / len(samples)
    wave = [value - mean for value in samples]
    energy = sum(value * value for value in wave)
    if energy <= 0:
        return None
    best_lag, best_score = 0, 0.0
    scores = {}
    shortest = max(2, int(min_s * fps))
    longest = min(len(wave) - 2, int(4 * fps))
    for lag in range(shortest, max(shortest + 1, longest)):
        head = wave[:len(wave) - lag]
        tail = wave[lag:]
        # Normalise by the energy that actually overlaps: without that, longer
        # lags lose samples and always score lower, which biases the answer to
        # the shortest lag in the window no matter what the panel was doing.
        numerator = sum(a * b for a, b in zip(head, tail))
        denominator = (sum(a * a for a in head) * sum(b * b for b in tail)) ** 0.5
        score = numerator / denominator if denominator else 0.0
        scores[lag] = score
        if score > best_score:
            best_score = score
    if best_score < 0.25:
        return None
    # A signal periodic at 0.5s is also periodic at 1.0 and 1.5, and taking the
    # strongest lag would report a multiple of the truth. Take the shortest lag
    # that matches nearly as well: the fundamental, not a harmonic of it.
    for lag in sorted(scores):
        if scores[lag] >= best_score - 0.02:
            best_lag = lag
            break
    if not best_lag:
        return None
    return best_lag / float(fps)


def formats(device: str) -> list:
    try:
        out = run(["v4l2-ctl", "-d", device, "--list-frmsizes"])
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"v4l2-ctl failed: {exc}", file=sys.stderr)
        return []
    found = []
    for line in out.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and "x" in parts[0]:
            width, _, height = parts[0].partition("x")
            if width.isdigit() and height.isdigit():
                found.append(f"{width}x{height} {parts[1]}")
    return found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="panel_cam",
                                     description="Watch the panel through a webcam.")
    parser.add_argument("--device", default=DEVICE)
    parser.add_argument("--rect", default="",
                        help=f"x,y,w,h crop of the camera frame "
                             f"(default {PANEL_RECT}; 'full' for the whole frame)")
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--size", default="", help="camera size, e.g. 1280x720")
    parser.add_argument("--fps", type=float, default=0.0)
    parser.add_argument("--warmup", type=int, default=WARMUP)
    parser.add_argument("--out", default="/tmp/opencode-panel-cam.png")
    parser.add_argument("--dir", default="/tmp/opencode-panel-movie")
    parser.add_argument("--cell", default="0,0,8,8", help="x,y,w,h inside a movie frame")
    parser.add_argument("--led", default="",
                        help="x,y,w,h in LED coordinates; needs tools/panel_calib.py")
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("mode", nargs="?", default="grab",
                        choices=["list", "grab", "movie", "motion"])
    args = parser.parse_args(argv)
    rect = calibration() if not args.rect else ("" if args.rect == "full" else args.rect)
    try:
        if args.mode == "list":
            for line in formats(args.device):
                print(line)
            return 0
        if args.mode == "grab":
            frame = grab(args.device, Path(args.out), rect, args.scale,
                         args.size, args.fps, args.warmup)
            print(run(["magick", "identify", str(frame)]).strip())
            return 0
        if args.mode == "movie":
            frames = movie(args.device, Path(args.dir), args.seconds, args.fps,
                           args.size, rect)
            print(f"{len(frames)} frames in {args.dir}")
            return 0
        cell = led_cell(args.led) if args.led else args.cell
        if not cell:
            print("no cell to watch: pass --led x,y,w,h (needs the calibration) "
                  "or --cell x,y,w,h", file=sys.stderr)
            return 2
        print(json.dumps(motion(Path(args.dir), cell, args.fps or 30.0), indent=2))
        return 0
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"camera failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
