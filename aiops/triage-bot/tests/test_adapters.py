"""Adapters: the offline ruleset, and the real HTTP shape of a hosted model.

The HTTP tests run against a stub server on `127.0.0.1`, so what the adapter
would send to DeepSeek is asserted *for real* - path, headers, model,
temperature, `response_format` - without a network, a key or a bill.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from triage_bot.adapters import (AdapterError, MockAdapter, OpenAICompatibleAdapter,
                                 build_adapter)
from triage_bot.config import LLM_PROVIDERS
from tests.stubs import make_settings

CONTEXT = {
    "prometheus": {"replicas_available[front-end]": "0",
                   "replicas_desired[front-end]": "1"},
    "loki": ["12:00 front-end-1: connection refused"],
    "missing": ["loki: window is short"],
}


def alert(name: str = "DeploymentReplicasUnavailable", deployment: str = "front-end") -> dict:
    labels = {"alertname": name, "namespace": "sock-shop"}
    if deployment:
        labels["deployment"] = deployment
    return {"status": "firing", "labels": labels, "annotations": {}}


class MockAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = MockAdapter(make_settings())

    def test_known_alert_produces_the_tabled_action(self) -> None:
        payload = self.adapter.decision_for(alert(), CONTEXT)
        self.assertEqual(payload["action_id"], "restart_deployment")
        self.assertEqual(payload["confidence"], 0.85)
        self.assertEqual(payload["params"],
                         {"namespace": "sock-shop", "deployment": "front-end"})

    def test_the_answer_is_deterministic(self) -> None:
        first = self.adapter.decide("system", "user", alert(), CONTEXT)
        second = self.adapter.decide("system", "user", alert(), CONTEXT)
        self.assertEqual(first.text, second.text)
        self.assertEqual(first.latency_ms, 0)
        self.assertEqual(first.usage, {})

    def test_evidence_comes_from_the_live_context(self) -> None:
        joined = " ".join(self.adapter.decision_for(alert(), CONTEXT)["evidence"])
        self.assertIn("replicas_available[front-end] = 0", joined)
        self.assertIn("connection refused", joined)
        self.assertIn("gap:", joined)

    def test_unknown_alert_escalates_instead_of_inventing_an_action(self) -> None:
        payload = self.adapter.decision_for(alert(name="SomethingBrandNew"), CONTEXT)
        self.assertEqual(payload["action_id"], "escalate_human")
        self.assertEqual(payload["confidence"], 0.40)

    def test_missing_deployment_label_escalates(self) -> None:
        payload = self.adapter.decision_for(alert(deployment=""), CONTEXT)
        self.assertEqual(payload["action_id"], "escalate_human")
        self.assertEqual(payload["confidence"], 0.5)
        self.assertIn("deployment", payload["reason"])

    def test_an_alert_contradicted_by_its_own_metrics_escalates(self) -> None:
        """A synthetic or stale alert carries labels but no problem: do not act.

        This is the shape a hand-posted or already-recovered alert has: the
        tier-0 table alone would have proposed a restart of a healthy Deployment.
        """
        healthy = {"prometheus": {"replicas_available[front-end]": "1",
                                  "replicas_desired[front-end]": "1"},
                   "loki": [], "missing": []}
        payload = self.adapter.decision_for(alert(), healthy)
        self.assertEqual(payload["action_id"], "escalate_human")
        self.assertEqual(payload["confidence"], 0.5)
        self.assertIn("replicas_available=1 >= replicas_desired=1", payload["reason"])

    def test_a_missing_metric_is_not_a_conflict(self) -> None:
        """An unverifiable premise must not block a safe step."""
        payload = self.adapter.decision_for(
            alert(), {"prometheus": {}, "loki": [], "missing": []})
        self.assertEqual(payload["action_id"], "restart_deployment")

    def test_premise_checks_point_at_real_tier0_decisions(self) -> None:
        for name in MockAdapter.PREMISES:
            self.assertIn(name, MockAdapter.RULES, name)

    def test_the_whole_table_is_self_consistent(self) -> None:
        from triage_bot.model import ACTIONS
        for name, (action_id, confidence, reason) in MockAdapter.RULES.items():
            self.assertIn(action_id, ACTIONS, name)
            self.assertTrue(reason.strip(), name)
            self.assertTrue(0.0 <= confidence <= 1.0, name)


class StubChatServer:
    """A tiny OpenAI-compatible endpoint that records what it was asked."""

    def __init__(self, responses: list) -> None:
        self.requests: list = []
        self.responses = responses
        outer = self

        class Handler(BaseHTTPRequestHandler):
            # HTTP/1.0 + `close_connection` on purpose: with HTTP/1.1 and
            # keep-alive, ThreadingHTTPServer.server_close() waits for a handler
            # thread that is still parked on a live connection - which is exactly
            # how a "fast" test suite hangs forever.
            protocol_version = "HTTP/1.0"

            def log_message(self, *_args) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                self.close_connection = True
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                outer.requests.append({
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": body,
                })
                index = min(len(outer.requests) - 1, len(outer.responses) - 1)
                code, payload = outer.responses[index]
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def __enter__(self) -> "StubChatServer":
        self.thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.server.shutdown()
        self.server.server_close()


def completion(text: str, prompt_tokens: int = 700, completion_tokens: int = 40,
               code: int = 200, error_text: str = "") -> tuple:
    if code != 200:
        return code, {"error": {"message": error_text or "bad request"}}
    return code, {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


ESCALATION = json.dumps({"action_id": "escalate_human", "confidence": 0.9,
                         "reason": "r", "evidence": ["e"]})


class OpenAICompatibleTests(unittest.TestCase):
    def test_request_shape_and_usage_are_what_a_provider_expects(self) -> None:
        with StubChatServer([completion(ESCALATION)]) as stub:
            settings = make_settings(model_adapter="deepseek", llm_base_url=stub.base_url)
            adapter = OpenAICompatibleAdapter(settings, base_url=stub.base_url,
                                              model="deepseek-chat", api_key="sk-test",
                                              provider_name="deepseek")
            result = adapter.decide("SYS", "USER", alert(), CONTEXT)

        request = stub.requests[0]
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(request["authorization"], "Bearer sk-test")
        self.assertEqual(request["body"]["model"], "deepseek-chat")
        self.assertEqual(request["body"]["temperature"], 0.0)
        self.assertEqual(request["body"]["response_format"], {"type": "json_object"})
        self.assertEqual([m["role"] for m in request["body"]["messages"]],
                         ["system", "user"])
        self.assertEqual(result.text, ESCALATION)
        self.assertEqual(result.prompt_tokens, 700)
        self.assertEqual(result.completion_tokens, 40)
        self.assertEqual(result.adapter, "deepseek")

    def test_a_provider_without_response_format_is_retried_once(self) -> None:
        with StubChatServer([completion("", code=400,
                                        error_text="unknown field: response_format"),
                             completion(ESCALATION)]) as stub:
            settings = make_settings(model_adapter="ollama", llm_base_url=stub.base_url)
            adapter = OpenAICompatibleAdapter(settings, base_url=stub.base_url,
                                              model="llama3.1", api_key="",
                                              provider_name="ollama")
            result = adapter.decide("SYS", "USER", alert(), CONTEXT)

        self.assertEqual(len(stub.requests), 2)
        self.assertIn("response_format", stub.requests[0]["body"])
        self.assertNotIn("response_format", stub.requests[1]["body"])
        self.assertIsNone(stub.requests[1]["authorization"])
        self.assertEqual(result.text, ESCALATION)

    def test_http_error_becomes_an_AdapterError_with_the_status(self) -> None:
        with StubChatServer([completion("", code=401, error_text="invalid api key")]) as stub:
            settings = make_settings(model_adapter="deepseek")
            adapter = OpenAICompatibleAdapter(settings, base_url=stub.base_url,
                                              model="deepseek-chat", api_key="bad",
                                              provider_name="deepseek")
            with self.assertRaises(AdapterError) as ctx:
                adapter.decide("SYS", "USER", alert(), CONTEXT)
        self.assertIn("401", str(ctx.exception))

class BuildAdapterTests(unittest.TestCase):
    def test_mock_needs_nothing(self) -> None:
        adapter = build_adapter(make_settings(model_adapter="mock"))
        self.assertIsInstance(adapter, MockAdapter)

    def test_mock_is_the_ruleset_not_a_missing_model(self) -> None:
        """A healthy stand must not report a reason for a tier that is available.

        /healthz printed "the provider has no base_url" for adapter=mock until the
        live run showed it: `mock` is not a model that is missing,
        it is the tier-0 ruleset itself.
        """
        settings = make_settings(model_adapter="mock")
        settings.llm_api_style = "mock"
        self.assertTrue(settings.model_tier_available)
        self.assertEqual(settings.model_tier_reason, "")

    def test_unknown_provider_is_refused(self) -> None:
        settings = make_settings(model_adapter="gpt-5-ultra")
        settings.llm_api_style = "unknown"
        with self.assertRaises(AdapterError):
            build_adapter(settings)

    def test_provider_without_a_key_is_refused_with_a_reason(self) -> None:
        settings = make_settings(model_adapter="deepseek",
                                 llm_api_key_file="/nonexistent/deepseek_api_key")
        settings.llm_api_style = LLM_PROVIDERS["deepseek"]["api_style"]
        settings.llm_base_url = LLM_PROVIDERS["deepseek"]["base_url"]
        self.assertFalse(settings.model_tier_available)
        self.assertIn("deepseek_api_key", settings.model_tier_reason)
        with self.assertRaises(AdapterError):
            build_adapter(settings)

    def test_deepseek_defaults_come_from_the_provider_table(self) -> None:
        key_file = Path(tempfile.mkdtemp()) / "deepseek_api_key"
        key_file.write_text("sk-live-test", encoding="utf-8")
        settings = make_settings(model_adapter="deepseek",
                                 llm_api_key_file=str(key_file))
        settings.llm_api_style = LLM_PROVIDERS["deepseek"]["api_style"]
        settings.llm_base_url = LLM_PROVIDERS["deepseek"]["base_url"]
        settings.model_name = LLM_PROVIDERS["deepseek"]["model"]
        adapter = build_adapter(settings)
        self.assertIsInstance(adapter, OpenAICompatibleAdapter)
        self.assertEqual(adapter.base_url, "https://api.deepseek.com/v1")
        self.assertEqual(adapter.model, "deepseek-chat")
        self.assertEqual(settings.llm_api_key(), "sk-live-test")


if __name__ == "__main__":
    unittest.main()
