"""One panel, many things to say: the scheduler that decides what is showing.

The port allows exactly one writer, so panel content is a scheduling problem -
not a set of programs taking turns to upload. This module holds that decision
(``PanelBus``) and the contract every kind of content implements (``Source``).
It draws nothing: each source renders its own frame with the same helpers the
AI Agent screen uses.

Why a scheduler instead of a stack of loops: an upload costs one round trip per
498-byte chunk (73ms each), so the panel cannot be *animated* by uploading. It is
animated by one upload that contains a whole self-contained loop, which the tip
plays until something replaces it (measured still looping 122s later - see
docs/panel-limits.md). So the bus is asked one question a second: *does what is
worth showing change?* - and mostly the answer is no, which costs nothing.

The contract:

* ``refresh(now)`` - fetch what this source wants to say. Must be cheap and
  tolerate being offline; the bus calls it every tick and never blocks on it.
* ``available(now)`` - does it have something to show right now.
* ``urgent(now)`` - should it pre-empt whatever is showing (a notification).
* ``gif(clock)`` - the upload to show, built to be self-sufficient: a whole
  number of turns for anything that spins, so the tip's loop is invisible.

Debugging: ``tick()`` always says which source it chose and why, and
``render_one()`` renders a single source to a PNG with no hardware involved.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable

from backend import ai_agent_screen


def expand(value: str) -> Path:
    """``~`` to a real path, without leaning on pathlib's helper name.

    Every configurable directory in the panel stack goes through here so the
    tilde convention is one behaviour, not four slightly different ones.
    """
    text = os.path.expandvars(str(value or "").strip())
    if text.startswith("~"):
        text = str(Path.home()) + text[1:]
    return Path(text)

# How long a screen holds the panel when nothing is pre-empting. The measured
# cost of a swap is 0.53s, so this is a comfort question, not a link one: under
# about 4s a panel becomes hard to read.
DEFAULT_DWELL_S = 10.0
# Guards against a source that says "available" but has nothing to draw.
MAX_EMPTY_STRIKES = 3
# Where the operator's say about what is showing lives: a small file, because
# the thing that changes it (a widget, a shell, a test) is a different process
# from the one that owns the port.
DEFAULT_VIEW_FILE = "~/.config/jsaux-pixel/view.json"
VIEW_FILE_ENV = "JSAUX_PIXEL_VIEW"
ROTATE = "rotate"
LOCK = "lock"


def view_file(override: str = "") -> Path:
    base = str(override or os.environ.get(VIEW_FILE_ENV, "")).strip()
    return expand(base or DEFAULT_VIEW_FILE)


class ViewLock:
    """Rotate through the screens, or hold the panel on one of them.

    This is read from a file once a tick rather than passed in on the command
    line, so changing it is a write - not a restart, and not a second process
    trying to take the port. That is what lets a desktop widget drive it: the
    runner stays the only writer and notices within a second.

    ``pick`` narrows a locked screen inside itself: with the gif screen locked,
    ``pick`` is which animation, and ``next`` walks the directory.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or view_file()
        self.mode = ROTATE
        self.screen = ""
        self.pick = ""
        self._stamp = None
        self.error = ""

    def reload(self, now: float | None = None) -> bool:
        """Re-read the file if it changed. Returns whether anything changed."""
        try:
            stamp = self.path.stat().st_mtime_ns
        except OSError:
            if self._stamp is None:
                return False
            self._stamp = None
            changed = (self.mode, self.screen, self.pick) != (ROTATE, "", "")
            self.mode, self.screen, self.pick = ROTATE, "", ""
            return changed
        if stamp == self._stamp:
            return False
        self._stamp = stamp
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError) as exc:
            self.error = str(exc)
            return False
        wanted = str(data.get("mode", ROTATE)).strip().lower()
        self.mode = LOCK if wanted == LOCK else ROTATE
        self.screen = str(data.get("screen", "")).strip().lower()
        self.pick = str(data.get("pick", "")).strip()
        self.error = ""
        return True

    def write(self, mode: str, screen: str = "", pick: str = "") -> dict:
        """Set the view, atomically, and say what it is now."""
        mode = LOCK if str(mode).strip().lower() == LOCK else ROTATE
        payload = {"mode": mode, "screen": screen.strip().lower(), "pick": pick.strip()}
        if mode == ROTATE:
            payload["screen"] = payload["pick"] = ""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".%d.tmp" % os.getpid())
        try:
            temp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            os.replace(str(temp), str(self.path))
        except OSError:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        self.reload()
        return self.state()

    def state(self) -> dict:
        return {"mode": self.mode, "screen": self.screen, "pick": self.pick,
                 "path": str(self.path), "error": self.error}

    def summary(self) -> str:
        if self.mode != LOCK:
            return ROTATE
        if not self.screen:
            return "lock (no screen named)"
        return f"lock {self.screen}" + (f":{self.pick}" if self.pick else "")

    def __repr__(self) -> str:
        return f"ViewLock({self.summary()})"


