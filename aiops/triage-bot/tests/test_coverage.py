"""Coverage gate: the tier-0 ruleset and the shipped alert rules cannot drift apart.

This is the cheapest guard in the whole component and the one that matters most
day to day: add a rule to `05-prometheus-alertrules.yaml`, forget the decision
table, and the bot escalates an alert it should have handled - or worse, says
nothing about an alert nobody is watching. Forgetting is now a red CI job.

The file is scanned with a regex rather than parsed as YAML on purpose: the
package has no YAML dependency (the pod runs on the standard library), and the
two constructs used here - `- alert: Name` and `aiops: "true"` - are stable.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from triage_bot.adapters import MockAdapter
from triage_bot.model import ACTIONS

RULES_FILE = (Path(__file__).resolve().parents[3]
              / "deploy" / "kubernetes" / "manifests-monitoring"
              / "05-prometheus-alertrules.yaml")

#: Alerts deliberately kept outside the bot's scope. Adding a name here is a
#: written decision (and a line in the docs) - which is the entire point.
NOT_TRIAGED: set = set()

ALERT_RE = re.compile(r"^\s*-\s*alert:\s*([A-Za-z0-9_]+)\s*$")
AIOPS_RE = re.compile(r"^\s*aiops:\s*[\"']?true[\"']?\s*$")


def parse_rules(text: str) -> list:
    """[(alertname, has_the_aiops_label), ...] in file order."""
    rules: list = []
    current = None
    has_label = False
    for line in text.splitlines():
        match = ALERT_RE.match(line)
        if match:
            if current is not None:
                rules.append((current, has_label))
            current, has_label = match.group(1), False
            continue
        if current is not None and AIOPS_RE.match(line):
            has_label = True
    if current is not None:
        rules.append((current, has_label))
    return rules


class CoverageGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(RULES_FILE.exists(), f"rules file not found: {RULES_FILE}")
        self.rules = parse_rules(RULES_FILE.read_text(encoding="utf-8"))
        self.names = [name for name, _labelled in self.rules]

    def test_the_rules_file_was_actually_parsed(self) -> None:
        # A gate that silently parses nothing would always be green.
        self.assertGreaterEqual(len(self.rules), 11, self.rules)

    def test_tier0_knows_every_rule_the_stand_ships(self) -> None:
        missing = [name for name in self.names if name not in MockAdapter.RULES]
        self.assertEqual(missing, [], f"no tier-0 decision for: {missing}")

    def test_tier0_has_no_decision_for_a_rule_that_does_not_exist(self) -> None:
        stale = sorted(set(MockAdapter.RULES) - set(self.names))
        self.assertEqual(stale, [], f"tier-0 refers to rules that are gone: {stale}")

    def test_every_rule_is_labelled_for_the_bot(self) -> None:
        unlabelled = [name for name, labelled in self.rules
                      if not labelled and name not in NOT_TRIAGED]
        self.assertEqual(
            unlabelled, [],
            "These rules fire without aiops=\"true\", so the bot never sees them. "
            "Add the label, or add the name to NOT_TRIAGED with a reason: "
            f"{unlabelled}")

    def test_every_tier0_decision_stays_inside_the_whitelist(self) -> None:
        for name, (action_id, confidence, reason) in MockAdapter.RULES.items():
            self.assertIn(action_id, ACTIONS, name)
            self.assertGreaterEqual(confidence, 0.0, name)
            self.assertLessEqual(confidence, 1.0, name)
            self.assertTrue(reason.strip(), name)

    def test_probe_failed_stays_below_the_threshold(self) -> None:
        """The cascade's model tier is exercised by a real rule, not by a test only."""
        confidence = MockAdapter.RULES["ProbeFailed"][1]
        self.assertLess(confidence, 0.7)



class PremiseThresholdTests(unittest.TestCase):
    def test_the_flapping_threshold_is_the_one_the_rule_uses(self) -> None:
        """The premise check re-asks the rule's own question, with the rule's number."""
        text = RULES_FILE.read_text(encoding="utf-8")
        match = re.search(
            r"alert:\s*PodRestartFlapping\s+expr:.*\[15m\]\)\s*>\s*(\d+)", text)
        self.assertIsNotNone(match, "PodRestartFlapping expr not found or changed shape")
        self.assertEqual(int(match.group(1)), MockAdapter.FLAPPING_RESTARTS_THRESHOLD)


if __name__ == "__main__":
    unittest.main()
