import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "decky-plugin"))

from backend import local_ai_stats  # noqa: E402


SCHEMA = """
create table session_v2 (
    id text, title text, agent text, model text,
    time_busy int, time_idle int, time_compacting int, time_updated int,
    tokens_input int, tokens_output int, tokens_reasoning int, tokens_cache_read int
);
create table session_message (id text, session_id text, type text, seq int, data text);
"""


def make_db(path, busy_ms, idle_ms, message):
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute(
        "insert into session_v2 values (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "ses_1",
            "Refactor the gateway",
            "build",
            json.dumps({"id": "iq3_s-256k", "providerID": "strata"}),
            busy_ms,
            idle_ms,
            None,
            max(busy_ms, idle_ms),
            1000,
            200,
            0,
            500,
        ),
    )
    if message is not None:
        connection.execute(
            "insert into session_message values (?,?,?,?,?)",
            ("msg_1", "ses_1", "assistant", 10, json.dumps(message)),
        )
    connection.commit()
    connection.close()


def assistant_message(created_ms, completed_ms, output, input_tokens, cache_read):
    return {
        "time": {"created": created_ms, "completed": completed_ms},
        "tokens": {
            "input": input_tokens,
            "output": output,
            "reasoning": 0,
            "cache": {"read": cache_read, "write": 0},
        },
    }


class OpenCodeReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "opencode.db"
        self.now = time.time()

    def test_busy_when_the_busy_stamp_is_the_newer_one(self):
        now_ms = int(self.now * 1000)
        make_db(
            self.db,
            now_ms - 5000,
            now_ms - 60000,
            assistant_message(now_ms - 5000, None, 120, 800, 9000),
        )
        state = local_ai_stats.read_opencode(self.db, self.now)
        self.assertIsNone(state["error"])
        self.assertTrue(state["busy"])
        self.assertTrue(state["in_flight"])
        self.assertEqual(state["session_title"], "Refactor the gateway")
        self.assertEqual(state["read_tokens"], 800)
        self.assertEqual(state["cache_read_tokens"], 9000)
        self.assertEqual(state["generated_tokens"], 120)

    def test_idle_when_the_session_settled_after_being_busy(self):
        now_ms = int(self.now * 1000)
        make_db(
            self.db,
            now_ms - 120000,
            now_ms - 1000,
            assistant_message(now_ms - 120000, now_ms - 1000, 630, 900, 0),
        )
        state = local_ai_stats.read_opencode(self.db, self.now)
        self.assertFalse(state["busy"])
        self.assertFalse(state["in_flight"])

    def test_a_stale_busy_stamp_counts_as_a_crash_not_a_request(self):
        stale = int((self.now - local_ai_stats.BUSY_STALE_SECONDS - 60) * 1000)
        make_db(self.db, stale, stale - 10000, None)
        state = local_ai_stats.read_opencode(self.db, self.now)
        self.assertFalse(state["busy"])

    def test_throughput_comes_from_the_message_timings(self):
        now_ms = int(self.now * 1000)
        created = now_ms - 20000
        completed = now_ms - 1000
        make_db(
            self.db,
            created,
            completed,
            assistant_message(created, completed, 95, 10, 0),
        )
        state = local_ai_stats.read_opencode(self.db, self.now)
        self.assertIsNotNone(state["tok_per_s"])
        # 95 tokens over the 19s the message was open.
        self.assertAlmostEqual(state["tok_per_s"], 5.0, delta=0.5)

    def test_a_missing_database_degrades_instead_of_raising(self):
        state = local_ai_stats.read_opencode(Path(self.tmp.name) / "absent.db", self.now)
        self.assertEqual(state["error"], "no_database")


