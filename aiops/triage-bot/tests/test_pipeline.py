"""The cascade: who gets asked, what is used, and what is never executed."""

from __future__ import annotations

import json
import unittest

from triage_bot.metrics import Metrics
from tests.stubs import (FakeLoki, FakePrometheus, SpyTelegram, StubAdapter,
                         alert_payload, make_pipeline, make_settings)


def escalation_text(reason: str = "правила не знают", proposal: str = "") -> str:
    payload = {"action_id": "escalate_human", "confidence": 0.3,
               "reason": reason, "evidence": ["no precedent in the ruleset"]}
    if proposal:
        payload["proposal"] = proposal
    return json.dumps(payload, ensure_ascii=False)


def action_text(action: str = "restart_deployment", confidence: float = 0.8) -> str:
    return json.dumps({
        "action_id": action, "confidence": confidence,
        "reason": "модель: перезапуск дешёвый и идемпотентный",
        "evidence": ["replicas_available[front-end] = 0"],
        "params": {"namespace": "sock-shop", "deployment": "front-end"},
    }, ensure_ascii=False)


class CascadeTests(unittest.TestCase):
    def test_confident_rules_never_call_the_model(self) -> None:
        settings = make_settings(model_adapter="deepseek")
        stub = StubAdapter(settings, text=action_text())
        pipeline = make_pipeline(settings=settings, model_adapter=stub)

        summary = pipeline.handle_webhook(alert_payload())

        self.assertEqual(summary["triaged"][0]["action_id"], "restart_deployment")
        self.assertEqual(stub.calls, [])
        incident = pipeline.find_incident(summary["triaged"][0]["id"])
        self.assertEqual(len(incident["tiers"]), 1)

    def test_escalating_rules_open_the_model_tier(self) -> None:
        settings = make_settings(model_adapter="deepseek")
        stub = StubAdapter(settings, text=action_text("rollback_deployment", 0.9),
                           usage={"prompt_tokens": 700, "completion_tokens": 40})
        pipeline = make_pipeline(settings=settings, model_adapter=stub)

        # ProbeFailed sits deliberately below the confidence threshold in the rules.
        summary = pipeline.handle_webhook(
            alert_payload(alertname="ProbeFailed", deployment=None))
        incident = pipeline.find_incident(summary["triaged"][0]["id"])

        self.assertEqual(len(stub.calls), 1)
        self.assertEqual(len(incident["tiers"]), 2)
        self.assertEqual(incident["final"]["action_id"], "rollback_deployment")
        self.assertEqual(incident["final"]["total_tokens"], 740)
        self.assertIn("подключена модель", incident["route_note"])

    def test_model_escalation_keeps_the_proposal_and_stays_inexecutable(self) -> None:
        settings = make_settings(model_adapter="deepseek")
        stub = StubAdapter(settings, text=escalation_text(proposal="проверить квоты"))
        pipeline = make_pipeline(settings=settings, model_adapter=stub)

        summary = pipeline.handle_webhook(alert_payload(alertname="ProbeFailed"))
        incident = pipeline.find_incident(summary["triaged"][0]["id"])

        self.assertFalse(incident["executable"])
        self.assertEqual(incident["final"]["proposal"], "проверить квоты")

    def test_missing_model_is_named_instead_of_hidden(self) -> None:
        settings = make_settings(model_adapter="deepseek")
        pipeline = make_pipeline(settings=settings, model_adapter=None)
        pipeline.model_unavailable_reason = "no key in /etc/aiops/secrets/deepseek_api_key"

        summary = pipeline.handle_webhook(alert_payload(alertname="ProbeFailed"))
        incident = pipeline.find_incident(summary["triaged"][0]["id"])

        self.assertIn("модель недоступна", incident["route_note"])
        self.assertIn("deepseek_api_key", incident["route_note"])
        self.assertFalse(incident["executable"])

    def test_eval_mode_measures_without_changing_the_decision(self) -> None:
        settings = make_settings(model_adapter="deepseek", eval_mode=True)
        stub = StubAdapter(settings, text=action_text("rollback_deployment", 0.95))
        pipeline = make_pipeline(settings=settings, model_adapter=stub)

        summary = pipeline.handle_webhook(alert_payload())
        incident = pipeline.find_incident(summary["triaged"][0]["id"])

        self.assertEqual(len(stub.calls), 1)
        self.assertEqual(len(incident["tiers"]), 2)
        self.assertEqual(incident["final"]["action_id"], "restart_deployment")
        self.assertIn("режим сравнения", incident["route_note"])

    def test_a_model_outage_becomes_an_escalation_not_an_exception(self) -> None:
        settings = make_settings(model_adapter="deepseek")
        stub = StubAdapter(settings, text="", fail="deepseek: HTTP 429: rate limited")
        pipeline = make_pipeline(settings=settings, model_adapter=stub)

        summary = pipeline.handle_webhook(alert_payload(alertname="ProbeFailed"))
        incident = pipeline.find_incident(summary["triaged"][0]["id"])

        self.assertFalse(incident["executable"])
        self.assertIn("429", incident["tiers"][-1]["error"])

    def test_prose_answer_is_counted_as_a_schema_violation(self) -> None:
        settings = make_settings(model_adapter="deepseek")
        stub = StubAdapter(settings, text="Я бы перезапустил front-end, наверное.")
        metrics = Metrics()
        pipeline = make_pipeline(settings=settings, model_adapter=stub, metrics=metrics)

        summary = pipeline.handle_webhook(alert_payload(alertname="ProbeFailed"))
        incident = pipeline.find_incident(summary["triaged"][0]["id"])

        self.assertTrue(any(name == "aiops_schema_violations"
                            for (name, _labels) in metrics.snapshot()))
        self.assertFalse(incident["executable"])
        self.assertTrue(incident["tiers"][-1]["validation_note"].startswith("not_json"))


