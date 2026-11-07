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
* `tools/panel_view.py` - say what the panel should be showing: the
  timed rotation, or one view held.

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
| Animations are **generated here, then budget-checked** | one whole-panel frame costs 2,045 bytes literal, 1,603 compressed, so "any GIF" is not a runtime property - the files in `media/gifs` only fit as 5-39 frame flickers. `tools/panel_anim.py` redraws the ones that are geometry; `tools/gif_prep.py` lints anything else and says what it did |
| Brightness and firmware animations are **not used** | both are unanswered or rejected on this tip (see the limits table) |
| The runner is one program, so it runs the same way here and on the Deck | when the panel moves, the Deck runs `panel_screen.py` (or the plugin) pointed at the bridge; nothing about the content changes |
| The view is held by **a file the runner re-reads**, not a flag or a restart | the thing that changes the view (a test, a desktop widget) is a different process from the one that owns the port, and a second writer on that link is exactly what the rest of the design avoids; a write lands inside a second |

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
| the view - rotate, or hold one screen | `~/.config/jsaux-pixel/view.json` · `JSAUX_PIXEL_VIEW` |
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

## Holding the panel on one view

The rotation is what makes four screens work on one small panel. Checking a
screen is the other case: watching one for 12 s and then losing it for eight
minutes makes a change hard to see. So the panel can be **held** on one screen,
and on one thing *inside* that screen.

The lock is a **file, not a restart**. `~/.config/jsaux-pixel/view.json`
(`JSAUX_PIXEL_VIEW` moves it) holds

    {"mode": "lock", "screen": "gif", "pick": "cube-field"}

and the runner re-reads it once a tick, on its modification time. That is the
whole reason it is a file rather than a flag or a second process: whatever
changes the view - a test, a shell, a hand at a keyboard - is a different process
from the one that owns the port, and two writers on that link is precisely the
mistake the rest of this design exists to avoid. A change lands within a second
and needs no restart.

Precedence: **a lock outranks the rotation, but not a notification**. Holding the
panel on an animation is a preference about what to decorate, not a reason to
miss something arriving; the card pre-empts and the held view comes back after
it. A lock on a screen that has nothing to say - the AI card with no agent
running - falls back to the rotation instead of showing a blank.

`pick` is the view's second degree. With the gif screen held it says *which* of
the sixteen, and while anything is pinned the gif cursor stops advancing: the
bus calls `gif()` every tick, and unpinned it would move on every second.
`set_pick` is a method on every source and a no-op on the base class, which is
why the calendar and the AI card simply ignore it. An unknown name is ignored
rather than jumped to, so a typo leaves the panel where it was instead of
showing nothing.

`tools/panel_view.py` is the door, and it never opens the port:

    tools/panel_view.py                          # what it is doing now
    tools/panel_view.py lock gif --pick gears     # that animation, and only it
    tools/panel_view.py next                      # next thing inside the lock
    tools/panel_view.py auto                      # back to the timed rotation
    tools/panel_view.py --json                    # one line, for something else to read

`~/.config/pleamar/shells/panel-view` is the pill on the bottom bar: **AI / CAL /
GIF / NEXT / AUTO**, with a dot for the state - leaf cycling, accent a held
screen, sand a held animation. NEXT is the cycling button, and it walks what is
inside wherever it is: the sixteen with an animation held, the screens with a
screen held, and from the rotation it starts holding the first animation.
It is a thin client on purpose: every press calls that same command, and the
read-back of its `--json` every 5 s is all the pill shows, so the command's
validation and its atomic write stay the only authority. That is the rule the
darkflash card follows for the case display, and the reason the pill can sit
open while the service runs. It belongs to the bar rather than taking a corner of
its own: darkflash holds `0..560` and strata `2864..3840`, so its surface runs
`0..2856` along the bottom and its pill ends 8 px short of strata, on the dock's
own 8 px line. A surface's empty part lets clicks through, which is what buys
that reach without covering anything.

If a click ever seems stolen from the dock or from darkflash, the answer is the
same surface: cut it back with a four-value `margin: 0, 984, 0, 1456` and move the
row's `at:` into what is left - a surface can be placed by margin instead of being
wide, but then it cannot be wider than what that leaves.

