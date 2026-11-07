"""AI Agent screen layout for direct USB rendering (no companion daemon).

Draws the local provider stats onto the 64x54 panel: the loaded model, how
much of the context window the running request has read, how far into its
answer it is, and the token counts. ``render_stats_gif`` returns the GIF
bytes that ``UsbSerialTransport.upload_gif`` plays.
"""

from __future__ import annotations

import os
from typing import Any

from . import gif_encoder

MATRIX_WIDTH = 64
MATRIX_HEIGHT = 54

FONT_W = 4
FONT_H = 5
ADVANCE = FONT_W + 1

# 4x5 uppercase glyph set (the panel is only 64px wide, so a 5x7 font
# would not leave room for two numbers side by side).
GLYPHS = {
    "A": ("0110", "1001", "1111", "1001", "1001"),
    "B": ("1110", "1001", "1110", "1001", "1110"),
    "C": ("0111", "1000", "1000", "1000", "0111"),
    "D": ("1110", "1001", "1001", "1001", "1110"),
    "E": ("1111", "1000", "1110", "1000", "1111"),
    "F": ("1111", "1000", "1110", "1000", "1000"),
    "G": ("0111", "1000", "1011", "1001", "0111"),
    "H": ("1001", "1001", "1111", "1001", "1001"),
    "I": ("1110", "0100", "0100", "0100", "1110"),
    "J": ("0001", "0001", "0001", "1001", "0110"),
    "K": ("1001", "1010", "1100", "1010", "1001"),
    "L": ("1000", "1000", "1000", "1000", "1111"),
    "M": ("1001", "1111", "1111", "1001", "1001"),
    "N": ("1001", "1101", "1011", "1001", "1001"),
    "O": ("0110", "1001", "1001", "1001", "0110"),
    "P": ("1110", "1001", "1110", "1000", "1000"),
    "Q": ("0110", "1001", "1001", "1110", "0111"),
    "R": ("1110", "1001", "1110", "1010", "1001"),
    "S": ("0111", "1000", "0110", "0001", "1110"),
    "T": ("1111", "0100", "0100", "0100", "0100"),
    "U": ("1001", "1001", "1001", "1001", "0110"),
    "V": ("1001", "1001", "1001", "1010", "0100"),
    "W": ("1001", "1001", "1111", "1111", "1001"),
    "X": ("1001", "0110", "0110", "0110", "1001"),
    "Y": ("1001", "1001", "0110", "0100", "0100"),
    "Z": ("1111", "0010", "0100", "1000", "1111"),
    "0": ("0110", "1001", "1001", "1001", "0110"),
    "1": ("0010", "0110", "0010", "0010", "0111"),
    "2": ("0110", "1001", "0010", "0100", "1111"),
    "3": ("1111", "0001", "0110", "0001", "1110"),
    "4": ("1001", "1001", "1111", "0001", "0001"),
    "5": ("1111", "1000", "1110", "0001", "1110"),
    "6": ("0110", "1000", "1110", "1001", "0110"),
    "7": ("1111", "0001", "0010", "0100", "0100"),
    "8": ("0110", "1001", "0110", "1001", "0110"),
    "9": ("0110", "1001", "0111", "0001", "0110"),
    " ": ("0000", "0000", "0000", "0000", "0000"),
    "-": ("0000", "0000", "1111", "0000", "0000"),
    "_": ("0000", "0000", "0000", "0000", "1111"),
    "+": ("0000", "0100", "1110", "0100", "0000"),
    ".": ("0000", "0000", "0000", "0000", "0100"),
    ":": ("0000", "0100", "0000", "0100", "0000"),
    "/": ("0001", "0001", "0010", "0100", "1000"),
    "%": ("1001", "0001", "0010", "0100", "1001"),
    "=": ("0000", "1111", "0000", "1111", "0000"),
    ">": ("1000", "0100", "0010", "0100", "1000"),
}

WHITE = (235, 235, 235)
DIM = (90, 90, 90)
CYAN = (40, 200, 230)
GREEN = (40, 220, 110)
AMBER = (250, 170, 40)
RED = (240, 60, 60)
BLUE = (70, 120, 250)

