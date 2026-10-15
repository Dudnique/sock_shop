# Alerting

Prometheus evaluates the alert rules; **Alertmanager** takes the firing alerts
and decides *where* to send them. This directory holds Alertmanager, its routing
config and its Service.

Pinned image: `prom/alertmanager:v0.34.1` (never `:latest` - reproducible stand).

## Files

| File | Purpose |
| ---- | ------- |
| `alertmanager-configmap.yaml` | Routing tree + receivers (Telegram, Slack). |
| `alertmanager-dep.yaml` | Deployment; mounts the config and the secrets. |
| `alertmanager-svc.yaml` | Service on port 9093. |
| `examples/` | Samples that `kubectl apply -f <dir>` must NOT pick up. |

## Two channels, and why `continue: true`

Both Telegram and Slack receive every alert. Alertmanager walks the routing tree
and, by default, follows **only the first matching child**, then stops - which
would skip the root receiver entirely. To fan out to both, the config keeps the
root receiver plus **two child routes without matchers**, the first carrying
`continue: true` so evaluation proceeds to the next sibling:

    route:
      receiver: 'telegram'        # fallback: only if no child matches
      routes:
        - receiver: 'telegram'
          continue: true          # keep going to the next sibling
        - receiver: 'slack'

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