"""Prepared animations: playing a GIF the panel was told to like.

This source only *serves* bytes that `tools/gif_prep.py` already accepted, and
the reason for that split is the measured link (docs/panel-limits.md): a whole
64x54 frame costs about 2,045 bytes and the tip's buffer is 32 KiB, so an
arbitrary GIF from the internet cannot be played - it has to be fitted, reduced
to few colours and trimmed to a budget at convert time. Serving un-audited bytes
here would mean uploads that the tip simply refuses.

Files come from ``JSAUX_PIXEL_GIF_DIR`` (default ``~/.local/share/jsaux-pixel/gif``
- the ``~`` is expanded by the bus helper) and are rotated through, longest
dwell first, because an animation is decoration and decoration should not hog
the panel.
"""

from __future__ import annotations

import os
from pathlib import Path

from backend import ai_agent_screen as screen
from backend.panel_screens import PlaybackClock, Source, expand
import struct
import zlib

# An animation gets a longer turn than a data screen: it is the one thing on the
# panel that has no number to read.
DWELL_S = 12.0
# The tip's measured GIF buffer. Anything at or over this is refused at GIF
# begin, so a file that got in by other means is skipped rather than uploaded.
MAX_GIF_BYTES = 32768


def gif_dir(override: str = "") -> Path:
    base = str(override or os.environ.get("JSAUX_PIXEL_GIF_DIR", "")).strip()
    if base:
        return expand(base)
    return Path.home() / ".local/share/jsaux-pixel/gif"


def looks_like_gif(data: bytes) -> bool:
    return data[:6] in (b"GIF87a", b"GIF89a")


def turn_seconds(data: bytes) -> float:
    """Total playback time declared inside the GIF, for the playback clock."""
    total = 0.0
    index = 0
    while index < len(data):
        at = data.find(b"\x21\xF9", index)
        if at < 0 or at + 5 >= len(data):
            break
        total += struct.unpack_from("<H", data[at + 3:at + 5])[0] / 100.0
        index = at + 2
    return max(0.0, total)


class GifSource(Source):
    """The next prepared animation in the rotation."""

    name = "gif"
    priority = 20
    dwell_s = DWELL_S

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or gif_dir()
        self._files: list = []
        self._cursor = 0
        self._checked_at = 0.0
        self._turn = 0.0

    def refresh(self, now: float) -> None:
        if now - self._checked_at < 5.0:
            return
        self._checked_at = now
        try:
            self._files = sorted(path for path in self.directory.glob("*.gif")
                                  if path.is_file())
        except OSError:
            self._files = []

    def available(self, now: float) -> bool:
        return bool(self._files)

    def turn_seconds(self) -> float:
        return self._turn

    def describe(self, now: float) -> str:
        current = self._files[self._cursor % len(self._files)] if self._files else None
        return f"gif:{current.name}" if current else "gif"

    def gif(self, clock: PlaybackClock) -> bytes | None:
        if not self._files:
            return None
        path = self._files[self._cursor % len(self._files)]
        self._cursor += 1
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if not looks_like_gif(data) or len(data) >= MAX_GIF_BYTES:
            # Skip it rather than hand the tip something it will reject.
            self._turn = 0.0
            return None
        self._turn = turn_seconds(data)
        return data
