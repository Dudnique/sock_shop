"""Context collection: the queries, the log window and the honest gaps."""

from __future__ import annotations

import unittest

from triage_bot.context import (LokiClient, collect_context, plan_queries,
                                resolve_deployment)
from tests.stubs import FakeLoki, FakePrometheus, make_settings


class PlanQueriesTests(unittest.TestCase):
    def test_deployment_alert_asks_about_that_deployment_only(self) -> None:
        queries = dict(plan_queries({"namespace": "sock-shop", "deployment": "front-end"}))
        self.assertIn("replicas_available[front-end]", queries)
        self.assertIn('deployment="front-end"', queries["replicas_available[front-end]"])
        self.assertNotIn("node_ready_condition", " ".join(queries))

    def test_node_alert_asks_about_nodes(self) -> None:
        queries = dict(plan_queries({"node": "k3d-sock-shop-agent-0"}))
        self.assertTrue(any(key.startswith("node_ready_condition") for key in queries))
        self.assertTrue(any(key.startswith("filesystem_") for key in queries))

    def test_probe_alert_asks_for_probe_success(self) -> None:
        queries = dict(plan_queries({"probe_scope": "ingress"}))
        self.assertIn("probe_success", queries)

    def test_a_label_less_alert_asks_the_namespace_which_deployments_are_broken(self) -> None:
        """A probe alert names no workload, so the bot asks the namespace.

        Without this query the report for a scaled-to-zero front-end contained
        only `probe_success=0`, and the model had no object it could name.
        """
        queries = dict(plan_queries({"namespace": "sock-shop", "probe_scope": "ingress"}))
        self.assertIn("deployments_at_zero[namespace]", queries)
        self.assertIn("deployments_unavailable[namespace]", queries)
        self.assertIn('namespace="sock-shop"', queries["deployments_at_zero[namespace]"])
        self.assertNotIn("replicas_available[", " ".join(queries))

    def test_always_asks_about_down_targets(self) -> None:
        queries = dict(plan_queries({}))
        self.assertIn("targets_down", queries)
        self.assertIn("down_targets", queries)


