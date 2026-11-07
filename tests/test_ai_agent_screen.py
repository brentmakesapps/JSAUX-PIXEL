import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "decky-plugin"))

from backend import ai_agent_screen as screen  # noqa: E402
from tests.test_gif_encoder import parse  # noqa: E402

DEMO = {
    "gateway": {"model": "qwen3.8-flash-next-iq3_s-256k", "reachable": True},
    "agent": {"read_tokens": 90000, "cache_read_tokens": 4000, "generated_tokens": 630,
              "sessions_active": 2, "error": None},
    "display": {"running": True, "phase_kind": "gen", "progress_percent": 44.0,
                "context_percent": 36.0, "read_percent": 100.0, "gen_percent": 7.7,
                "tokens_read_raw": 94000, "tokens_generated_raw": 630, "tok_per_s": 7.5},
}


def reading():
    return {
        "gateway": DEMO["gateway"],
        "agent": {"error": None},
        "display": {"running": True, "phase_kind": "read", "progress_percent": 62.0,
                    "context_percent": 36.0, "tokens_read_raw": 94000,
                    "tokens_generated_raw": None, "tok_per_s": 105.0},
    }


def resting():
    return {
        "gateway": DEMO["gateway"],
        "agent": {"error": None, "engine_state": "idle", "request_age_s": 600.0},
        "display": {"running": False, "phase_kind": "rest", "progress_percent": -1.0,
                    "context_percent": 36.0, "tokens_read_raw": 94000,
                    "tokens_generated_raw": None, "tok_per_s": 57.6},
    }


class FontTests(unittest.TestCase):
    def test_every_glyph_is_the_declared_size(self):
        for char, rows in screen.GLYPHS.items():
            self.assertEqual(len(rows), screen.FONT_H, char)
            for row in rows:
                self.assertEqual(len(row), screen.FONT_W, char)
                self.assertTrue(set(row) <= {"0", "1"}, char)

    def test_letters_and_digits_are_drawable(self):
        for char in "STRATA IQ3S256K READ GEN T/S 0123456789%":
            self.assertIn(char, screen.GLYPHS, char)

    def test_sanitize_drops_what_the_font_lacks(self):
        # The comma and the bang each collapse to one blank.
        self.assertEqual(screen.sanitize("hello, world!"), "HELLO  WORLD ")


class LayoutTests(unittest.TestCase):
    def test_text_width_matches_the_advance_step(self):
        self.assertEqual(screen.text_width(""), 0)
        self.assertEqual(screen.text_width("AB"), 2 * screen.FONT_W + 1)
        self.assertEqual(screen.text_width("AB", 2), 4 * screen.FONT_W + 2)

    def test_the_header_line_fits_the_panel(self):
        label = screen.model_label(DEMO["gateway"], DEMO["agent"])
        self.assertEqual(label, "IQ3 S 256K")
        self.assertLessEqual(screen.text_width("STRATA") + 8, screen.MATRIX_WIDTH)
        self.assertLessEqual(screen.text_width(label), screen.MATRIX_WIDTH)

    def test_token_labels_stay_short_enough_for_the_panel(self):
        self.assertEqual(screen.compact_tokens(94000), "94K")
        self.assertEqual(screen.compact_tokens(630), "630")
        self.assertLessEqual(len(screen.compact_tokens(1234567)), 3)
        self.assertLessEqual(
            screen.text_width("94K", 2) + screen.text_width("630", 2), screen.MATRIX_WIDTH
        )

    def test_percent_text(self):
        self.assertEqual(screen.percent_text(36.0), "36%")
        self.assertEqual(screen.percent_text(7.7), "7.7%")
        self.assertEqual(screen.percent_text(-1), "--")
        self.assertEqual(screen.percent_text(None), "--")


