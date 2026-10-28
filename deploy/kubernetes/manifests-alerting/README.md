# Alerting

Prometheus evaluates the alert rules; **Alertmanager** takes the firing alerts
and decides *where* to send them. This directory holds Alertmanager, its routing
config and its Service.

Pinned image: `prom/alertmanager:v0.34.1` (never `:latest` - reproducible stand).

## Files

| File | Purpose |
| ---- | ------- |
| `alertmanager-configmap.yaml` | Routing tree + receivers (Telegram, Slack, the triage bot webhook). |
| `alertmanager-dep.yaml` | Deployment; mounts the config and the secrets. |
| `alertmanager-svc.yaml` | Service on port 9093. |
| `examples/` | Samples that `kubectl apply -f <dir>` must NOT pick up. |

## Three receivers, and why `continue: true`

Telegram, Slack and the triage bot (`aiops-triage`) all see the same firing
alerts. Alertmanager walks the routing tree and, by default, follows **only the
first matching child** and then stops - so every route that must not be skipped
carries `continue: true`:

    route:
      receiver: 'telegram'        # fallback: only if no child matches
      routes:
        - receiver: 'aiops-triage'
          matchers: ['aiops="true"']
          continue: true          # the two human channels below still run
        - receiver: 'telegram'
          matchers: ['severity="critical"']
          continue: true          # a critical alert also reaches Slack below
        - receiver: 'slack'       # no matcher: every alert, so warnings land here

The bot's route is guarded by a matcher, so only the rules that opted in with
`aiops: "true"` reach it. The humans are routed by severity: a `critical` alert
goes to Telegram and Slack, a `warning` to Slack only. Before that every alert
reached both channels (and the bot's card on top), which is what made a short
`ScrapeTargetDown` or a `ProbeSlow` next to a `ProbeFailed` feel like spam.

One `inhibit_rules` entry removes a real duplicate: while `ProbeFailed` fires for
a probe, `ProbeSlow` for the same `instance` is muted - a probe that times out is
also slower than 2s. Check a route without sending anything:

    kubectl exec -n monitoring deploy/alertmanager -- amtool config routes test       --config.file=/etc/alertmanager/config.yml severity=warning aiops=true

## The triage bot route

`aiops-triage` is a plain `webhook_configs` entry pointing at
`http://triage-bot.monitoring.svc.cluster.local:8080/alert` (deployed from
`../manifests-aiops`). Two details are deliberate:

* **No authentication.** The endpoint is a ClusterIP reachable only from a pod of
  the `monitoring` namespace, which is what
  `../manifests-aiops/04-triage-bot-netpol.yaml` enforces - the network is the
  credential. A shared token would add a second secret to protect a port that is
  already unreachable from anywhere else.
* **`send_resolved: false`.** The bot triages firing incidents; a resolved
  webhook is answered `200` and recorded as `skipped: resolved`, so sending them
  would only inflate `aiops_alerts_skipped{reason="resolved"}`. Humans still get
  the resolution through the `slack` receiver (and `telegram` for criticals).

If the bot is down, Alertmanager's webhook fails and nothing else changes: the
human routes are independent siblings. A bot that were the single notifier would
be worse than no bot at all.

## Secret (never in git)

The config references a Kubernetes Secret named **`alertmanager-secrets`** in
namespace `monitoring`. Its keys are mounted as files and read with `*_file`
options, so no credential ever appears in this ConfigMap or in git:

| Secret key | Consumed as |
| ---------- | ----------- |
| `telegram_bot_token` | `telegram_configs.bot_token_file` |
| `slack_webhook_url`  | `slack_configs.api_url_file` |

`deploy.ps1` creates it idempotently from `deploy\secrets.local.ps1` (git-ignored), and if
that file is absent it reads the two values back out of the Secret already in the cluster,
so no token is ever typed as a literal argument; the `TELEGRAM_BOT_TOKEN` and
`SLACK_WEBHOOK_URL` environment variables are the CI path.

## Alert rules

Rules live next to Prometheus:
`../manifests-monitoring/05-prometheus-alertrules.yaml`.

## UI

Reachable without `port-forward` at **http://localhost:9093** - the host port is
published by `terraform/k3d` through the `exposed_node_ports` variable.