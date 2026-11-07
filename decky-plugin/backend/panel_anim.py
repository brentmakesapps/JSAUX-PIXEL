"""The media/gifs ideas, rebuilt as things a 64x54 dot panel can actually do.

Every one of these started as a GIF in ``media/gifs`` that was looked at, fitted
to the panel and rejected or kept by what the link costs (docs/panel-limits.md:
73ms a chunk, a 32 KiB GIF buffer, ~30 fps, and an upload whose cost follows how
much of the grid moves). A GIF does not survive that: the fitted files came out
at 5-17 frames of a 20-450 frame animation, which reads as a flicker, and cost
16-23 KB each. So these are not conversions - they are the same movement written
as geometry, with the two properties that make it affordable:

* one cell of movement per step, so consecutive frames differ where they must and
  nowhere else;
* thin features, because a cell that is on costs the same however it got there.

Each animation draws one step of a closed loop: the phase runs over exactly one
turn, so the frame the tip reaches at the end joins back onto the first one and
the loop it plays has no seam.

Colour is deliberately scarce - two colours plus at most one accent. Colour
index 0 is black, and the palette size decides the LZW code width, so a third
colour is paid for in every frame.
"""

from math import cos, pi, sin

from backend.panel_draw import BLACK, WHITE, Canvas, HEIGHT, WIDTH

TAU = 2 * pi


def wave(step: int, total: int, cycles: int = 1) -> float:
    """Phase for this step, running over exactly ``cycles`` turns per loop."""
    return TAU * cycles * step / float(total)


def dot_wave(canvas: Canvas, step: int, total: int) -> None:
    """dot-wave.gif: a lattice of rings with a wave travelling through it.

    The original is a field of dots with a front crossing it. At panel scale the
    dots become one-cell rings and the front is a diagonal phase, which is the
    only version of that idea where the movement is local.
    """
    phase = wave(step, total)
    gap = 4
    for row in range(2, HEIGHT - 1, gap):
        for column in range(2, WIDTH - 1, gap):
            reach = 1.6 + 1.4 * sin(phase + (column + row) * 0.16)
            canvas.ring(column, row, reach, WHITE)


def sphere_wave(canvas: Canvas, step: int, total: int) -> None:
    """spherewave.gif: rings of dots running outward over a ball.

    The lit ring is chosen by quantised distance and shifted by one each step,
    so every step moves the boundary between lit and dark dots and nothing else.
    Eight steps is one repeat of the pattern, and the loop is a whole number of
    them, so the frame the tip reaches last joins onto the first.
    """
    centre_x, centre_y = WIDTH // 2, HEIGHT // 2
    radius = 24
    canvas.ellipse(centre_x, centre_y, radius, radius * 0.85, WHITE)
    gap = 3
    for row in range(2, HEIGHT - 1, gap):
        for column in range(2, WIDTH - 1, gap):
            dx = (column - centre_x) / 1.0
            dy = (row - centre_y) / 0.85
            distance = (dx * dx + dy * dy) ** 0.5
            if distance > radius - 1:
                continue
            lit = (int(distance) + step) % 8 < 3
            canvas.px(column, row, WHITE if lit else BLACK)


def nested_bounce(canvas: Canvas, step: int, total: int) -> None:
    """nested-circle-bounce.gif: concentric rings, each bouncing in turn.

    This one fitted well (15 frames, 456 cells a frame) and geometry does it
    better: the rings are 26 cells each, so a whole frame is a few hundred cells.
    """
    phase = wave(step, total)
    centre_x, centre_y = WIDTH // 2, HEIGHT // 2
    for index in range(8):
        shift = phase - index * 0.5
        reach = 4 + index * 3.0 + 3.0 * sin(shift)
        canvas.ring(centre_x, centre_y, reach, WHITE)


def cyclone(canvas: Canvas, step: int, total: int) -> None:
    """cyclone-bubble.gif: stacked ellipses, a barrel of rings turning inside out.

    The fit kept 7 of 40 frames and cost 17 KB. Rings seen at an angle are
    ellipses, and animating their width in phase gives the same breathing.
    """
    phase = wave(step, total)
    centre_x = WIDTH // 2
    for index in range(14):
        along = index / 13.0
        y = 3 + int(along * (HEIGHT - 8))
        wide = 6 + 18 * sin(pi * along) + 4 * sin(phase + along * 4.0)
        tall = 1.2 + 2.4 * abs(sin(phase * 0.5 + along * 2.0))
        canvas.ellipse(centre_x, y, wide, tall, WHITE)


