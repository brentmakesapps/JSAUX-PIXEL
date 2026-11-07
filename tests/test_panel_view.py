"""Holding the panel on one view instead of cycling through them.

Three things are checked here: the view file round-trips and is noticed by a
reader that only looks at the clock; the bus obeys it (and does not let it
outrank a notification); and the gif screen can be pinned to one animation,
which is what a panel held on "gif" needs to be useful.
"""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT / "decky-plugin", ROOT / "tools"):
    sys.path.insert(0, str(folder))

from backend import panel_screens as bus  # noqa: E402
from backend.gif_screen import GifSource  # noqa: E402
import panel_view  # noqa: E402


class Screen(bus.Source):
    """A screen that shows fixed bytes and remembers what it was pinned to."""

    def real_init(self, name, priority=50, dwell=10.0, payload=b"one", ready=True):
        self.name = name
        self.priority = priority
        self.dwell_s = dwell
        self.payload = payload
        self.ready = ready
        self.picks = []
        self.is_urgent = False

    def available(self, now):
        return self.ready

    def gif(self, clock):
        return self.payload

    def urgent(self, now):
        return self.is_urgent

    def set_pick(self, pick):
        self.picks.append(pick)


def screen(name, **kwargs):
    """A Screen, built without a constructor so nothing can mangle one."""
    item = Screen()
    item.real_init(name, **kwargs)
    return item


class ViewFileTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.lock = bus.ViewLock(Path(self.folder.name) / "view.json")

    def tearDown(self):
        self.folder.cleanup()

    def test_a_missing_file_means_the_rotation(self):
        self.lock.reload()
        self.assertEqual(self.lock.mode, bus.ROTATE)
        self.assertEqual(self.lock.summary(), "rotate")

    def test_a_write_is_seen_by_a_reader_that_only_checks_the_clock(self):
        self.lock.write(bus.LOCK, "gif", "gears")
        other = bus.ViewLock(self.lock.path)
        self.assertTrue(other.reload())
        self.assertEqual((other.mode, other.screen, other.pick),
                          (bus.LOCK, "gif", "gears"))
        # The same file tells the reader nothing has changed until it changes.
        self.assertFalse(other.reload())

    def test_a_bad_file_leaves_the_last_view_alone(self):
        self.lock.write(bus.LOCK, "calendar")
        self.lock.path.write_text("{not json", encoding="utf-8")
        self.lock._stamp = None
        self.lock.reload()
        self.assertEqual((self.lock.mode, self.lock.screen), (bus.LOCK, "calendar"))
        self.assertTrue(self.lock.error)

    def test_auto_clears_the_screen_and_the_pick(self):
        self.lock.write(bus.LOCK, "gif", "gears")
        self.lock.write(bus.ROTATE)
        self.assertEqual((self.lock.screen, self.lock.pick), ("", ""))


class BusViewTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.lock = bus.ViewLock(Path(self.folder.name) / "view.json")

    def tearDown(self):
        self.folder.cleanup()

    def bus_with(self, *screens):
        logs = []
        scheduler = bus.PanelBus(list(screens), log=logs.append)
        scheduler.set_view(self.lock)
        return scheduler, logs

    def test_a_lock_holds_past_every_dwell(self):
        ai = screen("ai", priority=60, payload=b"ai")
        gif = screen("gif", priority=20, payload=b"gif")
        self.lock.write(bus.LOCK, "gif")
        scheduler, _ = self.bus_with(ai, gif)
        self.assertEqual(scheduler.tick(0.0)["reason"], "locked to gif")
        for moment in (0.0, 30.0, 900.0):
            self.assertEqual(scheduler.tick(moment)["screen"], "gif")
        # Nothing new to send, so the later ticks say so rather than re-uploading.
        self.assertEqual(scheduler.tick(901.0)["reason"], "unchanged")

    def test_without_a_lock_the_same_bus_rotates(self):
        ai = screen("ai", priority=60, payload=b"ai")
        gif = screen("gif", priority=20, payload=b"gif")
        scheduler, _ = self.bus_with(ai, gif)
        seen = [scheduler.tick(moment)["screen"] for moment in (0.0, 20.0, 40.0)]
        self.assertEqual(seen, ["ai", "gif", "ai"])

    def test_a_pin_reaches_the_screen_that_has_insides(self):
        ai = screen("ai", priority=60, payload=b"ai")
        gif = screen("gif", priority=20, payload=b"g")
        self.lock.write(bus.LOCK, "gif", "gears")
        scheduler, _ = self.bus_with(ai, gif)
        scheduler.tick(0.0)
        scheduler.tick(1.0)
        self.assertEqual(gif.picks, ["gears", "gears"])
        self.assertEqual(ai.picks, [])

    def test_a_notification_still_interrupts_a_lock(self):
        gif = screen("gif", priority=20, payload=b"g")
        card = screen("notify", priority=90, payload=b"card", ready=False)
        card.is_urgent = True
        self.lock.write(bus.LOCK, "gif")
        scheduler, _ = self.bus_with(gif, card)
        self.assertEqual(scheduler.tick(0.0)["screen"], "gif")
        card.ready = True
        self.assertEqual(scheduler.tick(1.0)["screen"], "notify")
        card.ready = False
        self.assertEqual(scheduler.tick(2.0)["screen"], "gif")

    def test_a_lock_on_a_screen_with_nothing_to_say_falls_back(self):
        quiet = screen("gif", priority=20, ready=False)
        loud = screen("ai", priority=60, payload=b"ai")
        self.lock.write(bus.LOCK, "gif")
        scheduler, _ = self.bus_with(quiet, loud)
        seen = [scheduler.tick(moment)["screen"] for moment in (0.0, 20.0, 40.0)]
        self.assertEqual(seen, ["ai", "ai", "ai"])