class RenderTests(unittest.TestCase):
    def lit(self, pixels, y):
        return [x for x in range(screen.MATRIX_WIDTH) if pixels[y * screen.MATRIX_WIDTH + x] != (0, 0, 0)]

    def test_the_screen_paints_something_in_every_band(self):
        pixels = screen.new_frame()
        screen.render_stats(pixels, DEMO, 0)
        for row in (0, 6, 13, 19, 24, 29, 34, 41, 50):
            self.assertTrue(self.lit(pixels, row), f"row {row} is empty")

    def test_the_bottom_row_is_not_clipped(self):
        pixels = screen.new_frame()
        screen.render_stats(pixels, DEMO, 0)
        self.assertTrue(self.lit(pixels, screen.MATRIX_HEIGHT - 2))

    def test_an_unknown_percent_leaves_the_bar_empty(self):
        pixels = screen.new_frame()
        screen.draw_bar(pixels, 0, 0, 40, 5, -1, screen.GREEN)
        bright = [p for p in pixels if p == screen.GREEN]
        self.assertEqual(bright, [])

    def test_bars_clamp_at_both_ends(self):
        for percent in (-5, 250):
            pixels = screen.new_frame()
            screen.draw_bar(pixels, 0, 0, 40, 5, percent, screen.AMBER)
            for row in range(1, 4):
                for col in range(1, 39):
                    if percent > 100:
                        self.assertEqual(pixels[row * 64 + col], screen.AMBER)

    def test_the_provider_name_stays_green(self):
        # Activity belongs to the spinner; the word is just the provider.
        for stats, phase in ((DEMO, 0), (reading(), 1), (resting(), 0)):
            pixels = screen.new_frame()
            screen.render_stats(pixels, stats, phase)
            self.assertEqual(pixels[1 * 64 + 0], screen.GREEN)
        offline = screen.new_frame()
        screen.render_stats(
            offline, {"gateway": {}, "agent": {"error": "metrics: URLError"}, "display": {}}, 0
        )
        self.assertNotEqual(offline[1 * 64 + 0], screen.GREEN)

    def test_the_spinner_is_the_activity_indicator(self):
        busy = screen.new_frame()
        screen.render_stats(busy, reading(), 0)
        idle = screen.new_frame()
        screen.render_stats(idle, resting(), 0)
        cells = lambda frame: [
            frame[row * 64 + col]
            for row in range(5)
            for col in range(screen.SPINNER_X, screen.SPINNER_X + screen.SPINNER_SIZE)
        ]
        self.assertIn(screen.BLUE, cells(busy))
        self.assertNotIn(screen.BLUE, cells(idle))
        self.assertIn(screen.OFFLINE, cells(idle))

    def test_the_mode_bar_takes_the_mode_colour(self):
        for stats, color in ((reading(), screen.BLUE), (DEMO, screen.GREEN)):
            pixels = screen.new_frame()
            screen.render_stats(pixels, stats, 0)
            filled = [p for p in pixels[19 * 64 + 2 : 20 * 64 + 2] if p == color]
            self.assertTrue(filled, color)

    def test_the_context_bar_stays_put_whatever_the_mode_is(self):
        for stats in (reading(), DEMO, resting()):
            pixels = screen.new_frame()
            screen.render_stats(pixels, stats, 0)
            self.assertEqual(pixels[29 * 64 + 2], screen.CYAN)

    def test_at_rest_the_mode_bar_resets_but_the_context_bar_does_not(self):
        pixels = screen.new_frame()
        screen.render_stats(pixels, resting(), 0)
        mode_band = slice(18 * 64, 22 * 64)
        self.assertNotIn(screen.BLUE, pixels[mode_band])
        self.assertNotIn(screen.GREEN, pixels[mode_band])
        self.assertEqual(pixels[29 * 64 + 2], screen.CYAN)

    def test_the_status_word_is_green_while_there_is_data(self):
        self.assertEqual(screen.status_color(True, True, 0), screen.GREEN)
        self.assertEqual(screen.status_color(False, True, 0), screen.GREEN)
        self.assertEqual(screen.status_color(False, False, 0), screen.OFFLINE)

    def test_the_spinner_is_a_ring_with_a_bright_run(self):
        pixels = screen.new_frame()
        screen.draw_spinner(pixels, 58, 0, 5, screen.BLUE, step=0, head=4)
        bright = [
            (row, col)
            for row in range(5)
            for col in range(58, 63)
            if pixels[row * 64 + col] == screen.BLUE
        ]
        self.assertEqual(len(bright), 4)
        # The rest of the outline is the same hue, just dimmed.
        dimmed = screen._mix(screen.BLUE, 0.34)
        rest = [
            pixels[row * 64 + col]
            for row in range(5)
            for col in range(58, 63)
            if pixels[row * 64 + col] == dimmed
        ]
        self.assertEqual(len(rest), 16 - 4)
        self.assertEqual(len(screen.ring_cells(58, 0, 5)), 16)

    def test_the_spinner_turns_with_the_phase(self):
        first, second = screen.new_frame(), screen.new_frame()
        screen.draw_spinner(first, 58, 0, 5, screen.GREEN, step=0)
        screen.draw_spinner(second, 58, 0, 5, screen.GREEN, step=1)
        moved = [
            i
            for i in range(len(first))
            if first[i] != second[i] and 58 <= i % 64 <= 62 and i // 64 < 5
        ]
        self.assertTrue(moved)

    def test_the_spinner_step_is_configurable(self):
        self.assertGreaterEqual(screen.SPINNER_STEP, 2)  # faster than one cell a frame
        for raw, expected in (("5", 5), ("nonsense", screen.SPINNER_STEP), ("0", 1)):
            os.environ[screen.SPINNER_STEP_ENV] = raw
            try:
                self.assertEqual(screen.spinner_step(), expected)  # never frozen
            finally:
                del os.environ[screen.SPINNER_STEP_ENV]

    def test_a_bigger_step_moves_the_head_further(self):
        def head_index(frame):
            ring = screen.ring_cells(screen.SPINNER_X, screen.SPINNER_Y, screen.SPINNER_SIZE)
            lit = [i for i, (cx, cy) in enumerate(ring) if frame[cy * 64 + cx] == screen.BLUE]
            return lit[0]

        for step in (1, 3, 6):
            first, second = screen.new_frame(), screen.new_frame()
            screen.render_stats(first, reading(), 0, step=step)
            screen.render_stats(second, reading(), 1, step=step)
            travelled = (head_index(second) - head_index(first)) % len(
                screen.ring_cells(screen.SPINNER_X, screen.SPINNER_Y, screen.SPINNER_SIZE)
            )
            self.assertEqual(travelled, step)

    def test_a_fast_spinner_keeps_one_continuous_run(self):
        step = 7  # bigger than SPINNER_HEAD
        pixels = screen.new_frame()
        screen.draw_spinner(pixels, 58, 0, 5, screen.GREEN, step=4, head=max(screen.SPINNER_HEAD, step))
        ring = screen.ring_cells(58, 0, 5)
        lit = [i for i, (cx, cy) in enumerate(ring) if pixels[cy * 64 + cx] == screen.GREEN]
        self.assertEqual(len(lit), step)
        # Consecutive on the ring, so the run reads as one mark, not as dashes.
        gaps = {(b - a) % len(ring) for a, b in zip(sorted(lit)[:-1], sorted(lit)[1:])}
        self.assertEqual(gaps, {1})

    def test_an_upload_starts_where_the_panels_loop_already_is(self):
        def head_index(frame):
            ring = screen.ring_cells(screen.SPINNER_X, screen.SPINNER_Y, screen.SPINNER_SIZE)
            lit = [i for i, (cx, cy) in enumerate(ring) if frame[cy * 64 + cx] == screen.BLUE]
            return lit[0]

        turn = screen.turn_seconds(16, 6)
        self.assertAlmostEqual(turn, 0.96)
        # Half a turn into the loop is where a replacement has to start, or the
        # mark throws itself sideways when it lands.
        cell = screen.playback_cells(turn / 2, turn)
        self.assertEqual(cell, screen.RING_CELLS // 2)
        frame = screen.new_frame()
        screen.render_stats(frame, reading(), 0, step=1, start_cell=cell)
        self.assertEqual(head_index(frame), cell)

    def test_a_replacement_is_built_for_when_it_lands_not_when_it_is_made(self):
        # Measured link: ~4.6KB/s, so a 2.5KB upload is on the panel half a
        # second after it is built - half a turn of drift if ignored.
        self.assertAlmostEqual(screen.upload_seconds(2300), 0.5, delta=0.15)

    def test_an_upload_carries_a_whole_turn(self):
        # The panel loops the GIF it holds; whole turns make that loop invisible.
        for frames in (1, 2, 4, 8):
            cells = screen.spinner_cells_for(frames)
            self.assertEqual((cells * frames) % screen.RING_CELLS, 0, frames)
        self.assertEqual(screen.spinner_cells_for(4, 3), 3)  # an explicit step wins

    def test_an_upload_carries_several_frames(self):
        # 16 frames of one cell each is a whole turn, and the frames after the
        # first patch a rectangle instead of resending the screen.
        frames = 16
        gif = screen.render_stats_gif(DEMO, 0, delay_cs=6, frames=frames)
        _, _, _, images = parse(gif)
        self.assertEqual(len(images), frames)
        self.assertEqual(images[0][2:4], (screen.MATRIX_WIDTH, screen.MATRIX_HEIGHT))
        self.assertLess(images[1][2] * images[1][3], 200)
        # The tip answers "rejected" to a GIF begin over ~32KB (measured between
        # 24.6KB accepted and 32.8KB rejected), so a turn has to stay inside that.
        self.assertLess(len(gif), 24_000)

    def test_the_big_numbers_are_labelled(self):
        pixels = screen.new_frame()
        screen.render_stats(pixels, DEMO, 0)
        left = {pixels[33 * 64 + x] for x in range(0, 9)}
        right = {pixels[33 * 64 + x] for x in range(54, 64)}
        self.assertIn(screen.DIM, left)
        self.assertIn(screen.DIM, right)

    def test_gif_output_is_playable_by_the_panel(self):
        gif = screen.render_stats_gif(DEMO, 0)
        self.assertTrue(gif.startswith(b"GIF89a"))
        self.assertGreater(len(gif), 100)

    def test_idle_and_unreachable_states_still_render(self):
        for stats in (
            {"gateway": {}, "agent": {"error": "no_database"}, "display": {}},
            {"gateway": {"model": "", "reachable": False}, "agent": {}, "display": {}},
        ):
            pixels = screen.new_frame()
            screen.render_stats(pixels, stats, 0)
            self.assertTrue(any(p != (0, 0, 0) for p in pixels))


if __name__ == "__main__":
    unittest.main()
