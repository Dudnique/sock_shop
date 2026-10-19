"""The report file: what a person opens when the chat is gone.

The card is an interface, the report is a record - and these tests pin the
difference. The tier-by-tier comparison the card deliberately omits has to be
here, because this reader is asking "on what grounds was this decided?" rather
than "what do I do right now?".
"""

from __future__ import annotations

import unittest

from triage_bot.report import build_report


def incident(**overrides) -> dict:
    base = {
        "id": "1a2b3c4d",
        "ts": 1_760_000_000,
        "alertname": "DeploymentReplicasUnavailable",
        "labels": {"severity": "critical", "namespace": "sock-shop",
                   "deployment": "front-end"},
        "annotations": {"summary": "front-end has no available replicas",
                        "runbook": "kubectl -n sock-shop get deploy,po"},
        "fingerprint": "abc123",
        "tiers": [
            {"tier": "rules", "adapter": "mock", "tokens": 0,
             "decision": {"action_id": "escalate_human", "confidence": 0.5}},
            {"tier": "model", "adapter": "deepseek", "tokens": 812,
             "decision": {"action_id": "restart_deployment", "confidence": 0.82}},
        ],
        "final": {
            "action_id": "restart_deployment", "confidence": 0.82,
            "reason": "0 доступных реплик при 1 желаемой",
            "evidence": ["replicas_available[front-end] = 0"],
            "command": "kubectl -n sock-shop rollout restart deployment/front-end",
            "proposal": "", "escalated": False, "adapter": "deepseek",
            "total_tokens": 812,
        },
        "executable": True,
        "route_note": "правила не дали уверенного ответа → подключена модель (deepseek)",
        "sources": ["loki", "prometheus"],
        "missing": ["loki: no log lines in the window"],
        "context_ms": 812,
        "dry_run": True,
    }
    base.update(overrides)
    return base


class ReportTests(unittest.TestCase):
    def test_reads_cause_then_grounds_then_the_step(self) -> None:
        report = build_report(incident())
        self.assertIn("# Инцидент #1a2b3c4d · DeploymentReplicasUnavailable", report)
        self.assertLess(report.index("## Почему"), report.index("## На чём это основано"))
        self.assertLess(report.index("## На чём это основано"), report.index("## Что сделать"))
        self.assertIn("rollout restart deployment/front-end", report)
        self.assertIn("## Не хватило данных", report)

    def test_keeps_the_cascade_that_the_card_omits(self) -> None:
        """The card hides tier comparison on purpose; the report is where it lives."""
        report = build_report(incident())
        self.assertIn("## Каскад", report)
        self.assertIn("| rules | mock | escalate_human | 0.5 | 0 |", report)
        self.assertIn("| model | deepseek | restart_deployment | 0.82 | 812 |", report)

    def test_lists_human_presses_in_chronological_order(self) -> None:
        events = [
            {"ts": 1_760_000_300, "kind": "human_decision", "action": "ignore",
             "user": "vlad"},
            {"ts": 1_760_000_100, "kind": "notification", "sent": True},
            {"ts": 1_760_000_200, "kind": "human_decision", "action": "advice",
             "user": "vlad"},
        ]
        report = build_report(incident(), events)
        self.assertIn("## Действия человека", report)
        self.assertLess(report.index("advice"), report.index("ignore"))
        # The journal is append-only, so a notification is an event too.
        self.assertIn("notification", report)

    def test_an_escalation_is_named_as_one(self) -> None:
        report = build_report(incident(executable=False, final={
            "action_id": "escalate_human", "confidence": 0.4,
            "reason": "нет данных о причине", "evidence": [], "command": "",
            "proposal": "проверить диск на узле", "escalated": True}))
        self.assertIn("эскалация: бот не предлагает действий", report)
        self.assertIn("## Предложение вне whitelist", report)
        self.assertIn("Не исполняется", report)
        # No whitelisted command, so the alert's own runbook is the concrete step.
        self.assertIn("## Что проверить (runbook алерта)", report)

    def test_a_pipe_in_the_model_text_does_not_break_a_table(self) -> None:
        report = build_report(incident(final={
            **incident()["final"], "validation_note": "params | dropped"}))
        self.assertIn("params \\| dropped", report)

    def test_an_empty_journal_still_produces_a_report(self) -> None:
        report = build_report(incident(annotations={}, missing=[], tiers=[],
                                       sources=[], fingerprint=""))
        self.assertNotIn("## Действия человека", report)
        self.assertNotIn("## Каскад", report)
        self.assertIn("(у алерта нет summary)", report)


if __name__ == "__main__":
    unittest.main()