class GifPinTests(unittest.TestCase):
    """The gif screen held on one animation."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.directory = Path(self.folder.name)
        for name in ("one", "two", "three"):
            (self.directory / (name + ".gif")).write_bytes(b"GIF89a" + name.encode())
        self.source = GifSource(self.directory)
        # The refresh is throttled to one a screen's worth of seconds, so the
        # first call has to be past the start of the clock.
        self.source.refresh(10.0)

    def tearDown(self):
        self.folder.cleanup()

    def served(self, times):
        """The files the screen would hand out, in order."""
        return [self.source.gif(None) or b"" for _ in range(times)]

    def files(self, *names):
        return [b"GIF89a" + name.encode() for name in names]

    def test_the_rotation_walks_the_directory(self):
        # The directory is served in name order, which is not the order the
        # names read like English: one, three, two.
        self.assertEqual(self.served(4), self.files("one", "three", "two", "one"))

    def test_a_pin_serves_the_same_file_every_time(self):
        self.source.set_pick("two")
        self.assertEqual(self.served(4), self.files("two") * 4)
        self.assertIn("two.gif", self.source.describe(0.0))

    def test_a_pin_survives_the_directory_being_rescanned(self):
        self.source.set_pick("three")
        self.source.refresh(100.0)
        self.assertEqual(self.served(1), self.files("three"))

    def test_an_unknown_pin_does_not_move_anything(self):
        self.source.set_pick("no-such-thing")
        self.assertEqual(self.served(2), self.files("one", "three"))

    def test_a_file_over_the_buffers_limit_is_skipped_not_uploaded(self):
        (self.directory / "aaa-big.gif").write_bytes(b"GIF89a" + b"x" * 40000)
        source = GifSource(self.directory)
        source.refresh(10.0)
        source.set_pick("aaa-big")
        self.assertIsNone(source.gif(None))


class CliTests(unittest.TestCase):
    """The command a widget drives, against a view file of its own."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.view = Path(self.folder.name) / "view.json"

    def tearDown(self):
        self.folder.cleanup()

    def call(self, *argv):
        """The command, with its report swallowed: a test is not a terminal."""
        buffer = io.StringIO()
        saved, sys.stdout = sys.stdout, buffer
        try:
            return panel_view.main(["--view", str(self.view)] + list(argv))
        finally:
            sys.stdout = saved

    def read(self):
        other = bus.ViewLock(self.view)
        other.reload()
        return other.state()

    def quiet(self, *argv):
        """The command and what it printed, for the tests that read it."""
        printed = []
        real = sys.stdout

        class Capture(io.StringIO):
            def write(self, text):
                printed.append(text)
                return len(text)

        sys.stdout = Capture()
        try:
            code = panel_view.main(["--view", str(self.view)] + list(argv))
        finally:
            sys.stdout = real
        return code, "".join(printed)

    def test_lock_then_auto(self):
        self.call("lock", "gif", "--pick", "gears")
        self.assertEqual(self.read()["pick"], "gears")
        self.call("auto")
        self.assertEqual(self.read()["mode"], bus.ROTATE)

    def test_next_walks_the_animations(self):
        self.call("lock", "gif", "--pick", "blob-wave")
        self.call("next")
        self.assertEqual(self.read()["pick"], "chevron")

    def test_an_unknown_screen_is_refused(self):
        self.assertEqual(self.quiet("lock", "not-a-screen")[0], 2)

    def test_the_json_a_widget_reads(self):
        self.call("lock", "gif")
        code, printed = self.quiet("--json")
        data = json.loads(printed.strip())
        self.assertEqual(code, 0)
        self.assertEqual(data["mode"], "lock")
        self.assertIn("name", data["screens"][0])


# The script guard, written through names so nothing here depends on a
# dunder surviving a copy-paste.
RUNNING_AS_A_SCRIPT = globals().get(chr(95) * 2 + "name" + chr(95) * 2) == \
    chr(95) * 2 + "main" + chr(95) * 2
if RUNNING_AS_A_SCRIPT:
    unittest.main()