class StrataMonitorTests(unittest.TestCase):
    """The engine monitor is the primary source: it knows request progress."""

    def setUp(self):
        self.now = time.time()

    def serve(self, metrics, slots=None, fail=False):
        """Stand in for the two HTTP calls the collector makes."""
        original = local_ai_stats._get_json_open
        seen = []

        def fake_get(url, api_key=""):
            seen.append(url)
            if fail:
                raise OSError("connection refused")
            if url.endswith("/metrics"):
                return metrics
            return slots if slots is not None else []

        local_ai_stats._get_json_open = fake_get
        self.addCleanup(lambda: setattr(local_ai_stats, "_get_json_open", original))
        return seen

    def test_reading_reports_prefill_progress(self):
        self.serve(
            {
                "engine": {"model": "qwen3.8-flash-next-iq3_s-256k", "max_context": 262144},
                "live": {
                    "state": "reading",
                    "phase": "reading the prompt",
                    "prompt_read": 48000,
                    "prompt_total": 96000,
                    "prompt_tokens": 96000,
                    "generated": 0,
                    "max_tokens": 4096,
                    "prefill_tok_s_mean": 105.1,
                    "queued": 0,
                },
                "hardware": {"gpu_mem_used": 32_000_000_000, "gpu_mem_total": 34_000_000_000},
                "totals": {"requests": 234, "output_tokens": 160024},
                "requests": [],
            },
            [{"id": 0, "n_ctx": 262144, "is_processing": True, "n_prompt_tokens": 96000}],
        )
        state = local_ai_stats.fetch_strata_monitor(
            {"base_url": "http://10.0.0.5:8080", "api_key": "k"}, None, self.now
        )
        self.assertIsNone(state["error"])
        self.assertTrue(state["busy"])
        self.assertEqual(state["engine_state"], "reading")
        self.assertEqual(state["phase_kind"], "read")
        self.assertEqual(state["progress_percent"], 50.0)
        self.assertEqual(state["read_percent"], 50.0)
        self.assertEqual(state["read_tokens"], 96000)
        self.assertEqual(state["max_output"], 4096)
        self.assertEqual(state["tok_per_s"], 105.1)
        self.assertAlmostEqual(state["vram_percent"], 94.1, delta=0.1)
        self.assertEqual(state["slots_busy"], 1)

    def test_generating_pins_the_read_arc_and_measures_the_answer(self):
        self.serve(
            {
                "engine": {"model": "m", "max_context": 262144},
                # The engine drops prompt_read / prompt_total once it starts
                # generating, which is itself the signal that prefill finished.
                "live": {
                    "state": "generating",
                    "phase": "answering",
                    "prompt_read": None,
                    "prompt_total": None,
                    "prompt_tokens": 96000,
                    "generated": 900,
                    "max_tokens": 4096,
                    "tok_s": 66.5,
                    "queued": 0,
                },
                "hardware": {},
                "totals": {},
                "requests": [],
            },
            [{"id": 0, "n_ctx": 262144, "is_processing": True, "n_prompt_tokens": 96000}],
        )
        state = local_ai_stats.fetch_strata_monitor(
            {"base_url": "http://h:8080", "api_key": ""}, None, self.now
        )
        self.assertEqual(state["read_percent"], 100.0)
        self.assertEqual(state["phase_kind"], "gen")
        self.assertEqual(state["progress_percent"], 22.0)
        self.assertEqual(state["context_percent"], 36.6)
        self.assertEqual(state["generated_tokens"], 900)
        self.assertEqual(state["tok_per_s"], 66.5)
        self.assertEqual(local_ai_stats.percent_of(state["generated_tokens"], state["max_output"]), 22.0)

    def test_at_rest_the_arcs_show_the_context_it_is_holding(self):
        self.serve(
            {
                "engine": {"model": "m", "max_context": 262144},
                "live": {"state": "idle", "tok_s": None, "prompt_read": None},
                "hardware": {"tok_s": 0.0, "prefill_tok_s_mean": 0.0},
                "totals": {"requests": 234},
                "requests": [
                    {
                        "id": "r1",
                        "finish": "stop",
                        "prompt_tokens": 42893,
                        "output_tokens": 1288,
                        "reused": 0,
                        "decode_tok_s": 57.6,
                        "time": self.now - 370,
                    }
                ],
            },
            [{"id": 0, "n_ctx": 262144, "is_processing": False, "n_prompt_tokens": 42893}],
        )
        state = local_ai_stats.fetch_strata_monitor(
            {"base_url": "http://h:8080", "api_key": ""}, None, self.now
        )
        self.assertFalse(state["busy"])
        # Nothing in flight: the mode bar resets, the context bar keeps the fill.
        self.assertEqual(state["phase_kind"], "rest")
        self.assertEqual(state["progress_percent"], local_ai_stats.UNKNOWN_PERCENT)
        self.assertAlmostEqual(state["context_percent"], 16.4, delta=0.1)
        # An idle rate meter reads zero; the last request's speed is the useful one.
        self.assertEqual(state["tok_per_s"], 57.6)
        self.assertEqual(state["generated_tokens"], 1288)
        self.assertEqual(state["requests_total"], 234)
        self.assertAlmostEqual(state["request_age_s"], 370.0, delta=1.0)

    def test_an_answer_that_finished_a_while_ago_stops_being_news(self):
        self.serve(
            {
                "engine": {"model": "m", "max_context": 262144},
                "live": {"state": "idle"},
                "hardware": {},
                "totals": {},
                "requests": [
                    {
                        "id": "r1",
                        "finish": "stop",
                        "prompt_tokens": 42893,
                        "output_tokens": 1288,
                        "decode_tok_s": 57.6,
                        "time": self.now - (local_ai_stats.REQUEST_LINGER_SECONDS + 60),
                    }
                ],
            },
            [{"id": 0, "n_ctx": 262144, "is_processing": False, "n_prompt_tokens": 42893}],
        )
        stats = local_ai_stats.collect(
            {"base_url": "http://127.0.0.1:8082/v1", "api_key": ""}, None, "", self.now
        )
        display = stats["display"]
        self.assertTrue(display["settled"])
        self.assertEqual(display["tokens_generated"], "--")
        self.assertIsNone(display["tokens_generated_raw"])
        self.assertEqual(display["phase_kind"], "rest")
        self.assertEqual(display["progress_percent"], local_ai_stats.UNKNOWN_PERCENT)
        # The context it is holding still shows.
        self.assertEqual(display["tokens_read"], "43K")
        self.assertAlmostEqual(display["context_percent"], 16.4, delta=0.1)

    def test_an_unreachable_monitor_falls_back_to_the_agent_database(self):
        self.serve({}, [], fail=True)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        now_ms = int(self.now * 1000)
        db = Path(tmp.name) / "opencode.db"
        make_db(db, now_ms - 4000, now_ms - 90000, assistant_message(now_ms - 4000, None, 40, 10, 0))
        previous = local_ai_stats.strata_settings
        local_ai_stats.strata_settings = lambda: {"base_url": "", "api_key": ""}
        self.addCleanup(lambda: setattr(local_ai_stats, "strata_settings", previous))
        stats = local_ai_stats.collect({"base_url": "", "api_key": ""}, db, "", self.now)
        self.assertEqual(stats["monitor"]["error"], "no_monitor_url")
        self.assertEqual(stats["agent"]["source"], "db")
        self.assertEqual(stats["display"]["session_name"], "Refactor the gateway")

    def test_the_monitor_port_is_derived_from_the_gateway_host(self):
        from os import environ

        previous = environ.get("JSAUX_STRATA_MONITOR_URL")
        environ["JSAUX_STRATA_MONITOR_URL"] = "http://10.0.0.9:9000"
        try:
            self.assertEqual(
                local_ai_stats.monitor_settings({"base_url": "http://10.0.0.5:8082/v1"})["base_url"],
                "http://10.0.0.9:9000",
            )
            environ["JSAUX_STRATA_MONITOR_URL"] = ""
            self.assertEqual(
                local_ai_stats.monitor_settings({"base_url": "http://10.0.0.5:8082/v1"})["base_url"],
                "",
            )
            del environ["JSAUX_STRATA_MONITOR_URL"]
            # Unset: the engine monitor is the gateway host on its own port.
            self.assertEqual(
                local_ai_stats.monitor_settings({"base_url": "http://10.0.0.5:8082/v1"})["base_url"],
                "http://10.0.0.5:8080",
            )
        finally:
            if previous is not None:
                environ["JSAUX_STRATA_MONITOR_URL"] = previous