class RetryAfterMalformedAnswerTests(unittest.TestCase):
    """One retry for a malformed answer, none for a forbidden one."""

    NO_EVIDENCE = json.dumps({"action_id": "escalate_human", "confidence": 0.5,
                              "reason": "нет данных"}, ensure_ascii=False)

    class Sequenced(StubAdapter):
        """Answers from a list, one per call - the first bad, the second good."""

        def __init__(self, settings, texts: list) -> None:
            super().__init__(settings, text="")
            self.texts = list(texts)

        def decide(self, system, user, alert, context):
            answer = super().decide(system, user, alert, context)
            answer.text = self.texts.pop(0) if self.texts else self.texts_last
            return answer

    def run_alert(self, texts: list):
        settings = make_settings(model_adapter="deepseek")
        stub = self.Sequenced(settings, texts)
        stub.texts_last = texts[-1]
        metrics = Metrics()
        pipeline = make_pipeline(settings=settings, model_adapter=stub, metrics=metrics)
        summary = pipeline.handle_webhook(alert_payload(alertname="ProbeFailed"))
        return stub, metrics, pipeline.find_incident(summary["triaged"][0]["id"])

    def test_a_missing_evidence_key_is_retried_once_and_the_retry_is_used(self) -> None:
        stub, metrics, incident = self.run_alert([self.NO_EVIDENCE, escalation_text()])
        self.assertEqual(len(stub.calls), 2)
        self.assertIn("REJECTED by the validator (schema)", stub.calls[1]["user"])
        self.assertIn("missing required keys", stub.calls[1]["user"])
        tier = incident["tiers"][-1]
        self.assertIn("retried after schema", tier["validation_note"])
        self.assertEqual(tier["decision"]["reason"], "правила не знают")
        self.assertTrue(any(name == "aiops_schema_retries"
                            for (name, _labels) in metrics.snapshot()))

    def test_a_second_bad_answer_escalates_and_is_not_retried_again(self) -> None:
        stub, _metrics, incident = self.run_alert([self.NO_EVIDENCE, self.NO_EVIDENCE])
        self.assertEqual(len(stub.calls), 2, "exactly one retry")
        tier = incident["tiers"][-1]
        self.assertIn("answer rejected by the validator", tier["decision"]["reason"])
        self.assertIn("after a retry", tier["validation_note"])
        self.assertFalse(incident["executable"])

    def test_a_forbidden_action_is_never_retried(self) -> None:
        bad = json.dumps({"action_id": "delete_namespace", "confidence": 0.9,
                          "reason": "x", "evidence": ["y"]})
        stub, _metrics, incident = self.run_alert([bad, escalation_text()])
        self.assertEqual(len(stub.calls), 1)
        self.assertFalse(incident["executable"])

    def test_a_good_first_answer_costs_one_call(self) -> None:
        stub, metrics, _incident = self.run_alert([escalation_text()])
        self.assertEqual(len(stub.calls), 1)
        self.assertFalse(any(name == "aiops_schema_retries"
                             for (name, _labels) in metrics.snapshot()))

    def test_the_prompt_says_evidence_is_never_omitted(self) -> None:
        from triage_bot.model import system_prompt
        self.assertIn("EVERY answer carries ALL four keys", system_prompt())


