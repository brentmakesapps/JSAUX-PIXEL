# JSAUX PIXEL (opensource)

This repository contains the open-source parts of the JSAUX PIXEL software for the
**Pixel Matrix Faceplate for Steam Machine**: the USB connection protocol docs, a
reference codec, the LED matrix API, and the Decky plugin sources.

For everyday use, install the official JSAUX PIXEL release from
[the Steam Machine faceplate app installation guide](https://jsaux.com/pages/steam-machine-faceplate-app-installation-guide).

[简体中文](./README-zh.md)

## About this repository

| | |
|--|--|
| **Product** | Pixel Matrix Faceplate for Steam Machine |
| **Corresponds to** | Official JSAUX PIXEL **V1.2** software line |
| **Official download / install** | [jsaux.com installation guide](https://jsaux.com/pages/steam-machine-faceplate-app-installation-guide) |
| **Changelog** | [`CHANGELOG.md`](./CHANGELOG.md) |
| **Feedback** | [GitHub Issues](https://github.com/JSAUX-Open-Source/JSAUX-PIXEL/issues) |
| **Security** | [`SECURITY.md`](./SECURITY.md) (private GitHub advisory) |
| **Contributing** | [`CONTRIBUTING.md`](./CONTRIBUTING.md) |

## What is open here

This tip **opens the host-side connection protocol and plugin sources**. The official
companion application (`jsaux-matrix-daemon`, Studio) remains closed and is
distributed only with the official JSAUX PIXEL release.

**Hardware calling path in this tip:** only the optimized open stack —
[`protocols/usb`](protocols/usb/) (`dy_frame` + `UsbSerialTransport`) and
[`tools/matrix_cli.py`](tools/matrix_cli.py). Do not expect the closed companion’s
internal serial implementation to be present or usable here.

| Open in this repository | Not part of this release |
|-------------------------|--------------------------|
| [USB host↔device connection protocol](docs/protocols/usb-serial-connection.md) | Firmware, manufacturing tools, and the official companion app |
| Optimized codec + serial transport [`protocols/usb/`](protocols/usb/) | Closed companion serial / daemon internals |
| CLI [`tools/matrix_cli.py`](tools/matrix_cli.py) (`pip install pyserial`) | Chip / PCBA / LED-driver silicon details |
| [LED matrix host API](docs/protocols/matrix-led-api.md) | Qt runtime redistributables for the closed installer |
| Decky plugin orchestration + checked-in UI bundle | Bundled Codex CLI / login UI |

Without the official companion, most Decky product UX will not talk to hardware.
For secondary development against the panel over USB serial, use `matrix_cli` /
`UsbSerialTransport` (see [`examples/`](examples/)).

## AI Agent (optional)

Shows **your own local provider**, not a cloud account — Strata talks about
itself directly, no agent in the loop:

| Source | Gives |
|--------|-------|
| Strata engine monitor — `GET /metrics`, `GET /slots` | the request in flight: phase, `prompt_read / prompt_total` (how much of the prompt is in), `generated / max_tokens` (how far into the answer), tokens/sec, VRAM + RAM, lifetime counters |
| Strata gateway — `GET /health`, `GET /v1/models` | loaded model, context size, model-switching state |
| opencode database (opened **read-only**, `mode=ro`) | used only when the monitor is unreachable: session title, per-request tokens |

Two bars, no guessing:

| Element | Means |
|---------|-------|
| `STRATA` (top-left word) | provider is answering: **solid green** with data, **dark** when nothing is answering |
| square outline (top-right) | the activity mark: a bright run of cells walks round it in the current mode's colour (blue reading, green generating); parked and dark at rest |
| `VR` + meter | VRAM used / total |
| bar 1, `READ` in **blue** | how much of the prompt is ingested so far |
| bar 1, `GEN` in **green** | how far into the answer it is (`generated / max_tokens`) — the two modes share this bar, so it is empty at rest |
| `CTX` + bar 2 (**cyan**) | how much of the context window is loaded, whatever the mode |
| `IN` / `OUT` numbers | tokens read into the context, tokens produced by the current (or last, for 2 min) answer |
| `T/S`, `READ/THINK/GEN/IDLE` | tokens per second, and the engine's own phase |

No OAuth, no `auth.json` reads, no cloud usage APIs; the Strata key is only ever
sent as a request header.

| Environment variable | Default | Meaning |
|----------------------|---------|---------|
| `JSAUX_AI_AGENT_PROVIDER` | `strata` | `strata` (local stats) · `codex` (the previous session scanner) · `off` |
| `JSAUX_AI_AGENT_RENDER` | `daemon` | `daemon` = the closed companion draws the screen · `direct` = this repo uploads its own frames over USB |
| `JSAUX_STRATA_URL` / `JSAUX_STRATA_API_KEY` / `JSAUX_STRATA_API_KEY_FILE` | read from `~/.config/opencode/` | where the gateway is and which key to send |
| `JSAUX_STRATA_MONITOR_URL` | gateway host, port `8080` | where the engine monitor is; set to an empty string to skip it |
| `JSAUX_OPENCODE_DB` | `~/.local/share/opencode/opencode.db` | the agent database (fallback source) |
| `JSAUX_AGENT_STATS_URL` | unset | read agent stats from another host (see below) |
| `JSAUX_AI_AGENT_PORT` / `JSAUX_AI_AGENT_BAUDRATE` | auto · 1000000 | direct-mode link |
| `JSAUX_AI_AGENT_INTERVAL` / `JSAUX_AI_AGENT_TICK_SECONDS` | 1s · derived | how often the provider is polled, and how often a frame is pushed (by default the poll rate: one upload keeps animating on the panel until the numbers change) |
| `JSAUX_AI_AGENT_FRAMES` / `JSAUX_AI_AGENT_FRAME_DELAY_CS` | 16 · 6cs | what one upload contains: 16 frames moving one cell each, so it **is** a whole turn of the activity mark, played at 16 steps a second. `frames × delay` is how long a turn takes, so `FRAME_DELAY_CS` is the spinner-speed knob (6cs = 0.96s a turn) |

| `JSAUX_AI_AGENT_SPINNER_STEP` | `16 / frames` | cells the activity mark moves per frame, when you want to set that instead of the turn length (the default keeps a turn whole, which is what makes it loop cleanly) |

Preview or push the screen without the companion:

```bash
python3 tools/ai_agent_screen.py --png preview.png   # look at it, no hardware
python3 tools/ai_agent_screen.py --push --power      # one frame over USB
python3 tools/ai_agent_screen.py --push --live       # keep it fresh
python3 tools/ai_agent_screen.py --push --live --for 60   # stop cleanly after 60s
```

If the panel stops answering at all — every push times out, even a power frame —
it is holding an upload that never finished, which is what killing a `--live`
run mid-upload leaves behind. The tool and the plugin both power-cycle it back
(`UsbSerialTransport.recover_link`); to do it by hand, unplug the panel for a
few seconds.

If the agent runs on a different machine than the faceplate, the faceplate reads
a **stats bridge** on the agent host. The bridge answers exactly the payload the
collector produces, so the faceplate host needs neither the Strata key nor the
agent database — and the key never leaves the agent host.

```bash
# on the agent host: a shared secret, then the bridge
python3 tools/agent_stats_server.py --host 0.0.0.0 --port 39060 \
    --token "$(openssl rand -hex 16)"
# keep it running (the token then comes from the file, not the flag):
#   ~/.config/opencode/jsaux-stats-token
#   systemctl --user enable --now jsaux-pixel-bridge
```

```bash
# on the faceplate host: the same secret, and where the bridge is
scp agent-host:.config/opencode/jsaux-stats-token .config/opencode/
export JSAUX_AGENT_STATS_URL=http://10.10.0.40:39060
# or set BRIDGE_URL in decky-plugin/backend/local_ai_stats.py so the plugin
# needs no environment at all (that is what the Deck side usually wants)
```

A read without the secret gets `401`, and the bridge never serves the key or
message text — counters, model name and phase only. If the bridge is down, the
faceplate falls back to the gateway and the local database.

## Frontend note

`decky-plugin/dist/index.js` is the checked-in Decky UI bundle. Matching TSX sources
are not in this tip, so the UI cannot be fully rebuilt from this repository alone.
Framing helpers in the bundle match `protocols/usb` (CRC-16/KERMIT).

## Replace the official Decky plugin

If the official closed companion is already installed, you can overlay this tip’s
plugin sources on top of it. See [`docs/replace-official-plugin.md`](docs/replace-official-plugin.md).

## Packaging helpers (plugin sources only)

- `Install Decky Plugin (opensource).desktop`
- `Remove Decky Plugin (opensource).desktop`

## Build / test

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j2
ctest --test-dir build --output-on-failure

python3 tests/test_usb_dy_frame.py
python3 tests/test_matrix_api.py
python3 tests/test_local_ai_stats.py
python3 tests/test_ai_agent_screen.py
node tests/plugin_ui_test.js
# or: python3 tools/run_device_tests.py
```

## Companion HTTP API

Set `JSAUX_DAEMON_URL` / `JSAUX_REFRESH_URL` only when a separately installed
official closed companion is already running.

## License

See root [`LICENSE`](./LICENSE) (MIT) for this repository’s open sources.
Third-party components remain under their own terms — see [`NOTICE`](./NOTICE).
Closed companion binaries obtained separately are **not** MIT-licensed by this tip.