class CollectContextTests(unittest.TestCase):
    def test_metrics_and_logs_are_collected(self) -> None:
        settings = make_settings()
        prometheus = FakePrometheus({"replicas_available": 0, "max_pod_restarts": 5})
        loki = FakeLoki(["12:00 front-end-1: error"])
        context = collect_context(
            {"labels": {"namespace": "sock-shop", "deployment": "front-end"}},
            settings, prometheus, loki)
        self.assertEqual(context["sources"], ["loki", "prometheus"])
        self.assertIn("replicas_available[front-end]", context["prometheus"])
        self.assertEqual(context["loki"], ["12:00 front-end-1: error"])
        self.assertEqual(context["missing"], [])

    def test_unreachable_prometheus_is_a_gap_not_a_crash(self) -> None:
        context = collect_context(
            {"labels": {"namespace": "sock-shop", "deployment": "front-end"}},
            make_settings(), FakePrometheus(fail=True), FakeLoki(["line"]))
        self.assertTrue(any("prometheus" in gap for gap in context["missing"]))
        self.assertEqual(context["prometheus"], {})

    def test_alert_without_deployment_says_logs_were_not_queried(self) -> None:
        loki = FakeLoki(["line"])
        context = collect_context({"labels": {"alertname": "HighErrorRate"}},
                                  make_settings(), FakePrometheus(), loki)
        self.assertEqual(loki.queries, [])
        self.assertTrue(any("no deployment/pod label" in gap
                            for gap in context["missing"]))

    def test_namespace_level_health_keeps_the_deployment_names(self) -> None:
        """The value has to carry the name, or the query adds nothing.

        `deployments_at_zero[namespace] = 0` would tell the model nothing it can
        act on; `front-end=0` is what turns "the probe fails" into "this object
        is scaled to zero".
        """
        class NamedPrometheus(FakePrometheus):
            def query(self, expression: str) -> list:
                self.queries.append(expression)
                if "kube_deployment_spec_replicas" in expression:
                    return [{"metric": {"deployment": "front-end"}, "value": "0"}]
                return []

        context = collect_context(
            {"labels": {"namespace": "sock-shop", "alertname": "ProbeFailed"}},
            make_settings(), NamedPrometheus(), FakeLoki([]))
        self.assertEqual(context["prometheus"]["deployments_at_zero[namespace]"],
                         "front-end=0")

    def test_empty_window_is_reported(self) -> None:
        context = collect_context(
            {"labels": {"namespace": "sock-shop", "deployment": "front-end"}},
            make_settings(), FakePrometheus(), FakeLoki([]))
        self.assertTrue(any("no log lines" in gap for gap in context["missing"]))

    def test_a_scaled_to_zero_deployment_says_so_instead_of_no_log_lines(self) -> None:
        """A deployment at `desired=0` has no pod, so there is nothing to log.

        The M1 demo produced "loki: no log lines in the window" for a front-end
        that had been scaled to zero - the same wording as the selector bug, and
        therefore the wrong clue for whoever reads the card. The number that
        settles it is already in the context, so the gap names it.
        """
        context = collect_context(
            {"labels": {"namespace": "sock-shop", "deployment": "front-end"}},
            make_settings(),
            FakePrometheus({"kube_deployment_spec_replicas": 0,
                            "kube_deployment_status_replicas_available": 0}),
            FakeLoki([]))
        joined = " ".join(context["missing"])
        self.assertIn("scaled to 0", joined)
        self.assertNotIn("no log lines", joined)

    def test_a_deployment_with_pods_keeps_the_plain_wording(self) -> None:
        """Only a known zero changes the sentence; a live-but-silent pod does not."""
        context = collect_context(
            {"labels": {"namespace": "sock-shop", "deployment": "front-end"}},
            make_settings(),
            FakePrometheus({"kube_deployment_spec_replicas": 1,
                            "kube_deployment_status_replicas_available": 1}),
            FakeLoki([]))
        self.assertIn("loki: no log lines in the window", context["missing"])

    def test_unknown_desired_replicas_is_not_treated_as_scaled_to_zero(self) -> None:
        """Silence is not evidence: without the number the wording stays honest."""
        context = collect_context(
            {"labels": {"namespace": "sock-shop", "deployment": "front-end"}},
            make_settings(), FakePrometheus(), FakeLoki([]))
        self.assertIn("loki: no log lines in the window", context["missing"])

    def test_error_filter_falls_back_to_latest_lines(self) -> None:
        class OnceEmptyLoki(FakeLoki):
            def log_lines(self, logql, window_seconds, limit):
                self.queries.append(logql)
                if "|~" in logql:
                    return []
                return ["12:00 plain line"]

        context = collect_context(
            {"labels": {"namespace": "sock-shop", "deployment": "front-end"}},
            make_settings(), FakePrometheus(), OnceEmptyLoki())
        self.assertEqual(context["loki"], ["12:00 plain line"])
        self.assertTrue(any("no error-matching lines" in gap for gap in context["missing"]))

    def test_loki_selector_uses_the_labels_promtail_actually_writes(self) -> None:
        """`pod` is a Prometheus label; in Loki the same thing is `pod_name`.

        The Prometheus names were used here first, and on the live stand every
        log query silently returned nothing while the service was logging the
        whole time - so the selector itself is asserted, not only the plumbing
        around it.
        """
        loki = FakeLoki(["12:00 front-end-1: error"])
        collect_context(
            {"labels": {"namespace": "sock-shop", "deployment": "front-end"}},
            make_settings(), FakePrometheus({"replicas_available": 0}), loki)
        self.assertTrue(loki.queries, "loki was never queried")
        self.assertIn('pod_name=~"front-end.*"', loki.queries[0])
        self.assertNotIn(", pod=", loki.queries[0])

    def test_a_pod_only_alert_selects_that_pod_name(self) -> None:
        loki = FakeLoki(["12:00 line"])
        collect_context(
            {"labels": {"namespace": "sock-shop", "pod": "front-end-7d9f4b-abcde"}},
            make_settings(), FakePrometheus(), loki)
        self.assertIn('pod_name="front-end-7d9f4b-abcde"', loki.queries[0])


class LokiClientParsingTests(unittest.TestCase):
    """The Loki response is reshaped: newest-first becomes oldest-first."""

    def test_lines_are_ordered_oldest_first_and_labelled(self) -> None:
        payload = {"status": "success", "data": {"result": [{
            "stream": {"pod_name": "front-end-abc"},
            "values": [["1700000002000000000", "second line"],
                       ["1700000001000000000", "first line"]]}]}}
        import triage_bot.context as context_mod

        saved = context_mod.http_get_json
        context_mod.http_get_json = lambda url, params, timeout: payload
        try:
            lines = LokiClient("http://loki.test").log_lines('{job="pods"}', 60, 10)
        finally:
            context_mod.http_get_json = saved

        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith("front-end-abc: first line"), lines[0])
        self.assertTrue(lines[1].endswith("front-end-abc: second line"), lines[1])

    def test_a_failing_source_raises_SourceError(self) -> None:
        import triage_bot.context as context_mod
        from triage_bot.context import SourceError

        saved = context_mod.http_get_json

        def boom(url, params, timeout):
            raise SourceError("loki is down")

        context_mod.http_get_json = boom
        try:
            with self.assertRaises(SourceError):
                LokiClient("http://loki.test").log_lines('{job="pods"}', 60, 10)
        finally:
            context_mod.http_get_json = saved



