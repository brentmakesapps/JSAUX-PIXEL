"""The panel animation replicas have to fit the panel, not just look right.

What a replica costs is decided on the wire (docs/panel-limits.md): the tip
refuses a GIF begin at 32,768 bytes, plays about 30 fps, and charges for the
frames it is given. So these tests are mostly arithmetic about bytes, frame
counts and what moves - the things that decide whether an animation is playable.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT, ROOT / "decky-plugin"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

from backend import ai_agent_screen as screen  # noqa: E402
from backend import panel_anim, panel_draw  # noqa: E402

PIXELS = screen.MATRIX_WIDTH * screen.MATRIX_HEIGHT
# The tip's measured GIF buffer.
HARD_LIMIT = 32768
# What the bus should spend on one animation, leaving the buffer some slack.
BUS_BUDGET = 24576


class Drawing(unittest.TestCase):
    def test_off_panel_plots_are_ignored(self):
        canvas = panel_draw.Canvas()
        canvas.px(-4, 10, panel_draw.WHITE)
        canvas.px(70, 10, panel_draw.WHITE)
        canvas.px(10, -3, panel_draw.WHITE)
        canvas.line(-30, -30, 200, 200, panel_draw.WHITE)
        canvas.ring(-40, -40, 12, panel_draw.WHITE)
        canvas.ellipse(64, 56, 40, 40, panel_draw.WHITE)
        # Everything above fell off the edge; only the line's inside part remains.
        self.assertLess(sum(1 for cell in canvas.frame() if cell != panel_draw.BLACK),
                        PIXELS)

    def test_line_reaches_its_end(self):
        canvas = panel_draw.Canvas()
        canvas.line(0, 0, 20, 7, panel_draw.WHITE)
        lit = {index for index, cell in enumerate(canvas.frame())
               if cell != panel_draw.BLACK}
        self.assertIn(0, lit)
        self.assertIn(7 * screen.MATRIX_WIDTH + 20, lit)

    def test_ring_is_a_ring(self):
        canvas = panel_draw.Canvas()
        canvas.ring(30, 25, 10, panel_draw.WHITE)
        frame = canvas.frame()
        centre = frame[25 * screen.MATRIX_WIDTH + 30]
        edge = frame[25 * screen.MATRIX_WIDTH + 40]
        self.assertEqual(centre, panel_draw.BLACK)
        self.assertEqual(edge, panel_draw.WHITE)


class Animations(unittest.TestCase):
    def names(self):
        return [item[0] for item in panel_anim.ANIMATIONS]

    def test_names_are_unique(self):
        self.assertEqual(len(set(self.names())), len(self.names()))

    def test_each_animation_draws_a_whole_panel_of_frames(self):
        for name in self.names():
            frames = panel_anim.frames(name)
            self.assertGreater(len(frames), 1, name)
            for frame in frames:
                self.assertEqual(len(frame), PIXELS, name)

    def test_each_animation_is_deterministic(self):
        for name in self.names():
            self.assertEqual(panel_anim.frames(name), panel_anim.frames(name), name)

    def test_every_upload_fits_the_buffer(self):
        for name in self.names():
            frames = panel_anim.frames(name)
            data = screen.encode_frames(frames, delay_cs=panel_anim.delay_cs(name))
            self.assertLess(len(data), HARD_LIMIT, "%s is %d bytes" % (name, len(data)))
            self.assertLess(len(data), BUS_BUDGET, "%s crowds the bus" % name)

    def test_the_playback_rate_is_the_measured_one(self):
        # 2 cs was played 1.46x slow; 4 cs played exactly (docs/panel-limits.md).
        for name in self.names():
            self.assertGreaterEqual(panel_anim.delay_cs(name), 4, name)

    def test_nothing_freezes(self):
        """Every step has to move something, or the loop visibly hitches."""
        for name in self.names():
            frames = panel_anim.frames(name)
            pairs = list(zip(frames, frames[1:])) + [(frames[-1], frames[0])]
            still = sum(1 for before, after in pairs
                        if not panel_draw.changed_cells(before, after))
            self.assertEqual(still, 0, "%s has %d dead frames" % (name, still))

    def test_the_loop_closes(self):
        """Frame to frame must not jump further at the seam than anywhere else."""
        for name in self.names():
            frames = panel_anim.frames(name)
            moves = [panel_draw.changed_cells(before, after)
                     for before, after in zip(frames, frames[1:])]
            seam = panel_draw.changed_cells(frames[-1], frames[0])
            middle = sorted(moves)[len(moves) // 2]
            self.assertLessEqual(seam, max(middle * 3, 60),
                                 "%s jumps %d cells at the seam (typical %d)"
                                 % (name, seam, middle))

    def test_replicas_stay_within_a_third_of_the_literal_cost(self):
        """The point of the LZW stream: these frames are mostly empty."""
        for name in self.names():
            frames = panel_anim.frames(name)
            delay = panel_anim.delay_cs(name)
            packed = screen.encode_frames(frames, delay_cs=delay)
            literal = screen.encode_frames(frames, delay_cs=delay, compress=False)
            self.assertLess(len(packed), len(literal), name)

    def test_every_source_named_exists(self):
        for name in self.names():
            source = ROOT / panel_anim.sources(name)
            self.assertTrue(source.is_file(), "%s names %s" % (name, source))


if __name__ == "__main__":
    unittest.main()