class PayloadTests(unittest.TestCase):
    def test_stats_map_onto_the_builtin_layout(self):
        stats = {
            "display": {
                "session_name": "Refactor the gateway",
                "running": True,
                "conversations": 3,
                "read_percent": 36.0,
                "gen_percent": 7.7,
                "tokens_read": "94K",
                "tokens_generated": "630",
            }
        }
        payload = local_ai_stats.screen_payload(stats)
        self.assertEqual(payload["sessionName"], "Refactor the gateway")
        self.assertTrue(payload["running"])
        self.assertEqual(payload["conversations"], 3)
        self.assertEqual(payload["primaryPercent"], 36)
        self.assertEqual(payload["secondaryPercent"], 7)
        self.assertEqual(payload["tokensPrimary"], "94K")
        self.assertEqual(payload["tokensSecondary"], "630")

    def test_unknown_progress_stays_unknown(self):
        payload = local_ai_stats.screen_payload({"display": {"read_percent": -1.0}})
        self.assertEqual(payload["primaryPercent"], -1)
        self.assertEqual(payload["secondaryPercent"], -1)
        self.assertEqual(payload["tokensPrimary"], "--")

    def test_the_screen_name_is_clipped(self):
        payload = local_ai_stats.screen_payload({"display": {"session_name": "x" * 90}})
        self.assertEqual(len(payload["sessionName"]), 40)

    def test_token_formatting(self):
        self.assertEqual(local_ai_stats.format_tokens(630), "630")
        self.assertEqual(local_ai_stats.format_tokens(94269), "94K")
        self.assertEqual(local_ai_stats.format_tokens(1500000), "1.5M")
        self.assertEqual(local_ai_stats.format_tokens(None), "--")

    def test_percent_stays_inside_the_panel_range(self):
        self.assertEqual(local_ai_stats.percent_of(900, 100), 100.0)
        self.assertEqual(local_ai_stats.percent_of(0, 0), local_ai_stats.UNKNOWN_PERCENT)