def owner(name: str) -> list:
    """A kube-state-metrics answer: one series whose label names the owner."""
    return [{"metric": {"owner_name": name}, "value": "1"}]


class ResolveDeploymentTests(unittest.TestCase):
    """PodCrashLooping names a pod and no Deployment; the owner chain names it."""

    LABELS = {"namespace": "sock-shop", "pod": "catalogue-67b8dccb79-kmbhk"}

    def test_pod_resolves_through_the_replicaset(self) -> None:
        prometheus = FakePrometheus({"kube_pod_owner": owner("catalogue-67b8dccb79"),
                                     "kube_replicaset_owner": owner("catalogue")})
        self.assertEqual(resolve_deployment(self.LABELS, prometheus), ("catalogue", ""))
        # Two lookups, and the second one asks about the ReplicaSet the first returned.
        self.assertIn('replicaset="catalogue-67b8dccb79"', prometheus.queries[1])

    def test_an_alert_that_already_names_the_deployment_is_left_alone(self) -> None:
        prometheus = FakePrometheus()
        labels = {**self.LABELS, "deployment": "front-end"}
        self.assertEqual(resolve_deployment(labels, prometheus), ("", ""))
        self.assertEqual(prometheus.queries, [])

    def test_a_pod_without_an_owner_is_a_named_gap(self) -> None:
        name, note = resolve_deployment(self.LABELS, FakePrometheus())
        self.assertEqual(name, "")
        self.assertIn("no ReplicaSet owner", note)

    def test_a_replicaset_without_a_deployment_is_a_named_gap(self) -> None:
        prometheus = FakePrometheus({"kube_pod_owner": owner("job-abc")})
        name, note = resolve_deployment(self.LABELS, prometheus)
        self.assertEqual(name, "")
        self.assertIn("no Deployment owner", note)

    def test_unreachable_prometheus_is_a_gap_not_a_crash(self) -> None:
        name, note = resolve_deployment(self.LABELS, FakePrometheus(fail=True))
        self.assertEqual(name, "")
        self.assertIn("not resolved", note)

    def test_a_hand_posted_pod_name_cannot_break_out_of_the_selector(self) -> None:
        prometheus = FakePrometheus()
        labels = {"namespace": "sock-shop", "pod": 'x"} or vector(1) #'}
        name, note = resolve_deployment(labels, prometheus)
        self.assertEqual(name, "")
        self.assertIn("not a valid object name", note)
        self.assertEqual(prometheus.queries, [], "nothing may be sent to Prometheus")


class PodAlertContextTests(unittest.TestCase):
    ALERT = {"labels": {"alertname": "PodCrashLooping", "namespace": "sock-shop",
                        "pod": "catalogue-67b8dccb79-kmbhk", "container": "catalogue"}}

    def collect(self, answers: dict) -> dict:
        return collect_context(self.ALERT, make_settings(), FakePrometheus(answers), FakeLoki())

    def test_the_resolved_deployment_is_stated_first_and_drives_the_queries(self) -> None:
        context = self.collect({"kube_pod_owner": owner("catalogue-67b8dccb79"),
                                "kube_replicaset_owner": owner("catalogue")})
        self.assertEqual(context["resolved"]["deployment"], "catalogue")
        self.assertEqual(next(iter(context["prometheus"])),
                         "deployment_of_pod[catalogue-67b8dccb79-kmbhk]")
        self.assertIn("replicas_available[catalogue]", [key for key, _ in plan_queries(
            {**self.ALERT["labels"], "deployment": "catalogue"})])
        self.assertEqual(self.ALERT["labels"].get("deployment"), None,
                         "the alert as received must not be edited")

    def test_waiting_reasons_keep_their_names(self) -> None:
        context = self.collect({
            "kube_pod_container_status_waiting_reason":
                [{"metric": {"reason": "CrashLoopBackOff"}, "value": "1"}]})
        self.assertEqual(
            context["prometheus"]["pod_waiting_reasons[catalogue-67b8dccb79-kmbhk]"],
            "CrashLoopBackOff=1")

    def test_an_answer_with_no_series_is_recorded_as_empty_not_as_missing(self) -> None:
        context = self.collect({})
        self.assertIn("pod_waiting_reasons[catalogue-67b8dccb79-kmbhk]", context["empty"])
        self.assertNotIn("pod_waiting_reasons[catalogue-67b8dccb79-kmbhk]",
                         " ".join(context["missing"]))

    def test_a_failed_query_is_not_recorded_as_empty(self) -> None:
        context = collect_context(self.ALERT, make_settings(),
                                  FakePrometheus(fail=True), FakeLoki())
        self.assertEqual(context["empty"], [])


if __name__ == "__main__":
    unittest.main()
