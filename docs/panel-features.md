# Content on the panel: design, decisions, and how to debug it

What the panel shows beyond the AI Agent screen - notifications, calendar,
prepared animations - and the rules that keep it smooth on a link that costs
73ms a round trip. Read [`panel-limits.md`](panel-limits.md) first: every number
quoted here comes from `tools/panel_probe.py`.

## The shape

One process owns the port (the flock makes that true), so the panel's content is
a **scheduler's** job, not a set of independent writers:

```
sources                  bus (one loop)                link
ai_agent   ---\         pick screen -> render         one upload per change,
notifications --+--> PanelBus.tick()  -------------->  whole-turn / self-looping
calendar ------/        skip identical bytes          GIF, tip loops it
prepared GIFs -/
```

* `decky-plugin/backend/panel_screens.py` - the bus: `PanelBus`, the `Source`
  base, priority pre-empt plus rotation, and the upload cache.
* `decky-plugin/backend/notifications_source.py` - pre-empting cards from a
  spool directory.
* `decky-plugin/backend/calendar_source.py` - next event from local `.ics`.
* `decky-plugin/backend/gif_screen.py` - plays a GIF that `tools/gif_prep.py`
  already approved.
* `tools/panel_screen.py` - the runner that hosts the bus and drives the
  transport. This is what `jsaux-pixel-panel.service` runs.
* `tools/panel_notify.py` - how anything on this box posts to the panel.
* `tools/gif_prep.py` - the budget linter for animations.

The AI Agent screen stays what it was (`backend/ai_agent_screen.py`); the bus
just wraps it as one source.

## Decisions, and why

| decision | why |
|---|---|
| Motion lives **inside one upload**, not in a stream of uploads | 73ms a chunk and a 32 KiB ceiling: a 2.5 KB self-looping turn costs one upload and plays forever. Verified: the tip is still looping **122 s** after a single upload |
| An upload carries a **whole number of turns** | the loop is then byte-identical, so when the replacement lands the mark is where the clock says it is. Phase comes from `playback_cells` plus measured `upload_seconds` |
| Screens **pre-empt** (priority) and otherwise **rotate** | notifications want to interrupt; calendar and animations want a turn. Rotation floor is 1.1 s measured, so dwell times are seconds, not frames |
| A notification is a **static card with one scrolling line** | a smaller GIF canvas does not patch - it clears the screen - so we pay one full frame either way; the scroll costs ~97 bytes a frame, so a long message fits |
| Calendar reads **local `.ics` files**, no network client | keeps the panel host free of credentials, matches the bridge split, and a 60 s poll is enough for a countdown |
| Animations are **pre-converted and budget-checked** | a whole 64x54 frame costs 2,045 bytes, so "any GIF" is not a runtime property. `gif_prep` decides at convert time and reports what it did |
| Brightness and firmware animations are **not used** | both are unanswered or rejected on this tip (see the limits table) |
| The runner is one program, so it runs the same way here and on the Deck | when the panel moves, the Deck runs `panel_screen.py` (or the plugin) pointed at the bridge; nothing about the content changes |

## What one upload costs, as built

Live numbers from the runner on this box (a `notify` card pre-empting a
rotation):

| screen | upload | why |
|---|---|---|
| `ai` | 2,447 B (~0.5 s) | 16 frames of 4cs, one whole turn of the mark |
| `calendar` | 2,102 B | one still frame - the countdown changes a minute at a time |
| `gif` | up to ~14 KB | frames `gif_prep` kept under its 24 KB budget |
| `notify` | capped at 8 KB | the scroll is trimmed rather than held: 8 KB is ~1.2 s of wire, and a 22 KB card once held the panel off the next screen for three seconds |

That cap is the one place the design trades content for latency, and it is the
right trade on a link where every byte is a round trip.

## Where things live

| what | path / env |
|---|---|
| notification spool | `~/.local/share/jsaux-pixel/notify/` · `JSAUX_PIXEL_NOTIFY_DIR` |
| calendars | `~/.config/jsaux-pixel/calendar.ics` (or every `.ics` in that dir) · `JSAUX_PIXEL_CALENDAR` |
| approved animations | `~/.local/share/jsaux-pixel/gif/` · `JSAUX_PIXEL_GIF_DIR` |
| the panel runner's state | nothing on disk: the tip holds the last upload |

## How to see it working

```bash
systemctl --user status jsaux-pixel-panel       # the bus, logging every switch
journalctl --user -f -u jsaux-pixel-panel
python3 tools/panel_notify.py "Build failed" "jsaux-pixel: 3 tests failed"
python3 tools/panel_screen.py --once --only calendar --png /tmp/cal.png   # look, no hardware
python3 tools/gif_prep.py big.gif              # convert + budget report
```

Every source can be rendered to a PNG with no hardware at all, which is the
fastest way to find out whether a layout problem is the renderer or the link.

## What to check when it misbehaves

1. **Nothing changes**: is the runner up (`systemctl --user status
   jsaux-pixel-panel`), and is a source actually `available` (no calendar file,
   empty spool, and no engine activity all mean "nothing to say")?
2. **One screen never appears**: rotation is by priority and dwell, so a
   high-priority source that never expires starves the rest. The log prints the
   chosen source and why.
3. **The panel freezes mid-animation**: check for a second writer in the log (the
   lock names the pid), then `UsbSerialTransport.recover_link()` - the runner
   calls it after one failed push.
4. **A GIF looks choppy or was cut**: `gif_prep` prints the budget it applied and
   what it dropped (frames, colours, changed area). Raising the budget is a flag,
   not a code change.
5. **Layout wrong**: `--png` a frame and look at it; the camera path
   (`tools/panel_cam.py grab`) is there to compare what was drawn with what the
   panel shows.

## Deliberately not done yet

* Deck-side install of the bus (only when the panel moves - see the note about
  `BRIDGE_URL` and the token).
* A D-Bus listener for system notifications: the spool is the seam, and a
  listener is a small program that writes into it.
* Timezone-aware calendar rules beyond UTC offsets and simple daily/weekly
  repeats - documented in the parser.
* Crossfades and anything that repaints the whole screen per frame: 2,045 bytes a
  frame is the measured price, and it does not fit the ceiling.
