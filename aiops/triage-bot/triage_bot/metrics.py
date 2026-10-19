"""A tiny Prometheus text-format registry.

No `prometheus_client` dependency: the bot is a standard-library-only image, and
the exposition format is simple enough to write correctly. The Service of the
bot carries `prometheus.io/scrape: "true"`, so the existing
`kubernetes-service-endpoints` job in Prometheus picks these series up - which
is what makes the "the bot itself went silent" alert possible.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

_HELP = {
    "aiops_alerts_received": "Alerts received from Alertmanager, by status.",
    "aiops_alerts_skipped": "Alerts that were not triaged, by reason.",
    "aiops_alerts_deduped": "Alerts dropped as duplicates of a recent incident.",
    "aiops_incidents": "Triaged incidents, by chosen action id.",
    "aiops_escalations": "Decisions turned into a human escalation, by reason.",
    "aiops_model_calls": "Model adapter calls, by adapter.",
    "aiops_model_errors": "Model adapter failures, by adapter.",
    "aiops_context_failures": "Context collectors that failed, by source.",
    "aiops_telegram_sent": "Telegram messages sent.",
    "aiops_telegram_suppressed": "Telegram messages suppressed by the rate limit.",
    "aiops_decisions_recorded": "Human decisions recorded from Telegram, by decision.",
    "aiops_last_incident_timestamp_seconds": "Unix time of the last triaged incident.",
}


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Metrics:
    """Counters and one gauge, keyed by label set, rendered on demand."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: "OrderedDict[tuple, float]" = OrderedDict()
        self._gauges: "OrderedDict[tuple, float]" = OrderedDict()

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + value
            # keep the label set so a series is rendered from its first sample
            if key[0] not in _HELP:
                _HELP[key[0]] = key[0]

    def set(self, name: str, value: float, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._gauges[key] = value
            if key[0] not in _HELP:
                _HELP[key[0]] = key[0]

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._counters)

    @staticmethod
    def _fmt_labels(labels: tuple) -> str:
        if not labels:
            return ""
        inner = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
        return "{" + inner + "}"

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            counters = list(self._counters.items())
            gauges = list(self._gauges.items())
        emitted: set = set()
        for name, labels in counters + gauges:
            if name in emitted:
                continue
            emitted.add(name)
            lines.append(f"# HELP {name} {_HELP.get(name, name)}")
            lines.append(f"# TYPE {name} counter" if name != "aiops_last_incident_timestamp_seconds"
                         else f"# TYPE {name} gauge")
        for (name, labels), value in counters + gauges:
            lines.append(f"{name}{self._fmt_labels(labels)} {value:g}")
        return "\n".join(lines) + "\n"