class FilterTests(unittest.TestCase):
    def test_resolved_and_unlabelled_alerts_are_skipped(self) -> None:
        pipeline = make_pipeline()
        summary = pipeline.handle_webhook(alert_payload(status="resolved"))
        self.assertEqual(summary["triaged"], [])
        self.assertEqual(summary["skipped"][0]["reason"], "resolved")

        summary = pipeline.handle_webhook(alert_payload(alertname="SomeOtherAlert",
                                                        aiops_label="false"))
        self.assertEqual(summary["triaged"], [])
        self.assertIn("no aiops label", summary["skipped"][0]["reason"])

    def test_duplicate_alert_is_triaged_once(self) -> None:
        pipeline = make_pipeline()
        first = pipeline.handle_webhook(alert_payload())
        second = pipeline.handle_webhook(alert_payload())
        self.assertEqual(len(first["triaged"]), 1)
        self.assertEqual(second["triaged"], [])
        self.assertEqual(second["skipped"][0]["reason"], "duplicate")


class GuardrailTests(unittest.TestCase):
    def test_execute_is_impossible_in_m1(self) -> None:
        pipeline = make_pipeline()
        with self.assertRaises(NotImplementedError):
            pipeline.execute({"action_id": "restart_deployment"})

    def test_approve_is_journalled_and_never_executed(self) -> None:
        telegram = SpyTelegram(enabled=True)
        pipeline = make_pipeline(telegram=telegram)
        incident = pipeline.triage(alert_payload()["alerts"][0])

        result = pipeline.handle_callback({
            "id": "cb1", "data": f"approve:{incident['id']}",
            "from": {"username": "vlad"}, "message": {"message_id": 42}})

        self.assertEqual(result["action"], "approve")
        decisions = [event for event in pipeline.journal.recent(10)
                     if event["kind"] == "human_decision"]
        self.assertEqual(len(decisions), 1)
        self.assertFalse(decisions[0]["executed"])
        self.assertEqual(decisions[0]["user"], "vlad")
        self.assertIn("rollout restart", decisions[0]["command"])
        self.assertEqual(telegram.cleared, [42])
        self.assertTrue(any("M1 не исполняет" in text for text in telegram.messages))

    def test_approve_is_refused_for_an_escalation(self) -> None:
        telegram = SpyTelegram(enabled=True)
        pipeline = make_pipeline(telegram=telegram)
        incident = pipeline.triage(
            alert_payload(alertname="NodeDiskPressure", deployment=None)["alerts"][0])
        self.assertFalse(incident["executable"])

        result = pipeline.handle_callback({
            "id": "cb2", "data": f"approve:{incident['id']}",
            "from": {"username": "vlad"}, "message": {"message_id": 43}})

        self.assertIsNone(result)
        self.assertTrue(any("нечего применять" in text
                            for _cid, text in telegram.answered))

    def test_ignore_snoozes_the_fingerprint(self) -> None:
        telegram = SpyTelegram(enabled=True)
        pipeline = make_pipeline(telegram=telegram)
        incident = pipeline.triage(alert_payload()["alerts"][0])
        pipeline.handle_callback({"id": "cb3", "data": f"ignore:{incident['id']}",
                                  "from": {"username": "vlad"},
                                  "message": {"message_id": 44}})

        summary = pipeline.handle_webhook(alert_payload())
        self.assertEqual(summary["triaged"], [])
        self.assertEqual(summary["skipped"][0]["reason"], "snoozed")


