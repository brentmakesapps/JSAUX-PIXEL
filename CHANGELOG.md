# Changelog

## Unreleased

- **The panel can be held on one view** instead of cycling, which is how a
  screen gets properly looked at. The lock is a small file
  (`~/.config/jsaux-pixel/view.json`) that the runner re-reads once a tick, so
  changing it is a write rather than a restart and never puts a second writer on
  the USB link. A lock outranks the rotation but not a notification; a lock on a
  screen with nothing to say falls back to the rotation. `pick` narrows a locked
  screen inside itself - with the gif screen held, which of the sixteen, and
  `tools/panel_view.py next` walks them.
  - `~/.config/pleamar/shells/panel-view`: a pill on the bottom bar - AI / CAL /
    GIF / NEXT / AUTO - that only ever calls that command, and reads its `--json`
    back to say what the panel is doing.

- **The panel now has more than one thing to say.** A scheduler (`backend/panel_screens.py`)
  owns the port and chooses what is showing; the AI Agent screen is one source
  among four. `tools/panel_screen.py` is the runner (and what the service runs):
  it ticks once a second and uploads only when the answer is new bytes, because
  the tip loops what it holds.
  - **Notifications** pre-empt everything: `tools/panel_notify.py "title" "body"`
    writes into a spool directory, so anything that can write a file can put a
    card on the panel. A message that fits is one still frame; a long one scrolls
    one row, and the scroll is trimmed to an 8 KB upload rather than held.
  - **Calendar** shows the next event from local `.ics` files with a countdown
    (`in 39m`, `in 3d`) - no account client, so the panel host keeps no
    credentials. Daily/weekly repeats are expanded; monthly ones are documented
    as not expanded rather than guessed at.
  - **Prepared animations**: `tools/gif_prep.py` fits a GIF to what the panel can
    actually play (64x54, small palette, 4cs floor, frames trimmed to a budget)
    and prints what it dropped, because the tip refuses a blob over 32 KiB and a
    whole frame costs 2,045 bytes.
  - `tools/panel_screen.py --list` and `--only NAME --gif out.gif` show what each
    source can say and write the upload out without touching hardware.
  - Design, decisions and the debug paths: `docs/panel-features.md`.

- AI Agent screen now shows **your own local provider** instead of Codex/OpenAI,
  read straight from Strata itself: the engine monitor (`GET /metrics`,
  `GET /slots`) plus the gateway (`/health`, `/v1/models`). Shows the loaded
  model, how much of the prompt the running request has read, how far into its
  answer it is, the engine's phase (`READ` / `THINK` / `GEN` / `IDLE`), token
  counts, tokens/sec and a VRAM meter. opencode's read-only session database is
  now only the fallback for when the monitor is unreachable; point the monitor
  somewhere else with `JSAUX_STRATA_MONITOR_URL`.
- Two render modes, switchable with `JSAUX_AI_AGENT_RENDER`: `daemon` (the
  companion draws it) or `direct` (this repo uploads its own frames over USB).
- `tools/ai_agent_screen.py` previews the screen as PNG/GIF or pushes it to the
  panel; `tools/agent_stats_server.py` serves agent stats to a different host.
- Panel layout: the two bars now mean two different things — bar 1 is the
  **current mode** (blue while reading the prompt, green while writing the
  answer, empty at rest so it visibly resets), bar 2 is **context fill** and
  stays put. The token counters are labelled `IN` / `OUT`, and the `STRATA`
  word itself is the status light (solid green when the provider is answering,
  dark when it is not) instead of an unlabeled dot, and activity is a square
  outline whose bright run of cells walks round it in the current mode's colour.
- The activity mark now **spins instead of jumping**: an upload carries 16
  frames that move it one cell each (16 steps a second), where it used to move
  four cells in four frames — one blink per side. That is only affordable
  because the GIF writer now sends each frame after the first as **only the
  rectangle that changed** (disposal method 1): the same 16-frame turn went from
  32,777 bytes to 2,499, which also matters because the tip answers "rejected" to
  a GIF begin over roughly 32KB (measured: 24,597 accepted, 32,777 rejected).
  Measured on the link: 0.53s for the patched turn, 1.28s for a full-frame one.
  `backend/gif_encoder.py` takes `difference=False` for the old whole-frame
  output, and `tests/test_gif_encoder.py` composites the patched stream back to
  the original frames.
- `JSAUX_AI_AGENT_RENDER` now defaults to **`direct`**: the plugin draws the AI
  Agent screen and uploads it itself, so the panel works with the opensource tip
  alone. `daemon` still hands the screen to the closed companion. In direct mode
  the plugin sends a power frame before its first upload, so a panel that has
  fallen asleep wakes for it.
