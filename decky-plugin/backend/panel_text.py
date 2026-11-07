"""Fitting words onto 64x54, and knowing when they do not fit.

The built-in glyph set is 4x5 pixels with a one-pixel space, so a line holds
``64 // (4*scale + scale)`` characters: 12 at scale 1, 6 at scale 2. Nine rows at
scale 1 - about 108 characters on the whole panel. That is the constraint every
content screen hits, so it lives here rather than being re-derived per screen,
and anything longer than that scrolls (see ``scroll_frames``).

Debugging layout without hardware: every screen can be rendered to a PNG
(``--png`` on the tools), and ``columns``/``wrap`` are what that picture shows.
"""

from __future__ import annotations

SCREEN = __import__("backend.ai_agent_screen", fromlist=["*"])

MATRIX_WIDTH = SCREEN.MATRIX_WIDTH
MATRIX_HEIGHT = SCREEN.MATRIX_HEIGHT
GLYPH_W = SCREEN.FONT_W
GLYPH_H = SCREEN.FONT_H
# A glyph plus the one-pixel gap after it.
STEP = SCREEN.ADVANCE


def columns(scale: int = 1) -> int:
    """Characters that fit on one line at this text size."""
    scale = max(1, int(scale))
    return MATRIX_WIDTH // (GLYPH_W * scale + scale)


def rows(scale: int = 1, top: int = 0) -> int:
    """How many lines fit below ``top`` at this size."""
    scale = max(1, int(scale))
    return max(0, (MATRIX_HEIGHT - int(top)) // (GLYPH_H * scale + scale))


def clean(text: str) -> str:
    """The glyphs the panel actually has; anything else becomes a space."""
    return SCREEN.sanitize(text)


def wrap(text: str, width: int, max_lines: int) -> list:
    """Fold ``text`` into at most ``max_lines`` lines of ``width``.

    The last line is truncated with an ellipsis rather than dropped, so a long
    message reads as long instead of looking complete.
    """
    width = max(1, int(width))
    words = clean(text).split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
        while len(current) > width:  # a word longer than the line
            lines.append(current[:width])
            current = current[width:]
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        lines[-1] = last[:max(1, width - 1)] + ".." if len(last) > width else last
    return lines


def text_width(text: str, scale: int = 1) -> int:
    """Pixels a string occupies at this size (including its trailing space)."""
    scale = max(1, int(scale))
    return len(clean(text)) * (GLYPH_W * scale + scale)


def fill_rect(pixels, x: int, y: int, width: int, height: int, color) -> None:
    """Paint a block - used for the rules and dividers on content cards."""
    for row in range(int(y), int(y) + max(1, int(height))):
        for column in range(int(x), int(x) + max(1, int(width))):
            if 0 <= column < MATRIX_WIDTH and 0 <= row < MATRIX_HEIGHT:
                pixels[row * MATRIX_WIDTH + column] = color


def draw_at(pixels, text: str, column: int, y_px: int, color, scale: int = 1) -> int:
    """Draw at a character column and a pixel row; returns the next column."""
    scale = max(1, int(scale))
    SCREEN.draw_text(pixels, clean(text), int(column) * STEP * scale, int(y_px),
                     color, scale)
    return int(column) + len(clean(text))


def draw_scrolled(pixels, text: str, offset_px: int, y_px: int, color,
                    scale: int = 1) -> None:
    """Draw a line pushed sideways by ``offset_px``, skipping what is off-panel.

    Only the glyphs that overlap the panel are drawn, which is what keeps a
    scrolling row to about a hundred bytes a frame instead of a full repaint.
    """
    scale = max(1, int(scale))
    text = clean(text)
    step = STEP * scale
    glyph_w = GLYPH_W * scale
    for position, character in enumerate(text):
        x = int(offset_px) + position * step
        if x >= MATRIX_WIDTH or x + glyph_w <= 0:
            continue
        SCREEN.draw_text(pixels, character, x, int(y_px), color, scale)


def scroll_frames(text: str, *, step_px: int = 3, scale: int = 1,
                   pad: int = 8) -> list:
    """X offsets that run one line across the panel and come back empty.

    Starting fully off the right edge and ending fully off the left makes the
    loop self-contained - the tip replays it, and a loop that begins and ends on
    an empty row has no visible seam. That is the whole reason the offsets are
    generated rather than cropped from a wide render.
    """
    step_px = max(1, int(step_px))
    width = text_width(text, scale)
    if not clean(text):
        return [0]
    # Left offset of the first character, from off-screen right to off-screen left.
    start = MATRIX_WIDTH + pad
    end = -(width + pad)
    offsets = list(range(start, end - 1, -step_px))
    if offsets[-1] != end:
        offsets.append(end)
    return offsets
