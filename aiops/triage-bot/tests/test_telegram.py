"""The card a human reads, the buttons they press, and the HTML that must survive.

These tests exist because a notification is a *user interface*: an unescaped
`<` in a log line used to turn a perfectly good decision into a Telegram 400,
and an approve button on an escalation teaches people to click without reading.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from triage_bot.telegram import TelegramClient, build_keyboard, esc, render_card
from tests.stubs import make_settings


def sample_incident(**overrides) -> dict:
    incident = {
        "id": "1a2b3c4d",
        "alertname": "DeploymentReplicasUnavailable",
        "labels": {"alertname": "DeploymentReplicasUnavailable", "severity": "critical",
                   "namespace": "sock-shop", "deployment": "front-end"},
        "annotations": {"summary": "deployment/front-end has no available replicas"},
        "tiers": [{
            "tier": "rules", "adapter": "mock", "latency_ms": 0,
            "decision": {
                "action_id": "restart_deployment", "confidence": 0.85,
                "reason": "rolling restart", "evidence": ["replicas_available = 0"],
                "command": "kubectl -n sock-shop rollout restart deployment/front-end",
                "proposal": "", "escalated": False,
            },
        }],
        "final": {
            "action_id": "restart_deployment", "confidence": 0.85,
            "reason": "rolling restart", "evidence": ["replicas_available = 0"],
            "command": "kubectl -n sock-shop rollout restart deployment/front-end",
            "proposal": "", "escalated": False, "adapter": "mock",
        },
        "executable": True,
        "sources": ["loki", "prometheus"],
        "missing": [],
    }
    incident.update(overrides)
    return incident


class EscapingTests(unittest.TestCase):
    def test_esc_escapes_html_metacharacters(self) -> None:
        self.assertEqual(esc('<b>&"x"'), "&lt;b&gt;&amp;&quot;x&quot;")

    def test_card_escapes_log_lines_and_model_text(self) -> None:
        incident = sample_incident(final={
            **sample_incident()["final"],
            "evidence": ["log: <script>alert(1)</script> & <boom>"],
            "reason": "5 > 3 & <b>bold</b>",
        })
        card = render_card(incident)
        self.assertNotIn("<script>", card)
        self.assertIn("&lt;script&gt;", card)
        self.assertNotIn("<boom>", card)
        self.assertIn("&amp;", card)

    def test_proposal_is_flagged_as_not_executable(self) -> None:
        incident = sample_incident(executable=False, final={
            **sample_incident()["final"],
            "action_id": "escalate_human", "command": "", "escalated": True,
            "proposal": "почистить диск на узле и повторить",
        })
        card = render_card(incident)
        self.assertIn("ПРЕДЛОЖЕНИЕ ВНЕ WHITELIST", card)
        self.assertIn("не исполняется", card)


class KeyboardTests(unittest.TestCase):
    def test_action_gets_three_buttons_including_approve(self) -> None:
        keyboard = build_keyboard("1a2b3c4d", executable=True)
        datas = [button["callback_data"] for button in keyboard[0]]
        self.assertEqual(datas, ["approve:1a2b3c4d", "advice:1a2b3c4d", "ignore:1a2b3c4d"])

    def test_escalation_has_no_approve_button(self) -> None:
        keyboard = build_keyboard("1a2b3c4d", executable=False)
        datas = [button["callback_data"] for button in keyboard[0]]
        self.assertEqual(datas, ["advice:1a2b3c4d", "ignore:1a2b3c4d"])


class CardContentTests(unittest.TestCase):
    def test_card_shows_both_tiers_when_both_answered(self) -> None:
        incident = sample_incident(tiers=[
            sample_incident()["tiers"][0],
            {"tier": "model", "adapter": "deepseek", "latency_ms": 4100,
             "decision": {"action_id": "escalate_human", "confidence": 0.72}},
        ], route_note="правила не дали уверенного ответа → подключена модель (deepseek)")
        card = render_card(incident)
        self.assertIn("варианты", card)
        self.assertIn("правила (tier 0)", card)
        self.assertIn("модель (tier 1)", card)
        self.assertIn("escalate_human", card)
        self.assertIn("подключена модель", card)

    def test_card_reports_context_and_gaps(self) -> None:
        incident = sample_incident(missing=["loki: no log lines in the window"])
        card = render_card(incident)
        self.assertIn("контекст: loki, prometheus", card)
        self.assertIn("пробелы", card)
        self.assertIn("no log lines", card)

    def test_card_keeps_its_html_tags_balanced(self) -> None:
        # Telegram rejects an unbalanced subset of HTML, so the tags the card
        # opens have to be closed - easy to check, expensive to discover live.
        card = render_card(sample_incident())
        for tag in ("b", "i", "code"):
            self.assertEqual(card.count(f"<{tag}>"), card.count(f"</{tag}>"), tag)


class RateLimitTests(unittest.TestCase):
    def test_messages_are_suppressed_above_the_hourly_ceiling(self) -> None:
        settings = make_settings(telegram_chat_id="42", telegram_max_per_hour=2)
        token_file = Path(tempfile.mkdtemp()) / "token"
        token_file.write_text("123:ABC", encoding="utf-8")
        settings.telegram_token_file = str(token_file)

        sent: list = []
        client = TelegramClient(settings)
        client._call = lambda method, payload, timeout=15.0: sent.append(method) or {}
        self.assertTrue(client.enabled)

        self.assertTrue(client.send_message("one"))
        self.assertTrue(client.send_message("two"))
        self.assertFalse(client.send_message("three"))
        self.assertEqual(sent, ["sendMessage", "sendMessage"])

    def test_disabled_client_is_a_no_op(self) -> None:
        client = TelegramClient(make_settings(telegram_chat_id=""))
        self.assertFalse(client.enabled)
        self.assertFalse(client.send_message("nobody is listening"))


if __name__ == "__main__":
    unittest.main()
