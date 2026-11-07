"""What is on next: the panel's calendar screen.

Reads local ``.ics`` files and shows the next event with a countdown. There is
no account client here on purpose: the panel host stays free of credentials, the
same way the stats bridge keeps the model key off it. Sync the calendar to a
file somewhere else (vdirsync, khal, a cron curl) and this reads it.

The parsing subset, stated honestly (see docs/panel-features.md):

* ``DTSTART``/``DTEND`` as DATE or DATE-TIME, read **as written**: a trailing
  ``Z`` is accepted, a ``TZID=`` parameter is tolerated and ignored. Right for a
  local-time export, wrong for one made in another zone.
* ``DURATION`` as an ISO-8601 duration (``PT30M``, ``P1DT2H``).
* ``RRULE`` with ``FREQ=DAILY`` or ``WEEKLY``, plus ``UNT``. Monthly and yearly
  rules are recognised and then left alone - only the first occurrence shows -
  rather than guessed at.
* ``SUMMARY`` with the usual backslash escapes.

Files come from ``JSAUX_PIXEL_CALENDAR`` (one file) or every ``*.ics`` in
``JSAUX_PIXEL_CALENDAR_DIR`` (default ``~/.config/jsaux-pixel``).
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from backend import ai_agent_screen as screen
from backend import panel_text
from backend.panel_screens import PlaybackClock, Source, expand

HORIZON_DAYS = 14
# The countdown only needs a minute of accuracy, and finer costs a round trip.
REFRESH_S = 30.0


def config_files() -> list:
    """The ``.ics`` files the panel should read, from the environment."""
    single = os.environ.get("JSAUX_PIXEL_CALENDAR", "").strip()
    if single:
        path = expand(single)
        return [path] if path.is_file() else []
    base = os.environ.get("JSAUX_PIXEL_CALENDAR_DIR", "").strip()
    for directory in ([expand(base)] if base
                      else [Path.home() / ".config/jsaux-pixel", Path.home() / ".config"]):
        found = [path for path in sorted(directory.glob("*.ics")) if path.is_file()]
        if found:
            return found
    return []


def unfold(text: str) -> list:
    """Logical lines, undoing the spec's 75-octet folding."""
    lines = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def parse_time(value):
    """DATE or DATE-TIME as written - no zone conversion (see the note above)."""
    match = re.match(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2}))?",
                      str(value or "").strip())
    if not match:
        return None
    year, month, day, hour, minute, second = match.groups()
    try:
        return datetime(int(year), int(month), int(day), int(hour or 0),
                         int(minute or 0), int(second or 0))
    except ValueError:
        return None


def parse_duration(value) -> timedelta:
    """The ISO-8601 durations calendars actually emit."""
    match = re.match(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$",
                      str(value or "").strip())
    if not match:
        return timedelta(0)
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def unescape(value) -> str:
    text = str(value or "")
    for token, plain in (("\\n", " "), ("\\,", ","), ("\\;", ";"), ("\\\\", "")):
        text = text.replace(token, plain)
    return text


class Event:
    """One occurrence of one event."""

    def __init__(self, *, summary: str, start, end, rrule: str = "",
                  all_day: bool = False) -> None:
        self.summary = summary
        self.start = start
        self.end = end
        self.rrule = rrule
        self.all_day = all_day

    def same_day(self, moment) -> bool:
        return self.start.date() == moment.date()

    def __repr__(self) -> str:
        return f"<{self.start} {self.summary}>"


def _build_event(fields: dict):
    start = parse_time(fields.get("DTSTART", ""))
    if start is None:
        return None
    end = parse_time(fields.get("DTEND", ""))
    if end is None and fields.get("DURATION"):
        end = start + parse_duration(fields["DURATION"])
    if end is None:
        end = start
    return Event(summary=unescape(fields.get("SUMMARY", "(no title)")),
                  start=start, end=end, rrule=fields.get("RRULE", ""),
                  all_day=len(str(fields.get("DTSTART", "")).strip()) == 8)


def parse_events(text: str) -> list:
    """``VEVENT`` blocks into Events; anything without a start is skipped."""
    events = []
    fields = None
    for line in unfold(text):
        upper = line.upper()
        if upper.startswith("BEGIN:VEVENT"):
            fields = {}
            continue
        if upper.startswith("END:VEVENT"):
            if fields:
                event = _build_event(fields)
                if event is not None:
                    events.append(event)
            fields = None
            continue
        if fields is None or ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields.setdefault(key.split(";", 1)[0].upper(), value)
    return events


