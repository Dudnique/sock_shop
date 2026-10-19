"""The manifests and the code cannot drift apart.

Every assertion here comes out of a live run or a live failure mode:

* an environment variable in the ConfigMap that the image never reads is a silent
  typo - the bot keeps its default and nobody notices until a demo;
* an image tag that does not match the one `deploy.ps1` builds is an
  `ImagePullBackOff` on a stand where the image is imported, not published;
* a guardrail that quietly disappears from the Deployment turns "the bot cannot
  act" from a fact into a hope - and the same three lines are enforced in CI by
  the `aiops-tests` job, from a different direction.

The files are scanned with regular expressions rather than parsed as YAML on
purpose, exactly like `test_coverage.py`: this component has no YAML dependency
at runtime, and the constructs used here (`data:` keys at two spaces,
`<key>: <value>` fields) are stable.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
AIOPS_DIR = ROOT / "deploy" / "kubernetes" / "manifests-aiops"
CONFIG_PY = Path(__file__).resolve().parents[1] / "triage_bot" / "config.py"
DEPLOY_PS1 = ROOT / "deploy.ps1"
ALERTMANAGER_CONFIG = (ROOT / "deploy" / "kubernetes" / "manifests-alerting"
                       / "alertmanager-configmap.yaml")

#: `_env("NAME")`, `_env_int("NAME", 1)`, `_env_float("NAME", 1.0)` - the three
#: ways config.py reads a variable, and therefore the complete list of names the
#: image understands.
ENV_READ_RE = re.compile(r'_env(?:_int|_float)?\(\s*"([A-Z0-9_]+)"')
DATA_KEY_RE = re.compile(r"^  ([A-Za-z][A-Za-z0-9_]*):")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def configmap_keys(text: str) -> list:
    """The `data:` keys of the document, in file order."""
    keys, inside = [], False
    for line in text.splitlines():
        if re.match(r"^data:\s*$", line):
            inside = True
            continue
        if inside:
            if line.strip() and not line.startswith(" "):
                break  # left the data block
            match = DATA_KEY_RE.match(line)
            if match:
                keys.append(match.group(1))
    return keys


class ConfigMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(AIOPS_DIR.is_dir(), f"manifests not found: {AIOPS_DIR}")
        self.text = read(AIOPS_DIR / "01-triage-bot-configmap.yaml")
        self.keys = configmap_keys(self.text)

    def test_the_configmap_was_actually_parsed(self) -> None:
        # A gate that silently reads nothing would always be green.
        self.assertGreaterEqual(len(self.keys), 15, self.keys)

    def test_every_key_is_an_environment_variable_the_code_reads(self) -> None:
        known = set(ENV_READ_RE.findall(read(CONFIG_PY)))
        unknown = [key for key in self.keys if key not in known]
        self.assertEqual(
            unknown, [],
            "these keys are set but no code reads them - a typo here means the bot "
            f"silently keeps its default: {unknown}")

    def test_the_settings_the_deployment_depends_on_are_all_set(self) -> None:
        for key in ("MODEL_ADAPTER", "PROMETHEUS_URL", "LOKI_URL", "ALERTMANAGER_URL",
                    "REQUIRE_LABEL", "ALLOWED_NAMESPACES", "JOURNAL_DIR",
                    "TELEGRAM_TOKEN_FILE"):
            self.assertIn(key, self.keys, f"{key} is not configured in the ConfigMap")


class DeploymentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = read(AIOPS_DIR / "02-triage-bot-dep.yaml")

    def test_the_pod_is_handed_no_cluster_token(self) -> None:
        self.assertIn("automountServiceAccountToken: false", self.text)

    def test_no_service_account_and_no_rbac_anywhere_in_the_directory(self) -> None:
        self.assertNotIn("serviceAccountName:", self.text)
        for path in sorted(AIOPS_DIR.glob("*.yaml")):
            self.assertNotIn("ClusterRole", read(path), path.name)
            self.assertNotIn("kind: Role", read(path), path.name)

    def test_the_container_keeps_a_read_only_root_and_no_privileges(self) -> None:
        self.assertIn("readOnlyRootFilesystem: true", self.text)
        self.assertIn("allowPrivilegeEscalation: false", self.text)

    def test_the_image_tag_is_the_one_deploy_ps1_builds(self) -> None:
        """A mismatch is an ImagePullBackOff: the tag is imported, not published."""
        built = re.search(r'\$botImage\s*=\s*"([^"]+)"', read(DEPLOY_PS1))
        self.assertIsNotNone(built, "deploy.ps1 no longer defines $botImage")
        self.assertIn(f"image: {built.group(1)}", self.text)

    def test_the_journal_and_the_secrets_are_mounted(self) -> None:
        self.assertIn("mountPath: /data", self.text)
        self.assertIn("secretName: aiops-llm", self.text)
        self.assertIn("secretName: alertmanager-secrets", self.text)
        # Without `optional` a missing key would stop the pod from starting, while
        # the whole cascade is designed to degrade loudly instead. Comments are
        # dropped first: the file explains `optional: true` twice, and prose is not
        # a setting.
        settings = [line.strip() for line in self.text.splitlines()
                    if not line.lstrip().startswith("#")]
        self.assertEqual(settings.count("optional: true"), 2,
                         "both secret mounts must be optional")


class ServiceAndPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = read(AIOPS_DIR / "03-triage-bot-svc.yaml")
        self.policy = read(AIOPS_DIR / "04-triage-bot-netpol.yaml")

    def test_the_service_is_scraped_by_the_existing_prometheus_job(self) -> None:
        # `kubernetes-service-endpoints` keeps only Services carrying this
        # annotation, so without it the bot's own metrics never appear.
        self.assertIn('prometheus.io/scrape: "true"', self.service)
        self.assertIn('prometheus.io/path: "/metrics"', self.service)
        self.assertIn("port: 8080", self.service)

    def test_the_service_is_not_published_outside_the_cluster(self) -> None:
        self.assertIn("type: ClusterIP", self.service)
        self.assertNotIn("nodePort", self.service)

    def test_ingress_is_the_webhook_and_the_scrape_only(self) -> None:
        self.assertIn("app: alertmanager", self.policy)
        self.assertIn("app: prometheus", self.policy)
        ingress = self.policy.split("egress:", 1)[0]
        self.assertNotIn("0.0.0.0/0", ingress)
        self.assertNotIn("namespaceSelector: {}", ingress)

    def test_egress_names_every_destination_and_stays_explicit(self) -> None:
        for needle in ("kubernetes.io/metadata.name: kube-system",
                       "port: 53",
                       "port: 9090",
                       "kubernetes.io/metadata.name: logging",
                       "port: 3100",
                       "cidr: 0.0.0.0/0",
                       "port: 443"):
            self.assertIn(needle, self.policy, f"missing egress destination: {needle}")


class AlertmanagerWiringTests(unittest.TestCase):
    """The route and the Service are two halves of one contract."""

    def setUp(self) -> None:
        self.config = read(ALERTMANAGER_CONFIG)

    def test_the_bot_is_a_receiver_pointing_at_its_service(self) -> None:
        self.assertIn("- name: 'aiops-triage'", self.config)
        self.assertIn(
            "url: 'http://triage-bot.monitoring.svc.cluster.local:8080/alert'",
            self.config)

    def test_only_labelled_alerts_reach_the_bot(self) -> None:
        self.assertIn('aiops="true"', self.config)
        self.assertIn('REQUIRE_LABEL: "aiops"',
                      read(AIOPS_DIR / "01-triage-bot-configmap.yaml"))

    def test_the_route_continues_to_the_human_channels(self) -> None:
        # `continue: true` is what keeps a bot outage from becoming a silent one.
        route = self.config.split("- receiver: 'aiops-triage'", 1)[1]
        route = route.split("- receiver: 'telegram'", 1)[0]
        self.assertIn("continue: true", route)


if __name__ == "__main__":
    unittest.main()