class ConfigTests(unittest.TestCase):
    def test_model_limits_come_from_the_agent_config(self):
        from os import environ

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name) / "config"
        (home / "opencode").mkdir(parents=True)
        (home / "opencode" / "opencode.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "strata": {
                            "settings": {"baseURL": "http://10.0.0.5:8082/v1"},
                            "models": {
                                "iq3_s-256k": {
                                    "modelID": "qwen3.8-flash-next-iq3_s-256k",
                                    "limit": {"context": 262144, "output": 8192},
                                }
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        previous = environ.get("XDG_CONFIG_HOME")
        environ["XDG_CONFIG_HOME"] = str(home)
        try:
            limits = local_ai_stats.model_limits()
            self.assertEqual(limits["iq3_s-256k"], {"context": 262144, "output": 8192})
            settings = local_ai_stats.strata_settings()
            self.assertEqual(settings["base_url"], "http://10.0.0.5:8082/v1")
        finally:
            if previous is None:
                environ.pop("XDG_CONFIG_HOME", None)
            else:
                environ["XDG_CONFIG_HOME"] = previous


class StatsBridgeTests(unittest.TestCase):
    """The agent host holds the key and serves the snapshot; the faceplate does not."""

    SNAPSHOT = {
        "gateway": {"model": "strata-model", "reachable": True},
        "agent": {"read_tokens": 1200, "busy": True},
        "display": {"running": True, "phase_kind": "gen", "progress_percent": 40.0},
    }

    def stub(self, reply):
        original = local_ai_stats._get_json_open
        seen = []

        def fake_get(url, api_key=""):
            seen.append((url, api_key))
            return reply()

        local_ai_stats._get_json_open = fake_get
        self.addCleanup(lambda: setattr(local_ai_stats, "_get_json_open", original))
        return seen

    def test_the_bridge_snapshot_is_taken_whole(self):
        seen = self.stub(lambda: {"snapshot": dict(self.SNAPSHOT)})
        stats = local_ai_stats.collect(bridge_url="http://10.10.0.141:39060/stats")
        self.assertEqual(stats["gateway"]["model"], "strata-model")
        self.assertEqual(stats["display"]["phase_kind"], "gen")
        self.assertEqual(stats["bridge"]["url"], "http://10.10.0.141:39060/stats")
        # One call, not the gateway plus the monitor plus the database.
        self.assertEqual([url for url, _ in seen], ["http://10.10.0.141:39060/stats"])

    def test_the_shared_token_travels_as_a_bearer_value(self):
        os.environ["JSAUX_AGENT_STATS_TOKEN"] = "s3cret"
        self.addCleanup(lambda: os.environ.pop("JSAUX_AGENT_STATS_TOKEN", None))
        seen = self.stub(lambda: {"snapshot": dict(self.SNAPSHOT)})
        local_ai_stats.collect(bridge_url="http://10.10.0.141:39060")
        self.assertEqual(seen[0][1], "s3cret")

    def test_the_configured_bridge_host_is_the_default(self):
        previous = local_ai_stats.BRIDGE_URL
        local_ai_stats.BRIDGE_URL = "http://10.10.0.30:39060"
        self.addCleanup(setattr, local_ai_stats, "BRIDGE_URL", previous)
        seen = self.stub(lambda: {"snapshot": dict(self.SNAPSHOT)})
        stats = local_ai_stats.collect()
        self.assertEqual(stats["bridge"]["url"], "http://10.10.0.30:39060")
        self.assertEqual(seen[0][0], "http://10.10.0.30:39060/stats")

    def test_a_dead_bridge_falls_back_to_local_sources(self):
        def boom():
            raise OSError("connection refused")

        self.stub(boom)
        stats = local_ai_stats.collect(bridge_url="http://10.10.0.141:39060")
        self.assertNotIn("bridge", stats)
        self.assertIn("display", stats)


if __name__ == "__main__":
    unittest.main()
