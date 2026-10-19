"""The contract tests: everything a model is *not* allowed to do."""

from __future__ import annotations

import unittest

from triage_bot import model as model_mod
from tests.stubs import make_settings


def valid(**overrides) -> dict:
    payload = {
        "action_id": "restart_deployment",
        "confidence": 0.9,
        "reason": "rolling restart",
        "evidence": ["replicas_available = 0"],
        "params": {"namespace": "sock-shop", "deployment": "front-end"},
    }
    payload.update(overrides)
    return payload


class ValidateDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = make_settings()

    def test_valid_action_passes(self) -> None:
        decision = model_mod.validate_decision(valid(), self.settings, adapter="mock")
        self.assertEqual(decision.action_id, "restart_deployment")
        self.assertFalse(decision.escalated)
        self.assertEqual(
            decision.to_dict()["command"],
            "kubectl -n sock-shop rollout restart deployment/front-end")

    def test_unknown_action_is_rejected(self) -> None:
        with self.assertRaises(model_mod.DecisionError) as ctx:
            model_mod.validate_decision(valid(action_id="delete_everything"), self.settings)
        self.assertEqual(ctx.exception.kind, "unknown_action")

    def test_extra_keys_are_rejected(self) -> None:
        with self.assertRaises(model_mod.DecisionError) as ctx:
            model_mod.validate_decision(valid(kubectl="rm -rf /"), self.settings)
        self.assertEqual(ctx.exception.kind, "schema")

    def test_low_confidence_escalates(self) -> None:
        with self.assertRaises(model_mod.DecisionError) as ctx:
            model_mod.validate_decision(valid(confidence=0.5), self.settings)
        self.assertEqual(ctx.exception.kind, "low_confidence")

    def test_escalation_is_allowed_below_threshold(self) -> None:
        decision = model_mod.validate_decision(
            {"action_id": "escalate_human", "confidence": 0.1,
             "reason": "not sure", "evidence": ["nothing to go on"]}, self.settings)
        self.assertTrue(decision.escalated)

    def test_replicas_out_of_range_is_rejected(self) -> None:
        for replicas in (0, 11, -1):
            with self.assertRaises(model_mod.DecisionError) as ctx:
                model_mod.validate_decision(
                    valid(action_id="scale_deployment",
                          params={"namespace": "sock-shop", "deployment": "front-end",
                                  "replicas": replicas}), self.settings)
            self.assertEqual(ctx.exception.kind, "bad_params")

    def test_namespace_outside_allowlist_is_rejected(self) -> None:
        with self.assertRaises(model_mod.DecisionError) as ctx:
            model_mod.validate_decision(
                valid(params={"namespace": "kube-system", "deployment": "coredns"}),
                self.settings)
        self.assertEqual(ctx.exception.kind, "bad_params")

    def test_command_injection_in_the_name_is_rejected(self) -> None:
        with self.assertRaises(model_mod.DecisionError) as ctx:
            model_mod.validate_decision(
                valid(params={"namespace": "sock-shop",
                              "deployment": "front-end; rm -rf /"}), self.settings)
        self.assertEqual(ctx.exception.kind, "bad_params")

    def test_proposal_is_allowed_only_with_escalation(self) -> None:
        decision = model_mod.validate_decision(
            {"action_id": "escalate_human", "confidence": 0.2, "reason": "unknown",
             "evidence": ["no data"], "proposal": "почистить диск и повторить"},
            self.settings)
        self.assertEqual(decision.proposal, "почистить диск и повторить")
        with self.assertRaises(model_mod.DecisionError) as ctx:
            model_mod.validate_decision(valid(proposal="а ещё сделай вот это"), self.settings)
        self.assertEqual(ctx.exception.kind, "schema")

    def test_proposal_is_truncated(self) -> None:
        decision = model_mod.validate_decision(
            {"action_id": "escalate_human", "confidence": 0.2, "reason": "x",
             "evidence": ["y"], "proposal": "z" * 900}, self.settings)
        self.assertEqual(len(decision.proposal), model_mod.MAX_PROPOSAL_CHARS)

    def test_evidence_must_be_present_and_trimmed(self) -> None:
        with self.assertRaises(model_mod.DecisionError):
            model_mod.validate_decision(valid(evidence=[]), self.settings)
        decision = model_mod.validate_decision(
            valid(evidence=["  fact one  ", "", "fact two"]), self.settings)
        self.assertEqual(decision.evidence, ["fact one", "fact two"])


class ParseTests(unittest.TestCase):
    def test_plain_json(self) -> None:
        self.assertEqual(model_mod.parse_decision_json('{"a": 1}'), {"a": 1})

    def test_fenced_json_is_tolerated(self) -> None:
        text = '```json\n{"action_id": "escalate_human"}\n```'
        self.assertEqual(model_mod.parse_decision_json(text)["action_id"],
                         "escalate_human")

    def test_prose_is_not_json(self) -> None:
        with self.assertRaises(model_mod.DecisionError) as ctx:
            model_mod.parse_decision_json("I think you should restart it.")
        self.assertEqual(ctx.exception.kind, "not_json")

    def test_array_is_not_an_object(self) -> None:
        with self.assertRaises(model_mod.DecisionError) as ctx:
            model_mod.parse_decision_json("[1, 2, 3]")
        self.assertEqual(ctx.exception.kind, "schema")


class PromptTests(unittest.TestCase):
    def test_prompt_lists_every_allowed_action(self) -> None:
        prompt = model_mod.system_prompt()
        for action_id in model_mod.ACTIONS:
            self.assertIn(action_id, prompt)

    def test_prompt_keeps_braces_and_has_no_placeholder(self) -> None:
        prompt = model_mod.system_prompt()
        self.assertIn('{"action_id"', prompt)
        self.assertNotIn("{action_ids}", prompt)

    def test_user_prompt_contains_evidence_and_gaps(self) -> None:
        alert = {"labels": {"alertname": "X", "namespace": "sock-shop"},
                 "annotations": {"summary": "s"}, "status": "firing"}
        context = {"prometheus": {"replicas_available": "0"}, "loki": ["line one"],
                   "missing": ["loki: no lines"]}
        text = model_mod.build_user_prompt(alert, context)
        self.assertIn("replicas_available", text)
        self.assertIn("line one", text)
        self.assertIn("[missing]", text)


if __name__ == "__main__":
    unittest.main()
