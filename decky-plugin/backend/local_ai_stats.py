"""Local AI provider stats for the AI Agent screen.

This replaces the upstream Codex/OpenAI session scanner. Sources, all local
to you and all plain HTTP / read-only:

* **Strata engine monitor** — ``GET /metrics`` + ``GET /slots`` on the engine
  (default port 8080). This is the live view: which phase the running request
  is in, how much of its prompt it has read, how many tokens it has generated
  toward its cap, tokens/sec, VRAM and RAM. Preferred, and it needs no agent.
* **Strata gateway** — ``GET /health`` + ``GET /v1/models`` on the gateway
  (default port 8082). Loaded model, context size, model-swap state.
* **opencode** — a **read-only** SQLite open of the agent's own database, used
  only when the engine monitor is unreachable: it adds the session title.

Nothing here touches OAuth, a cloud usage API, or the agent's auth store. The
Strata key is only ever used as a request header; it is never returned in a
status payload.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

HTTP_TIMEOUT_SECONDS = 4.0
# The engine's own monitor (/metrics, /slots) sits on its own port, in front of
# which the gateway runs. Derived from the gateway host unless overridden.
MONITOR_DEFAULT_PORT = 8080
# The faceplate host has no Strata key and no agent database, so it takes the
# snapshot from the bridge on the agent host (tools/agent_stats_server.py).
# Put the machine you run the agent on here; JSAUX_AGENT_STATS_URL overrides it.
BRIDGE_URL = ""
BRIDGE_TOKEN_ENV = "JSAUX_AGENT_STATS_TOKEN"
BRIDGE_TOKEN_FILE = "jsaux-stats-token"
# An agent "busy" stamp older than this is a crash, not a running request.
BUSY_STALE_SECONDS = 900.0
# Sessions touched inside this window count as "your current work".
SESSION_ACTIVE_WINDOW_SECONDS = 24 * 3600.0
# How long a finished request's output counter stays on screen at rest.
REQUEST_LINGER_SECONDS = 120.0
# How far a single request may run ahead of the DB's own bookkeeping.
MIN_CONTEXT_LIMIT = 1024
UNKNOWN_PERCENT = -1.0


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else None


def _config_home() -> Path:
    override = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config"


def _data_home() -> Path:
    override = os.environ.get("XDG_DATA_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share"


def _candidate_homes() -> list[Path]:
    """Decky runs the plugin as root (HOME=/root); the deck user's files sit
    under /home/deck, so check that too."""
    homes = [_config_home(), _data_home()]
    deck = Path("/home/deck")
    extra = [deck / ".config", deck / ".local" / "share"]
    return homes + [p for p in extra if p.is_dir()]


def opencode_config_path() -> Path | None:
    override = _env_path("JSAUX_OPENCODE_CONFIG")
    if override:
        return override if override.is_file() else None
    for home in _candidate_homes():
        candidate = home / "opencode" / "opencode.json"
        if candidate.is_file():
            return candidate
    return None


def opencode_db_path() -> Path | None:
    override = _env_path("JSAUX_OPENCODE_DB")
    if override:
        return override if override.is_file() else None
    for home in _candidate_homes():
        candidate = home / "opencode" / "opencode.db"
        if candidate.is_file():
            return candidate
    return None


def _opencode_config() -> dict[str, Any]:
    path = opencode_config_path()
    if path is None:
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def model_limits() -> dict[str, dict[str, int]]:
    """Model id/alias → declared context and output ceilings, straight out of
    the opencode config, so the screen knows what a request is filling toward."""
    limits: dict[str, dict[str, int]] = {}
    for provider in (_opencode_config().get("providers") or {}).values():
        if not isinstance(provider, dict):
            continue
        for alias, model in (provider.get("models") or {}).items():
            if not isinstance(model, dict):
                continue
            limit = model.get("limit") or {}
            entry = {
                "context": int(limit.get("context") or 0),
                "output": int(limit.get("output") or 0),
            }
            for name in (str(model.get("modelID") or ""), str(alias)):
                if name:
                    limits.setdefault(name, entry)
    return limits


def strata_settings() -> dict[str, Any]:
    """Base URL + key for the Strata provider, from env or opencode's config."""
    config = _opencode_config()
    provider = ((config.get("providers") or {}).get("strata") or {})
    settings = provider.get("settings") or {}
    base_url = os.environ.get("JSAUX_STRATA_URL", "").strip() or str(
        settings.get("baseURL") or "http://127.0.0.1:8082/v1"
    )
    key = os.environ.get("JSAUX_STRATA_API_KEY", "").strip()
    if not key:
        key_file = _env_path("JSAUX_STRATA_API_KEY_FILE") or (
            _config_home() / "opencode" / "strata-api-key"
        )
        if key_file.is_file():
            key = key_file.read_text(encoding="utf-8").strip()
    if not key:
        key = str(settings.get("apiKey") or "")
    return {"base_url": base_url.rstrip("/"), "api_key": key}


