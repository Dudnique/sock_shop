# Sock Shop — a working stand on k3d

> **English** | [Русский](README.ru.md)

A ready-to-run fork of the **Sock Shop** demo application
(upstream: [microservices-demo/microservices-demo](https://github.com/microservices-demo/microservices-demo)).

The repository contains **everything needed to stand the shop up**: a declarative
[Terraform](./terraform/k3d/) definition of the local [k3d](https://k3d.io) cluster,
Kubernetes manifests for every service and database, an Ingress, the monitoring and
alerting stacks, and a one-command deployment script. The Docker images are public
and pulled automatically from Docker Hub, so they are not vendored into the repository.

> What was fixed compared to upstream — see [FIXES.md](./FIXES.md).

---

## Requirements

- Windows (or Linux/macOS) with:
  - [Terraform](https://developer.hashicorp.com/terraform/downloads) >= 1.5
  - [Docker](https://docs.docker.com/get-docker/) (running)
  - [kubectl](https://kubernetes.io/docs/tasks/tools/)

The [k3d](https://k3d.io/) CLI is convenient for inspecting the cluster, but it is
not required: the Terraform provider talks to Docker directly.

Verify:

```powershell
terraform version
docker version
kubectl version --client
```

## Quick start (one command)

```powershell
.\deploy.ps1
```

The script is a thin orchestrator and does **six** things:

1. provisions the k3d cluster with **Terraform** (`terraform/k3d`) - the single
   source of truth for the infrastructure;
2. applies the application manifests (`deploy/kubernetes/manifests`);
3. applies the logging stack (`deploy/kubernetes/manifests-logging` - Loki + Promtail);
4. applies the tracing stack (`deploy/kubernetes/manifests-jaeger`);
5. applies the monitoring stack (`deploy/kubernetes/manifests-monitoring`);
6. applies the alerting stack (`deploy/kubernetes/manifests-alerting`) plus its secrets.

The shop then opens at **http://localhost:8080**.

To tear the whole stand down again:

```powershell
.\deploy.ps1 -Down
```

### Secrets (never committed)

The alerting stack needs two credentials - a Telegram bot token and a Slack
incoming-webhook URL. They are never committed to git; provide them either as
environment variables or in a local, git-ignored file that `deploy.ps1` sources
automatically:

```powershell
# option A: environment variables
$env:TELEGRAM_BOT_TOKEN = '...'
$env:SLACK_WEBHOOK_URL  = 'https://hooks.slack.com/services/...'

# option B: a git-ignored local file
Copy-Item deploy\secrets.local.ps1.example deploy\secrets.local.ps1
# then edit deploy\secrets.local.ps1 and fill in the real values
```

## Manual deployment (without the script)

```powershell
# 1. Cluster + kubeconfig (Terraform is the single source of truth for infra)
cd terraform\k3d
terraform init
terraform apply
$env:KUBECONFIG = (terraform output -raw kubeconfig_path)
cd ..\..

# 2. Application
kubectl apply -f deploy\kubernetes\manifests
kubectl wait --for=condition=available --timeout=300s deployment --all -n sock-shop

# 3. Logging - Loki (store) + Promtail (collector)
kubectl apply -f deploy\kubernetes\manifests-logging

# 4. Tracing - Jaeger
kubectl apply -f deploy\kubernetes\manifests-jaeger

# 5. Monitoring
kubectl apply -f deploy\kubernetes\manifests-monitoring

# 6. Alerting - the secret must exist first, then apply the manifests
kubectl -n monitoring create secret generic alertmanager-secrets `
    --from-literal=telegram_bot_token=$env:TELEGRAM_BOT_TOKEN `
    --from-literal=slack_webhook_url=$env:SLACK_WEBHOOK_URL `
    --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f deploy\kubernetes\manifests-alerting
```

The application can also be applied from a single file:
`kubectl apply -f deploy/kubernetes/complete-demo.yaml`.

## Accessing the application and tools

Everything is reachable from the host **without** `kubectl port-forward`: the host
ports are published by `terraform/k3d`.

| Port  | URL | What |
|-------|-----|------|
| 8080  | <http://localhost:8080>  | Sock Shop (through Traefik/Ingress) |
| 9090  | <http://localhost:9090>  | Prometheus |
| 3000  | <http://localhost:3000>  | Grafana |
| 9093  | <http://localhost:9093>  | Alertmanager |
| 16686 | <http://localhost:16686> | Jaeger UI (traces) |

Grafana (login `admin` / `admin`) is the single entry point for both the logs and
the traces: it comes with a `loki` datasource (logs) and a `jaeger` datasource
(traces), both defined in `manifests-monitoring/20-grafana-configmap.yaml` and
imported by the `grafana-import-dashboards` Job. Loki has **no** UI and **no**
host port on purpose - it is an internal store queried only by Grafana
(`http://loki.logging.svc.cluster.local:3100`): read the logs in **Explore** with
the `loki` datasource, e.g. the query `{namespace="sock-shop"}`.

The front-end Service is `ClusterIP`: the app is reachable **only** through the
Ingress (Traefik) on port `8080`. It used to be a `NodePort` on `30001` before the
ingress was added (see `FIXES.md`). For debugging you can still port-forward:

```powershell
kubectl port-forward -n sock-shop svc/front-end 8081:80
# then http://localhost:8081
```

## Layout

```
.
├── deploy.ps1                              # one-command deployment (Terraform + kubectl)
├── FIXES.md                                # what was fixed and why (EN)
├── FIXES.ru.md                             # what was fixed and why (RU)
├── deploy/
│   └── kubernetes/
│       ├── manifests/                      # application, per-file (00 ... 29)
│       ├── manifests-logging/              # Loki + Promtail (pod logs)
│       ├── manifests-jaeger/               # Jaeger all-in-one (traces)
│       ├── manifests-monitoring/           # Prometheus, Grafana, exporters
│       ├── manifests-alerting/             # Alertmanager (Telegram + Slack)
│       ├── complete-demo.yaml              # application in one file
│       └── helm-chart/                     # alternative: Helm chart
└── terraform/
    └── k3d/                                # declarative definition of the cluster
```

## Stop / remove

```powershell
.\deploy.ps1 -Down             # tear the whole stand down (terraform destroy)
```

Or, to only pause the cluster:

```powershell
k3d cluster stop sock-shop     # stop
k3d cluster start sock-shop    # start again
k3d cluster delete sock-shop   # remove entirely
```

## License

See [LICENSE](./LICENSE). Distributed under the Apache 2.0 license.
