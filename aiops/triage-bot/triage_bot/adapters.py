"""Model adapters: one interface, several providers, none of them required.

Why an adapter layer at all: everything *after* the adapter - the JSON schema,
the action whitelist, the confidence threshold, the human approval - must not
depend on which model answered. The adapter is the only place that knows how to
talk to a provider, so adding one is a row in `config.LLM_PROVIDERS` and nothing
else changes.

Two kinds of adapter, one shape:

* `MockAdapter` - the tier-0 ruleset, not a model. It looks up `alertname` in a
  table and returns a JSON **string**, exactly like a real model would, so it
  travels the same validation path. Its *decision* is a template; its *evidence*
  and *params* come from the live context (real Prometheus values, real Loki
  lines, real alert labels), so the card it produces is not fictional. Because it
  is deterministic it is also the baseline every model answer is compared
  against in the eval mode.
* `OpenAICompatibleAdapter` - DeepSeek, OpenAI and Ollama behind one
  implementation, because all three expose `POST /chat/completions`.

Neither adapter parses or executes anything: the raw answer goes back as text and
is validated by `model.validate_decision`.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .config import KNOWN_ADAPTERS, Settings
from .model import ACTIONS, _NAME_RE

log = logging.getLogger("aiops.adapters")


class AdapterError(RuntimeError):
    """The model could not answer. The pipeline escalates instead of guessing."""


class Adapter:
    name = "base"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def decide(self, system: str, user: str, alert: dict, context: dict) -> str:
        """Return the model's raw answer as text (validated by the caller)."""
        raise NotImplementedError

    def answer(self, system: str, user: str) -> ModelAnswer:
        """Free-text follow-up: shown to a human, executed by nobody.

        Separate from `decide` on purpose. The text is not parsed, not validated
        against the whitelist and cannot become an action, so it must not travel
        through the decision schema at all - an answer that *looked* like a
        decision but skipped validation would be the worst of both worlds.
        """
        raise NotImplementedError


