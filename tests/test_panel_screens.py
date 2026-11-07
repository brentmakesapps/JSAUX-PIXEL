import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "decky-plugin"))

from backend import panel_screens as bus  # noqa: E402


class Fake(bus.Source):
    """A screen that shows whatever it is told to, and counts its calls."""

    def __init__(self, name, *, priority=50, dwell=10.0, payload=b"one", ready=True,
                   now_urgent=False, fail="") -> None:
        self.name = name
        self.priority = priority
        self.dwell_s = dwell
        self.payload = payload
        self.ready = ready
        self.now_urgent = now_urgent
        self.fail = fail
        self.refreshes = 0

    def refresh(self, now):
        self.refreshes += 1
        if self.fail == "refresh":
            raise RuntimeError("refresh blew up")

    def available(self, now):
        if self.fail == "available":
            raise RuntimeError("available blew up")
        return self.ready

    def urgent(self, now):
        return self.now_urgent

    def gif(self, clock):
        if self.fail == "gif":
            raise RuntimeError("render blew up")
        return self.payload


def build(*sources, **kwargs):
    logs = []
    scheduler = bus.PanelBus(list(sources), log=logs.append, **kwargs)
    return scheduler, logs


class SchedulingTests(unittest.TestCase):
    def test_a_notification_pre_empts_and_the_old_screen_comes_back(self) -> None:
        stats = Fake("ai", priority=60, payload=b"stats")
        notify = Fake("notify", priority=90, payload=b"card", ready=False,
                       now_urgent=True)
        scheduler, _ = build(stats, notify)
        self.assertEqual(scheduler.tick(0.0)["screen"], "ai")
        notify.ready = True
        self.assertEqual(scheduler.tick(1.0)["screen"], "notify")
        # When it goes away the stats screen resumes.
        notify.ready = False
        self.assertEqual(scheduler.tick(2.0)["screen"], "ai")

    def test_rotation_moves_on_after_the_dwell(self) -> None:
        a = Fake("a", priority=50, dwell=5.0)
        b = Fake("b", priority=40, dwell=5.0)
        scheduler, _ = build(a, b)
        self.assertEqual(scheduler.tick(0.0)["screen"], "a")
        self.assertEqual(scheduler.tick(2.0)["screen"], "a")
        self.assertEqual(scheduler.tick(6.0)["screen"], "b")
        self.assertEqual(scheduler.tick(12.0)["screen"], "a")

    def test_identical_output_does_not_cost_an_upload(self) -> None:
        a = Fake("a", payload=b"same")
        scheduler, _ = build(a)
        self.assertEqual(scheduler.tick(0.0)["bytes"], 4)
        result = scheduler.tick(1.0)
        self.assertIsNone(result.get("gif"))
        self.assertEqual(result["reason"], "unchanged")

    def test_a_broken_source_cannot_take_the_panel_down(self) -> None:
        bad = Fake("bad", priority=99, fail="gif", ready=True)
        good = Fake("good", priority=10, payload=b"one")
        scheduler, logs = build(bad, good)
        # The broken one is preferred by priority, draws nothing three times,
        # and is then parked so the healthy screen gets the panel.
        for step in range(0, 3):
            self.assertEqual(scheduler.tick(float(step))["gif"], None)
        self.assertEqual(scheduler.tick(3.0)["gif"], b"one")
        self.assertTrue(any("render blew up" in line for line in logs))

    def test_every_source_is_polled_even_when_not_showing(self) -> None:
        a = Fake("a")
        b = Fake("b", priority=1)
        scheduler, _ = build(a, b)
        scheduler.tick(0.0)
        scheduler.tick(1.0)
        self.assertEqual(a.refreshes, 2)
        self.assertEqual(b.refreshes, 2)


class ClockTests(unittest.TestCase):
    def test_the_clock_advances_by_what_the_panel_will_have_played(self) -> None:
        clock = bus.PlaybackClock(upload_seconds=lambda size: 0.5)
        clock.mark_upload(2500, turn_s=0.64, now=0.0)
        # The upload lands half a second later, so nothing has been played yet.
        self.assertAlmostEqual(clock.elapsed_turns(0.2), 0.0)
        # One turn of a 0.64s animation ends 0.5 + 0.64 after the tick.
        self.assertAlmostEqual(clock.elapsed_turns(1.14), 1.0)

    def test_a_source_with_nothing_to_draw_falls_back(self) -> None:
        quiet = Fake("quiet", priority=90, payload=None)
        loud = Fake("loud", priority=5, payload=b"show")
        scheduler, _ = build(quiet, loud)
        self.assertEqual(scheduler.tick(0.0)["gif"], None)
        self.assertEqual(scheduler.tick(2.0)["gif"], None)
        self.assertEqual(scheduler.tick(4.0)["gif"], None)
        self.assertEqual(scheduler.tick(6.0)["gif"], b"show")


if __name__ == "__main__":
    unittest.main()
