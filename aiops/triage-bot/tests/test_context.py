"""Context collection: the queries, the log window and the honest gaps."""

from __future__ import annotations

import unittest

from triage_bot.context import LokiClient, collect_context, plan_queries
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

    def test_empty_window_is_reported(self) -> None:
        context = collect_context(
            {"labels": {"namespace": "sock-shop", "deployment": "front-end"}},
            make_settings(), FakePrometheus(), FakeLoki([]))
        self.assertTrue(any("no log lines" in gap for gap in context["missing"]))

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


if __name__ == "__main__":
    unittest.main()