class FailureHandlingTests(unittest.TestCase):
    def test_unreachable_sources_do_not_break_triage(self) -> None:
        pipeline = make_pipeline(prometheus=FakePrometheus(fail=True),
                                 loki=FakeLoki(fail=True))
        summary = pipeline.handle_webhook(alert_payload())
        incident = pipeline.find_incident(summary["triaged"][0]["id"])

        self.assertEqual(incident["sources"], [])
        self.assertEqual(incident["final"]["action_id"], "restart_deployment")
        # Every failure is recorded, but a failing Prometheus is ONE line with a
        # count - not one line per query, which would drown the evidence.
        gaps = " ".join(incident["missing"])
        self.assertIn("prometheus:", gaps)
        self.assertIn("loki:", gaps)
        self.assertLessEqual(len([gap for gap in incident["missing"]
                                  if gap.startswith("prometheus")]), 2)
        self.assertTrue(any("queries failed" in gap for gap in incident["missing"]))

    def test_notification_is_journalled_even_when_telegram_is_off(self) -> None:
        pipeline = make_pipeline(telegram=SpyTelegram(enabled=False))
        pipeline.handle_webhook(alert_payload())
        notifications = [event for event in pipeline.journal.recent(10)
                         if event["kind"] == "notification"]
        self.assertEqual(len(notifications), 1)
        self.assertFalse(notifications[0]["sent"])
        self.assertFalse(notifications[0]["telegram_enabled"])