class MockAdapter(Adapter):
    """Tier 0: deterministic, offline, evidence-driven (the runbook in code)."""

    name = "mock"

    #: alertname -> (action_id, confidence, reason). The keys are the alert
    #: names shipped in `manifests-monitoring/05-prometheus-alertrules.yaml`;
    #: `tests/test_coverage.py` fails if a rule exists that this table does not
    #: know about, so "the bot forgot about a new alert" is caught in CI.
    RULES = {
        "DeploymentReplicasUnavailable": (
            "restart_deployment", 0.85,
            "Доступных реплик меньше желаемого: rolling restart - самый дешёвый "
            "идемпотентный шаг, он не трогает данные и виден в rollout-истории."),
        "PodCrashLooping": (
            "restart_deployment", 0.80,
            "Под в CrashLoopBackOff: перезапуск деплоя пересоздаёт поды и снимает "
            "залипшее состояние, если причина была в самом процессе."),
        "PodRestartFlapping": (
            "restart_deployment", 0.80,
            "Под перезапускается слишком часто: rolling restart выравнивает "
            "состояние и даёт чистый старт для сравнения."),
        "HighErrorRate": (
            "rollback_deployment", 0.75,
            "Доля 5xx выше порога: если ошибки начались вместе с последним "
            "релизом, откат возвращает рабочую ревизию быстрее, чем разбор."),
        "NodeNotReady": (
            "escalate_human", 0.95,
            "Узел недоступен: это инфраструктура, автоматическое действие здесь "
            "опаснее ожидания - нужен человек."),
        "NodeDiskPressure": (
            "escalate_human", 0.95,
            "Диск узла заполнен: безопасного автоматического шага нет "
            "(удалять данные или тома бот не должен)."),
        "NodeHighMemory": (
            "escalate_human", 0.90,
            "Память узла на пределе: нужно решение о вытеснении или "
            "перераспределении нагрузки, а не перезапуск наугад."),
        "NodeHighCPU": (
            "escalate_human", 0.85,
            "CPU узла на пределе: возможны несколько причин, разбор нужен человеку."),
        "ScrapeTargetDown": (
            "escalate_human", 0.80,
            "Цель мониторинга недоступна: сначала надо понять, упала ли цель или "
            "сам скрейпинг."),
        # Deliberately below the 0.7 threshold: the cascade must open the model
        # tier on this one, and the unit tests assert that it does.
        "ProbeFailed": (
            "restart_deployment", 0.60,
            "Внешняя проба не проходит, но по одной пробе нельзя отличить сбой "
            "приложения от сети - уверенности недостаточно."),
        "ProbeSlow": (
            "restart_deployment", 0.55,
            "Проба медленная: рост задержки может быть и нагрузкой, и деградацией."),
    }

    #: Alerts whose *claim* can be re-checked against the very series they are
    #: meant to be computed from: `(lower metric, upper metric)`, and the premise
    #: holds while `lower < upper`. A synthetic, stale or mis-scoped alert arrives
    #: with labels but no real problem, and comparing two numbers the bot already
    #: collected is the cheapest way to notice - far cheaper than restarting a
    #: healthy Deployment. Only alerts listed here are re-checked: for the rest
    #: the ruleset is not second-guessing anything it cannot verify.
    #:
    #: A **missing** metric is not a conflict. Silence must not block a safe step,
    #: only a contradiction may - otherwise the bot would stop acting on every
    #: alert from a stand where one exporter is broken.
    PREMISES = {
        "DeploymentReplicasUnavailable": ("replicas_available", "replicas_desired"),
    }

    DEFAULT = ("escalate_human", 0.40,
               "Такой алерт не описан в правилах мока: безопасный шаг не очевиден.")

    def decide(self, system: str, user: str, alert: dict, context: dict) -> ModelAnswer:
        """Ignore the prompt (this is not a model) and answer as JSON text."""
        return ModelAnswer(
            text=json.dumps(self.decision_for(alert, context), ensure_ascii=False),
            adapter=self.name,
            model=self.settings.model_name,
            latency_ms=0,
            usage={},
        )

    def answer(self, system: str, user: str) -> ModelAnswer:
        """Tier 0 has no prose to offer: say so instead of inventing advice."""
        return ModelAnswer(
            text=("Оффлайн-режим (mock): подробный разбор доступен только с моделью.\n"
                  "Что проверить руками:\n"
                  "1. kubectl -n sock-shop get deploy,po,ep -o wide\n"
                  "2. kubectl -n sock-shop describe deploy/<service>\n"
                  "3. в Loki: {namespace=\"sock-shop\", service=\"<service>\"}"),
            adapter=self.name,
            model=self.settings.model_name,
            latency_ms=0,
            usage={},
        )

    def decision_for(self, alert: dict, context: dict) -> dict:
        """The tier-0 decision for one alert: template + live evidence."""
        labels = alert.get("labels") or {}
        alertname = labels.get("alertname", "")
        namespace = labels.get("namespace") or "sock-shop"
        deployment = (labels.get("deployment") or labels.get("deployment_name") or "")
        action_id, confidence, reason = self.RULES.get(alertname, self.DEFAULT)
        evidence = self._evidence(context)

        # The rule fires on `available == 0`, which is also how "somebody scaled
        # the Deployment to zero" is caught. The generic premise check below
        # compares available with desired and would call 0 >= 0 "already as
        # desired" - the exact opposite of the truth, and the live stand shipped
        # that sentence to Telegram while the site was down. So this case is
        # named for what it is, and the confidence stays below the threshold on
        # purpose: the fix is a scale, which the tier-0 table does not have, so
        # the cascade (or a human) has to decide.
        if alertname == "DeploymentReplicasUnavailable":
            available = self._value(context, "replicas_available")
            desired = self._value(context, "replicas_desired")
            if available is not None and available == 0 and desired == 0:
                return {
                    "action_id": "escalate_human",
                    "confidence": 0.5,
                    "reason": (
                        f"Деплоймент {deployment} масштабирован до нуля "
                        "(desired=0, available=0): подов нет, поэтому сервис не "
                        "отвечает и проба падает. Rolling restart ничего не "
                        "создаст - нужен scale до ненулевого числа реплик."),
                    "evidence": evidence,
                }

        # The alert says what is wrong; the metrics say whether that is still
        # true. When the two disagree, the alert loses: an automated step taken
        # on the strength of a false premise is exactly the damage this component
        # is supposed to prevent. The escalation carries both numbers, so a human
        # can see *why* the bot refused rather than being told "not sure".
        conflict = self._premise_conflict(context, alertname)
        if conflict and not ACTIONS[action_id].is_escalation:
            return {
                "action_id": "escalate_human",
                "confidence": 0.5,
                "reason": (
                    f"Алерт {alertname} не подтверждается метриками ({conflict}): "
                    "состояние уже соответствует желаемому, автоматический шаг "
                    "ничего не исправит - решение за человеком."),
                "evidence": evidence,
            }

        spec = ACTIONS[action_id]
        params: dict = {}
        if spec.params:
            if not deployment or not _NAME_RE.match(deployment):
                # The ruleset may be sure about the *shape* of the problem, but
                # without a deployment label it cannot name the object - and a
                # guessed object name is worse than no action at all.
                return {
                    "action_id": "escalate_human",
                    "confidence": 0.5,
                    "reason": ("В алерте нет корректной метки deployment, поэтому "
                               "действие с конкретным объектом невозможно - нужен "
                               "человек."),
                    "evidence": evidence,
                }
            params = {"namespace": namespace, "deployment": deployment}

        return {
            "action_id": action_id,
            "confidence": confidence,
            "reason": reason,
            "evidence": evidence,
            **({"params": params} if params else {}),
        }

    @staticmethod
    def _evidence(context: dict, limit: int = 5) -> list:
        """Facts from the collected context - never invented, sometimes absent."""
        items = [f"{key} = {value}" for key, value in
                 list((context.get("prometheus") or {}).items())[:limit]]
        for line in (context.get("loki") or [])[:2]:
            items.append(f"log: {line}")
        if not items:
            items.append("ни метрик, ни логов в окне собрать не удалось")
        for miss in (context.get("missing") or [])[:1]:
            items.append(f"gap: {miss}")
        return items

    @staticmethod
    def _value(context: dict, prefix: str):
        """One collected metric, looked up by the name `plan_queries` gave it.

        The keys carry the object in brackets (`replicas_available[front-end]`),
        so the look-up is by prefix. A value that is not a number counts as
        absent instead of raising: a broken scrape must not abort a triage.
        """
        for key, value in (context.get("prometheus") or {}).items():
            if key == prefix or key.startswith(f"{prefix}["):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None
        return None

    def _premise_conflict(self, context: dict, alertname: str) -> str:
        """How this alert is contradicted by live metrics, or "" if it is not.

        Deliberately asymmetric: only a contradiction is reported. No metric at
        all means "not verifiable", which is not the same as "not happening".
        """
        premise = self.PREMISES.get(alertname)
        if not premise:
            return ""
        lower_key, upper_key = premise
        lower = self._value(context, lower_key)
        upper = self._value(context, upper_key)
        if lower is None or upper is None or lower < upper:
            return ""
        return f"{lower_key}={lower:g} >= {upper_key}={upper:g}"


