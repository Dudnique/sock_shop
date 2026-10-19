"""The HTTP surface: four endpoints, no framework, no dependencies.

* `POST /alert`  - the Alertmanager webhook. Alertmanager retries on a non-2xx,
  so a bad payload is answered 400 (loudly, in its logs) while a triage problem
  is answered 200 with the failure described in the body: a model that is down
  must not turn into a retry storm against the bot.
* `GET /healthz` - liveness/readiness. It reports the *configuration* too, so
  "which adapter answered" is answerable without reading a card.
* `GET /metrics` - Prometheus text format; the Service carries
  `prometheus.io/scrape`, so the existing scrape job picks it up.
* `GET /incidents?limit=N` - the journal, newest first. This is what makes the
  stand debuggable without Telegram: the same data a human sees in the chat.
* `POST /decision` - a synthetic button press (`{"incident_id": "...", "action":
  "approve"}`), used by the offline demo and by the tests. It calls exactly the
  same pipeline method the Telegram callback calls, so there is no second
  implementation to drift.
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__

log = logging.getLogger("aiops.server")

MAX_BODY_BYTES = 512 * 1024


def make_server(settings, metrics, pipeline, journal):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"aiops-triage/{__version__}"
        protocol_version = "HTTP/1.1"

        # --- helpers ---------------------------------------------------------
        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self._send(code, body, "application/json; charset=utf-8")

        def _read_json(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if length <= 0 or length > MAX_BODY_BYTES:
                return None
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                return None

        def log_message(self, fmt: str, *args) -> None:
            log.debug("%s %s", self.address_string(), fmt % args)

        # --- routes ----------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                self._json(200, {"status": "ok", "version": __version__,
                                 "settings": settings.describe()})
            elif path == "/metrics":
                self._send(200, metrics.render().encode("utf-8"),
                           "text/plain; version=0.0.4; charset=utf-8")
            elif path == "/incidents":
                limit = 10
                if "limit=" in self.path:
                    try:
                        limit = max(1, min(200, int(self.path.split("limit=")[1].split("&")[0])))
                    except ValueError:
                        limit = 10
                self._json(200, {"incidents": journal.recent(limit)})
            else:
                self._json(404, {"error": "not found", "path": path})

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            payload = self._read_json()
            if payload is None:
                self._json(400, {"error": "body must be a JSON object"})
                return
            if path == "/alert":
                try:
                    summary = pipeline.handle_webhook(payload)
                except Exception as exc:  # a triage failure is visible, not silent
                    log.exception("triage failed: %s", exc)
                    self._json(200, {"error": f"triage failed: {exc}"})
                    return
                self._json(200, summary)
                return
            if path == "/decision":
                incident_id = str(payload.get("incident_id") or "")
                action = str(payload.get("action") or "")
                result = pipeline.handle_callback({
                    "id": "local",
                    "data": f"{action}:{incident_id}",
                    "from": {"username": str(payload.get("user") or "local-test")},
                    "message": {"message_id": 0},
                })
                self._json(200, {"decision": result})
                return
            self._json(404, {"error": "not found", "path": path})

    return ThreadingHTTPServer((settings.listen_host, settings.listen_port), Handler)