- **Stats over the network.** When the faceplate is on the Steam Machine and the
  agent on another box, `tools/agent_stats_server.py` now serves the **whole
  snapshot** (`collect()`'s own output) instead of only the opencode view, and
  the collector takes that snapshot whole when a bridge is configured — one
  request, and the faceplate host needs no Strata key and no agent database.
  The bridge can require a shared secret (`--token`, `JSAUX_AGENT_STATS_TOKEN`,
  or `~/.config/opencode/jsaux-stats-token`) and answers `401` without it;
  `BRIDGE_URL` in `backend/local_ai_stats.py` is the no-environment default for
  the Deck side. A bridge that is down still falls back to the gateway and the
  local database.
- **The link's limits are now measured, in the repo.** `tools/panel_probe.py`
  runs the measurements that the design quotes (chunk size, blob ceiling, cost
  per frame, rotation cost, playback rate) with a time box per test so a rejected
  blob cannot hang a run, and it refuses to measure unless it holds the port
  lock. Results in [`docs/panel-limits.md`](docs/panel-limits.md). What changed
  as a result: the tip's real ceiling is a **32 KiB** GIF buffer; **498 is the
  device's chunk size** (996 and up are rejected - an earlier reading of
  "16000 accepted" was the tip answering `GIF begin` for a blob it already had);
  a chunk costs **73ms** so upload time is `ceil(bytes/498) × 73ms`; and the tip
  plays **about 30 fps and no faster**, so `JSAUX_AI_AGENT_FRAME_DELAY_CS` now
  defaults to 4 (25 steps a second) instead of 6 - 2cs measured 1.46x slow.
- **The panel can look at itself.** `tools/panel_calib.py` lights the matrix
  border and finds it by diffing against a dark frame, writing
  `~/.config/opencode/jsaux-panel-calib.json`; `tools/panel_cam.py` grabs, films  and measures through that calibration in **LED coordinates**. The probe uses it
  to time playback by filming the activity mark, which is how the frame-rate
  floor above was measured instead of guessed.
- Two processes driving the panel at once — how a leftover `--push --live` run
  ends up fighting a new one, interleaving frames and leaving the tip deaf — is
  now refused: `UsbSerialTransport` takes an advisory lock on the port and the
  second opener is told which pid already holds it.
- The turn is now **phase-locked to the panel's own loop**. Each upload carries
  exactly one turn, so the loop keeps step with a clock, and the next upload is
  built to start on the cell that clock will have reached **when the upload
  lands** — including the ~0.5s it spends on the wire. Before that, every upload
  began at cell 0, so a frame arriving 1.5 turns in threw the mark sideways: the
  "a turn and a half, then a jump" the first smooth build showed.
  `ai_agent_screen.playback_cells()` / `upload_seconds()` carry that clock, and
  the screen takes a `start_cell`.
- Direct mode: each upload now carries a **whole turn** of the activity mark
  (`JSAUX_AI_AGENT_FRAMES` × `JSAUX_AI_AGENT_FRAME_DELAY_CS`, 4 × 25cs = 1s), so
  the panel looping what it holds is invisible and the mark can never rewind in
  front of the next upload — the jump-per-second the previous shape produced.
  The turn's length is the spinner speed, and one upload keeps animating until
  the numbers change, so uploads are paced by `JSAUX_AI_AGENT_INTERVAL` rather
  than by the animation. Measured on the link: ~2 uploads/s for a single-frame
  GIF and ~0.9/s for a three-frame one, so no animation can ride on the upload
  rate. `JSAUX_AI_AGENT_SPINNER_STEP` overrides cells-per-frame directly.
- A panel left holding a half-finished upload (what killing a live run does)
  answered nothing at all afterwards — every push timed out, even a power frame.
  `UsbSerialTransport.recover_link()` closes the dangling session and
  power-cycles the screen, and both the plugin loop and `tools/ai_agent_screen.py`
  now call it: the tool probes with a power frame when it connects and recovers
  on the first failed push. Measured 30/30 uploads landing after a recovery,
  0/10 before. `--for SECONDS` stops a live run cleanly so it cannot do that
  damage itself.
- `JSAUX_AI_AGENT_PROVIDER=codex` keeps the previous Codex session scanner.
- New tests: `tests/test_local_ai_stats.py` (incl. the monitor's reading /
  generating / at-rest cases), `tests/test_ai_agent_screen.py`,
  `tests/test_gif_encoder.py`.

- Fixed the GIF writer: `_gif_from_indexed` emitted a stream strict decoders
  reject (the panel showed a single lit row). `backend/gif_encoder.py` now writes
  a spec-clean GIF (literal-only LZW), and `matrix_api` delegates to it, so the
  countdown and `matrix_push` frames are valid too. `tests/test_gif_encoder.py`
  round-trips the stream.
- Fixed GIF finalize failures on real hardware: the reply allowance for the
  finalize reply was too tight, so it timed out, the transport retried the
  session, the device answered "same content", and the retry ended in
  `device rejected GIF finalize`. Uploads of the same content are also skipped
  now, since that path cannot succeed, and a begin that answers "I already hold
  this content" (`0x02`) makes the matching finalize refusal harmless — the
  frame is already on the panel. That was the failure on the first push after
  every restart.

## 1.2.0 — 2026-10-23 (first public release)

- Open host USB connection protocol + optimized codec / serial transport + `matrix_cli`, LED matrix API, and Decky plugin sources.
- Hardware calling path in the tip is the optimized open transport/CLI only (not the closed companion serial stack).
- Official companion app (`jsaux-matrix-daemon` / Studio) remains closed; install from the official JSAUX PIXEL release.
- AI Agent shows local Codex session name + running state only (no OAuth / usage APIs).
- README basics: Steam Machine product, V1.2 line, official install guide, feedback and security contacts.
