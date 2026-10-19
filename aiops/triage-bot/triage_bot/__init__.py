"""AIOps triage bot for the sock-shop stand (milestone M1: advisor only).

The bot receives Alertmanager webhooks, collects read-only context from
Prometheus and Loki, asks a model adapter for a decision, validates that
decision against a strict schema and a fixed action whitelist, and sends the
result to Telegram with approve / advice / ignore buttons.

Deliberate M1 limits (see README.md in this directory):

* **no Kubernetes API and no ServiceAccount** - the pod is deployed with
  `automountServiceAccountToken: false`, the code imports no client and shells
  out to no command, so it *cannot* perform an action even if the model asks;
* the model is a swappable adapter (`mock` by default), and only the `mock`
  adapter is used offline, so the whole pipeline is reproducible without an API
  key and without internet access;
* an approved decision is recorded and the command that M2 *would* run is shown
  to a human - nothing is executed.
"""

__version__ = "0.1.0-m1"