def _root_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base[: -len("/v1")] if base.endswith("/v1") else base


def _get_json(url: str, api_key: str = "") -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read(262_144).decode("utf-8", "replace"))


def fetch_strata(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Gateway view: loaded model, context size, model-switch state."""
    settings = settings or strata_settings()
    root = _root_url(settings.get("base_url", ""))
    key = settings.get("api_key", "")
    state: dict[str, Any] = {
        "source": "strata",
        "base_url": settings.get("base_url", ""),
        "reachable": False,
        "model": "",
        "max_context": 0,
        "loaded": False,
        "loading_model": "",
        "switching_enabled": False,
        "models_loaded": 0,
        "models_total": 0,
        "context_by_model": {},
        "error": None,
    }
    if not root:
        state["error"] = "no_base_url"
        return state
    try:
        health = _get_json(f"{root}/health", key)
        state["reachable"] = True
        state["model"] = str(health.get("model") or "")
        state["max_context"] = int(health.get("max_context") or 0)
        state["loaded"] = bool(health.get("loaded"))
        gateway = health.get("gateway") or {}
        state["loading_model"] = str(gateway.get("loading_model") or "")
        state["switching_enabled"] = bool(gateway.get("switching_enabled"))
    except Exception as exc:
        state["error"] = f"health: {type(exc).__name__}"
        return state
    try:
        listing = _get_json(f"{root}/v1/models", key)
        models = listing.get("data") or []
        state["models_total"] = len(models)
        state["models_loaded"] = sum(
            1 for m in models if (m.get("status") or {}).get("value") == "loaded"
        )
        for entry in models:
            n_ctx = int((entry.get("meta") or {}).get("n_ctx") or 0)
            if n_ctx and "alias_of" not in entry:
                state["context_by_model"][str(entry.get("id") or "")] = n_ctx
    except Exception:
        # /health is enough for the screen; the model list is a bonus.
        pass
    return state


def _get_json_open(url: str, api_key: str = "") -> Any:
    """Fetch JSON with the key, then without it: the monitor port guards
    itself with the same key today, but a bare engine needs no auth."""
    keys = [api_key, ""] if api_key else [""]
    last_error: Exception | None = None
    for key in keys:
        try:
            return _get_json(url, key)
        except Exception as exc:
            last_error = exc
    raise last_error or RuntimeError("unreachable")


def monitor_settings(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Where the engine monitor lives: same host as the gateway, its own port.
    Override with ``JSAUX_STRATA_MONITOR_URL`` (empty string disables it)."""
    settings = settings or strata_settings()
    url = ""
    override = os.environ.get("JSAUX_STRATA_MONITOR_URL")
    if override is None:
        parts = urllib.parse.urlsplit(settings.get("base_url", ""))
        if parts.hostname:
            url = f"{parts.scheme or 'http'}://{parts.hostname}:{MONITOR_DEFAULT_PORT}"
    else:
        url = override.strip()
    return {"base_url": url.rstrip("/"), "api_key": settings.get("api_key", "")}


def _number(value: Any) -> float | None:
    """The monitor reports "not happening right now" as a JSON null."""
    if value is None or value == "" or value is False:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _whole(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def fetch_strata_monitor(
    monitor: dict[str, Any] | None = None,
    gateway: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Engine view: how far the running request has got, and at what rate.

    ``/metrics`` carries ``live`` (the request in flight), ``hardware`` (VRAM,
    RAM, GPU), ``totals`` (lifetime counters) and the last requests; ``/slots``
    says whether a slot is processing and how much context it holds. Field
    names match the engine's own payload; everything degrades to ``error``.
    """
    monitor = monitor or monitor_settings()
    root = str(monitor.get("base_url") or "").rstrip("/")
    now = time.time() if now is None else now
    state: dict[str, Any] = {
        "source": None,
        "monitor_url": root,
        "reachable": False,
        "error": None,
        "session_title": "",
        "agent": "",
        "model": "",
        "engine_state": "",
        "phase": "",
        "queued": 0,
        "busy": False,
        "in_flight": False,
        "request_age_s": None,
        "read_tokens": 0,
        "cache_read_tokens": 0,
        "generated_tokens": 0,
        "reasoning_tokens": 0,
        "context_used": 0,
        "context_limit": 0,
        "max_output": 0,
        "read_percent": None,
        "phase_kind": "rest",
        "progress_percent": UNKNOWN_PERCENT,
        "context_percent": None,
        "tok_per_s": None,
        "prefill_tok_per_s": None,
        "prompt_read": None,
        "prompt_total": None,
        "reused_tokens": 0,
        "slots_total": 0,
        "slots_busy": 0,
        "sessions_active": 0,
        "vram_used": 0,
        "vram_total": 0,
        "vram_percent": None,
        "ram_used": 0,
        "ram_total": 0,
        "gpu_util": None,
        "gpu_temp": None,
        "requests_total": 0,
        "prompt_total_all": 0,
        "output_total": 0,
        "reused_total": 0,
        "last_request": {},
    }
    if not root:
        state["error"] = "no_monitor_url"
        return state
    try:
        metrics = _get_json_open(f"{root}/metrics", str(monitor.get("api_key") or ""))
    except Exception as exc:
        state["error"] = f"metrics: {type(exc).__name__}"
        return state
    state["source"] = "monitor"
    state["reachable"] = True

    engine = metrics.get("engine") if isinstance(metrics.get("engine"), dict) else {}
    live = metrics.get("live") if isinstance(metrics.get("live"), dict) else {}
    hardware = metrics.get("hardware") if isinstance(metrics.get("hardware"), dict) else {}
    totals = metrics.get("totals") if isinstance(metrics.get("totals"), dict) else {}
    cache = (
        metrics.get("conversation_cache")
        if isinstance(metrics.get("conversation_cache"), dict)
        else {}
    )
    recent = metrics.get("requests")
    recent = recent if isinstance(recent, list) else []
    last = recent[-1] if recent and isinstance(recent[-1], dict) else {}

    state["model"] = str(engine.get("model") or (gateway or {}).get("model") or "")
    state["context_limit"] = _whole(engine.get("max_context") or engine.get("context"))
    state["engine_state"] = str(live.get("state") or "")
    state["phase"] = str(live.get("phase") or "")
    state["queued"] = _whole(live.get("queued"))

    # --- the request in flight -------------------------------------------
    prompt_read = _number(live.get("prompt_read"))
    prompt_total = _number(live.get("prompt_total"))
    generated = _number(live.get("generated"))
    max_tokens = _number(live.get("max_tokens"))
    state["prompt_read"] = prompt_read
    state["prompt_total"] = prompt_total
    state["generated_tokens"] = _whole(generated)
    state["max_output"] = _whole(max_tokens)
    state["read_tokens"] = _whole(live.get("prompt_tokens"))
    state["tok_per_s"] = _number(live.get("tok_s"))
    state["prefill_tok_per_s"] = _number(live.get("prefill_tok_s_mean"))

    # --- slots: is anything actually being processed ---------------------
    slots: Any = []
    key = str(monitor.get("api_key") or "")
    try:
        slots = _get_json_open(f"{root}/slots", key)
    except Exception:
        slots = []
    if not isinstance(slots, list) and gateway is not None:
        # The gateway proxies /slots, so it works even on a merged port setup.
        try:
            gw_root = _root_url(strata_settings().get("base_url", ""))
            slots = _get_json(f"{gw_root}/slots", key)
        except Exception:
            slots = []
    slots = [s for s in slots if isinstance(s, dict)] if isinstance(slots, list) else []
    state["slots_total"] = len(slots)
    state["slots_busy"] = sum(1 for s in slots if s.get("is_processing"))
    state["sessions_active"] = max(1, len(slots))
    slot_prompt = max((_whole(s.get("n_prompt_tokens")) for s in slots), default=0)
    slot_context = max((_whole(s.get("n_ctx")) for s in slots), default=0)
    if not state["context_limit"]:
        state["context_limit"] = slot_context
    if not state["read_tokens"]:
        state["read_tokens"] = slot_prompt
    state["context_used"] = state["read_tokens"] or slot_prompt
    if not state["generated_tokens"]:
        state["generated_tokens"] = _whole(last.get("output_tokens"))

    processing = bool(state["slots_busy"]) or bool(state["queued"])
    live_active = state["engine_state"] not in ("", "idle", "None")
    state["busy"] = live_active or processing
    state["in_flight"] = live_active or processing

    # --- how far along --------------------------------------------------
    # While the prompt is going in, prompt_read climbs to prompt_total: that is
    # prefill progress. Once generation starts the engine drops both fields, so
    # a running request reads as fully ingested; at rest the arc is how full
    # the context window currently is.
    if prompt_read is not None and prompt_total:
        state["phase_kind"] = "read"
        state["read_percent"] = percent_of(prompt_read, prompt_total)
    elif state["in_flight"]:
        # prompt_read / prompt_total went null: prefill is finished.
        state["phase_kind"] = "gen"
        state["read_percent"] = 100.0
    else:
        state["phase_kind"] = "rest"
    # The second bar is always the same thing: how full the window is.
    if state["read_tokens"] and state["context_limit"]:
        state["context_percent"] = percent_of(state["read_tokens"], state["context_limit"])
    # Progress of whatever it is doing right now; empty at rest, so the bar
    # visibly resets when the request ends.
    if state["phase_kind"] == "read":
        state["progress_percent"] = state["read_percent"]
    elif state["phase_kind"] == "gen":
        state["progress_percent"] = percent_of(
            state["generated_tokens"], state["max_output"]
        )
    else:
        state["progress_percent"] = UNKNOWN_PERCENT

    # --- hardware + lifetime counters -----------------------------------
    state["vram_used"] = _whole(hardware.get("gpu_mem_used"))
    state["vram_total"] = _whole(hardware.get("gpu_mem_total"))
    state["vram_percent"] = (
        percent_of(state["vram_used"], state["vram_total"]) if state["vram_total"] else None
    )
    state["ram_used"] = _whole(hardware.get("ram_used"))
    state["ram_total"] = _whole(hardware.get("ram_total"))
    state["gpu_util"] = _number(hardware.get("gpu_util"))
    state["gpu_temp"] = _number(hardware.get("gpu_temp"))
    if state["tok_per_s"] is None:
        # Prefill reports its own rate; a stale decode average would be worse.
        state["tok_per_s"] = state["prefill_tok_per_s"] or _number(hardware.get("tok_s"))
    if state["prefill_tok_per_s"] is None:
        state["prefill_tok_per_s"] = _number(hardware.get("prefill_tok_s_mean"))
    state["requests_total"] = _whole(totals.get("requests"))
    state["prompt_total_all"] = _whole(totals.get("prompt_tokens"))
    state["output_total"] = _whole(totals.get("output_tokens"))
    state["reused_total"] = _whole(totals.get("reused"))
    state["reused_tokens"] = _whole(cache.get("last_reused") or last.get("reused"))
    if last:
        state["last_request"] = {
            "id": str(last.get("id") or ""),
            "finish": str(last.get("finish") or ""),
            "prompt_tokens": _whole(last.get("prompt_tokens")),
            "output_tokens": _whole(last.get("output_tokens")),
            "reused": _whole(last.get("reused")),
            "decode_tok_s": _number(last.get("decode_tok_s")),
            "duration_s": _number(last.get("duration_s")),
            "age_s": (
                round(now - float(last["time"]), 1) if _number(last.get("time")) else None
            ),
        }
        state["request_age_s"] = state["last_request"]["age_s"]
        if not state["tok_per_s"]:
            # At rest the engine's rate meters decay to zero; the last real
            # request's speed is the more useful number to leave up.
            state["tok_per_s"] = state["last_request"]["decode_tok_s"]
    return state


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def read_opencode(db_path: Path | None = None, now: float | None = None) -> dict[str, Any]:
    """Agent view: which session is live and how far its request has got.

    The database is opened read-only (``mode=ro``); a WAL sidecar may be
    mid-write, so every failure degrades to ``error`` instead of raising.
    """
    db_path = db_path or opencode_db_path()
    now = time.time() if now is None else now
    state: dict[str, Any] = {
        "source": None,
        "db_path": str(db_path) if db_path else "",
        "session_title": "",
        "agent": "",
        "model": "",
        "busy": False,
        "compacting": False,
        "request_age_s": None,
        "in_flight": False,
        "read_tokens": 0,
        "cache_read_tokens": 0,
        "generated_tokens": 0,
        "reasoning_tokens": 0,
        "context_used": 0,
        "context_limit": 0,
        "max_output": 0,
        "tok_per_s": None,
        "sessions_active": 0,
        "error": None,
    }
    if db_path is None or not Path(db_path).is_file():
        state["error"] = "no_database"
        return state
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
    except Exception as exc:
        state["error"] = f"open: {type(exc).__name__}"
        return state
    try:
        connection.row_factory = sqlite3.Row
        sessions = [
            dict(zip(row.keys(), row))
            for row in connection.execute(
                "select id, title, agent, model, time_busy, time_idle, time_compacting,"
                " time_updated, tokens_input, tokens_output, tokens_reasoning,"
                " tokens_cache_read from session_v2 order by time_updated desc limit 8"
            )
        ]
        if not sessions:
            state["error"] = "no_sessions"
            return state
        live = max(
            sessions,
            key=lambda s: max(
                int(s.get("time_busy") or 0), int(s.get("time_idle") or 0), int(s.get("time_updated") or 0)
            ),
        )
        state["source"] = "db"
        state["session_title"] = str(live.get("title") or "")
        state["agent"] = str(live.get("agent") or "")
        state["model"] = str(_json_object(live.get("model")).get("id") or "")
        limits = model_limits().get(state["model"], {})
        state["context_limit"] = int(limits.get("context") or 0)
        state["max_output"] = int(limits.get("output") or 0)
        busy_ms = int(live.get("time_busy") or 0)
        idle_ms = int(live.get("time_idle") or 0)
        compacting = live.get("time_compacting")
        state["compacting"] = bool(compacting) and (
            now - int(compacting) / 1000.0) < BUSY_STALE_SECONDS
        busy_recent = bool(busy_ms) and (now - busy_ms / 1000.0) <= BUSY_STALE_SECONDS
        state["busy"] = bool(busy_recent and busy_ms >= idle_ms)
        state["request_age_s"] = round(now - busy_ms / 1000.0, 1) if busy_ms else None
        state["sessions_active"] = sum(
            1
            for s in sessions
            if max(int(s.get("time_updated") or 0), int(s.get("time_busy") or 0))
            / 1000.0 >= now - SESSION_ACTIVE_WINDOW_SECONDS
        )

        message = connection.execute(
            "select data from session_message where session_id = ? and type = 'assistant'"
            " order by seq desc limit 1",
            (str(live.get("id") or ""),),
        ).fetchone()
        payload = _json_object(message[0] if message else "")
        tokens = _json_object(payload.get("tokens"))
        cache = _json_object(tokens.get("cache"))
        timing = _json_object(payload.get("time"))
        if payload:
            state["in_flight"] = timing.get("completed") in (None, 0)
            state["read_tokens"] = int(tokens.get("input") or 0)
            state["cache_read_tokens"] = int(cache.get("read") or 0)
            state["generated_tokens"] = int(tokens.get("output") or 0)
            state["reasoning_tokens"] = int(tokens.get("reasoning") or 0)
            started = int(timing.get("created") or 0)
            if started:
                state["request_age_s"] = round(now - started / 1000.0, 1)
            finished = int(timing.get("completed") or timing.get("streamed") or 0)
            if started and finished and finished > started:
                span = (finished - started) / 1000.0
                produced = state["generated_tokens"] + state["reasoning_tokens"]
                if span > 0.2 and produced:
                    state["tok_per_s"] = round(produced / span, 1)
        if state["in_flight"] and state["tok_per_s"] is None and state["request_age_s"]:
            produced = state["generated_tokens"] + state["reasoning_tokens"]
            if produced:
                state["tok_per_s"] = round(produced / max(state["request_age_s"], 1.0), 1)

        # Request-level numbers win; session totals are the running fallback.
        state["context_used"] = state["read_tokens"] + state["cache_read_tokens"] or int(
            (live.get("tokens_input") or 0) + (live.get("tokens_cache_read") or 0)
        )
        if not state["generated_tokens"]:
            state["generated_tokens"] = int(live.get("tokens_output") or 0)
        if not state["reasoning_tokens"]:
            state["reasoning_tokens"] = int(live.get("tokens_reasoning") or 0)
    except Exception as exc:  # pragma: no cover - sqlite surprises
        state["error"] = f"read: {type(exc).__name__}"
    finally:
        try:
            connection.close()
        except Exception:
            pass
    return state


def bridge_settings() -> tuple[str, str]:
    """Where the stats bridge is and the secret it wants, env first then files.

    The faceplate host has neither the Strata key nor the agent database, so it
    reads the snapshot from the agent host (``tools/agent_stats_server.py``).
    Set ``BRIDGE_URL`` here for the machine you run the agent on, or use
    ``JSAUX_AGENT_STATS_URL``.
    """
    url = os.environ.get("JSAUX_AGENT_STATS_URL", "").strip() or BRIDGE_URL.strip()
    token = os.environ.get(BRIDGE_TOKEN_ENV, "").strip()
    if not token:
        path = _env_path("JSAUX_AGENT_STATS_TOKEN_FILE") or (
            _config_home() / "opencode" / BRIDGE_TOKEN_FILE
        )
        if path.is_file():
            token = path.read_text(encoding="utf-8").strip()
    return url, token


def bridge_token() -> str:
    """Shared secret for the stats bridge, if one is configured."""
    return bridge_settings()[1]


def fetch_snapshot(url: str) -> dict[str, Any]:
    """The whole stats snapshot, collected on the machine that runs the agent.

    The bridge runs this same collector, so the Strata key stays on the host
    that has it and the faceplate host needs no secrets at all.
    """
    try:
        payload = _get_json_open(url.rstrip("/").removesuffix("/stats") + "/stats",
                                  bridge_settings()[1])
    except Exception as exc:  # noqa: BLE001 - any HTTP failure means "use local"
        return {"error": f"bridge: {type(exc).__name__}"}
    snapshot = payload.get("snapshot") if isinstance(payload, dict) else None
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("display"), dict):
        return {"error": "bridge: no snapshot"}
    snapshot["bridge"] = {"url": url}
    return snapshot


def fetch_agent_stats(url: str) -> dict[str, Any]:
    """Agent view from a remote stats bridge (see tools/agent_stats_server.py),
    for when the faceplate host is not the host running the agent."""
    try:
        payload = _get_json(url.rstrip("/").removesuffix("/stats") + "/stats")
    except Exception as exc:
        return {"source": None, "error": f"bridge: {type(exc).__name__}"}
    agent = payload.get("agent") if isinstance(payload.get("agent"), dict) else payload
    agent["source"] = "bridge"
    return agent


def context_limit_for(agent: dict[str, Any], gateway: dict[str, Any]) -> int:
    limit = int(agent.get("context_limit") or 0)
    if not limit:
        limit = int(gateway.get("context_by_model", {}).get(agent.get("model", ""), 0) or 0)
    if not limit:
        limit = int(gateway.get("max_context") or 0)
    return max(limit, MIN_CONTEXT_LIMIT) if limit else 0


def percent_of(part: float, whole: float) -> float:
    if not whole:
        return UNKNOWN_PERCENT
    value = (float(part) / float(whole)) * 100.0
    return round(max(0.0, min(100.0, value)), 1)


def format_tokens(value: float | int | None) -> str:
    if value in (None, ""):
        return "--"
    number = float(value)
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 10_000:
        return f"{number / 1000:.0f}K"
    if number >= 1000:
        return f"{number / 1000:.1f}K"
    return str(int(number))


def screen_payload(stats: dict[str, Any]) -> dict[str, Any]:
    """Map collected stats onto the built-in AI Agent screen fields.

    ``primaryPercent`` / ``secondaryPercent`` are the two arcs (context read,
    answer written); ``tokensPrimary`` / ``tokensSecondary`` are the two
    counters. ``-1`` and ``"--"`` mean "nothing to show" to the renderer.
    """
    display = stats.get("display") or {}
    try:
        conversations = int(display.get("conversations") or 0)
    except (TypeError, ValueError):
        conversations = 0
    return {
        "sessionName": str(display.get("session_name") or "")[:40],
        "running": bool(display.get("running")),
        "conversations": min(max(conversations, 0), 999),
        "primaryPercent": int(display.get("read_percent") or UNKNOWN_PERCENT),
        "secondaryPercent": int(display.get("gen_percent") or UNKNOWN_PERCENT),
        "tokensPrimary": str(display.get("tokens_read") or "--"),
        "tokensSecondary": str(display.get("tokens_generated") or "--"),
    }


def collect(
    settings: dict[str, Any] | None = None,
    db_path: Path | None = None,
    bridge_url: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    """Merged provider + agent stats, with the panel-facing display block."""
    now = time.time() if now is None else now
    settings = settings or strata_settings()
    bridge_url = bridge_url or bridge_settings()[0]
    if bridge_url:
        # The agent host runs this same collector and holds the key, so take
        # its answer whole rather than re-deriving it here without either.
        snapshot = fetch_snapshot(bridge_url)
        if not snapshot.get("error"):
            return snapshot
    gateway = fetch_strata(settings)
    # The engine knows its own request progress, so ask it first; opencode and
    # the stats bridge only fill in when the monitor is not there.
    monitor = fetch_strata_monitor(monitor_settings(settings), gateway, now)
    agent = monitor
    if agent.get("error"):
        agent = read_opencode(db_path, now)
        # The faceplate host is often not the host running the agent: fall
        # back to the stats bridge when the local database is missing too.
        if bridge_url and agent.get("error"):
            agent = fetch_agent_stats(bridge_url)

    limit = context_limit_for(agent, gateway)
    read_tokens = int(agent.get("read_tokens") or 0) + int(agent.get("cache_read_tokens") or 0)
    generated = int(agent.get("generated_tokens") or 0) + int(agent.get("reasoning_tokens") or 0)
    max_output = int(agent.get("max_output") or 0)
    running = bool(agent.get("busy")) or bool(gateway.get("loading_model"))
    model_label = str(gateway.get("model") or agent.get("model") or "")
    # The engine reports prefill progress directly; otherwise the read arc is
    # simply how full the context window is.
    read_percent = agent.get("read_percent")
    if not isinstance(read_percent, (int, float)) or float(read_percent) < 0:
        read_percent = percent_of(read_tokens, limit) if limit else UNKNOWN_PERCENT
    # One bar for what it is doing now (reading / generating), one for how
    # full the context window is.
    phase_kind = str(agent.get("phase_kind") or "")
    if phase_kind not in ("read", "gen", "rest"):
        phase_kind = ("gen" if generated else "read") if running else "rest"
    progress = agent.get("progress_percent")
    if not isinstance(progress, (int, float)):
        if phase_kind == "read":
            progress = read_percent
        elif phase_kind == "gen" and max_output:
            progress = percent_of(generated, max_output)
        else:
            progress = UNKNOWN_PERCENT
    context_percent = agent.get("context_percent")
    if not isinstance(context_percent, (int, float)):
        context_percent = percent_of(read_tokens, limit) if limit else UNKNOWN_PERCENT
    # An answer that finished a while ago stops being news.
    age = agent.get("request_age_s")
    settled = (
        not running
        and isinstance(age, (int, float))
        and float(age) > REQUEST_LINGER_SECONDS
    )
    display = {
        "session_name": str(agent.get("session_title") or "") or model_label,
        "running": running,
        "conversations": max(1, int(agent.get("sessions_active") or 0)),
        "model": model_label,
        "read_percent": read_percent,
        "gen_percent": percent_of(generated, max_output) if max_output else UNKNOWN_PERCENT,
        "phase_kind": phase_kind,
        "progress_percent": progress,
        "context_percent": context_percent,
        "settled": settled,
        "tokens_read": format_tokens(read_tokens),
        "tokens_generated": "--" if settled else format_tokens(generated),
        "tokens_read_raw": read_tokens,
        "tokens_generated_raw": None if settled else generated,
        "tok_per_s": agent.get("tok_per_s"),
        "context_limit": limit,
        "context_used": int(agent.get("context_used") or 0),
        "vram_percent": agent.get("vram_percent"),
        "phase": str(agent.get("phase") or ""),
        "engine_state": str(agent.get("engine_state") or ""),
        "slots_busy": int(agent.get("slots_busy") or 0),
        "queued": int(agent.get("queued") or 0),
        "agent_source": agent.get("source"),
    }
    return {
        "ok": bool(gateway.get("reachable")) or not bool(gateway.get("error")),
        "provider": "strata",
        "generated_at": int(now),
        "gateway": gateway,
        "monitor": monitor,
        "agent": agent,
        "display": display,
    }