def dropplet(canvas: Canvas, step: int, total: int) -> None:
    """dropplet.gif / dropplet-3.gif: a funnel of rings, a wave running down it.

    The originals are 90-240 frames of a cone of rings opening and closing. The
    replica keeps the rings and moves one ring's width per step, so the loop is
    long and the change per frame is one ring.
    """
    phase = wave(step, total)
    centre_x = WIDTH // 2
    for index in range(13):
        along = index / 12.0
        y = 4 + int(along * (HEIGHT - 10))
        narrow = 2 + along * 20
        wide = narrow + 5 * sin(phase - along * 3.4)
        canvas.ellipse(centre_x, y, max(1.0, wide), 1.5 + along * 1.5, WHITE)


def hex_warp(canvas: Canvas, step: int, total: int) -> None:
    """hexigon-warp.gif: nested hexagons scaling through each other."""
    phase = wave(step, total)
    centre_x, centre_y = WIDTH // 2, HEIGHT // 2
    for index in range(6):
        reach = 3 + index * 4.5 + 4 * sin(phase - index * 0.55)
        reach = max(1.0, reach)
        points = []
        for corner in range(6):
            angle = TAU * corner / 6.0 + pi / 6.0
            points.append((centre_x + reach * cos(angle),
                           centre_y + reach * 0.85 * sin(angle)))
        canvas.poly(points, WHITE)


def pyramid_spin(canvas: Canvas, step: int, total: int) -> None:
    """piramid-spin.gif: one square pyramid turning over its own base.

    Only the base and the four apex edges are drawn. Adding the stacked layers
    the original has turned the picture into a scribble at panel scale.
    """
    phase = wave(step, total)
    centre_x, centre_y = WIDTH // 2, HEIGHT // 2 + 8
    apex_y = centre_y - 24
    corners = []
    for corner in range(4):
        angle = phase + TAU * corner / 4.0
        corners.append((centre_x + 20 * sin(angle),
                        centre_y + 8 * cos(angle)))
    canvas.poly(corners, WHITE)
    for point in corners:
        canvas.line(centre_x, apex_y, point[0], point[1], WHITE)
    inner = []
    for corner in range(4):
        angle = phase + TAU * corner / 4.0
        inner.append((centre_x + 9 * sin(angle), centre_y + 4 * cos(angle)))
    canvas.poly(inner, WHITE)


