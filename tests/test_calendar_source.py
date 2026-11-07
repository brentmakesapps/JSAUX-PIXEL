import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "decky-plugin"))

from backend import ai_agent_screen  # noqa: E402
from backend import calendar_source as cal  # noqa: E402

ICS = """
BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:Stand-up meeting
DTSTART:20261106T093000
DTEND:20261106T094500
END:VEVENT
BEGIN:VEVENT
SUMMARY:Long one; with \\,
  escapes here
DTSTART:20261107T140000
DURATION:PT1H30M
RRULE:FREQ=WEEKLY
END:VEVENT
BEGIN:VEVENT
SUMMARY:No start
END:VEVENT
END:VCALENDAR
"""


class ParseTests(unittest.TestCase):
    def test_events_are_parsed_and_folding_is_undone(self) -> None:
        events = cal.parse_events(ICS)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].summary, "Stand-up meeting")
        self.assertEqual(events[0].start, datetime(2026, 11, 6, 9, 30))
        # The folded SUMMARY continuation was rejoined before parsing.
        self.assertEqual(events[1].start, datetime(2026, 11, 7, 14, 0))

    def test_duration_becomes_an_end_time(self) -> None:
        events = cal.parse_events(ICS)
        self.assertEqual(events[1].end - events[1].start, timedelta(hours=1,
                                                                     minutes=30))

    def test_weekly_rule_shows_the_next_occurrence(self) -> None:
        events = cal.parse_events(ICS)
        now = datetime(2026, 11, 20, 10, 0)
        found = cal.upcoming(events, now)
        starts = [event.start for event in found]
        self.assertIn(datetime(2026, 11, 21, 14, 0), starts)

    def test_escapes_are_unescaped(self) -> None:
        events = cal.parse_events(ICS)
        self.assertEqual(events[1].summary, "Long one; with , escapes here")


class CountdownTests(unittest.TestCase):
    def test_units(self) -> None:
        now = datetime(2026, 11, 6, 12, 0)
        self.assertEqual(cal.countdown(now, now + timedelta(minutes=25)), "in 25m")
        self.assertEqual(cal.countdown(now, now + timedelta(minutes=125)), "in 2h05")
        self.assertEqual(cal.countdown(now, now + timedelta(days=3)), "in 3d")
        self.assertEqual(cal.countdown(now, now - timedelta(minutes=1)), "NOW")


class SourceTests(unittest.TestCase):
    def test_a_card_is_small_and_valid(self) -> None:
        now = datetime(2026, 11, 6, 8, 0)
        source = cal.CalendarSource(paths=[])
        source._events = cal.parse_events(ICS)
        gif = source.gif(None)
        self.assertTrue(gif.startswith(b"GIF8"))
        self.assertLess(len(gif), 32 * 1024)

    def test_no_events_means_no_screen(self) -> None:
        source = cal.CalendarSource(paths=[])
        source._events = []
        self.assertFalse(source.available(0.0))
        self.assertIsNone(source.gif(None))


if __name__ == "__main__":
    unittest.main()
