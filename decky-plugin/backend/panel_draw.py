"""Drawing on the 64x54 grid: the shapes the replicated animations are made of.

A frame in this project is a flat row-major list of RGB tuples, one per cell,
which is what ``ai_agent_screen.encode_frames`` expects. This is the small
vector layer on top of that: points, lines, rings and ellipses, so an animation
is written as geometry instead of as arithmetic on list offsets.

Two rules come from the measured link (docs/panel-limits.md) and shape how the
primitives are written:

* a cell is on or off per colour, so nothing here blends or anti-aliases - a
  curve is a set of cells, and a curve that samples more than one cell per step
  just costs bytes;
* what an upload costs is decided by how much of the grid moves between frames,
  so the primitives plot exactly the cells they are asked for and no more.
"""

from math import cos, sin, pi

from backend import ai_agent_screen as screen

WIDTH = screen.MATRIX_WIDTH
HEIGHT = screen.MATRIX_HEIGHT
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)


def blank(color=BLACK) -> list:
    """An empty panel as a frame."""
    return [color] * (WIDTH * HEIGHT)


class Canvas:
    """A panel being drawn on, addressed by cell."""

    def __init__(self, background=BLACK) -> None:
        self.pixels = blank(background)
        self.background = background

    def reset(self) -> None:
        self.pixels = blank(self.background)

    def px(self, x: int, y: int, color) -> None:
        """One cell, silently ignored when it falls off the panel."""
        column, row = int(x), int(y)
        if 0 <= column < WIDTH and 0 <= row < HEIGHT:
            self.pixels[row * WIDTH + column] = color

    def line(self, x0: int, y0: int, x1: int, y1: int, color) -> None:
        """Bresenham: every cell the segment touches, no gaps on steep runs."""
        x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        step_x = 1 if x0 < x1 else -1
        step_y = 1 if y0 < y1 else -1
        error = dx + dy
        while True:
            self.px(x0, y0, color)
            if x0 == x1 and y0 == y1:
                break
            twice = 2 * error
            if twice >= dy:
                error += dy
                x0 += step_x
            if twice <= dx:
                error += dx
                y0 += step_y

    def poly(self, points, color, close: bool = True) -> None:
        """A polyline, or a closed shape when ``close`` is set."""
        if not points:
            return
        pairs = [(int(x), int(y)) for x, y in points]
        last = len(pairs) - 1
        for index in range(last):
            self.line(pairs[index][0], pairs[index][1],
                      pairs[index + 1][0], pairs[index + 1][1], color)
        if close and last:
            self.line(pairs[last][0], pairs[last][1], pairs[0][0], pairs[0][1], color)

    def ring(self, cx: int, cy: int, radius: float, color, thick: int = 1) -> None:
        """A circle as a set of cells.

        Sampling is dense enough that the ring never breaks into dashes: about
        one cell per step of perimeter.
        """
        radius = float(radius)
        if radius <= 0:
            self.px(cx, cy, color)
            return
        steps = max(8, int(6.5 * radius))
        for index in range(steps):
            angle = 6.2831853 * index / steps
            for weight in range(max(1, int(thick))):
                reach = radius - weight
                if reach <= 0:
                    continue
                self.px(cx + reach * cos(angle), cy + reach * sin(angle), color)

    def ellipse(self, cx: int, cy: int, wide: float, tall: float, color,
                 thick: int = 1) -> None:
        """An ellipse, which is what a ring seen at an angle looks like."""
        wide, tall = float(wide), float(tall)
        steps = max(8, int(3.3 * (wide + tall)))
        for index in range(steps):
            angle = 6.2831853 * index / steps
            for weight in range(max(1, int(thick))):
                reach_x = wide - weight
                reach_y = tall - weight
                if reach_x <= 0 or reach_y <= 0:
                    continue
                self.px(cx + reach_x * cos(angle), cy + reach_y * sin(angle), color)

    def frame(self) -> list:
        """The finished frame."""
        return list(self.pixels)


def changed_cells(before: list, after: list) -> int:
    """How many cells move between two frames.

    This is the number that decides what an upload costs, so every animation
    reports it and the tests keep it honest.
    """
    return sum(1 for left, right in zip(before, after) if left != right)
