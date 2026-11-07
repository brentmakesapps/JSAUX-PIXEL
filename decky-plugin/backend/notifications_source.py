"""Notification cards: the one source allowed to take the panel off someone else.

Reads a spool directory - one JSON file per message - so anything that can write
a file can put a card on the panel: ``tools/panel_notify.py``, a CI callback, a
D-Bus listener added later. Nothing here talks to the panel.

Why the card is drawn the way it is (docs/panel-features.md): the panel is 64x54
with a 4x5 glyph set, so about 108 characters fit at once and a smaller GIF
canvas does not patch the screen - it replaces it. So a card is one whole frame,
and the only cheap motion available is one scrolling row (~97 bytes a frame).
That is why a short message is a single still frame and a long one scrolls once
per loop instead of paging.

There is deliberately **no live countdown on the card**: a number that changes
every second costs a fresh upload (five round trips) every second, for information
nobody reads off a panel. The card simply stops interrupting when its TTL is up.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from backend import ai_agent_screen as screen
from backend import panel_text
from backend.panel_screens import PlaybackClock, Source, expand

# A card may interrupt for this long before it goes back to waiting its turn.
DEFAULT_TTL_S = 45.0
# Pixels the scrolling row advances a frame. 3px at 4cs is ~15 characters a
# second: readable, and 55 frames of motion costs about 5KB total.
SCROLL_STEP_PX = 3
SCROLL_DELAY_CS = 4
MAX_TITLE_LINES = 2
MAX_BODY_LINES = 2
# How much of a message is worth scrolling. At 3px a step, 120 frames is ~360px
# of travel - about 70 characters - and costs ~12KB of the 32 KiB the tip allows.
MAX_SCROLL_CHARS = 90
MAX_SCROLL_FRAMES = 120
# A card must not hold the wire: at 73ms a chunk this is about 1.2s of upload.
MAX_CARD_BYTES = 8192


def spool_dir(override: str = "") -> Path:
    if override:
        return expand(override)
    base = os.environ.get("JSAUX_PIXEL_NOTIFY_DIR", "").strip()
    if base:
        return expand(base)
    return Path.home() / ".local/share/jsaux-pixel/notify"


class Notification:
    def __init__(self, *, title: str, body: str, source: str, posted_at: float,
                   ttl: float) -> None:
        self.title = str(title or "")
        self.body = str(body or "")
        self.source = str(source or "local")
        self.posted_at = float(posted_at)
        self.ttl = max(1.0, float(ttl))

    @property
    def expires_at(self) -> float:
        return self.posted_at + self.ttl

    def alive(self, now: float) -> bool:
        return now < self.expires_at

    def __repr__(self) -> str:
        return f"<{self.source}: {self.title!r}>"


def enqueue(directory: Path, *, title: str, body: str = "", source: str = "local",
             ttl: float = DEFAULT_TTL_S, now: float | None = None) -> Path:
    """Write one message into the spool. Newest sorts last by name."""
    now = time.time() if now is None else now
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"title": title, "body": body, "source": source,
                "posted_at": now, "ttl": float(ttl)}
    # The suffix keeps two messages posted in the same second distinct.
    path = directory / f"{now:013.3f}-{source[:12]}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def read_pending(directory: Path, now: float) -> list:
    """Unexpired messages, oldest first (so a queue drains in order)."""
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return []
    items = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A half-written or foreign file is not our problem to keep.
            continue
        item = Notification(title=data.get("title", ""), body=data.get("body", ""),
                             source=data.get("source", "local"),
                             posted_at=float(data.get("posted_at", now)),
                             ttl=float(data.get("ttl", DEFAULT_TTL_S)))
        if not item.alive(now):
            try:
                path.unlink()
            except OSError:
                pass
            continue
        items.append(item)
    return items


def clear(directory: Path) -> int:
    try:
        paths = list(directory.glob("*.json"))
        for path in paths:
            path.unlink()
        return len(paths)
    except OSError:
        return 0


class NotificationsSource(Source):
    """The newest unexpired message, shown until its TTL runs out."""

    name = "notify"
    # Above every other screen: a notification interrupts, everything else waits.
    priority = 90
    dwell_s = 15.0

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or spool_dir()
        self._pending: list = []
        self._refreshed_at = 0.0
        self._turn = 0.0

    def refresh(self, now: float) -> None:
        # A directory listing every tick is cheap; skip it inside the same second.
        if now - self._refreshed_at < 0.5:
            return
        self._refreshed_at = now
        self._pending = read_pending(self.directory, time.time())

    def available(self, now: float) -> bool:
        real_now = time.time()
        return any(item.alive(real_now) for item in self._pending)

    def urgent(self, now: float) -> bool:
        return self.available(now)

    def describe(self, now: float) -> str:
        item = self.current()
        return f"notify:{item.source}" if item else "notify"

    def current(self) -> Notification | None:
        real_now = time.time()
        for item in self._pending:
            if item.alive(real_now):
                return item
        return None

    def turn_seconds(self) -> float:
        return self._turn

    def gif(self, clock: PlaybackClock) -> bytes | None:
        item = self.current()
        if item is None:
            return None
        frames = card_frames(item)
        # A card has to fit the link as well as the screen: at 73ms a 498-byte
        # chunk, a 22KB scroll would sit on the wire for three seconds and hold
        # the panel off whatever is next. Keep the scroll, not the whole message.
        data = screen.encode_frames(frames, delay_cs=SCROLL_DELAY_CS)
        keep = len(frames)
        while len(data) > MAX_CARD_BYTES and keep > 1:
            keep = max(1, int(keep * 0.7))
            data = screen.encode_frames(frames[:keep], delay_cs=SCROLL_DELAY_CS)
        self._turn = keep * SCROLL_DELAY_CS / 100.0
        return data


def body_lines_for(text: str, columns: int, limit: int) -> tuple:
    """Wrapped lines, plus whether the message was too long for them."""
    flat = " ".join(panel_text.clean(text).split())
    lines = panel_text.wrap(flat, columns, limit)
    fits = panel_text.wrap(flat, columns, 99)
    return lines, len(fits) > limit


def card_frames(item: Notification) -> list:
    """The card as whole frames: one if it fits, one per scroll step if not.

    Everything that does not move is redrawn identically each frame, because the
    GIF writer patches frames - the unchanged pixels cost nothing a second time.
    """
    columns = panel_text.columns(1)
    source_label = panel_text.clean(item.source)[:columns - 2]
    title_lines = panel_text.wrap(item.title, columns, MAX_TITLE_LINES)
    flat_body = " ".join(panel_text.clean(item.body).split())
    # A scroll that would not fit is cut rather than stretched: the tip's GIF
    # buffer is 32 KiB (docs/panel-limits.md) and a scrolling row costs about 97
    # bytes a frame, so this is the budget that fits with room to spare.
    if len(flat_body) > MAX_SCROLL_CHARS:
        flat_body = flat_body[:MAX_SCROLL_CHARS].rstrip() + ".."
    static_lines, overflow = body_lines_for(flat_body, columns, MAX_BODY_LINES)

    # A message that fits is a single still frame: no motion, one small upload.
    offsets = panel_text.scroll_frames(flat_body, step_px=SCROLL_STEP_PX) \
        if overflow else [0]
    offsets = offsets[:MAX_SCROLL_FRAMES]

    frames = []
    for offset in offsets:
        pixels = screen.new_frame()
        # Top row: who is speaking, and a marker that this interrupted something.
        panel_text.draw_at(pixels, source_label, 0, 0, screen.AMBER)
        panel_text.draw_at(pixels, "!", columns - 1, 0, screen.RED)
        y = 7
        for line in title_lines:
            panel_text.draw_at(pixels, line, 0, y, screen.WHITE)
            y += 6
        panel_text.fill_rect(pixels, 0, y + 1, screen.MATRIX_WIDTH, 1, screen.DIM)
        y += 3
        for line in static_lines:
            panel_text.draw_at(pixels, line, 0, y, screen.CYAN)
            y += 6
        if overflow:
            # The tail of the message runs along the bottom row. Only this row
            # changes between frames, which is what keeps a long message cheap.
            row = max(y, screen.MATRIX_HEIGHT - screen.FONT_H - 1)
            panel_text.draw_scrolled(pixels, flat_body, offset, row, screen.CYAN)
        frames.append(pixels)
    return frames or [screen.new_frame()]