class PlaybackClock:
    """Where the tip's own loop will be when the next upload lands.

    The panel keeps replaying the last upload, so an upload that starts a new
    animation has to begin at the phase the loop has already reached, or the
    mark jumps. ``advance_for`` answers that: seconds since the upload, plus
    the time this upload will spend on the wire.
    """

    def __init__(self, upload_seconds: Callable[[int], float] | None = None) -> None:
        self._upload_seconds = upload_seconds or ai_agent_screen.upload_seconds
        self._shown_at: float | None = None
        self._turn_s = 0.0
        self._bytes = 0

    def mark_upload(self, gif_len: int, turn_s: float, now: float | None = None) -> None:
        """Record what is about to go on screen and when it will land."""
        self._shown_at = (now if now is not None else time.monotonic()) + \
            self._upload_seconds(int(gif_len))
        self._turn_s = max(0.05, float(turn_s))
        self._bytes = int(gif_len)

    def elapsed_turns(self, now: float | None = None) -> float:
        """Turns of the current animation the panel has already played."""
        return self.elapsed_seconds(now) / self._turn_s

    def elapsed_seconds(self, now: float | None = None) -> float:
        """Seconds the panel has been looping the current upload for."""
        if self._shown_at is None:
            return 0.0
        now = now if now is not None else time.monotonic()
        return max(0.0, now - self._shown_at)

    def advance_for(self, frames: int, delay_cs: int) -> float:
        """Turns an upload carries, so the next one knows where to resume."""
        turn = max(1, int(frames)) * max(1, int(delay_cs)) / 100.0
        return turn


class Source:
    """Something the panel could be showing."""

    name = "source"
    # Pre-empt order: a higher number wins a contested panel.
    priority = 50
    dwell_s = DEFAULT_DWELL_S

    def refresh(self, now: float) -> None:
        """Pull whatever this source reads. Never raise; the bus keeps going."""

    def available(self, now: float) -> bool:
        return False

    def urgent(self, now: float) -> bool:
        """True to take the panel now rather than waiting for its turn."""
        return False

    def gif(self, clock: PlaybackClock) -> bytes | None:
        return None

    def turn_seconds(self) -> float:
        """How long one loop of this source's animation is (0 for stills)."""
        return 0.0

    def set_pick(self, pick: str) -> None:
        """Hold this screen on one thing inside itself, if it has insides.

        Only the lock asks this: a panel pinned to the gif screen can be pointed
        at one animation. A screen with nothing to choose between - the calendar,
        the AI card - ignores it.
        """
        return None

    def describe(self, now: float) -> str:
        return self.name


