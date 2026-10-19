"""The triage cascade: webhook in, human approval out.

The order is deliberate and is the whole design:

1. **Tier 0 - the ruleset** (`MockAdapter`). Free, instant, deterministic, and it
   never sends anything off the machine. It answers the alerts this stand
   actually fires.
2. **Tier 1 - a model**, asked *only* when tier 0 answers "I don't know"
   (`escalate_human`) or when `EVAL_MODE` is on. DeepSeek, OpenAI or a local
   Ollama - the pipeline does not care which, only that the answer survives
   validation.
3. **Validation**, identical for both tiers: strict JSON, action id from the
   whitelist, parameters in range, confidence above the threshold. A rejected
   answer becomes an escalation and is counted
   (`aiops_schema_violations{kind=...}`), because a prompt that produces invalid
   answers is a defect to be measured, not a mystery to be retried.

`EVAL_MODE` is measurement, not behaviour: the model is called even when the
ruleset answered, both answers are stored and shown, and the *ruleset's* answer
is still the one that counts. That is how "the model is worth its price" becomes
a number - `tier0_share`, agreement, tokens, latency - instead of an opinion.

There is no execution path in this file. `execute()` exists to say so out loud.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict

from . import model as model_mod
from .adapters import AdapterError, MockAdapter, ModelAnswer, build_adapter
from .config import Settings
from .context import LokiClient, PrometheusClient, collect_context
from .journal import Journal
from .telegram import (DECISIONS, TelegramClient, build_keyboard, esc, render_card)

log = logging.getLogger("aiops.pipeline")

#: How long an "ignore" press silences a fingerprint.
SNOOZE_SECONDS = 4 * 3600


def incident_id(alert: dict) -> str:
    """A short, stable id: the same alert keeps the same id for a human's eyes."""
    parts = (alert.get("fingerprint", "") or "",
             (alert.get("labels") or {}).get("alertname", ""),
             (alert.get("labels") or {}).get("namespace", ""),
             (alert.get("labels") or {}).get("deployment", ""),
             (alert.get("labels") or {}).get("pod", ""),
             (alert.get("labels") or {}).get("node", ""))
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:8]