The pill is not in this repo, following darkflash: the shells belong to the
desktop, not to the project. If the panel moves to another machine, that file
pair - `shells/panel-view/panel-view.plm` and `.luau` - plus one line in
`~/.config/pleamar/autostart` is the whole of it.

Verified on the desk - and the first attempt failed in a way worth recording:
the running service was the process started *before* this code existed, so the
service has to be restarted once for the lock to mean anything. After that,
`lock gif --pick cube-field` logged `view: lock gif:cube-field` and `screen: gif
(locked to gif)`, and then 26 s of nothing at all - no rotation, no re-upload.
Two camera grabs 24 s apart showed the same animation. `next` logged `view: lock
gif:cyclone` and pushed 22,498 bytes, and `auto` put the rotation back.

## Replicating the GIFs

`media/gifs` is the vendor's own animation pack (23 files, 63 MB) and it is
**not in the repo** - `.gitignore` keeps it out, so the paths below are local
paths. It is only the measuring stick: `gif_prep --survey` fits every source
into 64x54, encodes it with the same encoder the panel plays, and
binary-searches how many frames the 24 KiB budget keeps.

```bash
.venv/bin/python tools/gif_prep.py --survey media/gifs
```

What that says for this set: nine files keep all their frames, and their loops
are 0.72 s to 1.60 s - `lady.gif` 18 frames/7,941 B/0.72 s, `gears.gif`
25/15,341/1.00 s, `hexigon-warp.gif` 23/13,557/0.92 s. A one-second loop of a
figure flashing is a flicker, not an animation. The rest get cut to fit:
`dot-wave.gif` keeps 26 frames of 45, `spinning-nested-wheels.gif` 35 of 130,
`nested-circle-bounce.gif` 35 of 269.

The `moved` column is the part that decides it. `skeletons.gif` changes 3,411 of
the panel's 3,456 cells every frame, `cyclone-2.gif` 2,053, `hexigon-warp.gif`
1,973 - a near-whole-panel repaint, which is the one thing the link cannot pay
for many frames of. Two sources do fit honestly as themselves (`dropplet-2` keeps
122 frames and 4.88 s, `spherewave` 107 frames, 4.28 s and 36 cells a frame) and
their redraws are near-equivalents of them.

So the set is **redrawn**: `backend/panel_anim.py` holds sixteen procedural
replicas, one per source that is really geometry. The rules that make a redraw
work at panel scale, each learned from a version that failed:

* **One figure, not several.** Six copies of a thing at panel scale is a field of
  noise; one gear, one pyramid, one ball.
* **A few cells of movement a step, never a whole side at once.** A cube's three
  spokes rotating reads as a turn; its three faces lighting in sequence reads as
  a blink. Same for a pyramid's faces.
* **Phase over exactly one turn of the geometry.** Then frame N joins onto frame
  0 and the loop has no seam; the test in `tests/test_panel_anim.py` measures
  that (the step from the last frame back to the first must not be bigger than a
  typical step).
* **Something has to change every frame.** A wave whose threshold crosses nothing
  for ten steps is ten frames of frozen panel; the test counts dead frames and a
  few replicas were rebuilt because of it (`sphere-wave`, `spokes`, `pivot`,
  `sheet-fan`, `hex-warp`).
* **Dots, not lines.** At 64x54 a 2px line is a smear; a dotted curve - a spirograph,
  a ball of dots - is legible and costs 8 cells a frame instead of 300.
* **4 cs frames.** 3 cs is the measured playback floor, and 4 cs times exactly.

What the set costs, and what it looks like on the panel (all of it measured by
`tools/panel_anim.py --list` and then confirmed by eye, see below):