class PanelBus:
    """Chooses what to show, and says when that means an upload."""

    def __init__(self, sources: list[Source], *, rotation: bool = True,
                  upload_seconds: Callable[[int], float] | None = None,
                  log: Callable[[str], None] | None = None) -> None:
        self.sources = list(sources)
        self.rotation = bool(rotation)
        self.clock = PlaybackClock(upload_seconds)
        self.log = log or (lambda message: None)
        self._current: Source | None = None
        self._since = 0.0
        self._last_gif: bytes | None = None
        self._strikes: dict[str, int] = {}
        # Sources that keep drawing nothing are parked for a while instead of
        # winning the choice forever: "available" and "has pixels" are not the
        # same question, and a source can pass the first and fail the second.
        self._parked_until: dict[str, float] = {}
        # The operator's say: rotate, or hold one screen. None means rotate.
        self.view: ViewLock | None = None

    def set_view(self, view: ViewLock | None) -> None:
        """Give the bus the lock to read (see ``ViewLock``)."""
        self.view = view

    def view_summary(self) -> str:
        return self.view.summary() if self.view is not None else ROTATE

    def source(self, name: str) -> Source | None:
        for item in self.sources:
            if item.name == name:
                return item
        return None

    def choose(self, now: float, candidates: list[Source]) -> tuple[Source | None, str]:
        """The screen to show, and why."""
        if self.view is not None and self.view.reload(now):
            self.log("view: " + self.view.summary())
        if not candidates:
            return None, "nothing to show"
        urgent = [item for item in candidates if self._safe_urgent(item, now)]
        if urgent:
            winner = max(urgent, key=lambda item: (item.priority, item.name))
            if self._current is not winner:
                return winner, f"pre-empted by {winner.name}"
            return winner, f"holding {winner.name}"
        # A lock outranks the rotation but not a notification: holding the panel
        # on one screen is a preference about what to decorate, not a reason to
        # miss something arriving.
        if self.view is not None and self.view.mode == LOCK and self.view.screen:
            pinned = [item for item in candidates if item.name == self.view.screen]
            if pinned:
                winner = pinned[0]
                winner.set_pick(self.view.pick)
                if self._current is not winner:
                    return winner, f"locked to {winner.name}"
                return winner, f"holding {winner.name}"
            if not self.rotation:
                return None, f"locked to {self.view.screen}, which is not showing"
        if not self.rotation:
            winner = max(candidates, key=lambda item: (item.priority, item.name))
            return winner, "highest priority"
        # Rotation: keep the current screen until its dwell expires. A pre-emption
        # does not reset the clock of the screen it interrupted.
        if self._current in candidates and (now - self._since) < self._current.dwell_s:
            return self._current, f"holding {self._current.name}"
        ordered = sorted(candidates, key=lambda item: (-item.priority, item.name))
        if self._current in ordered:
            index = ordered.index(self._current)
            winner = ordered[(index + 1) % len(ordered)]
        else:
            winner = ordered[0]
        return winner, f"rotation to {winner.name}"

    def tick(self, now: float | None = None) -> dict:
        """One scheduler step: refresh, choose, and maybe produce an upload."""
        now = time.monotonic() if now is None else now
        for item in self.sources:
            try:
                item.refresh(now)
            except Exception as exc:  # a source's bad day is not the panel's
                self.log(f"{item.name}: refresh failed: {exc}")
        candidates = [item for item in self.sources
                       if self._parked_until.get(item.name, 0.0) <= now
                       and self._safe_available(item, now)]
        chosen, reason = self.choose(now, candidates)
        if chosen is None:
            return {"gif": None, "screen": None, "reason": reason}
        if chosen is not self._current:
            self._current = chosen
            self._since = now
        gif = self._safe_gif(chosen)
        if gif is None:
            # A source that is available but has nothing to draw would otherwise
            # starve the rotation quietly.
            strikes = self._strikes.get(chosen.name, 0) + 1
            self._strikes[chosen.name] = strikes
            if strikes >= MAX_EMPTY_STRIKES:
                self._strikes[chosen.name] = 0
                self._current = None
                self._parked_until[chosen.name] = now + max(
                    5.0, float(chosen.dwell_s) * 2.0)
                reason = (f"{chosen.name} drew nothing {MAX_EMPTY_STRIKES} times, "
                          "parked for a while")
            return {"gif": None, "screen": chosen.name, "reason": reason}
        self._strikes[chosen.name] = 0
        if gif == self._last_gif:
            # The tip refuses a blob whose CRC it already has, so asking for the
            # same picture is a wasted round trip - say so and upload nothing.
            return {"gif": None, "screen": chosen.name, "reason": "unchanged"}
        self._last_gif = gif
        self.clock.mark_upload(len(gif), chosen.turn_seconds(), now)
        return {"gif": gif, "screen": chosen.name, "reason": reason,
                "bytes": len(gif)}

    # Each source is somebody else's code: one that raises must not take the
    # panel down with it.
    def _safe_available(self, item: Source, now: float) -> bool:
        try:
            return bool(item.available(now))
        except Exception as exc:
            self.log(f"{item.name}: available failed: {exc}")
            return False

    def _safe_urgent(self, item: Source, now: float) -> bool:
        try:
            return bool(item.urgent(now))
        except Exception as exc:
            self.log(f"{item.name}: urgent failed: {exc}")
            return False

    def _safe_gif(self, item: Source) -> bytes | None:
        try:
            return item.gif(self.clock)
        except Exception as exc:
            self.log(f"{item.name}: render failed: {exc}")
            return None