def _repeat_step(rule: str):
    """Daily and weekly repeats only; anything else is left alone."""
    match = re.search(r"FREQ=(DAILY|WEEKLY)", str(rule or "").upper())
    if not match:
        return None
    return timedelta(days=1) if match.group(1) == "DAILY" else timedelta(days=7)


def _repeat_until(rule: str):
    match = re.search(r"UNT=(\d{8}T?\d{6})", str(rule or "").upper())
    return parse_time(match.group(1)) if match else None


def upcoming(events: list, now, limit: int = 8) -> list:
    """The next occurrences from ``now``, repeating rules expanded in place."""
    horizon = now + timedelta(days=HORIZON_DAYS)
    found = []
    for event in events:
        step = _repeat_step(event.rrule)
        until = _repeat_until(event.rrule)
        if step is None:
            if event.end >= now and event.start <= horizon:
                found.append(event)
            continue
        start = event.start
        guard = 0
        while start < now and start <= horizon and (until is None or start <= until):
            start += step
            guard += 1
            if guard > 400:  # an endless rule still has to stop somewhere
                break
        if start > horizon or (until is not None and start > until):
            continue
        found.append(Event(summary=event.summary, start=start,
                            end=event.end + (start - event.start),
                            rrule=event.rrule, all_day=event.all_day))
    found.sort(key=lambda item: item.start)
    return found[:limit]


def countdown(now, then) -> str:
    """``IN 25M``, ``IN 19H``, ``TMR`` - the one unit that matters.

    Six characters or fewer, because the card draws this at scale 2 where the
    panel fits six: ``in 19h51`` ran off the right-hand edge. Precision past an
    hour is noise anyway - nobody plans on 51 minutes.
    """
    delta = then - now
    minutes = int(delta.total_seconds() // 60)
    if minutes < 0:
        return "NOW"
    if minutes < 60:
        return f"IN {minutes}M"
    if delta.days <= 0:
        return f"IN {minutes // 60}H"
    if delta.days == 1:
        return "TMR"
    return f"IN {delta.days}D"


def next_event(events: list, now):
    """The first occurrence at or after ``now``."""
    for event in upcoming(events, now, 1):
        if event.start >= now:
            return event
    return None


class CalendarSource(Source):
    """The next event, when it starts, and how long until then."""

    name = "calendar"
    priority = 40
    dwell_s = 8.0

    def __init__(self, paths: list | None = None) -> None:
        self.paths = paths
        self._events: list = []
        self._checked_at = 0.0

    def refresh(self, now: float) -> None:
        if now - self._checked_at < REFRESH_S:
            return
        self._checked_at = now
        events = []
        for path in (self.paths if self.paths is not None else config_files()):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            events.extend(parse_events(text))
        self._events = events

    def available(self, now: float) -> bool:
        return next_event(self._events, datetime.now()) is not None

    def describe(self, now: float) -> str:
        event = next_event(self._events, datetime.now())
        return f"calendar:{event.summary[:12]}" if event else "calendar"

    def gif(self, clock: PlaybackClock) -> bytes | None:
        now = datetime.now()
        event = next_event(self._events, now)
        if event is None:
            return None
        columns = panel_text.columns(1)
        pixels = screen.new_frame()
        panel_text.draw_at(pixels, "NEXT", 0, 0, screen.CYAN)
        when = "ALL DAY" if event.all_day else event.start.strftime("%H:%M")
        panel_text.draw_at(pixels, when, columns - len(when), 0, screen.DIM)
        y = 7
        for line in panel_text.wrap(event.summary, columns, 3):
            panel_text.draw_at(pixels, line, 0, y, screen.WHITE)
            y += 6
        left = countdown(now, event.start)
        # Scale 2 is six glyphs wide and that is the panel; if a countdown ever
        # grows past that it drops to scale 1 instead of running off the edge.
        big = panel_text.text_width(left, 2) <= screen.MATRIX_WIDTH
        panel_text.draw_at(pixels, left, 0, 27, screen.AMBER, scale=2 if big else 1)
        panel_text.draw_at(pixels, event.start.strftime("%a %d %b"), 0,
                           screen.MATRIX_HEIGHT - 6, screen.DIM)
        # One still frame: the countdown changes a minute at a time, which costs
        # one upload a minute - the right price for a number nobody times.
        return screen.encode_frames([pixels], delay_cs=100)
