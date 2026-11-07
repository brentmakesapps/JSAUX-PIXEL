# What the panel link can actually do

Measured on this machine with `tools/panel_probe.py` (and `tools/panel_cam.py`
for the things only a camera can see). Re-run the probe after any change to the
cable, hub, chunk size or firmware - these numbers move.

Run it with the panel service paused, which the probe does for you and undoes on
the way out:

```bash
sg dialout -c '.venv/bin/python -u tools/panel_probe.py all --report limits.json'
```

## The numbers

| | measured | what it means |
|---|---|---|
| chunk size | **498 only.** 996, 2000, 4000, 8000, 16000 were each rejected (3 of 3 attempts) | the reference chunk size is the device's limit, not a convention. There is no speedup available by sending bigger chunks |
| per-chunk cost | **73 ms** (12,327 bytes = 25 chunks in 1.826 s) | upload time is `ceil(bytes/498) x 73ms`. The bytes themselves are ~4 ms of a chunk - we pay for round trips, not bandwidth |
| duplicate blob | **0.138 s** for the same 12,327 bytes | the tip checks the CRC32 in `GIF begin` and answers "same content, skip the rest" - so a repeated upload measures nothing (and its finalize is refused, which is why the code skips identical frames in software) |
| biggest blob | **30,732 accepted, 32,777 rejected** | the tip's GIF buffer is **32 KiB**. Anything over that is refused at `GIF begin`, before a single chunk goes out |
| frame delay | delay 10, 6, 4 cs → turn period **exactly** as asked (ratio 1.00 / 0.97 / 1.04). 2 cs came out **1.46x slow** | the tip plays about **30 fps and no faster** - it floors the delay around 3 cs. Asking for 100 fps buys nothing |
| frames per upload | 32, 64, 128 and **256** frames all played the same 0.933 s turn | frame count costs bytes, not playback speed. A long self-contained loop is cheap |
| screen swap | **0.527 s** median for a ~3 KB frame | a rotation of screens wants at least ~1.1 s each |
| data change to panel | **0.464 s** | live numbers reach the panel in under half a second |

## Behaviour the design assumes, now tested

| assumption | result |
|---|---|
| the panel keeps looping the last upload | **yes** - filmed still turning at 2, 22, 62 and **122 s** after a single upload. No keep-alive uploads needed, which is what lets a quiet screen cost one upload |
| a smaller GIF canvas patches its corner | **no** - a 16x16 upload cleared the rest of the screen (a lit far side went 65,021 -> 5,804). Every upload replaces the whole display, so there is no cheap partial-screen update: a notification costs a full frame plus its own changes |
| the panel sleeps quickly | it did not - after **120 s** untouched it still answered a power frame, and the loop test could still see it lit at 122 s |
| built-in firmware animations are available | **no** - ids 0, 1, 2, 3, 5, 8, 16 and 20 all answered `0x00` (rejected). An attract mode has to be uploaded like everything else |
| `set_brightness` works | **it gets no reply at all** on this tip. Don't put it in a frame sequence: an unanswered command costs the retry ladder (seconds), not the milliseconds it looks like. `matrix_api.push_payload` still lists a brightness frame - treat that as decoration, not something to send |

One caveat about the loop measurements: over a short film the period can read as a *multiple* of the real turn (the loop test's films read 1.8 s for a 0.64 s turn). Use "is it still moving" from a short film and "how fast" only from the framerate test, which films long enough.

## What a frame costs, by how much of it moves

`gif_encoder` patches frames after the first, so this is the real cost curve:

| motion | bytes a frame |
|---|---|
| 4-cell run on the activity ring | 26 |
| one 64-pixel ticker row | 97 |
| 16x16 block | 223 |
| 42x42 block | 1,185 |
| every pixel (noise) | 2,045 |

So the budget is set by **how much of the picture changes**, not by how many
frames there are. Under the 32 KiB ceiling that means:

| a single upload can carry | at 30 fps |
|---|---|
| ~110 frames of a moving sprite | 3.7 s of animation |
| ~150 frames of a scrolling row | 5 s of ticker |
| ~15 frames of whole-screen change | 0.5 s, the only "video-ish" case, and it is the one case that does not fit |

## The two rules that fall out of this

1. **Never animate by uploading.** Upload once, carrying a self-contained loop,
   and let the panel loop it. At 30 fps a loop can run for seconds and cost one
   upload; uploading per frame costs 73 ms a chunk and tops out at about two
   whole frames a second.
2. **Move as little of the picture as possible.** A spinner, a ticker, a progress
   bar or a blinking icon is tens to hundreds of bytes a frame. A screen-wide
   crossfade is 2 KB a frame - the same animation, eighty times the price.

## What the camera adds

`tools/panel_calib.py` lights the matrix border, diffs it against a dark frame
and records where the panel is in the camera frame (`~/.config/opencode/jsaux-panel-calib.json`),
so measurements are addressed in **LED coordinates** (`--led 58,0,2,2`) instead of
hand-counted pixels. `panel_probe.py` then measures playback by filming the
activity mark and reading the period of its travel - which is how the frame-rate
floor above was found. The camera runs at 30 fps, so it cannot resolve a period
shorter than about 0.4 s; the probe reports `resolvable: false` rather than
guessing when asked to.
