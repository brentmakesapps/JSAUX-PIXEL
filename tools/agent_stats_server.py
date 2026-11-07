#!/usr/bin/env python3
"""Serve the stats snapshot over HTTP for the faceplate host.

Run this on the machine that runs the agent and holds the Strata key, then
point the Decky plugin at it with ``JSAUX_AGENT_STATS_URL=http://<that-host>:39060``.
It answers a read-only snapshot - the very payload ``local_ai_stats.collect()``
produces, so the faceplate draws it as-is and needs no key and no agent
database. It never serves the Strata key and never serves message text.

    python3 tools/agent_stats_server.py                      # loopback only
    python3 tools/agent_stats_server.py --host 10.10.0.141    # the LAN can read it
    python3 tools/agent_stats_server.py --token <secret>      # and must present it
    python3 tools/agent_stats_server.py --once
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "decky-plugin"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from backend import local_ai_stats  # noqa: E402


class StatsHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _wanted(self):
        """A shared secret, if the bridge was started with one."""
        token = local_ai_stats.bridge_token()
        if not token:
            return True
        given = self.headers.get("Authorization", "")
        return given.strip() in (f"Bearer {token}", token)

    def _reply(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if not self._wanted():
            self._reply(401, {"error": "unauthorized"})
            return
        if path in ("", "/stats"):
            # The whole snapshot: the faceplate host renders it as-is, so the
            # Strata key and the agent database never leave this machine.
            self._reply(
                200,
                {"snapshot": local_ai_stats.collect(), "generated_at": int(time.time())},
            )
        elif path == "/opencode":
            self._reply(
                200,
                {"agent": local_ai_stats.read_opencode(), "generated_at": int(time.time())},
            )
        elif path == "/health":
            db = local_ai_stats.opencode_db_path()
            self._reply(200, {"ok": True, "database": str(db or ""),
                              "authenticated": bool(local_ai_stats.bridge_token())})
        else:
            self._reply(404, {"error": "not_found"})


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="agent_stats_server", description="Read-only agent stats endpoint."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=39060)
    parser.add_argument("--token", default="",
                        help="require this secret on every read (the faceplate host "
                             "sets the same JSAUX_AGENT_STATS_TOKEN)")
    parser.add_argument("--once", action="store_true", help="print one snapshot and exit")
    args = parser.parse_args(argv)
    if args.token:
        os.environ["JSAUX_AGENT_STATS_TOKEN"] = args.token

    if args.once:
        print(json.dumps({"agent": local_ai_stats.read_opencode()}, indent=2))
        return 0

    server = ThreadingHTTPServer((args.host, args.port), StatsHandler)
    print(f"agent stats on http://{args.host}:{args.port}/stats "
          f"(read-only{', token required' if local_ai_stats.bridge_token() else ''})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
