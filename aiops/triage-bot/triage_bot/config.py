"""Runtime configuration of the triage bot.

Everything is read from environment variables: the Deployment feeds them from a
ConfigMap, so there is no config file to parse and therefore no YAML dependency
- the container runs on the Python standard library alone. Secrets are the only
exception and they are read from *files* (mounted from the existing
`alertmanager-secrets` Secret), exactly like Alertmanager reads its own token.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_PROMETHEUS_URL = "http://prometheus.monitoring.svc.cluster.local:9090"
DEFAULT_LOKI_URL = "http://loki.logging.svc.cluster.local:3100"
DEFAULT_ALERTMANAGER_URL = "http://alertmanager.monitoring.svc.cluster.local:9093"

#: Per-provider defaults. DeepSeek, OpenAI and Ollama all speak the same
#: OpenAI-compatible `/chat/completions` protocol, so ONE adapter implementation
#: covers all of them - and any other gateway, self-hosted vLLM or LiteLLM proxy
#: that follows the same shape. That is the point of the adapter layer: adding a
#: provider is a row in this table, not a new integration.
#:
#: `mock` is not a model at all: it is the tier-0 ruleset - decisions written by
#: hand and keyed by the alert names this stand actually fires (checked against
#: the rule file by a test, so the two cannot drift apart). It needs no network,
#: never changes its answer, and it is what the unit tests and CI use, so the
#: suite can neither fail because a provider is down nor cost money. In the
#: cascade it also decides whether a model is needed at all.
LLM_PROVIDERS: dict = {
    "mock": {
        "api_style": "mock",
        "base_url": "",
        "model": "mock-deterministic-v1",
        "api_key_file": "",
    },
    "deepseek": {
        "api_style": "openai-compatible",
        # DeepSeek exposes an OpenAI-compatible API; `deepseek-chat` is the
        # general chat model and supports `response_format: json_object`.
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key_file": "/etc/aiops/secrets/deepseek_api_key",
    },
    "openai": {
        "api_style": "openai-compatible",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "api_key_file": "/etc/aiops/secrets/openai_api_key",
    },
    "ollama": {
        "api_style": "openai-compatible",
        # A local model: same protocol, no key, and nothing leaves the machine.
        "base_url": "http://host.docker.internal:11434/v1",
        "model": "llama3.1",
        "api_key_file": "",
    },
}

KNOWN_ADAPTERS = tuple(LLM_PROVIDERS)


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name))
    except ValueError:
        return default


def _read_file(path: str) -> str:
    """Read a secret from a mounted file; a missing file is simply empty."""
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


@dataclass
class Settings:
    # --- HTTP -----------------------------------------------------------------
    listen_host: str = "0.0.0.0"
    listen_port: int = 8080

    # --- read-only data sources ----------------------------------------------
    prometheus_url: str = DEFAULT_PROMETHEUS_URL
    loki_url: str = DEFAULT_LOKI_URL
    alertmanager_url: str = DEFAULT_ALERTMANAGER_URL

    # --- model ---------------------------------------------------------------
    #: Tier-1 provider: mock | deepseek | openai | ollama (see LLM_PROVIDERS).
    #: `mock` in tier 1 as well means "no model at all" - the cascade then ends
    #: at the ruleset, which is the offline-safe default of this stand.
    model_adapter: str = "mock"
    model_name: str = ""
    llm_base_url: str = ""
    llm_api_key_file: str = ""
    llm_api_style: str = "mock"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 700
    llm_timeout_seconds: float = 30.0
    #: Cascade: ask the model only when the ruleset itself escalates.
    cascade_enabled: bool = True
    #: Eval: ask the model even when the rules answered, and keep BOTH answers
    #: in the journal/card - this is how the baseline is measured, not assumed.
    eval_mode: bool = False
    #: No key for the provider: fall back to the ruleset and SAY SO in the card.
    llm_fallback_to_mock: bool = True

    # --- triage policy -------------------------------------------------------
    confidence_threshold: float = 0.7
    require_label: str = "aiops"
    dedupe_window_seconds: int = 1800
    log_window_seconds: int = 900
    max_log_lines: int = 40
    max_evidence_items: int = 6
    http_timeout_seconds: float = 10.0

    # --- notification --------------------------------------------------------
    telegram_token_file: str = "/etc/aiops/secrets/telegram_bot_token"
    telegram_chat_id: str = ""
    telegram_max_per_hour: int = 20

    # --- storage / logging ---------------------------------------------------
    journal_dir: str = "/data"
    log_level: str = "INFO"

    #: True in M1 by construction: the pipeline never touches the cluster.
    dry_run: bool = True
    #: labels the bot is allowed to operate on at all (M2 will enforce this).
    allowed_namespaces: tuple = ("sock-shop",)
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        settings = cls(
            listen_host=_env("LISTEN_HOST", "0.0.0.0"),
            listen_port=_env_int("LISTEN_PORT", 8080),
            prometheus_url=_env("PROMETHEUS_URL", DEFAULT_PROMETHEUS_URL),
            loki_url=_env("LOKI_URL", DEFAULT_LOKI_URL),
            alertmanager_url=_env("ALERTMANAGER_URL", DEFAULT_ALERTMANAGER_URL),
            model_adapter=_env("MODEL_ADAPTER", "mock").lower(),
            model_name=_env("MODEL_NAME", ""),
            llm_base_url=_env("LLM_BASE_URL", ""),
            llm_api_key_file=_env("LLM_API_KEY_FILE", ""),
            llm_temperature=_env_float("LLM_TEMPERATURE", 0.0),
            llm_max_tokens=_env_int("LLM_MAX_TOKENS", 700),
            llm_timeout_seconds=_env_float("LLM_TIMEOUT_SECONDS", 30.0),
            cascade_enabled=_env_int("CASCADE_ENABLED", 1) == 1,
            eval_mode=_env_int("EVAL_MODE", 0) == 1,
            llm_fallback_to_mock=_env_int("LLM_FALLBACK_TO_MOCK", 1) == 1,
            confidence_threshold=_env_float("CONFIDENCE_THRESHOLD", 0.7),
            require_label=_env("REQUIRE_LABEL", "aiops"),
            dedupe_window_seconds=_env_int("DEDUPE_WINDOW_SECONDS", 1800),
            log_window_seconds=_env_int("LOG_WINDOW_SECONDS", 900),
            max_log_lines=_env_int("MAX_LOG_LINES", 40),
            max_evidence_items=_env_int("MAX_EVIDENCE_ITEMS", 6),
            http_timeout_seconds=_env_float("HTTP_TIMEOUT_SECONDS", 10.0),
            telegram_token_file=_env("TELEGRAM_TOKEN_FILE",
                                     "/etc/aiops/secrets/telegram_bot_token"),
            telegram_chat_id=_env("TELEGRAM_CHAT_ID", ""),
            telegram_max_per_hour=_env_int("TELEGRAM_MAX_PER_HOUR", 20),
            journal_dir=_env("JOURNAL_DIR", "/data"),
            log_level=_env("LOG_LEVEL", "INFO").upper(),
        )
        provider = LLM_PROVIDERS.get(settings.model_adapter)
        if provider is None:
            # An unknown name is a configuration error, not a reason to guess:
            # the name stays in the settings so /healthz and the card show it.
            settings.llm_api_style = "unknown"
            settings.model_name = settings.model_name or "unknown"
        else:
            settings.llm_api_style = provider["api_style"]
            settings.llm_base_url = settings.llm_base_url or provider["base_url"]
            settings.llm_api_key_file = (
                settings.llm_api_key_file or provider["api_key_file"])
            settings.model_name = settings.model_name or provider["model"]
        namespaces = _env("ALLOWED_NAMESPACES", "sock-shop")
        settings.allowed_namespaces = tuple(
            name.strip() for name in namespaces.split(",") if name.strip())
        return settings

    # --- derived helpers -----------------------------------------------------
    def telegram_token(self) -> str:
        return _read_file(self.telegram_token_file)

    @property
    def telegram_enabled(self) -> bool:
        """Telegram is optional: without a token the bot still triages and logs."""
        return bool(self.telegram_token() and self.telegram_chat_id)

    def llm_api_key(self) -> str:
        return _read_file(self.llm_api_key_file)

    @property
    def model_tier_available(self) -> bool:
        """Can the model tier run at all? (`mock` is not a model: it always can.)"""
        if self.llm_api_style == "mock":
            return True
        if self.llm_api_style == "unknown" or not self.llm_base_url:
            return False
        if self.llm_api_key_file and not self.llm_api_key():
            return False
        return True

    @property
    def model_tier_reason(self) -> str:
        """Why the model tier is unavailable - shown in the card, never hidden."""
        if self.llm_api_style == "mock":
            # `mock` is not a model that is missing: it IS the tier-0 ruleset, so
            # the honest answer is "no reason". Without this, /healthz reported
            # "the provider has no base_url" on a perfectly healthy stand - visible
            # in the live run and fixed because a misleading health field costs
            # more than the three lines it takes to remove it.
            return ""
        if self.llm_api_style == "unknown":
            return f"unknown adapter {self.model_adapter!r}"
        if not self.llm_base_url:
            return "the provider has no base_url"
        if self.llm_api_key_file and not self.llm_api_key():
            return f"no key in {self.llm_api_key_file}"
        return ""

    def describe(self) -> dict:
        """Small, secret-free summary used by /healthz and the logs."""
        return {
            "adapter": self.model_adapter,
            "model": self.model_name,
            "model_tier_available": self.model_tier_available,
            "model_tier_reason": self.model_tier_reason,
            "cascade": self.cascade_enabled,
            "eval_mode": self.eval_mode,
            "prometheus": self.prometheus_url,
            "loki": self.loki_url,
            "confidence_threshold": self.confidence_threshold,
            "require_label": self.require_label,
            "telegram_enabled": self.telegram_enabled,
            "dry_run": self.dry_run,
            "journal_dir": self.journal_dir,
        }
