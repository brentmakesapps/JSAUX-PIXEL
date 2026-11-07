"""The AI Agent screen as a bus source (the renderer itself is unchanged).

``ai_agent_screen`` draws and animates; this is the thin wrapper that lets the
scheduler treat it like any other screen: poll the provider, and build an upload
that carries a whole turn of the activity mark starting on the cell the panel's
own loop will have reached (docs/panel-features.md).
"""

from __future__ import annotations

import time

from backend import ai_agent_screen as screen
from backend import local_ai_stats
from backend.panel_screens import PlaybackClock, Source

# How often provider numbers are worth re-reading. Under a second buys nothing:
# an upload costs five round trips.
POLL_S = 1.0
FRAMES = 16
DELAY_CS = 4


class AiAgentSource(Source):
    """Live local-provider stats, with the activity mark phase-locked."""

    name = "ai"
    priority = 60
    dwell_s = 12.0

    def __init__(self, frames: int = FRAMES, delay_cs: int = DELAY_CS) -> None:
        self.frames = max(1, int(frames))
        self.delay_cs = max(1, int(delay_cs))
        self._stats: dict = {}
        self._polled_at = 0.0

    def refresh(self, now: float) -> None:
        if now - self._polled_at < POLL_S:
            return
        self._polled_at = now
        self._stats = local_ai_stats.collect()

    def available(self, now: float) -> bool:
        # The AI screen is the panel's default view, so it is always a candidate:
        # a panel with nothing else to say shows these numbers.
        return bool(self._stats)

    def turn_seconds(self) -> float:
        return self.frames * self.delay_cs / 100.0

    def describe(self, now: float) -> str:
        display = (self._stats or {}).get("display") or {}
        return "ai:" + str(display.get("phase_kind") or "idle")

    def gif(self, clock: PlaybackClock) -> bytes | None:
        if not self._stats:
            return None
        # Where the tip's own loop will be when this upload lands, so the mark
        # resumes instead of jumping (see ai_agent_screen.playback_cells).
        turn = self.turn_seconds()
        elapsed = clock.elapsed_seconds(time.monotonic())
        start_cell = screen.playback_cells(elapsed, turn)
        return screen.render_stats_gif(self._stats, 0, delay_cs=self.delay_cs,
                                         frames=self.frames, start_cell=start_cell)