def gears(canvas: Canvas, step: int, total: int) -> None:
    """gears.gif: two toothed wheels, meshing.

    Teeth are single cells on a ring, and one turn of the small wheel is one
    loop, so the whole frame changes by a tooth or two.
    """
    phase = wave(step, total, cycles=1)
    for centre_x, centre_y, radius, teeth, offset in (
    (20, HEIGHT // 2, 14, 12, 0.0),
    (44, HEIGHT // 2 - 6, 9, 8, pi / 8),
    ):
        canvas.ring(centre_x, centre_y, radius, WHITE)
        canvas.ring(centre_x, centre_y, radius - 3, WHITE)
        for tooth in range(teeth):
            angle = phase * (1 if radius > 12 else -1.5) + offset + TAU * tooth / teeth
            canvas.line(centre_x + (radius - 3) * cos(angle),
                        centre_y + (radius - 3) * sin(angle),
                        centre_x + (radius + 2) * cos(angle),
                        centre_y + (radius + 2) * sin(angle), WHITE)


def cube_field(canvas: Canvas, step: int, total: int) -> None:
    """shifting-cubes.gif: an isometric lattice whose cubes turn over.

    A cube on this panel is a hexagon with three spokes to alternating corners,
    and turning it over is those spokes moving to the other three corners. Six
    of them, spaced so they never touch: a lattice that overlaps reads as a net.
    """
    phase = wave(step, total, cycles=2)
    side = 6
    for row in range(2):
        for column in range(3):
            centre_x = 12 + column * (2 * side + 3)
            centre_y = 14 + row * (int(1.8 * side) + 6)
            corners = []
            for corner in range(6):
                angle = TAU * corner / 6.0
                corners.append((centre_x + side * cos(angle),
                                centre_y + side * sin(angle)))
            canvas.poly(corners, WHITE)
            lean = 0.52 * sin(phase + (column + row) * 0.9)
            first = 0 if sin(phase + (column + row) * 0.9) < 0 else 1
            for spoke in range(3):
                angle = TAU * (first + spoke * 2) / 6.0 + lean
                canvas.line(centre_x, centre_y,
                            centre_x + side * cos(angle),
                            centre_y + side * sin(angle), WHITE)
def pinwheel(canvas: Canvas, step: int, total: int) -> None:
    """star.gif / spinning-nested-wheels.gif: a four-armed wheel that turns.

    The original fills a disc with arcs, which is the expensive kind of motion.
    A wheel of arms turns at the same apparent speed for a fraction of the cells.
    """
    phase = wave(step, total)
    centre_x, centre_y = WIDTH // 2, HEIGHT // 2
    for arm in range(4):
        angle = phase + TAU * arm / 4.0
        canvas.line(centre_x, centre_y,
                    centre_x + 24 * cos(angle), centre_y + 24 * sin(angle), WHITE)
        canvas.line(centre_x + 24 * cos(angle), centre_y + 24 * sin(angle),
                    centre_x + 10 * cos(angle + 0.9),
                    centre_y + 10 * sin(angle + 0.9), WHITE)
    canvas.ring(centre_x, centre_y, 3, WHITE)


def spirograph(canvas: Canvas, step: int, total: int) -> None:
    """spinning-nested-wheels.gif as a pen tracing a curve that keeps moving.

    The curve is one closed lap of an epicycloid; what is drawn is a length of it
    that advances, so the head adds a couple of cells and the tail drops the same
    number. Leaving the curve where the pen had been would have meant one frame
    at the loop point wiping the whole panel.
    """
    centre_x, centre_y = WIDTH // 2, HEIGHT // 2
    big, small, pen = 22.0, 7.0, 12.0
    reach = big - small
    # The pen term is angle * big / small, so the curve only closes after the
    # angle has run round ``small`` times.
    laps = int(small)
    samples = total * 4
    window = samples // 6
    for offset in range(window):
        index = (step * 4 + offset) % samples
        angle = TAU * index / float(samples) * laps
        x = centre_x + reach * cos(angle) + pen * cos(angle * (big / small))
        y = centre_y + reach * sin(angle) - pen * sin(angle * (big / small))
        canvas.px(x, y, WHITE)


def chevron(canvas: Canvas, step: int, total: int) -> None:
    """zig-zag--wave.gif: rows of zigzag running sideways one cell a step.

    The original is a solid field with a front through it; a solid field is a
    whole-panel repaint every frame, so this keeps the zigzag and lets the rows
    carry the movement instead.
    """
    offset = (step * 2) % 16
    for row in range(2, HEIGHT - 2, 6):
        points = []
        x = -16 + offset
        while x < WIDTH + 8:
            points.append((x, row))
            points.append((x + 4, row - 3))
            x += 8
        canvas.poly(points, WHITE, close=False)


def spokes(canvas: Canvas, step: int, total: int) -> None:
    """static-hairs.gif: dashed rays from a point, turning.

    Three dashes deep at a time, marching outward, so something moves every
    frame. The rays turn by a quarter of the gap between them each step, which
    brings a ray exactly onto its neighbour's place after eighteen steps and a
    full turn after seventy two.
    """
    centre_x, centre_y = WIDTH // 2, HEIGHT // 2
    rays = 18
    for ray in range(rays):
        angle = TAU * (ray + step * 0.25) / float(rays)
        for index in range(10):
            if (index + step) % 3:
                continue
            reach = 4 + index * 3
            canvas.px(centre_x + reach * cos(angle),
                      centre_y + reach * sin(angle), WHITE)
    canvas.ring(centre_x, centre_y, 2, WHITE)


def blob_wave(canvas: Canvas, step: int, total: int) -> None:
    """wavy-blob.gif: a rounded body with a ripple running round its edge."""
    phase = wave(step, total)
    centre_x, centre_y = WIDTH // 2, HEIGHT // 2
    points = []
    for index in range(96):
        angle = TAU * index / 96.0
        reach = 18 + 3.0 * sin(phase + angle * 3.0)
        points.append((centre_x + reach * cos(angle),
                       centre_y + reach * 0.82 * sin(angle)))
    canvas.poly(points, WHITE)


def sheet_fan(canvas: Canvas, step: int, total: int) -> None:
    """spinning-sheets.gif: a ruled surface, the rules fanning round.

    Each rule swings with its own offset instead of all of them together: a
    ruled surface seen edge on pauses at the ends of its swing, and here that
    read as the animation stopping.
    """
    phase = wave(step, total)
    apex_x, apex_y = WIDTH // 2, 4
    for index in range(13):
        sway = 16.0 * sin(phase + index * 0.55)
        canvas.line(apex_x, apex_y,
                    apex_x + (index - 6) * 10 + sway, HEIGHT - 4, WHITE)


def pivot(canvas: Canvas, step: int, total: int) -> None:
    """pivot-machine.gif: bars hinged through the centre, the pair also turning.

    The hinge opens and closes, and the whole assembly makes one turn per loop.
    The hinge alone retraces itself, which is half a loop of nothing moving;
    the turn is what carries the movement, at nearly two cells a step out at the
    ends of the bars.
    """
    phase = wave(step, total)
    spin = TAU * step / float(total)
    centre_x, centre_y = WIDTH // 2, HEIGHT // 2
    reach = 18
    swing = 0.5 + 0.45 * sin(phase)
    for mirror in (1, -1):
        angle = spin + mirror * swing
        end_x = centre_x + reach * cos(angle)
        end_y = centre_y + reach * sin(angle)
        canvas.line(centre_x - reach * cos(angle), centre_y - reach * sin(angle),
                    end_x, end_y, WHITE)
        second = angle + mirror * 0.9
        canvas.line(end_x, end_y, end_x + 8 * cos(second), end_y + 8 * sin(second),
                    WHITE)
    canvas.ring(centre_x, centre_y, 2, WHITE)


# Everything that can be drawn, in the order the rotation shows it.
ANIMATIONS = (
    ("dot-wave", 52, 4, dot_wave, "media/gifs/dot-wave.gif"),
    ("sphere-wave", 64, 4, sphere_wave, "media/gifs/spherewave.gif"),
    ("nested-bounce", 56, 4, nested_bounce, "media/gifs/nested-circle-bounce.gif"),
    ("cyclone", 80, 4, cyclone, "media/gifs/cyclone-bubble.gif"),
    ("dropplet", 88, 4, dropplet, "media/gifs/dropplet.gif"),
    ("hex-warp", 48, 4, hex_warp, "media/gifs/hexigon-warp.gif"),
    ("pyramid-spin", 64, 4, pyramid_spin, "media/gifs/piramid-spin.gif"),
    ("gears", 88, 4, gears, "media/gifs/gears.gif"),
    ("cube-field", 64, 4, cube_field, "media/gifs/shifting-cubes.gif"),
    ("pinwheel", 88, 4, pinwheel, "media/gifs/star.gif"),
    ("spirograph", 120, 4, spirograph, "media/gifs/spinning-nested-wheels.gif"),
    ("chevron", 56, 4, chevron, "media/gifs/zig-zag--wave.gif"),
    ("spokes", 72, 4, spokes, "media/gifs/static-hairs.gif"),
    ("blob-wave", 112, 4, blob_wave, "media/gifs/wavy-blob.gif"),
    ("sheet-fan", 56, 4, sheet_fan, "media/gifs/spinning-sheets.gif"),
    ("pivot", 64, 4, pivot, "media/gifs/pivot-machine.gif"),
)


def frames(name: str, total: int = 0) -> list:
    """Every frame of one animation, as flat RGB lists."""
    for label, frames, delay, painter, _ in ANIMATIONS:
        if label != name:
            continue
        total = max(2, int(total or frames))
        canvas = Canvas()
        out = []
        for step in range(total):
            canvas.reset()
            painter(canvas, step, total)
            out.append(canvas.frame())
        return out
    raise KeyError(name)


def delay_cs(name: str) -> int:
    for label, _frames, delay, _painter, _ in ANIMATIONS:
        if label == name:
            return delay
    raise KeyError(name)


def sources(name: str) -> str:
    for label, _frames, _delay, _painter, source in ANIMATIONS:
        if label == name:
            return source
    raise KeyError(name)
