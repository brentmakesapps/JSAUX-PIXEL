import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import panel_cam  # noqa: E402
import unittest  # noqa: E402


def wave(hertz: float, seconds: float, fps: float) -> list:
    return [math.sin(2 * math.pi * hertz * (index / fps))
            for index in range(int(seconds * fps))]


class PlaybackMeasurementTests(unittest.TestCase):
    """The camera measurement behind the "about 30 fps" figure we quote."""

    def test_a_repeating_pattern_is_found_at_its_own_period(self) -> None:
        # Two turns a second filmed at 30 fps: 15 frames a turn, plenty to see.
        samples = wave(2.0, 4.0, 30.0)
        period = panel_cam.dominant_period(samples, 30.0)
        self.assertIsNotNone(period)
        self.assertAlmostEqual(period, 0.5, delta=0.05)

    def test_a_steady_panel_reports_nothing(self) -> None:
        self.assertIsNone(panel_cam.dominant_period([7.0] * 60, 30.0))

    def test_a_period_too_fast_for_the_camera_reads_as_a_multiple(self) -> None:
        """A pattern faster than the movie can resolve is never reported as
        shorter than it is - which is why the probe compares a *ratio* to one
        and flags the unresolvable case instead of trusting the number.
        """
        samples = wave(5.0, 6.0, 30.0)          # 0.2s a turn
        period = panel_cam.dominant_period(samples, 30.0, min_s=0.4)
        self.assertIsNotNone(period)
        self.assertGreaterEqual(period, 0.4)
        self.assertAlmostEqual(period / 0.2, round(period / 0.2), delta=0.15)

    def test_it_reports_the_fundamental_not_a_harmonic(self) -> None:
        samples = wave(1.0, 6.0, 30.0)          # 1s a turn
        period = panel_cam.dominant_period(samples, 30.0)
        self.assertIsNotNone(period)
        self.assertAlmostEqual(period, 1.0, delta=0.07)

    def test_noise_does_not_look_like_a_turn(self) -> None:
        samples = [(index * 37) % 11 for index in range(120)]
        period = panel_cam.dominant_period(samples, 30.0, min_s=0.4)
        # A 3-cycle pattern in the noise is a real 0.1s period, below the window.
        self.assertTrue(period is None or period >= 0.4)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
