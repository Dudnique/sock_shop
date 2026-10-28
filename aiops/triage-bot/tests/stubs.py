"""Shared test doubles: no network, no cluster, no API keys.

Every test in this directory runs on the standard library alone and finishes in
well under a second, which is the point: CI can run them on every push, and a
failing test always means *our* code is wrong rather than a provider being down.
"""

from __future__ import annotations

import tempfile
import time

from triage_bot.adapters import Adapter, ModelAnswer
from triage_bot.config import Settings
from triage_bot.context import SourceError
from triage_bot.journal import Journal
from triage_bot.metrics import Metrics


def make_settings(**overrides) -> Settings:
    settings = Settings(
        journal_dir=tempfile.mkdtemp(prefix="aiops-test-"),
        telegram_chat_id="",
        model_name="mock-deterministic-v1",
        confidence_threshold=0.7,
    )
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


class FakePrometheus:
    def __init__(self, answers: dict | None = None, fail: bool = False) -> None:
        self.answers = answers or {}
        self.fail = fail
        self.queries: list = []

    def query(self, expression: str) -> list:
        self.queries.append(expression)
        if self.fail:
            raise SourceError("prometheus is not reachable (stub)")
        for needle, value in self.answers.items():
            if needle in expression:
                if isinstance(value, list):  # a ready-made answer, labels included
                    return value
                return [{"metric": {}, "value": str(value)}]
        return []


class FakeLoki:
    def __init__(self, lines: list | None = None, fail: bool = False) -> None:
        self.lines = lines or []
        self.fail = fail
        self.queries: list = []

    def log_lines(self, logql: str, window_seconds: int, limit: int) -> list:
        self.queries.append(logql)
        if self.fail:
            raise SourceError("loki is not reachable (stub)")
        return list(self.lines)[:limit]


class SpyTelegram:
    """Records what would have been sent; never touches the network."""

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled
        self.messages: list = []
        self.keyboards: list = []
        self.answered: list = []
        self.cleared: list = []
        self.documents: list = []

    def send_message(self, text: str, keyboard=None) -> bool:
        self.messages.append(text)
        self.keyboards.append(keyboard)
        return self.enabled

    def send_document(self, filename: str, content, caption: str = "") -> bool:
        self.documents.append({"filename": filename, "content": content,
                               "caption": caption})
        return self.enabled

    def answer_callback(self, callback_id: str, text: str) -> None:
        self.answered.append((callback_id, text))

    def clear_keyboard(self, message_id) -> None:
        self.cleared.append(message_id)


class StubAdapter(Adapter):
    """A model stand-in that returns whatever the test tells it to."""

    def __init__(self, settings, text: str, name: str = "stub", usage: dict | None = None,
                 fail: str = "", advice: str = "") -> None:
        super().__init__(settings)
        self.name = name
        self.text = text
        self.advice = advice
        self.usage = usage or {}
        self.fail = fail
        self.calls: list = []
        self.follow_ups: list = []

    def decide(self, system: str, user: str, alert: dict, context: dict) -> ModelAnswer:
        self.calls.append({"system": system, "user": user})
        if self.fail:
            from triage_bot.adapters import AdapterError
            raise AdapterError(self.fail)
        return ModelAnswer(text=self.text, adapter=self.name, model="stub-model",
                           latency_ms=7, usage=self.usage)

    def answer(self, system: str, user: str) -> ModelAnswer:
        """The prose path: recorded separately from `decide` on purpose."""
        self.follow_ups.append({"system": system, "user": user})
        if self.fail:
            from triage_bot.adapters import AdapterError
            raise AdapterError(self.fail)
        return ModelAnswer(text=self.advice or "гипотеза: сети, а не приложение",
                           adapter=self.name, model="stub-model", latency_ms=5,
                           usage=self.usage)


def make_pipeline(settings=None, model_adapter=None, telegram=None, prometheus=None,
                  loki=None, metrics=None):
    from triage_bot.pipeline import TriagePipeline
    settings = settings or make_settings()
    journal = Journal(settings.journal_dir)
    pipeline = TriagePipeline(settings, metrics or Metrics(), journal,
                              prometheus or FakePrometheus({"replicas_available": 0}),
                              loki or FakeLoki(["12:00:00 front-end-1: boom"]),
                              telegram or SpyTelegram())
    pipeline.model_adapter = model_adapter
    return pipeline


def alert_payload(alertname: str = "DeploymentReplicasUnavailable", status: str = "firing",
                  aiops_label: str = "true", **labels) -> dict:
    """An Alertmanager webhook body, as close to the real shape as it matters."""
    label_set = {"alertname": alertname, "severity": "critical",
                 "namespace": "sock-shop", "deployment": "front-end",
                 "aiops": aiops_label}
    label_set.update(labels)
    label_set = {key: value for key, value in label_set.items() if value is not None}
    return {
        "version": "4",
        "status": status,
        "groupLabels": {"alertname": alertname},
        "commonLabels": label_set,
        "alerts": [{
            "status": status,
            "labels": label_set,
            "annotations": {"summary": f"{alertname} on front-end",
                            "description": "no available replicas",
                            "runbook": "kubectl -n sock-shop describe deploy/front-end"},
            "startsAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "fingerprint": "aabbccddeeff0011",
        }],
    }