class TriagePipeline:
    def __init__(self, settings: Settings, metrics, journal: Journal,
                 prometheus: PrometheusClient, loki: LokiClient,
                 telegram: TelegramClient) -> None:
        self.settings = settings
        self.metrics = metrics
        self.journal = journal
        self.prometheus = prometheus
        self.loki = loki
        self.telegram = telegram
        self.rule_adapter = MockAdapter(settings)
        self.model_adapter = None
        self.model_unavailable_reason = ""
        if settings.llm_api_style != "mock":
            try:
                self.model_adapter = build_adapter(settings)
            except AdapterError as exc:
                self.model_unavailable_reason = str(exc)
                log.error("model tier unavailable: %s", exc)
        self._recent: "OrderedDict[str, dict]" = OrderedDict()

    # --- the guardrail, stated in code ---------------------------------------
    def execute(self, decision) -> None:
        """M2's entry point - and the proof that M1 cannot act.

        This pod has no ServiceAccount token, no Kubernetes client and no
        `kubectl`; even if a model returned the perfect command, there is nothing
        here that could run it. M2 will add exactly this one method, behind a
        scoped ServiceAccount and a second human confirmation.
        """
        raise NotImplementedError(
            "M1 is an advisor: no ServiceAccount, no Kubernetes API, no execution")

    # --- helpers -------------------------------------------------------------
    def _remember(self, incident: dict) -> None:
        self._recent[incident["id"]] = incident
        while len(self._recent) > 200:
            self._recent.popitem(last=False)

    def find_incident(self, ident: str) -> dict | None:
        incident = self._recent.get(ident)
        if incident:
            return incident
        for record in self.journal.recent(200):
            if record.get("kind") == "incident" and record.get("incident", {}).get("id") == ident:
                return record["incident"]
        return None

    # --- tier 1 call ---------------------------------------------------------
    def _ask(self, adapter, alert: dict, context: dict, tier: str) -> dict:
        """Call one adapter and validate its answer - never trusting it."""
        labels = alert.get("labels") or {}
        alertname = labels.get("alertname", "")
        try:
            answer: ModelAnswer = adapter.decide(
                model_mod.system_prompt(), model_mod.build_user_prompt(alert, context),
                alert, context)
        except AdapterError as exc:
            self.metrics.inc("aiops_model_errors", adapter=adapter.name)
            log.error("adapter %s failed: %s", adapter.name, exc)
            return {"tier": tier, "adapter": adapter.name, "model": "",
                    "latency_ms": 0, "error": str(exc),
                    "decision": model_mod.escalate(
                        f"model unavailable: {exc}", adapter=adapter.name).to_dict()}

        self.metrics.inc("aiops_model_calls", adapter=answer.adapter)
        try:
            payload = model_mod.parse_decision_json(answer.text)
            decision = model_mod.validate_decision(
                payload, self.settings, adapter=answer.adapter, model=answer.model)
            note = ""
        except model_mod.DecisionError as exc:
            # A rejection is data: it counts, it is visible, and it escalates.
            self.metrics.inc("aiops_schema_violations", kind=exc.kind)
            decision = model_mod.escalate(
                f"answer rejected by the validator ({exc.kind}): {exc.message}",
                adapter=answer.adapter, model=answer.model, raw={"raw_answer": answer.text[:500]})
            note = exc.kind
        return {
            "tier": tier,
            "adapter": answer.adapter,
            "model": answer.model,
            "latency_ms": answer.latency_ms,
            "tokens": answer.prompt_tokens + answer.completion_tokens,
            "validation_note": note,
            "alertname": alertname,
            "decision": decision.to_dict(),
        }

    # --- the cascade ---------------------------------------------------------
    def triage(self, alert: dict) -> dict:
        labels = alert.get("labels") or {}
        annotations = alert.get("annotations") or {}
        ident = incident_id(alert)
        context = collect_context(alert, self.settings, self.prometheus, self.loki,
                                  metrics=self.metrics)

        tiers = [self._ask(self.rule_adapter, alert, context, tier="rules")]
        rules_escalated = bool(tiers[0]["decision"].get("escalated"))
        route_note = ""
        final = tiers[0]["decision"]

        if rules_escalated and self.model_adapter is not None:
            route_note = ("правила не дали уверенного ответа → подключена модель "
                          f"({self.settings.model_adapter})")
            tiers.append(self._ask(self.model_adapter, alert, context, tier="model"))
            final = tiers[-1]["decision"]
            self.metrics.inc("aiops_incidents_tier", tier="model")
        elif rules_escalated and self.model_adapter is None:
            route_note = ("правила не дали уверенного ответа, а модель недоступна: "
                          f"{self.model_unavailable_reason or 'не задана'}")
            self.metrics.inc("aiops_incidents_tier", tier="rules_only")
        elif self.settings.eval_mode and self.model_adapter is not None:
            # Measurement must not change behaviour: the model is called, both
            # answers are kept, and the ruleset's decision still stands.
            route_note = "режим сравнения: модель вызвана для замера, решает правила"
            tiers.append(self._ask(self.model_adapter, alert, context, tier="model"))
            self.metrics.inc("aiops_incidents_tier", tier="rules")
        else:
            self.metrics.inc("aiops_incidents_tier", tier="rules")

        total_tokens = sum(int(tier.get("tokens") or 0) for tier in tiers)
        if total_tokens:
            final["total_tokens"] = total_tokens

        incident = {
            "id": ident,
            "ts": time.time(),
            "status": alert.get("status", ""),
            "alertname": labels.get("alertname", ""),
            "labels": labels,
            "annotations": {key: annotations[key] for key in
                            ("summary", "description", "runbook") if key in annotations},
            "fingerprint": alert.get("fingerprint", ""),
            "tiers": tiers,
            "final": final,
            "executable": not final.get("escalated", False),
            "route_note": route_note,
            "sources": context.get("sources", []),
            "missing": context.get("missing", []),
            "context_ms": context.get("duration_ms", 0),
            "dry_run": True,
        }

        self.metrics.inc("aiops_incidents", action_id=final.get("action_id", "?"))
        if incident["executable"]:
            self.journal.append_event("incident", {"incident": incident})
        else:
            self.metrics.inc("aiops_escalations",
                             reason=final.get("escalation_reason", "unknown")[:60])
            self.journal.append_event("incident", {"incident": incident})
        self.metrics.set("aiops_last_incident_timestamp_seconds", time.time())
        self._remember(incident)
        return incident

    # --- dedupe / snooze ------------------------------------------------------
    @staticmethod
    def _dedupe_key(alert: dict) -> str:
        labels = alert.get("labels") or {}
        return alert.get("fingerprint") or "|".join((
            labels.get("alertname", ""), labels.get("namespace", ""),
            labels.get("deployment", ""), labels.get("pod", ""), labels.get("node", "")))

    def _snoozed(self, alert: dict) -> bool:
        """Has a human pressed 'ignore' for this fingerprint recently?"""
        state = self.journal.load_state()
        snooze = state.get("snooze") or {}
        expiry = snooze.get(self._dedupe_key(alert))
        if not expiry:
            return False
        if float(expiry) < time.time():
            snooze.pop(self._dedupe_key(alert), None)
            state["snooze"] = snooze
            self.journal.save_state(state)
            return False
        return True

    def snooze(self, key: str, seconds: int = SNOOZE_SECONDS) -> None:
        state = self.journal.load_state()
        snooze = state.get("snooze") or {}
        snooze[key] = time.time() + seconds
        state["snooze"] = snooze
        self.journal.save_state(state)

    # --- Alertmanager entry point --------------------------------------------
    def handle_webhook(self, payload: dict) -> dict:
        """One Alertmanager POST: every alert is filtered, deduped and triaged."""
        alerts = payload.get("alerts") or []
        summary: dict = {"received": len(alerts), "triaged": [], "skipped": []}
        for alert in alerts:
            labels = alert.get("labels") or {}
            status = alert.get("status", "") or "unknown"
            alertname = labels.get("alertname", "")
            self.metrics.inc("aiops_alerts_received", status=status)
            if status != "firing":
                summary["skipped"].append({"alertname": alertname, "reason": "resolved"})
                self.metrics.inc("aiops_alerts_skipped", reason="resolved")
                continue
            if (self.settings.require_label
                    and labels.get(self.settings.require_label) != "true"):
                summary["skipped"].append({"alertname": alertname,
                                           "reason": f"no {self.settings.require_label} label"})
                self.metrics.inc("aiops_alerts_skipped", reason="not_labelled")
                continue
            if self._snoozed(alert):
                summary["skipped"].append({"alertname": alertname, "reason": "snoozed"})
                self.metrics.inc("aiops_alerts_skipped", reason="snoozed")
                continue
            if not self.journal.dedupe_check_and_record(
                    self._dedupe_key(alert), self.settings.dedupe_window_seconds):
                summary["skipped"].append({"alertname": alertname, "reason": "duplicate"})
                self.metrics.inc("aiops_alerts_deduped")
                continue

            incident = self.triage(alert)
            notified = self._notify(incident)
            summary["triaged"].append({
                "id": incident["id"],
                "alertname": alertname,
                "action_id": incident["final"].get("action_id"),
                "escalated": incident["executable"] is False,
                "notified": notified,
            })
        return summary

    # --- notification ---------------------------------------------------------
    def _notify(self, incident: dict) -> bool:
        sent = self.telegram.send_message(
            render_card(incident),
            build_keyboard(incident["id"], incident["executable"]))
        self.journal.append_event("notification", {
            "incident_id": incident["id"], "sent": sent,
            "telegram_enabled": self.telegram.enabled})
        return sent

    # --- human decisions ------------------------------------------------------
    def handle_callback(self, callback: dict) -> dict | None:
        """A button press. It is a *journal entry*, never an execution."""
        action, _, ident = (callback.get("data") or "").partition(":")
        callback_id = callback.get("id", "")
        message_id = (callback.get("message") or {}).get("message_id")
        sender = (callback.get("from") or {})
        user = sender.get("username") or str(sender.get("id", ""))

        if action not in DECISIONS or not ident:
            self.telegram.answer_callback(callback_id, "неизвестная кнопка")
            return None
        incident = self.find_incident(ident)
        if incident is None:
            self.telegram.answer_callback(
                callback_id, "инцидент не найден: журнал пода эфемерный")
            return None

        command = (incident.get("final") or {}).get("command") or ""
        if action == "approve" and not incident.get("executable"):
            # The keyboard never offers this, but a stale message might.
            self.telegram.answer_callback(callback_id, "нечего применять: это эскалация")
            return None

        if action == "approve":
            ack = ("✅ <b>принято в план</b> (" + esc(user) + "):\n"
                   f"<code>{esc(command)}</code>\n"
                   "M1 не исполняет команд: исполнение появится в M2 "
                   "(scoped ServiceAccount + второе подтверждение).")
        elif action == "advice":
            ack = f"📝 записано как «только совет» ({esc(user)}). Действий не будет."
        else:
            key = self._dedupe_key(incident)
            self.snooze(key, SNOOZE_SECONDS)
            ack = (f"🔇 <b>игнор 4 часа</b> ({esc(user)}): повторные срабатывания "
                   f"<code>{esc(incident.get('alertname', '?'))}</code> "
                   "не будут разбираться до истечения окна.")

        self.journal.append_event("human_decision", {
            "incident_id": ident,
            "alertname": incident.get("alertname", ""),
            "action": action,
            "user": user,
            "executable": bool(incident.get("executable")),
            "command": command,
            "executed": False,
        })
        self.metrics.inc("aiops_decisions_recorded", decision=action)
        self.telegram.answer_callback(callback_id, f"записано: {action}")
        self.telegram.clear_keyboard(message_id)
        self.telegram.send_message(ack)
        return {"incident_id": ident, "action": action, "user": user}

    # --- chat commands --------------------------------------------------------
    def handle_message(self, message: dict) -> str | None:
        """`/status`, `/last`, `/help` - anything else is ignored on purpose."""
        text = (message.get("text") or "").strip()
        if text in ("/status", "/status@aiops_triage_bot"):
            describe = self.settings.describe()
            body = "\n".join(f"{esc(key)}: <code>{esc(value)}</code>"
                             for key, value in describe.items())
            reply = ("<b>aiops-triage</b>\n" + body +
                     "\n<code>/last</code> — последний разобранный инцидент")
        elif text.startswith("/last"):
            if not self._recent:
                reply = "инцидентов в этой сессии ещё не было"
            else:
                reply = render_card(list(self._recent.values())[-1])
        elif text.startswith("/"):
            reply = ("команды: <code>/status</code> — настройки и лимиты, "
                     "<code>/last</code> — последний инцидент, "
                     "<code>/help</code> — эта подсказка")
        else:
            return None
        self.telegram.send_message(reply)
        return reply
