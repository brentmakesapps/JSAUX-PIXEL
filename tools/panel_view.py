#!/usr/bin/env python3
"""Say what the panel should be showing: the timed rotation, or one view held.

The runner owns the port and asks once a second what is worth showing. This
command does not touch the port at all: it writes a small file
(``~/.config/jsaux-pixel/view.json``, or ``JSAUX_PIXEL_VIEW``) that the runner
re-reads every tick, so a view change lands within a second without restarting
the service and without a second writer on the link. That is what makes it safe
to hang a desktop widget on it.

    tools/panel_view.py                        # what it is doing now
    tools/panel_view.py lock gif                # hold the panel on one animation
    tools/panel_view.py lock gif --pick gears   # ...that animation
    tools/panel_view.py next                    # next thing inside the lock
    tools/panel_view.py auto                    # back to the timed rotation
    tools/panel_view.py --json                  # one line, for a widget to read

A lock holds the panel against the *rotation*, not against a notification: a card
still pre-empts, then the panel goes back to the view it was locked to.
"""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT, ROOT / "decky-plugin"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

from backend.ai_agent_source import AiAgentSource  # noqa: E402
from backend.calendar_source import CalendarSource  # noqa: E402
from backend.gif_screen import GifSource, gif_dir  # noqa: E402
from backend.notifications_source import NotificationsSource  # noqa: E402
from backend.panel_screens import LOCK, ROTATE, ViewLock  # noqa: E402

# The order `next` walks, and the names the panel knows. Built from the sources
# themselves so a screen cannot be listed here and forgotten there.
SCREENS = [item.name for item in (AiAgentSource(), NotificationsSource(),
                                  CalendarSource(), GifSource())]


def pick_list() -> list:
    """The animations the gif screen can be pinned to, in its order."""
    directory = gif_dir()
    try:
        return [path.stem for path in sorted(directory.glob("*.gif"))
                 if path.is_file()]
    except OSError:
        return []


def describing(lock: ViewLock) -> dict:
    """The lock, plus what each screen would say if it had the panel."""
    now = time.monotonic()
    sources = (AiAgentSource(), NotificationsSource(), CalendarSource(), GifSource())
    screens = []
    for source in sources:
        source.refresh(now)
        ready = False
        note = source.describe(now)
        try:
            ready = bool(source.available(now))
        except Exception as exc:  # a source's bad day is not this command's
            note = str(exc)
        if lock.mode == LOCK and lock.screen == source.name and lock.pick:
            # The pin is part of what the screen would say, so ask it after
            # setting it - the gif screen names the animation it is holding.
            source.set_pick(lock.pick)
            note = source.describe(now)
        screens.append({"name": source.name, "ready": ready, "describe": note,
                        "priority": source.priority, "dwell_s": source.dwell_s})
    showing = next((item["describe"] for item in screens
                    if lock.mode == LOCK and item["name"] == lock.screen),
                   "rotation")
    return {"mode": lock.mode, "screen": lock.screen, "pick": lock.pick,
            "path": str(lock.path), "showing": showing, "screens": screens,
            "picks": pick_list() if lock.screen == "gif" or not lock.screen else [],
            "error": lock.error}


def show(state: dict) -> None:
    print("view: " + ("the timed rotation" if state["mode"] != LOCK
                     else "held on " + state["screen"] +
                     (" (" + state["pick"] + ")" if state["pick"] else "")))
    print("  file " + state["path"])
    print("  would show: " + state["showing"])
    for item in state["screens"]:
        print("  %-12s priority %-3s %s"
              % (item["name"], item["priority"],
                 item["describe"] if item["ready"] else "nothing to say"))
    if state["error"]:
        print("  the file could not be read: " + state["error"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="panel_view",
                                     description="Choose what the panel shows.")
    parser.add_argument("command", nargs="?", default="status",
                         choices=("status", "lock", "auto", "next"))
    parser.add_argument("screen", nargs="?", default="",
                         help="which screen to hold: " + " / ".join(SCREENS))
    parser.add_argument("--pick", default="",
                         help="inside a locked screen: which animation "
                              "(or + for the next one)")
    parser.add_argument("--view", default="", help="the view file to write")
    parser.add_argument("--json", dest="as_json", action="store_true",
                         help="one line of JSON, for a widget")
    args = parser.parse_args(argv)

    lock = ViewLock(Path(args.view).expanduser() if args.view else None)
    lock.reload()

    if args.command == "lock":
        name = (args.screen or "").strip().lower()
        if name not in SCREENS:
            print("which screen? " + " / ".join(SCREENS), file=sys.stderr)
            return 2
        pick = args.pick
        picks = pick_list()
        if name == "gif" and pick in ("+", "+1") and picks:
            try:
                at = picks.index(lock.pick)
            except ValueError:
                at = -1
            pick = picks[(at + 1) % len(picks)]
        lock.write(LOCK, name, pick)
    elif args.command == "auto":
        lock.write(ROTATE)
    elif args.command == "next":
        picks = pick_list()
        if lock.mode == LOCK and lock.screen == "gif" and picks:
            try:
                at = picks.index(lock.pick)
            except ValueError:
                at = -1
            lock.write(LOCK, "gif", picks[(at + 1) % len(picks)])
        elif lock.mode == LOCK and lock.screen in SCREENS:
            at = SCREENS.index(lock.screen)
            lock.write(LOCK, SCREENS[(at + 1) % len(SCREENS)])
        else:
            lock.write(LOCK, "gif", picks[0] if picks else "")
    if args.as_json:
        print(json.dumps(describing(lock)))
    else:
        show(describing(lock))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
