"""Read-only context collection: Prometheus and Loki, and nothing else.

There is deliberately **no Kubernetes API client** and no `kubectl` call in this
package. The bot does not need them to be useful: the alert already carries
`namespace` / `deployment` / `pod` labels, and metrics plus logs answer the
"what is it doing right now" question. What the bot cannot see (pod events, node
conditions, the actual `describe` output) is written into `missing` instead of
being guessed - an honest gap costs one line in the report, a guess costs
credibility.

Every HTTP call goes through `urllib.request` from the standard library and every
failure is recorded as data, never as an exception that aborts the triage.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("aiops.context")


class SourceError(RuntimeError):
    """A read-only source could not be reached or returned a bad answer."""


def _urlencode(params: dict) -> str:
    # `quote_via=quote` keeps `{}`, `"` and `=` readable in the request line,
    # which matters when reading these queries in logs and in the journal.
    return urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


def http_get_json(url: str, params: dict, timeout: float) -> dict:
    query = _urlencode(params)
    full = f"{url.rstrip('/')}?{query}" if query else url
    request = urllib.request.Request(full, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SourceError(f"GET {full} failed: {exc}") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise SourceError(f"GET {full} returned non-JSON: {body[:200]!r}") from exc


class PrometheusClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url
        self.timeout = timeout

    def query(self, expression: str) -> list[dict]:
        """Instant query. Returns `[{"metric": {...}, "value": "1"}, ...]`."""
        payload = http_get_json(f"{self.base_url}/api/v1/query",
                                {"query": expression}, self.timeout)
        if payload.get("status") != "success":
            raise SourceError(f"prometheus error for {expression!r}: "
                              f"{payload.get('error', 'unknown')}")
        data = payload.get("data", {})
        if data.get("resultType") == "vector":
            return [{"metric": item.get("metric", {}),
                     "value": (item.get("value") or ["", ""])[1]}
                    for item in data.get("result", [])]
        # A scalar/string answer is still an answer worth keeping.
        return [{"metric": {}, "value": str(data.get("result"))}]


class LokiClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url
        self.timeout = timeout

    def log_lines(self, logql: str, window_seconds: int, limit: int) -> list[str]:
        end = time.time()
        start = end - max(30, window_seconds)
        payload = http_get_json(f"{self.base_url}/loki/api/v1/query_range", {
            "query": logql,
            "start": f"{int(start * 1e9)}",
            "end": f"{int(end * 1e9)}",
            "limit": str(max(1, limit)),
            "direction": "backward",
        }, self.timeout)
        if payload.get("status") != "success":
            raise SourceError(f"loki error for {logql!r}: {payload.get('error', 'unknown')}")
        lines: list[str] = []
        for stream in payload.get("data", {}).get("result", []):
            labels = stream.get("stream", {})
            name = labels.get("pod_name") or labels.get("container_name") or ""
            for entry in stream.get("values", []):
                if len(entry) < 2:
                    continue
                ts, message = entry[0], entry[1]
                try:
                    seconds = int(ts) / 1e9
                    stamp = time.strftime("%H:%M:%S", time.localtime(seconds))
                except (TypeError, ValueError):
                    stamp = "?"
                clean = " ".join(str(message).split())[:400]
                lines.append(f"{stamp} {name}: {clean}" if name else f"{stamp} {clean}")
        # Loki returns newest first; the report reads better oldest first.
        lines.reverse()
        return lines[:limit]


def _first(result: list[dict]) -> str:
    return result[0]["value"] if result else ""


def plan_queries(labels: dict) -> list:
    """The metric questions worth asking about *this* alert - and no others.

    The plan is derived from the labels Alertmanager already sent. Asking about
    a `node` that is not in the alert would spend a query to learn nothing, and
    asking about nothing at all turns the report into a template.
    """
    namespace = labels.get("namespace", "sock-shop")
    deployment = labels.get("deployment") or labels.get("deployment_name") or ""
    pod = labels.get("pod") or labels.get("pod_name") or ""
    node = labels.get("node") or ""
    queries: list = [
        ("targets_down", "sum(up == 0)"),
        ("down_targets", "up == 0"),
    ]
    if deployment:
        selector = f'namespace="{namespace}", deployment="{deployment}"'
        queries += [
            (f"replicas_available[{deployment}]",
             f"kube_deployment_status_replicas_available{{{selector}}}"),
            (f"replicas_desired[{deployment}]",
             f"kube_deployment_spec_replicas{{{selector}}}"),
            (f"replicas_unavailable[{deployment}]",
             f"kube_deployment_status_replicas_unavailable{{{selector}}}"),
            (f"max_pod_restarts[{deployment}]",
             "max(kube_pod_container_status_restarts_total"
             f'{{namespace="{namespace}", pod=~"{deployment}.*"}})'),
            (f"waiting_reasons[{deployment}]",
             "max by (reason) (kube_pod_container_status_waiting_reason"
             f'{{namespace="{namespace}", pod=~"{deployment}.*"}}) > 0'),
            (f"cpu_cores_sum[{deployment}]",
             "sum(rate(container_cpu_usage_seconds_total"
             f'{{namespace="{namespace}", pod=~"{deployment}.*", container!="POD"}}[5m]))'),
            (f"memory_bytes_sum[{deployment}]",
             "sum(container_memory_working_set_bytes"
             f'{{namespace="{namespace}", pod=~"{deployment}.*", container!="POD"}})'),
        ]
    if not deployment and not node:
        # The alert names no workload (a probe, a dead scrape target, a pod that
        # belongs to some Deployment). Without this, the report for such an alert
        # contained nothing but "probe_success=0" and the model had no object it
        # could name - live, DeepSeek answered "escalate" for a stand whose
        # front-end was simply scaled to zero. Ask the namespace instead of
        # guessing, and keep the names (see the formatting in collect_context):
        # a bare "0" is not evidence, "front-end=0" is.
        queries += [
            ("deployments_at_zero[namespace]",
             "max by (deployment) (kube_deployment_spec_replicas"
             f'{{namespace="{namespace}"}}) == 0'),
            ("deployments_unavailable[namespace]",
             "max by (deployment) (kube_deployment_status_replicas_unavailable"
             f'{{namespace="{namespace}"}}) > 0'),
        ]
    if pod:
        queries.append(
            (f"pod_restarts[{pod}]",
             "max(kube_pod_container_status_restarts_total"
             f'{{namespace="{namespace}", pod="{pod}"}})'))
        queries.append(
            (f"pod_phase[{pod}]",
             f'kube_pod_status_phase{{namespace="{namespace}", pod="{pod}"}} == 1'))
    if node:
        queries += [
            (f"node_ready_condition[{node}]",
             f'kube_node_status_condition{{node="{node}", condition="Ready"}} == 1'),
            ("filesystem_min_free_ratio[cluster wide]",
             "min(node_filesystem_avail_bytes{fstype!~\"(tmpfs|overlay)\"} "
             "/ node_filesystem_size_bytes{fstype!~\"(tmpfs|overlay)\"})"),
            ("cpu_busy_ratio[cluster wide]",
             '1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))'),
        ]
    if labels.get("probe_scope") or labels.get("instance"):
        queries.append(("probe_success", "probe_success"))
    return queries


def collect_context(alert: dict, settings, prometheus: PrometheusClient,
                    loki: LokiClient, metrics=None) -> dict:
    """Collect whatever of the plan answers, and say what did not."""
    labels = alert.get("labels") or {}
    namespace = labels.get("namespace", "sock-shop")
    deployment = labels.get("deployment") or labels.get("deployment_name") or ""
    pod = labels.get("pod") or labels.get("pod_name") or ""

    context: dict = {
        "prometheus": {},
        "loki": [],
        "missing": [],
        "collected_at": time.time(),
        "sources": [],
    }
    started = time.time()
    #: One line per failing source instead of one per query: nine failed Prometheus
    #: queries mean the same thing ("Prometheus is not reachable"), and a report
    #: that repeats it nine times hides the actual evidence.
    source_errors: dict = {}

    for key, expression in plan_queries(labels):
        try:
            result = prometheus.query(expression)
        except SourceError as exc:
            entry = source_errors.setdefault(
                "prometheus", {"count": 0, "sample": key, "error": str(exc)})
            entry["count"] += 1
            if metrics:
                metrics.inc("aiops_context_failures", source="prometheus")
            continue
        if not result:
            continue  # an empty answer is normal for a metric that does not fit
        if key == "down_targets":
            names = [f'{item["metric"].get("job", "?")}/'
                     f'{item["metric"].get("instance", "?")}' for item in result[:5]]
            context["prometheus"][key] = "; ".join(names)
        elif key.endswith("[namespace]"):
            # One value per Deployment, and the name is the point: this query
            # exists to tell the model *which* object is broken, so dropping the
            # label would leave it exactly as blind as before the query.
            context["prometheus"][key] = "; ".join(
                f'{item["metric"].get("deployment", "?")}={item["value"]}'
                for item in result[:8])
        else:
            value = _first(result)
            if value == "":
                continue
            context["prometheus"][key] = value
        context["sources"].append("prometheus")

    if deployment or pod:
        # Loki's labels are cut out of the log *file path* on the node, not taken
        # from the Kubernetes API (see manifests-logging/04-promtail-configmap.yaml):
        # they are `namespace`, `pod_name` and `container_name`. There is no `pod`
        # label and no `deployment` label in Loki at all, so a selector written
        # with the Prometheus names matches nothing - and the report would claim
        # "no log lines" about a service that is logging the whole time. This was
        # a real bug found by running the bot against the live stand.
        selector = (f'namespace="{namespace}", pod_name=~"{deployment}.*"' if deployment
                    else f'namespace="{namespace}", pod_name="{pod}"')
        try:
            lines = loki.log_lines(
                f'{{{selector}}} |~ "(?i)(error|fail|panic|timeout|refused)"',
                settings.log_window_seconds, settings.max_log_lines)
            if not lines:
                lines = loki.log_lines(f"{{{selector}}}", settings.log_window_seconds,
                                       max(10, settings.max_log_lines // 2))
                if lines:
                    context["missing"].append(
                        "loki: no error-matching lines in the window - "
                        "showing the latest lines instead")
            context["loki"] = lines
            if lines:
                context["sources"].append("loki")
            else:
                context["missing"].append("loki: no log lines in the window")
        except SourceError as exc:
            context["missing"].append(f"loki: {exc}")
            if metrics:
                metrics.inc("aiops_context_failures", source="loki")
    else:
        context["missing"].append(
            "no deployment/pod label in the alert - logs were not queried")

    if not context["prometheus"]:
        context["missing"].append(
            "prometheus: none of the planned queries returned a series")

    for source, info in source_errors.items():
        if info["count"] == 1:
            context["missing"].append(
                f"{source}: {info['sample']}: {info['error']}")
        else:
            context["missing"].append(
                f"{source}: {info['count']} queries failed ({info['error']}); "
                f"first was {info['sample']}")

    context["duration_ms"] = int((time.time() - started) * 1000)
    context["sources"] = sorted(set(context["sources"]))
    return context
