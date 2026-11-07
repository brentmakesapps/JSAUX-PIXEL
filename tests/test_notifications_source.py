import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "decky-plugin"))

from backend import notifications_source as notify  # noqa: E402
from backend import panel_text  # noqa: E402

# The tip's measured GIF buffer (docs/panel-limits.md): nothing we upload is
# allowed to be bigger than this.
PANEL_GIF_CEILING = 32 * 1024


class SpoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_enqueue_and_read_round_trip(self) -> None:
        notify.enqueue(self.dir, title="Build failed", body="3 tests",
                        source="ci", ttl=30.0, now=1000.0)
        items = notify.read_pending(self.dir, 1001.0)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Build failed")
        self.assertEqual(items[0].source, "ci")

    def test_expired_messages_are_dropped_and_removed(self) -> None:
        notify.enqueue(self.dir, title="Old", ttl=5.0, now=1000.0)
        self.assertEqual(len(notify.read_pending(self.dir, 1002.0)), 1)
        self.assertEqual(notify.read_pending(self.dir, 1010.0), [])
        self.assertEqual(list(self.dir.glob("*.json")), [])

    def test_queue_drains_in_order(self) -> None:
        notify.enqueue(self.dir, title="first", ttl=60.0, now=1000.000)
        notify.enqueue(self.dir, title="second", ttl=60.0, now=1000.100)
        items = notify.read_pending(self.dir, 1001.0)
        self.assertEqual([item.title for item in items], ["first", "second"])

    def test_clear(self) -> None:
        notify.enqueue(self.dir, title="a", ttl=60.0, now=1000.0)
        notify.enqueue(self.dir, title="b", ttl=60.0, now=1001.0)
        self.assertEqual(notify.clear(self.dir), 2)
        self.assertEqual(notify.read_pending(self.dir, 1002.0), [])


class CardTests(unittest.TestCase):
    def item(self, title="Deploy done", body="web 3 of 3 OK", **kwargs) -> notify.Notification:
        defaults = {"ttl": 30.0, "posted_at": 1000.0, "source": "ci"}
        defaults.update(kwargs)
        return notify.Notification(title=title, body=body, **defaults)

    def test_a_short_message_is_one_still_frame(self) -> None:
        frames = notify.card_frames(self.item())
        self.assertEqual(len(frames), 1)

    def test_a_long_message_scrolls_instead_of_paging(self) -> None:
        long = "the quick brown fox jumps over the lazy dog again and again"
        frames = notify.card_frames(self.item(body=long))
        self.assertGreater(len(frames), 10)

    def test_only_the_scroll_row_changes_between_frames(self) -> None:
        long = "the quick brown fox jumps over the lazy dog"
        frames = notify.card_frames(self.item(body=long))
        before, after = frames[0], frames[len(frames) // 2]
        changed = sum(1 for a, b in zip(before, after) if a != b)
        self.assertGreater(changed, 0)
        # A full repaint would change most of the 3456 pixels; one row is at most
        # a couple of hundred, which is why scrolling is affordable at all.
        self.assertLess(changed, 600)

    def test_the_card_stays_under_the_panel_ceiling(self) -> None:
        long = "notification text that goes on and on " * 4
        gif = self.item(body=long)
        frames = notify.card_frames(gif)
        from backend import ai_agent_screen
        data = ai_agent_screen.encode_frames(frames, delay_cs=notify.SCROLL_DELAY_CS)
        self.assertLess(len(data), PANEL_GIF_CEILING)

    def test_source_shows_a_card_and_pre_empts(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        source = notify.NotificationsSource(directory)
        now = time.monotonic()
        self.assertFalse(source.available(now))
        notify.enqueue(directory, title="Hello", body="panel", ttl=30.0)
        source.refresh(now)
        self.assertTrue(source.available(now))
        self.assertTrue(source.urgent(now))
        gif = source.gif(None)
        self.assertTrue(gif and gif.startswith(b"GIF8"))
        self.assertGreater(source.turn_seconds(), 0.0)


class TextTests(unittest.TestCase):
    def test_columns_match_the_panel_width(self) -> None:
        self.assertEqual(panel_text.columns(1), 12)
        self.assertEqual(panel_text.columns(2), 6)

    def test_long_words_are_broken_not_dropped(self) -> None:
        lines = panel_text.wrap("extraordinarily long", 8, 3)
        self.assertTrue(all(len(line) <= 8 for line in lines))
        self.assertIn("EXTRAORD", lines[0])

    def test_scroll_offsets_start_and_end_off_panel(self) -> None:
        offsets = panel_text.scroll_frames("hello there", step_px=3)
        self.assertGreater(offsets[0], 64)
        self.assertLess(offsets[-1], -panel_text.text_width("hello there"))


if __name__ == "__main__":
    unittest.main()
