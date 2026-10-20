"""The `/metrics` endpoint must be *parsable*, not merely non-empty.

There was no test here at all, and the gap cost a night: `render()` emitted
`# HELP ('aiops_alerts_received', ()) ...` because the counters dictionary was
unpacked one tuple too shallow. The output still contained every metric name, so
a `assertIn("aiops_alerts_received", text)` would have passed - but Prometheus
rejects the **whole scrape** on the first invalid line, `up` becomes 0, and the
`ScrapeTargetDown` rule fires on the bot's own endpoint. The bot then triaged
itself, sent a card, Alertmanager sent its own message, and the loop repeated
after every restart. That is a formatting bug that presents as an outage.

So these tests parse the exposition the way Prometheus does - line by line, with
the text format's grammar - instead of looking for substrings.
"""

from __future__ import annotations

import re
import unittest

from triage_bot.metrics import Metrics

#: `# HELP <name> <text>` / `# TYPE <name> counter|gauge`, with the metric name
#: grammar from the Prometheus exposition format.
META_RE = re.compile(r"^# (HELP|TYPE) ([a-zA-Z_:][a-zA-Z0-9_:]*)(?: (.*))?$")
#: `<name>{<labels>} <value>`; the label block is optional.
SAMPLE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{.*\})? (-?[0-9.eE+]+)$")


def lines_of(text: str) -> list:
    return [line for line in text.splitlines() if line.strip()]


def parse(text: str) -> tuple:
    """Split the exposition into (help, type, samples); fail on anything else."""
    help_lines, type_lines, samples = {}, {}, []
    for line in lines_of(text):
        if line.startswith("#"):
            match = META_RE.match(line)
            if not match:
                raise AssertionError(f"unparsable meta line: {line!r}")
            kind, name, rest = match.groups()
            if kind == "HELP":
                if not rest:
                    raise AssertionError(f"HELP without text: {line!r}")
                help_lines[name] = rest
            else:
                if rest not in ("counter", "gauge"):
                    raise AssertionError(f"unknown metric type: {line!r}")
                type_lines[name] = rest
            continue
        match = SAMPLE_RE.match(line)
        if not match:
            raise AssertionError(f"unparsable sample line: {line!r}")
        samples.append((match.group(1), match.group(2) or "", match.group(3)))
    return help_lines, type_lines, samples


class ExpositionFormatTests(unittest.TestCase):
    def test_the_exposition_parses(self) -> None:
        metrics = Metrics()
        metrics.inc("aiops_alerts_received", status="firing")
        metrics.set("aiops_last_incident_timestamp_seconds", 1700000000.0)
        parse(metrics.render())  # raises on the first invalid line

    def test_a_metric_name_never_starts_with_a_bracket(self) -> None:
        """The exact regression: `# HELP ('name', ()) …` killed the scrape."""
        metrics = Metrics()
        metrics.inc("aiops_alerts_received", status="firing")
        text = metrics.render()
        self.assertNotIn("# HELP (", text)
        self.assertNotIn("# TYPE (", text)

    def test_every_sample_has_help_and_type(self) -> None:
        metrics = Metrics()
        metrics.inc("aiops_model_calls", adapter="deepseek")
        metrics.inc("aiops_model_calls", adapter="mock")
        metrics.inc("aiops_incidents", action_id="rollout_restart")
        help_lines, type_lines, samples = parse(metrics.render())
        names = {name for name, _labels, _value in samples}
        self.assertEqual(names, set(help_lines))
        self.assertEqual(names, set(type_lines))

    def test_help_names_the_metric_before_describing_it(self) -> None:
        metrics = Metrics()
        metrics.inc("aiops_alerts_received", status="firing")
        text = metrics.render()
        for line in lines_of(text):
            if line.startswith("# HELP "):
                name = line.split(" ", 3)[2]
                self.assertTrue(line.startswith(f"# HELP {name} "))

    def test_one_help_per_name_and_not_per_label_set(self) -> None:
        metrics = Metrics()
        metrics.inc("aiops_alerts_received", status="firing")
        metrics.inc("aiops_alerts_received", status="resolved")
        text = metrics.render()
        self.assertEqual(text.count("# HELP aiops_alerts_received "), 1)
        self.assertEqual(text.count("# TYPE aiops_alerts_received "), 1)
        self.assertEqual(len([l for l in lines_of(text) if l.startswith("aiops_alerts_received{")]), 2)

    def test_the_incident_timestamp_is_a_gauge(self) -> None:
        metrics = Metrics()
        metrics.inc("aiops_incidents", action_id="x")
        metrics.set("aiops_last_incident_timestamp_seconds", 1700000000.0)
        _help, type_lines, _samples = parse(metrics.render())
        self.assertEqual(type_lines["aiops_incidents"], "counter")
        self.assertEqual(type_lines["aiops_last_incident_timestamp_seconds"], "gauge")

    def test_build_info_is_a_gauge_and_is_always_present(self) -> None:
        """An idle bot still exposes one sample, so /metrics is visibly alive."""
        metrics = Metrics()
        metrics.set("aiops_build_info", 1, version="0.1.0-m1", tier1="deepseek")
        _help, type_lines, samples = parse(metrics.render())
        self.assertEqual(type_lines["aiops_build_info"], "gauge")
        self.assertEqual(samples, [("aiops_build_info",
                                   '{tier1="deepseek",version="0.1.0-m1"}', "1")])

    def test_label_values_are_escaped(self) -> None:
        metrics = Metrics()
        metrics.inc("aiops_model_errors", adapter='a"b\\c\nd')
        sample = [l for l in lines_of(metrics.render()) if l.startswith("aiops_model_errors{")][0]
        self.assertIn(r'adapter="a\"b\\c\nd"', sample)

    def test_an_empty_registry_is_not_a_parse_error(self) -> None:
        self.assertEqual(parse(Metrics().render()), ({}, {}, []))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