# Bar one follows the engine's mode: blue while it reads the prompt in, green
# while it writes the answer, nothing at rest.
MODES = {
    "read": ("READ", BLUE),
    "gen": ("GEN", GREEN),
    "rest": ("IDLE", DIM),
}

# The activity mark: a square outline with a run of cells walking round it, in
# whatever colour the current mode uses. Parked and dark at rest.
SPINNER_X, SPINNER_Y, SPINNER_SIZE = 58, 0, 5
SPINNER_HEAD = 4
# Cells around the ring: set next to ring_cells() below.
# Cells the head advances per frame. With one upload carrying 4 frames, 4 cells
# a frame is exactly one turn per upload, which is what makes the panel's own
# looping of that upload seamless.
SPINNER_STEP = 4
SPINNER_STEP_ENV = "JSAUX_AI_AGENT_SPINNER_STEP"


def spinner_step() -> int:
    """Cells per frame: ``JSAUX_AI_AGENT_SPINNER_STEP`` if set, else SPINNER_STEP."""
    try:
        value = int(float(os.environ.get(SPINNER_STEP_ENV, "")))
    except (TypeError, ValueError):
        return max(1, int(SPINNER_STEP))
    return max(1, value)


def spinner_cells(frames: int) -> int:
    """Cells a frame for an upload of ``frames`` frames: a whole turn each.

    The panel loops whatever it is holding until the next upload replaces it.
    If an upload carries a whole number of turns, that loop is invisible; if it
    carries a part-turn, the mark jumps back the way it did at one second a pop.
    Speed comes from the upload's own length (frames x delay), not from this.
    """
    return max(1, RING_CELLS // max(1, int(frames)))


def spinner_cells_for(frames: int, step: int | None = None) -> int:
    """``step`` if given, else the env override, else a whole turn per upload."""
    if step is not None and int(step) > 0:
        return max(1, int(step))
    if SPINNER_STEP_ENV in os.environ:
        return spinner_step()
    return spinner_cells(frames)


# Measured on the link: ~4.6 KB/s, so a 2.5KB upload has been on the panel about
# half a second after the process started putting it on the wire.
LINK_BYTES_PER_SECOND = 4600


def upload_seconds(gif_bytes: int) -> float:
    """How long an upload of this size spends on the wire."""
    return max(0.0, float(gif_bytes) / LINK_BYTES_PER_SECOND)


def turn_seconds(frames: int, delay_cs: int) -> float:
    """How long one upload plays for, which is one turn of the mark."""
    return max(1, int(frames)) * max(1, int(delay_cs)) / 100.0


def playback_cells(elapsed_s: float, turn_s: float) -> int:
    """Ring cell the mark should be on after ``elapsed_s`` of looping.

    An upload carries exactly one turn, so the panel's loop stays in phase with
    this clock. Building the next upload from the cell the clock has reached is
    what makes the replacement land in step instead of throwing the mark
    sideways by a part of a turn.
    """
    if turn_s <= 0:
        return 0
    return int(round(float(elapsed_s) * RING_CELLS / float(turn_s))) % RING_CELLS


def sanitize(text: str) -> str:
    """Uppercase and drop anything the 4x5 font cannot draw."""
    return "".join(ch if ch in GLYPHS else " " for ch in str(text or "").upper())


def _advance(scale: int) -> int:
    """Pixels from one glyph's left edge to the next one's."""
    return FONT_W * scale + scale


def text_width(text: str, scale: int = 1) -> int:
    if not text:
        return 0
    return len(text) * _advance(scale) - scale


def _fit(text: str, width_px: int, scale: int = 1) -> str:
    return text[: max(0, width_px // _advance(scale))]


def compact_tokens(value: Any, max_chars: int = 3) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"
    if number >= 1_000_000:
        text = f"{number / 1_000_000:.1f}M"
    elif number >= 10_000:
        text = f"{number / 1000:.0f}K"
    elif number >= 1000:
        text = f"{number / 1000:.1f}K"
    else:
        text = str(int(number))
    return text if len(text) <= max_chars else text.replace(".", "")[:max_chars]


def percent_text(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"
    if number < 0:
        return "--"
    return f"{number:.0f}%" if number >= 10 or number == 0 else f"{number:.1f}%"


def draw_text(pixels, text: str, x: int, y: int, color, scale: int = 1) -> None:
    cursor = x
    for ch in text:
        glyph = GLYPHS.get(ch, GLYPHS[" "])
        for row, pattern in enumerate(glyph):
            for col, bit in enumerate(pattern):
                if bit != "1":
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        px, py = cursor + col * scale + dx, y + row * scale + dy
                        if 0 <= px < MATRIX_WIDTH and 0 <= py < MATRIX_HEIGHT:
                            pixels[py * MATRIX_WIDTH + px] = color
        cursor += _advance(scale)


def draw_bar(pixels, x: int, y: int, width: int, height: int, percent: float, color) -> None:
    """Frame the whole bar dim, fill the reached part bright."""
    for row in range(y, y + height):
        for col in range(x, x + width):
            if not (0 <= col < MATRIX_WIDTH and 0 <= row < MATRIX_HEIGHT):
                continue
            index = row * MATRIX_WIDTH + col
            edge = col in (x, x + width - 1) or row in (y, y + height - 1)
            pixels[index] = DIM if edge else (18, 18, 18)
    if percent is None or float(percent) < 0:
        return
    clamped = max(0.0, min(100.0, float(percent)))
    reached = int(round(clamped / 100.0 * (width - 2)))
    for row in range(y + 1, y + height - 1):
        for col in range(x + 1, x + 1 + reached):
            index = row * MATRIX_WIDTH + col
            if 0 <= col < MATRIX_WIDTH and 0 <= row < MATRIX_HEIGHT:
                pixels[index] = color


def model_label(gateway: dict, agent: dict) -> str:
    """Shorten e.g. qwen3.8-flash-next-iq3_s-256k to something the panel fits."""
    raw = str(gateway.get("model") or agent.get("model") or "")
    if not raw:
        return "NO MODEL"
    # "qwen3.8-flash-next-iq3_s-256k" -> "IQ3 S 256K"
    parts = [p for p in raw.replace("_", " ").replace("-", " ").split() if p]
    picked = parts[-3:] if len(parts) > 3 else parts
    return sanitize(" ".join(picked))[:12] or sanitize(raw)[-12:]


# Engine phase -> something the 4x5 font can fit on the footer line.
PHASE_CODES = {
    "reading": "READ",
    "prefill": "READ",
    "generating": "GEN",
    "thinking": "THINK",
    "answering": "GEN",
    "decoding": "GEN",
    "loading": "LOAD",
    "switching": "LOAD",
    "idle": "IDLE",
}


DIM_RED = (95, 22, 22)
OFFLINE = (26, 26, 26)


def _put(pixels, x: int, y: int, color) -> None:
    if 0 <= x < MATRIX_WIDTH and 0 <= y < MATRIX_HEIGHT:
        pixels[y * MATRIX_WIDTH + x] = color


def _mix(color, factor: float):
    return tuple(max(0, min(255, int(component * factor))) for component in color)


def ring_cells(x: int, y: int, size: int) -> list:
    """Perimeter of a square, clockwise from the top-left corner."""
    cells = [(x + col, y) for col in range(size)]
    cells += [(x + size - 1, y + row) for row in range(1, size)]
    cells += [(x + col, y + size - 1) for col in range(size - 2, -1, -1)]
    cells += [(x, y + row) for row in range(size - 2, 0, -1)]
    return cells


RING_CELLS = len(ring_cells(SPINNER_X, SPINNER_Y, SPINNER_SIZE))


def draw_spinner(pixels, x: int, y: int, size: int, color, step: int = 0,
                  head: int = 4, active: bool = True) -> None:
    """A square outline with a bright run of cells walking around it: that is
    the "model is active" mark. Parked (dim, nothing moving) when idle.

    ``step`` is where the head sits on the ring this frame; ``head`` is how many
    cells the bright run covers. Keep ``head`` at least as large as the number of
    cells the head moves per frame, or a fast spinner reads as dashes.
    """
    ring = ring_cells(x, y, size)
    frame = _mix(color, 0.34) if active else OFFLINE
    for cx, cy in ring:
        _put(pixels, cx, cy, frame)
    if not active:
        return
    total = len(ring)
    for offset in range(max(1, int(head))):
        cx, cy = ring[(int(step) + offset) % total]
        _put(pixels, cx, cy, color)


def status_color(running: bool, reachable: bool = True, phase: int = 0):
    """Colour of the provider name: green whenever we have data, dark when we
    do not. Activity is the spinner's job, not the word's."""
    return GREEN if reachable else OFFLINE


def state_code(agent: dict, running: bool = False) -> str:
    """What the engine says it is doing, shortened for the panel."""
    engine_state = str(agent.get("engine_state") or "").strip().lower()
    phase = str(agent.get("phase") or "").strip().lower()
    if engine_state == "generating" and "think" in phase:
        return "THINK"
    for name in (engine_state, phase):
        if name in PHASE_CODES:
            return PHASE_CODES[name]
    if agent.get("error"):
        return "OFF"
    return "BUSY" if running else "IDLE"


def render_stats(pixels, stats: dict, phase: int = 0, step: int | None = None,
                  start_cell: int = 0) -> None:
    """Paint one frame of the stats screen into a flat RGB pixel list.

    ``step`` overrides how far the spinner travels this frame; by default it
    comes from ``spinner_step()``. ``start_cell`` is the ring cell the head sits
    on at frame 0 - a driver sets it so an upload starts where the panel's own
    loop already is, which is what makes the handoff invisible.
    """
    gateway = stats.get("gateway") or {}
    agent = stats.get("agent") or {}
    display = stats.get("display") or {}
    running = bool(display.get("running"))

    for index in range(MATRIX_WIDTH * MATRIX_HEIGHT):
        pixels[index] = (0, 0, 0)

    # The mode decides the spinner's colour, so resolve it first.
    mode = str(display.get("phase_kind") or "")
    if mode not in MODES:
        mode = "gen" if running else "rest"
    mode_label, mode_color = MODES[mode]
    active = mode in ("read", "gen")
    # How far the activity mark travels between frames.
    cells = max(1, spinner_step() if step is None else int(step))

    # Header: the provider name is the status light, so there is no unlabeled
    # dot next to the VRAM meter to guess at.
    draw_text(pixels, "STRATA", 0, 0, status_color(running, not agent.get("error"), phase))
    try:
        vram = float(display.get("vram_percent", (stats.get("monitor") or {}).get("vram_percent")))
    except (TypeError, ValueError):
        vram = None
    if vram is not None and vram >= 0:
        draw_text(pixels, "VR", 31, 0, DIM)
        draw_bar(pixels, 41, 0, 16, 5, vram, CYAN)
    draw_spinner(
        pixels,
        SPINNER_X,
        SPINNER_Y,
        SPINNER_SIZE,
        mode_color,
        step=int(phase) * cells + int(start_cell),
        head=max(SPINNER_HEAD, cells),
        active=active,
    )

    draw_text(pixels, _fit(model_label(gateway, agent), MATRIX_WIDTH), 0, 6, WHITE)

    # Bar 1: what it is doing right now, in that mode's colour.
    progress = display.get("progress_percent")
    if not isinstance(progress, (int, float)):
        progress = -1.0
    draw_text(pixels, mode_label, 0, 13, mode_color)
    mode_percent = _fit(percent_text(progress), MATRIX_WIDTH - 24)
    draw_text(
        pixels,
        mode_percent,
        MATRIX_WIDTH - text_width(mode_percent) - 1,
        13,
        mode_color,
    )
    draw_bar(pixels, 0, 18, MATRIX_WIDTH, 4, progress, mode_color)

    # Bar 2: how much of the context window is loaded, always.
    context = display.get("context_percent")
    if not isinstance(context, (int, float)):
        context = display.get("read_percent")
    draw_text(pixels, "CTX", 0, 23, DIM)
    context_percent = _fit(percent_text(context), MATRIX_WIDTH - 24)
    draw_text(
        pixels,
        context_percent,
        MATRIX_WIDTH - text_width(context_percent) - 1,
        23,
        CYAN,
    )
    draw_bar(pixels, 0, 28, MATRIX_WIDTH, 4, context, CYAN)

    # The two big numbers, labelled so they are not a guess: tokens read into
    # the context (left) and tokens produced by the current / last answer.
    read_text = compact_tokens(display.get("tokens_read_raw", agent.get("read_tokens", 0)))
    gen_text = compact_tokens(display.get("tokens_generated_raw", agent.get("generated_tokens", 0)))
    draw_text(pixels, "IN", 0, 33, DIM)
    draw_text(pixels, "OUT", MATRIX_WIDTH - text_width("OUT") - 1, 33, DIM)
    draw_text(pixels, read_text, 0, 39, WHITE, 2)
    draw_text(pixels, gen_text, MATRIX_WIDTH - text_width(gen_text, 2) - 1, 39, WHITE, 2)

    rate = display.get("tok_per_s")
    footer = sanitize(
        f"T/S {rate:.0f}" if isinstance(rate, (int, float)) and rate >= 0 else "T/S --"
    )
    try:
        sessions = int(agent.get("sessions_active") or 0)
    except (TypeError, ValueError):
        sessions = 0
    right = state_code(agent, running)
    if sessions > 1:
        right = f"{right} {sessions}"
    # Drop the session count, then shorten the rate, before anything clips.
    if text_width(footer) + text_width(right) + 2 > MATRIX_WIDTH:
        right = state_code(agent, running)
    if text_width(footer) + text_width(right) + 2 > MATRIX_WIDTH:
        footer = _fit(footer, MATRIX_WIDTH - text_width(right) - 2)
    draw_text(pixels, footer, 0, 49, DIM)
    draw_text(pixels, right, MATRIX_WIDTH - text_width(right) - 1, 49, DIM)


def new_frame() -> list:
    """A blank 64x54 RGB buffer (row-major, index = y * 64 + x)."""
    return [(0, 0, 0) for _ in range(MATRIX_WIDTH * MATRIX_HEIGHT)]


def quantize(pixels: list):
    """Palette + index list for the GIF writer (this screen uses few colors)."""
    palette: list = []
    return palette, _quantize_into(pixels, palette, {})


def _quantize_into(pixels: list, palette: list, index_of: dict) -> list:
    indices: list = []
    for color in pixels:
        key = (color[0], color[1], color[2])
        entry = index_of.get(key)
        if entry is None:
            if len(palette) >= 128:
                entry = 0
            else:
                entry = len(palette)
                palette.append(key)
                index_of[key] = entry
        indices.append(entry)
    if not palette:
        palette.append((0, 0, 0))
    return indices


def encode_frames(buffers, delay_cs: int = 10) -> bytes:
    """Quantize whole RGB frames onto one shared palette and encode them.

    Every screen builds frames the same way - draw, then hand them here - so the
    palette rule (one palette for the whole upload, colours added as they are
    seen) is stated once. Keeping the palette shared is also what keeps patched
    frames small.
    """
    palette: list = []
    index_of: dict = {}
    indices = [_quantize_into(pixels, palette, index_of) for pixels in buffers]
    return gif_encoder.encode(MATRIX_WIDTH, MATRIX_HEIGHT, palette, indices,
                                delay_cs=max(1, int(delay_cs)))


def render_stats_gif(
    stats: dict,
    phase: int = 0,
    delay_cs: int = 33,
    frames: int = 3,
    step: int | None = None,
    start_cell: int = 0,
) -> bytes:
    """Return GIF bytes for ``upload_gif``.

    The panel animates what an upload contains, then loops it, so an upload
    carries ``frames`` frames of motion and always a whole number of turns -
    that is what makes the loop invisible. The next upload still has to start on
    the cell the loop has reached (``start_cell``, see ``playback_cells``), or
    the mark visibly jumps sideways when it lands. ``step`` is how far the mark
    travels per frame (``JSAUX_AI_AGENT_SPINNER_STEP``).
    """
    frame_count = max(1, int(frames))
    cells = spinner_cells_for(frame_count, step)
    buffers = []
    for index in range(frame_count):
        pixels = new_frame()
        render_stats(pixels, stats, phase * frame_count + index, step=cells,
                      start_cell=start_cell)
        buffers.append(pixels)
    palette: list = []
    index_of: dict = {}
    indices = [_quantize_into(pixels, palette, index_of) for pixels in buffers]
    return gif_encoder.encode(MATRIX_WIDTH, MATRIX_HEIGHT, palette, indices, delay_cs=delay_cs)
