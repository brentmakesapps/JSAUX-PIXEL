# Decky plugin (opensource tip)

Python backend (`main.py`) + checked-in UI bundle (`dist/index.js`).

- Talks to a **separately installed** closed companion over local HTTP when present.
- Exposes `MatrixDisplayAPI` RPCs for secondary development.
- AI Agent: your own local provider — Strata's **own** monitor (`GET /metrics`,
  `GET /slots`: request phase, prompt-read progress, answer progress, tokens/sec,
  VRAM) plus the gateway for the loaded model. opencode's read-only session
  database is only the fallback when the monitor is unreachable.
  `JSAUX_AI_AGENT_PROVIDER=codex` restores the old session scanner.
- `backend/local_ai_stats.py` collects it; `backend/ai_agent_screen.py` draws the
  64×54 frame used by direct USB mode.
- The original TSX project is **not** shipped here; do not expect a reproducible UI rebuild from this tip alone.