class FollowUpTests(unittest.TestCase):
    """The two buttons that answer a question instead of recording a verdict."""

    def _triaged(self, pipeline) -> dict:
        summary = pipeline.handle_webhook(alert_payload())
        return pipeline.find_incident(summary["triaged"][0]["id"])

    @staticmethod
    def _press(action: str, ident: str, callback_id: str = "cb1") -> dict:
        return {"id": callback_id, "data": f"{action}:{ident}",
                "from": {"username": "vlad"}, "message": {"message_id": 10}}

    def test_advice_asks_the_model_for_prose_and_executes_nothing(self) -> None:
        settings = make_settings(model_adapter="deepseek")
        stub = StubAdapter(settings, text=action_text(),
                           advice="Проверить:\nkubectl -n sock-shop get po")
        telegram = SpyTelegram(enabled=True)
        pipeline = make_pipeline(settings=settings, model_adapter=stub, telegram=telegram)
        incident = self._triaged(pipeline)

        result = pipeline.handle_callback(self._press("advice", incident["id"]))

        self.assertEqual(result["action"], "advice")
        # A prose call, not a second decision: the cascade is not re-run.
        self.assertEqual(stub.calls, [])
        self.assertEqual(len(stub.follow_ups), 1)
        self.assertIn(incident["alertname"], stub.follow_ups[0]["user"])
        self.assertIn("kubectl -n sock-shop get po", telegram.messages[-1])
        self.assertIn("ничего не исполнено", telegram.messages[-1])
        # The keyboard stays: after reading, a human can still press "отчёт".
        self.assertEqual(telegram.cleared, [])
        events = [event for event in pipeline.journal.recent(10)
                  if event.get("action") == "advice"]
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]["executed"])

    def test_a_double_press_does_not_pay_for_the_model_twice(self) -> None:
        settings = make_settings(model_adapter="deepseek")
        stub = StubAdapter(settings, text=action_text())
        telegram = SpyTelegram(enabled=True)
        pipeline = make_pipeline(settings=settings, model_adapter=stub, telegram=telegram)
        incident = self._triaged(pipeline)

        pipeline.handle_callback(self._press("advice", incident["id"], "cb1"))
        second = pipeline.handle_callback(self._press("advice", incident["id"], "cb2"))

        self.assertIsNone(second)
        self.assertEqual(len(stub.follow_ups), 1)
        self.assertEqual(telegram.answered[-1][1], "разбор уже идёт — секунду")

    def test_advice_without_a_model_says_so_instead_of_faking_it(self) -> None:
        telegram = SpyTelegram(enabled=True)
        pipeline = make_pipeline(telegram=telegram)
        incident = self._triaged(pipeline)

        pipeline.handle_callback(self._press("advice", incident["id"]))

        self.assertIn("модель не подключена", telegram.messages[-1])
        self.assertIn("карточке выше", telegram.messages[-1])

    def test_a_failing_model_is_reported_not_swallowed(self) -> None:
        settings = make_settings(model_adapter="deepseek")
        stub = StubAdapter(settings, text=action_text(), fail="HTTP 503: overloaded")
        telegram = SpyTelegram(enabled=True)
        pipeline = make_pipeline(settings=settings, model_adapter=stub, telegram=telegram)
        incident = self._triaged(pipeline)

        pipeline.handle_callback(self._press("advice", incident["id"]))

        self.assertIn("модель не ответила", telegram.messages[-1])
        self.assertIn("503", telegram.messages[-1])
        # The press is still recorded: a failed follow-up is an event too.
        events = [event for event in pipeline.journal.recent(10)
                  if event.get("action") == "advice"]
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]["model_asked"] is False)  # adapter was configured

    def test_report_is_sent_as_a_markdown_document_with_the_human_actions(self) -> None:
        settings = make_settings(model_adapter="deepseek")
        stub = StubAdapter(settings, text=action_text())
        telegram = SpyTelegram(enabled=True)
        pipeline = make_pipeline(settings=settings, model_adapter=stub, telegram=telegram)
        incident = self._triaged(pipeline)
        pipeline.handle_callback(self._press("advice", incident["id"], "cb1"))

        result = pipeline.handle_callback(self._press("report", incident["id"], "cb2"))

        self.assertTrue(result["sent"])
        document = telegram.documents[0]
        self.assertTrue(document["filename"].endswith(".md"))
        self.assertIn(incident["id"], document["filename"])
        self.assertIn("📄", document["caption"])
        body = document["content"]
        self.assertIn(f"# Инцидент #{incident['id']}", body)
        self.assertIn(incident["final"]["reason"], body)
        self.assertIn(incident["final"]["command"], body)
        self.assertIn("## Действия человека", body)
        self.assertIn("advice", body)
        self.assertIn("**vlad**", body)
        # The command came from the card; the report must not imply it was chosen.
        self.assertIn("шаг:", body)

    def test_a_rejected_document_is_reported_back(self) -> None:
        telegram = SpyTelegram(enabled=False)
        pipeline = make_pipeline(telegram=telegram)
        incident = self._triaged(pipeline)

        result = pipeline.handle_callback(self._press("report", incident["id"]))

        self.assertFalse(result["sent"])
        self.assertIn("файл не ушёл", telegram.messages[-1])
        events = [event for event in pipeline.journal.recent(10)
                  if event.get("action") == "report"]
        self.assertFalse(events[0]["sent"])


if __name__ == "__main__":
    unittest.main()