| replica | of | frames | bytes | loop | reads as |
|---|---|---|---|---|---|
| spirograph | spinning-nested-wheels | 120 | 6,628 | 4.80s | a dotted rose tracing itself |
| sphere-wave | spherewave | 64 | 7,666 | 2.56s | a ball of dots with rings running out |
| pivot | pivot-machine | 64 | 7,206 | 2.56s | bars hinged through the centre, turning |
| cube-field | shifting-cubes | 64 | 8,416 | 2.56s | hexagons whose spokes turn: cubes flipping |
| pyramid-spin | piramid-spin | 64 | 9,247 | 2.56s | a wireframe pyramid turning over its base |
| hex-warp | hexigon-warp | 48 | 9,506 | 1.92s | nested hexagons warping |
| spokes | static-hairs | 72 | 10,511 | 2.88s | dashed rays marching out of a centre |
| blob-wave | wavy-blob | 112 | 11,150 | 4.48s | a blob outline that morphs |
| pinwheel | star | 88 | 15,148 | 3.52s | a five-point star turning |
| chevron | zig-zag--wave | 56 | 15,339 | 2.24s | a wall of zigzag marching |
| dropplet | dropplet | 88 | 18,583 | 3.52s | a cone of layers with a ripple through them |
| gears | gears | 88 | 18,946 | 3.52s | two toothed wheels, meshing |
| nested-bounce | nested-circle-bounce | 56 | 19,749 | 2.24s | rings growing out of each other |
| cyclone | cyclone-bubble | 80 | 22,498 | 3.20s | nested rings breathing |
| dot-wave | dot-wave | 52 | 23,044 | 2.08s | diagonal bands of dots |
| sheet-fan | spinning-sheets | 56 | 18,059 | 2.24s | rules fanning from an apex |

`dot-wave` and `chevron` are the two to drop first if the rotation ever feels
loud: both move continuously and both are faithful to their source, but at
panel scale a field of dots a third of a second wide reads as weather, not as a
figure. Everything else reads as the thing it is named after.

Not replicated, deliberately: the figurative sources (`lady`, `lady-2`, the
skeletons, the character loops). At 64x54 a face is a blob of changing cells,
which is the one cost case the link cannot pay - a repaint of most of the panel
is ~1,600 bytes a frame compressed, so six frames of it is a second and a half of
loop for 10 KB. There is no version of "a person" that fits; the redraws are
geometry because geometry is what fits.

### What sixteen of them does to the rotation

Dwell is per source, measured from the last upload: `gif` 12 s at priority 20,
`ai` 12 s at 60, `calendar` 8 s at 40, `notify` 15 s at 100 and pre-empting. The
journal says a pass of the three rotating sources is **32.1 s** on this box, and
`GifSource` hands out the next file in the directory each time it is visited - one
visit in six got skipped, which is why the gif gap measured 51.6 s that pass. So
with sixteen installed, a given animation is on the panel for ~14 s about once
every **eight to nine minutes**. That is the honest cost of a big set on a serial
link; the three ways to spend it differently are a smaller rotation directory, a
shorter `gif` dwell, or drawing the next file at random instead of in order. None
of them is obviously right, so the code keeps the simple one and this paragraph
records what it costs.

### How an animation gets accepted

`tools/panel_review.py` is the review. It stops `jsaux-pixel-panel.service` so
nothing else is writing to the port, uploads one GIF, films 3 s of the panel, and
writes a filmstrip of twelve frames 0.2 s apart, cropped to the matrix and
labelled with the time. Then the next one. The service is back up on the way out.

```bash
sg dialout -c '.venv/bin/python tools/panel_review.py'          # all of them
sg dialout -c '.venv/bin/python tools/panel_review.py gears'    # one of them
```

Stop-the-service is not ceremony: with the rotation running, the panel shows
whatever the rotation chose, so watching the spinner once filmed the calendar
instead. And the filmstrip, not the motion estimator, is what gets believed -
the panel's glass reflects the room, and a 30 fps camera locks onto harmonics, so
a "period" reading can be the person walking past. Every one of the sixteen in
the table above has been through this pass and reads as intended.

## Deliberately not done yet

* Deck-side install of the bus (only when the panel moves - see the note about
  `BRIDGE_URL` and the token).
* A D-Bus listener for system notifications: the spool is the seam, and a
  listener is a small program that writes into it.
* Timezone-aware calendar rules beyond UTC offsets and simple daily/weekly
  repeats - documented in the parser.
* Crossfades and anything that repaints the whole screen per frame: 2,045 bytes a
  frame is the measured price, and it does not fit the ceiling.