@dataclass
class ModelAnswer:
    """One raw model answer plus what it cost - measured, not estimated."""

    text: str
    adapter: str = ""
    model: str = ""
    latency_ms: int = 0
    usage: dict = field(default_factory=dict)

    @property
    def prompt_tokens(self) -> int:
        return int(self.usage.get("prompt_tokens") or 0)

    @property
    def completion_tokens(self) -> int:
        return int(self.usage.get("completion_tokens") or 0)


class OpenAICompatibleAdapter(Adapter):
    """DeepSeek, OpenAI and Ollama: one protocol, three rows in LLM_PROVIDERS.

    The only provider-specific knowledge here is small and deliberate:
    `response_format: json_object` is requested (DeepSeek and OpenAI honour it),
    and if a provider answers 400 because it does not know that field, the call
    is retried once without it. That retry is what makes "any OpenAI-compatible
    gateway" true rather than a claim - and the answer is validated either way.
    """

    def __init__(self, settings: Settings, base_url: str, model: str,
                 api_key: str, provider_name: str) -> None:
        super().__init__(settings)
        self.name = provider_name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key

    def _post(self, payload: dict) -> dict:
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(
                    request, timeout=self.settings.llm_timeout_seconds) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise AdapterError(f"{self.name}: HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AdapterError(f"{self.name}: request failed: {exc}") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise AdapterError(
                f"{self.name}: response is not JSON: {body[:200]!r}") from exc

    def _messages(self, system: str, user: str) -> list:
        return [{"role": "system", "content": system},
                {"role": "user", "content": user}]

    def _complete(self, payload: dict, json_mode: bool) -> tuple[dict, int]:
        """One request, with the single fallback this project has measured.

        Some OpenAI-compatible gateways answer 400 to `response_format`. That
        retry is what makes "any OpenAI-compatible gateway" true rather than a
        claim - and only a JSON request ever needs it.
        """
        started = time.time()
        try:
            data = self._post(payload)
        except AdapterError as exc:
            if not json_mode or "response_format" not in str(exc):
                raise
            log.warning("%s does not know response_format - retrying without it", self.name)
            payload.pop("response_format", None)
            data = self._post(payload)
        return data, int((time.time() - started) * 1000)

    def _answer_from(self, data: dict, latency_ms: int) -> ModelAnswer:
        choices = data.get("choices") or []
        if not choices:
            raise AdapterError(f"{self.name}: no choices in the response")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise AdapterError(f"{self.name}: empty message content")
        return ModelAnswer(text=content, adapter=self.name, model=self.model,
                           latency_ms=latency_ms, usage=data.get("usage") or {})

    def decide(self, system: str, user: str, alert: dict, context: dict) -> ModelAnswer:
        payload = {
            "model": self.model,
            "temperature": self.settings.llm_temperature,
            "max_tokens": self.settings.llm_max_tokens,
            "response_format": {"type": "json_object"},
            "messages": self._messages(system, user),
        }
        data, latency_ms = self._complete(payload, json_mode=True)
        return self._answer_from(data, latency_ms)

    def answer(self, system: str, user: str) -> ModelAnswer:
        """Prose for a human: no response_format, no schema, no action.

        `response_format` is deliberately absent here. Asking for JSON and then
        rendering that JSON as advice is how a follow-up ends up looking like a
        decision to somebody who is skimming the chat.
        """
        payload = {
            "model": self.model,
            "temperature": self.settings.llm_temperature,
            "max_tokens": self.settings.llm_max_tokens,
            "messages": self._messages(system, user),
        }
        data, latency_ms = self._complete(payload, json_mode=False)
        return self._answer_from(data, latency_ms)


def build_adapter(settings: Settings) -> Adapter:
    """The tier-1 adapter. `mock` here means "no model at all".

    Raising on a misconfigured provider is deliberate: the pipeline catches it,
    falls back to the ruleset and prints the reason in the card. A silent
    downgrade to a weaker path is the failure mode this project keeps fighting.
    """
    if settings.llm_api_style == "mock":
        return MockAdapter(settings)
    if settings.llm_api_style != "openai-compatible":
        raise AdapterError(
            f"unknown adapter {settings.model_adapter!r}; known: {list(KNOWN_ADAPTERS)}")
    if not settings.model_tier_available:
        raise AdapterError(f"{settings.model_adapter}: {settings.model_tier_reason}")
    return OpenAICompatibleAdapter(
        settings,
        base_url=settings.llm_base_url,
        model=settings.model_name,
        api_key=settings.llm_api_key(),
        provider_name=settings.model_adapter,
    )
