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

## Scope and limitations

This is a **personal lab, not a production system**. Saying that up front is part of the
point: the interesting work here is the *shape* of the platform, not the traffic it serves.

| What this is | What this is not |
|---|---|
| A reproducible k3s/k3d cluster defined in Terraform, brought up by one command (`.\deploy.ps1`) | A cloud cluster: no cloud account, no managed control plane, no load balancer |
| 14 workloads (8 services + 5 databases + RabbitMQ), Ingress, 8 HPA, 17 NetworkPolicy | Multi-tenant: one operator, one namespace per layer, no dev/stage/prod separation |
| All three observability pillars wired end to end: 14 alert rules in 4 groups, 7 dashboards, 3 data sources | Production durability: Prometheus TSDB is on `emptyDir`, Jaeger is in-memory, Loki is on `emptyDir`, so logs live only as long as its pod |
| Alerts that carry `namespace/deployment/pod` and a runbook command, delivered to Telegram **and** Slack | An on-call rota, escalation policy, error budgets or SLO reporting |
| An AIOps triage bot that validates LLM output against a whitelist and acts only through a scoped ServiceAccount | Anything touching real user data: no PII, no payment integration, no real customers |
| 136 automated tests for the bot, plus CI gates (`kubeconform`, `helm lint`, chart↔manifest parity, `terraform validate`, `promtool`) | A secret store: `Secret` objects plus a git-ignored local file, no Vault, no sealed-secrets |
| Incidents found and fixed on the way, written up with the reasoning ([FIXES.md](./FIXES.md)) | Proof that you can carry a pager. A lab cannot prove that, and it should not pretend to |

Everything above is deliberate scoping, not a gap I am unaware of.

## What I would do differently in production

Each item below is a decision that is *wrong* here only because this is a single-Docker-host
lab — in production the same stand would look like this:

1. **Managed control plane and real nodes** (EKS/GKE/AKS, or Hetzner + k3s): no `k3d`
   containers, node pools, one node per failure domain, a real LoadBalancer with TLS.
2. **etcd backups and a rehearsed upgrade.** Today a cluster loss costs a re-deploy; in
   production it needs scheduled snapshots, a restore tested on a timer, and an upgrade
   plan with a pinned rollback version.
3. **External secrets with rotation.** Keeping them out of git is the floor, not the
   ceiling: Vault / External Secrets / SOPS, per-namespace scope, rotation on a schedule.
4. **Persistence with a backup story.** `emptyDir` for Prometheus is a demo choice;
   production needs a PVC, a retention policy and a restore procedure for the TSDB —
   losing history after an incident is how you lose the incident.
5. **Identity instead of static RBAC.** OIDC/SSO groups mapped to roles, no long-lived
   kubeconfigs, one ServiceAccount per workload, and audit logs shipped where the
   cluster admin cannot delete them.
6. **GitOps instead of a deploy script.** `deploy.ps1` is deliberately a *learning*
   artefact: it shows every step out loud. Production wants Argo CD/Flux, image digests
   instead of tags, and a rollback that is a `git revert`.
7. **SLOs, error budgets and on-call.** The rules exist, but a page is a process:
   severity levels, escalation, runbooks executed at least once, blameless postmortems.
8. **Cost and capacity as a signal.** Requests and limits are set, but nothing reports
   what the stand costs or how much headroom is left; production keeps that on a dashboard.

What is *not* on this list: the alert rules, the network policies, the CI gates and the
triage-bot guardrails are already the shape I would ship. The difference is scale and
process, not design.

---

## Requirements

- Windows (or Linux/macOS) with:
  - [Terraform](https://developer.hashicorp.com/terraform/downloads) >= 1.5
  - [Docker](https://docs.docker.com/get-docker/) (running)
  - [kubectl](https://kubernetes.io/docs/tasks/tools/)

The Docker Desktop VM needs **8 GB**: WSL2 defaults to half of the host RAM, so on a 16 GB machine
the ceiling is 7.6 GB, and that is exactly where this stand stops working - it grows to ~7.4 GB
after a few days (FIXES.md, section 22). `memory=8GB` in `%UserProfile%\.wslconfig` gives it 7.76 GB,
`swap=2GB` absorbs a spike, and `autoMemoryReclaim=gradual` hands the unused part back to Windows
instead of holding it until the next `wsl --shutdown`. It is a host-level setting, so the scripts do
not touch it.

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

The script is a thin orchestrator and does **eight** things, plus two optional extras:

1. provisions the k3d cluster with **Terraform** (`terraform/k3d`) - the single
   source of truth for the infrastructure;
2. applies the application manifests (`deploy/kubernetes/manifests`);
3. applies the **network policies** (`deploy/kubernetes/manifests-policy`): a real
   default-deny on both ingress and egress, plus one allow rule per service. Before
   this step the directory was validated by CI and applied by nobody, and its
   "default deny" was a pre-1.7 namespace annotation that modern clusters ignore -
   so every pod could reach every other pod, all databases included;
4. applies the **autoscalers** (`deploy/kubernetes/autoscaling`, `autoscaling/v2`)
   and then waits until an HPA actually reports CPU metrics. The objects exist
   either way, but without metrics-server (k3s ships it) and `requests.cpu` they
   would never scale - which is exactly the state this repository was in;
5. applies the logging stack (`deploy/kubernetes/manifests-logging` - Loki + Promtail);
6. applies the tracing stack (`deploy/kubernetes/manifests-jaeger`);
7. applies the monitoring stack (`deploy/kubernetes/manifests-monitoring`) and then does
   three extra things that plain `kubectl apply` cannot: it waits until the alert-rule
   ConfigMap has actually been refreshed **inside the running pod**, validates the mounted
   rule file with `promtool check rules`, and only then triggers `POST /-/reload`.
   Prometheus re-reads rule files at startup only, and a malformed rule file is **not**
   fatal - it logs an error and keeps serving requests **without those alerts**. A silent
   monitoring gap is worse than a failed deployment, so an invalid file aborts the script;
8. applies the alerting stack (`deploy/kubernetes/manifests-alerting`) plus its secrets;
9. *(optional, `-WithLoadtest`)* applies the load generator (`deploy/kubernetes/manifests-loadtest`);
10. *(optional, `-WithAIOps`)* deploys the **AIOps triage bot** (`aiops/triage-bot`): builds the
    image `triage-bot:0.1.0-m1`, imports it into the k3d node (`k3d image import` - it is published
    nowhere, which is exactly why the Deployment uses `imagePullPolicy: IfNotPresent`), creates the
    optional `aiops-llm` Secret when `DEEPSEEK_API_KEY` is available, applies
    `deploy/kubernetes/manifests-aiops` and waits for the rollout. The bot triages the `aiops="true"`
    alerts that Alertmanager POSTs to it and answers with a Telegram card; it holds no
    service-account token, no RBAC and no `kubectl`, and in M1 it executes nothing (`FIXES.md` §16).

The shop then opens at **http://localhost:8080**.

Bring the stand up with traffic - the load generator is what makes dashboards, alert rules
and traces non-empty, so it is the recommended way to run a demo:

```powershell
.\deploy.ps1 -WithLoadtest
```

Put the AIOps bot on the same stand (it needs the alerting stack, and takes the optional model key
from the very same `deploy\secrets.local.ps1`):

```powershell
.\deploy.ps1 -WithAIOps
```

Install the application from the Helm chart instead of the raw manifests - the rest of the
stand (policies, autoscalers, monitoring, alerting, bot) is unchanged, because the chart
labels its pods exactly like `manifests/` does (see "The Helm path" below):

```powershell
.\deploy.ps1 -Helm
```


To tear the whole stand down again:

```powershell
.\deploy.ps1 -Down
```

### Secrets (never committed)

The alerting stack needs two credentials - a Telegram bot token and a Slack
incoming-webhook URL. They live in `deploy\secrets.local.ps1`, a git-ignored file
that `deploy.ps1` sources on every run. Create it once and fill it in once - no
later step asks you for the values again:

```powershell
Copy-Item deploy\secrets.local.ps1.example deploy\secrets.local.ps1
# then edit deploy\secrets.local.ps1 and fill in the real values
```

Environment variables (`TELEGRAM_BOT_TOKEN`, `SLACK_WEBHOOK_URL`) work as well -
that is the CI path - but nothing here requires you to set them by hand.

If that file is not there at all, that is not an error either: when the stand is already
up, `deploy.ps1` reads both values back out of the cluster's own `alertmanager-secrets`
Secret (kubectl hands Secret values over base64-encoded) and re-applies it. Either way
nothing is pasted anywhere and nothing is typed on a command line.

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

# 3. Network policies - default-deny (ingress AND egress) plus one allow rule per
#    service. Proof that they do something:
#      .\deploy\kubernetes\manifests-policy\verify-netpol.ps1
kubectl apply -f deploy\kubernetes\manifests-policy

# 4. Autoscalers (HPA v2). They need metrics-server (k3s ships it) and requests.cpu;
#    `TARGETS` must show a percentage - `<unknown>` means the autoscaler is dead:
kubectl apply -f deploy\kubernetes\autoscaling
kubectl -n sock-shop get hpa

# 5. Logging - Loki (store) + Promtail (collector)
kubectl apply -f deploy\kubernetes\manifests-logging

# 6. Tracing - Jaeger
kubectl apply -f deploy\kubernetes\manifests-jaeger

# 7. Monitoring
kubectl apply -f deploy\kubernetes\manifests-monitoring

# 7b. Prometheus re-reads rule files at startup only, and a malformed rule file is
#     not fatal: it logs an error and keeps running WITHOUT those alerts. So validate
#     the mounted file first, then reload without a restart (a restart would wipe the
#     TSDB, which lives on emptyDir):
kubectl -n monitoring exec deploy/prometheus-deployment -- `
    promtool check rules /etc/prometheus-rules/alert.rules
kubectl -n monitoring exec deploy/prometheus-deployment -- `
    wget -q -O- --post-data='' http://localhost:9090/-/reload
#     .\deploy.ps1 also waits here until the ConfigMap file has actually been refreshed
#     inside the pod (kubelet does that asynchronously, up to ~2 minutes) - otherwise
#     the reload would silently re-read the OLD file.

# 8. Alerting - the secret must exist first, then apply the manifests.
#    The values are read from the very file deploy.ps1 uses, so nothing has to be
#    typed into a command line (where it would stay in your shell history):
. .\deploy\secrets.local.ps1
kubectl -n monitoring create secret generic alertmanager-secrets `
    --from-literal=telegram_bot_token=$env:TELEGRAM_BOT_TOKEN `
    --from-literal=slack_webhook_url=$env:SLACK_WEBHOOK_URL `
    --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f deploy\kubernetes\manifests-alerting

# 9. (optional) load generator - without traffic the dashboards stay empty and the
#    alert rules have nothing to fire on
kubectl apply -f deploy\kubernetes\manifests-loadtest

# 10. (optional) AIOps triage bot. The image is built here and published nowhere, so
#     `imagePullPolicy: IfNotPresent` in the Deployment is not a preference: with
#     `Always` the kubelet would go to Docker Hub for it and the pod would never start.
docker build -t triage-bot:0.1.0-m1 aiops\triage-bot
k3d image import triage-bot:0.1.0-m1 -c sock-shop
#     The model key is optional (`optional: true` on the mount): without it the
#     cascade falls back to the ruleset and /healthz says which key file is missing.
. .\deploy\secrets.local.ps1
kubectl -n monitoring create secret generic aiops-llm `
    --from-literal=deepseek_api_key=$env:DEEPSEEK_API_KEY `
    --dry-run=client -o yaml | kubectl apply -f -
#     Never `examples\`: `kubectl apply -f <dir>` is not recursive, and that is exactly
#     what keeps `manifests-aiops\examples\` out of the cluster.
kubectl apply -f deploy\kubernetes\manifests-aiops
kubectl -n monitoring rollout status deploy/triage-bot --timeout=90s
kubectl -n monitoring port-forward svc/triage-bot 8080:8080
#     then: curl http://localhost:8080/healthz
```

The application can also be applied from a single file:
`kubectl apply -f deploy/kubernetes/complete-demo.yaml`.

## The Helm path (application only)

The same application objects are packaged as a chart, and both deploy paths are
checked in CI (`helm lint --strict`, then `helm template | kubeconform` on the
rendered output):

```powershell
.\deploy.ps1 -Helm     # the application comes from the chart; everything else is unchanged
# or by hand, on a cluster Terraform already created:
helm upgrade --install sock-shop deploy\kubernetes\helm-chart --namespace sock-shop --create-namespace `
    --server-side=true --force-conflicts
```

`--force-conflicts` is not decoration: once the HPAs have resized a deployment, the
autoscaler owns `spec.replicas`, and a server-side apply that insists on the
chart's value fails with `conflict with "k3s" with subresource "scale"` - upgrade
and rollback both (FIXES.md, section 25).

What the chart covers is the application: the 8 services, the 5 datastores,
RabbitMQ and the ingress. Network policies, autoscalers, logging, tracing,
monitoring, alerting and the triage bot stay on the raw manifests - and they keep
working, because the chart labels its pods `name: <service>` exactly like
`manifests/` does, which is what the NetworkPolicy and HPA selectors match on.

Three limits are worth knowing before choosing this path:

- `manifests/` stays the source of truth. The chart is the same objects written a
  second time, so a change lands in both or it is a bug. On top of `helm lint` and
  `kubeconform` - which only prove that each side is valid on its own - CI compares
  the two (`deploy/kubernetes/helm-chart/check-parity.py`: replicas, every
  container's name, ports, image, `command`, `args`, `env`, `resources` and
  `volumeMounts`, every pod volume's full source, for all 14 workloads; every
  Service's type, labels, ports and annotations; the Ingress; and which objects each
  side renders at all). No image difference is allowlisted: both sides ship
  `mongo:4.4.0`, `user-db:0.4.0` and `rabbitmq:3.13.7-management` (`FIXES.md` §25).
- Install into a **fresh** cluster. `helm` refuses to adopt objects that an earlier
  `kubectl apply` created (`invalid ownership metadata`), so switching paths on a
  live stand means `.\deploy.ps1 -Down` first.
- Both paths carry container probes on all eight application deployments (readiness
  of `carts` and `shipping` starts at 360 s), so `--wait` needs about six minutes on
  a quiet install and about fifteen when the HPAs add replicas mid-roll-out
  (`deploy.ps1` passes `--timeout 15m`, and the plain path waits 900 s for
  `condition=available`). The timings are deliberately looser than upstream's -
  `periodSeconds: 10`, `timeoutSeconds: 5`, `failureThreshold: 6` - because the
  upstream set (`3` / `1` / `3`) restarted healthy JVMs under the load generator.
  The four manifest services that used to have no probes (`carts`, `orders`,
  `queue-master`, `shipping`) carry the same set now: without them the pod joined
  its Service while the JVM was still starting, and half the `POST /cart` requests
  came back `500 connect ECONNREFUSED` to the `carts` ClusterIP (`FIXES.md` §26).

Both paths ask for the same **1.7 GiB** of memory in total. That equality is
checked, not assumed: the chart used to request `2000Mi` on each of the four JVM
services - 10 GiB of requests on a stand that has 8 GB - while every CI run stayed
green, because "the chart renders" and "the chart describes the same pod" are two
different claims (`FIXES.md` §23). That gate now covers replicas, resources, image,
`command`/`args`/`env`, ports, `volumeMounts`, volumes and probes; what still
differs is deliberate and named (`nodeSelector`), and §23 lists the rows that had
to be reconciled on a cluster before `-Helm` could be called equivalent rather than
a demonstration that the application packages as a chart.

## Accessing the application and tools

Everything except Grafana's ingress is reachable from the host **without**
`kubectl port-forward`: the host ports are published by `terraform/k3d`. Grafana
is reached through Traefik on a host name of its own (see below).

| Port  | URL | What |
|-------|-----|------|
| 8080  | <http://localhost:8080>  | Sock Shop (through Traefik/Ingress) |
| 8080  | <http://grafana.localhost:8080> | Grafana (same Traefik entrypoint, own `Host` header) |
| 9090  | <http://localhost:9090>  | Prometheus |
| 9093  | <http://localhost:9093>  | Alertmanager |
| 16686 | <http://localhost:16686> | Jaeger UI (traces) |

Grafana is the single entry point for both the logs and the traces: it comes with
a `loki` datasource (logs) and a `jaeger` datasource (traces), both defined in
`manifests-monitoring/20-grafana-configmap.yaml` and imported by the
`grafana-import-dashboards` Job. Loki has **no** UI and **no** host port on
purpose - it is an internal store queried only by Grafana
(`http://loki.logging.svc.cluster.local:3100`): read the logs in **Explore** with
the `loki` datasource, e.g. the query `{namespace="sock-shop"}`.

Grafana's Service is `ClusterIP` and no longer published on a host port: as a
`NodePort` it was reachable on **every** interface of the machine with the default
`admin`/`admin`. It is served by the same Traefik entrypoint as the application
and distinguished by the HTTP `Host` header - one port, several backends, the same
idea as `server_name` in nginx:

```powershell
# one-off, in an editor started as Administrator, add a line to
#   C:\Windows\System32\drivers\etc\hosts
#   127.0.0.1 grafana.localhost
# then browse to http://grafana.localhost:8080
# no hosts entry (or no admin rights)? use the tunnel instead:
kubectl port-forward -n monitoring svc/grafana 3000:80   # http://localhost:3000
```

The admin password is not in the repository either: `deploy.ps1` creates the
`grafana-admin` Secret - reusing an existing one, because Grafana keeps the
password in its own database - and both the Deployment and the import Job read
the credentials from it. Set `GRAFANA_ADMIN_PASSWORD` to choose your own value;
otherwise a random one is generated and printed at the end of the deploy.

**Logging in.** User `admin` (or `GRAFANA_ADMIN_USER`); the password can always be
read back out of the Secret - the same trick `deploy.ps1` uses, and it is printed
in the deploy summary as well:

```powershell
kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' |
  ForEach-Object { [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($_)) }
```

Anonymous access is off **on purpose** (`GF_AUTH_ANONYMOUS_ENABLED=false` in
`21-grafana-dep.yaml`): Grafana used to be an unauthenticated `NodePort` with
`admin`/`admin` reachable on every interface of the host. If you want the UI to
open without a login on this local stand, flip that variable to `true` and set
`GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer` so anonymous visitors stay read-only while the
admin login keeps guarding edits.

### Monitoring and alerting at a glance

| What | Where | Notes |
|---|---|---|
| Metrics | Prometheus <http://localhost:9090> | app targets are auto-discovered from pod/Service annotations; plus `node-exporter` (3 nodes) and `kube-state-metrics` |
| Dashboards | Grafana <http://grafana.localhost:8080> | 7 dashboards and 3 datasources imported by the `grafana-import-dashboards` Job |
| Alerts | Prometheus rules + Alertmanager <http://localhost:9093> | 14 rules in 4 groups - `availability`, `application`, `infrastructure`, `meta`; delivered to Telegram and Slack |
| Probes | `blackbox-exporter` | two paths, labelled `probe_scope`: `ingress` (Traefik Service) and `external` (the published port, `host.docker.internal:8080`) |
| Logs | Loki (only through Grafana **Explore**) | no UI and no host port on purpose |

The `meta` group is the one worth explaining: `ScrapeTargetDown` fires when a **scrape
target itself** stops answering - it watches the monitoring instead of the application.
Adding it immediately exposed three targets that had been permanently DOWN in the config
(a wrong exporter port, a service that only serves Actuator JSON, and a wrong
Alertmanager metrics path) - see [FIXES.md](./FIXES.md), section 11.

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
├── aiops/
│   └── triage-bot/                         # AIOps M1 advisor: code, 136 tests, run-local.ps1
├── deploy/
│   └── kubernetes/
│       ├── manifests/                      # application, per-file (00 ... 29)
│       ├── manifests-logging/              # Loki + Promtail (pod logs)
│       ├── manifests-jaeger/               # Jaeger all-in-one (traces)
│       ├── manifests-monitoring/           # Prometheus, Grafana, exporters
│       ├── manifests-alerting/             # Alertmanager (Telegram + Slack)
│       ├── manifests-loadtest/             # optional k6 load generator
│       ├── manifests-policy/               # NetworkPolicy: default-deny + per-service allows
│       ├── manifests-aiops/                # AIOps triage bot (M1): ConfigMap, Deployment, Service, NetworkPolicy
│       ├── autoscaling/                    # HPA v2 for the stateless services (+ legacy/)
│       ├── complete-demo.yaml              # application in one file
│       └── helm-chart/                     # alternative: Helm chart (application only)
│           └── check-parity.py             # CI: the chart must describe the same pods as manifests/
├── terraform/
│   └── k3d/                                # declarative definition of the cluster
└── legacy/                                 # archived upstream material, never deployed
    └── README.md                           # what is in there and why it is not used
```

Two folders in that tree are deliberately outside the deployment path: `legacy/`
(archived upstream material - the old compose files, the `kind`-based CI, the AWS
Terraform, the helper images) and `deploy\kubernetes\helm-chart` (an alternative way
to install the application, described above). `legacy\README.md` lists what each
folder was and why it is not used; nothing in it is ever applied, because
`kubectl apply -f <dir>` does not recurse.

## Stop / remove

```powershell
.\deploy.ps1 -Down             # tear the whole stand down (terraform destroy)
```

Or, to only pause the stand:

```powershell
.\deploy\stand.ps1 -Stop -Free   # stop the nodes in order; -Free drops the grown logs/TSDB first
.\deploy\stand.ps1 -Status       # engine, VM, containers, nodes - changes nothing
.\deploy\stand.ps1 -Start        # start them one at a time, waiting for the API and for each node
```

Do not `docker stop k3d-sock-shop-*` and expect `docker start` to bring the stand back. k3d creates
those containers with the `unless-stopped` policy, so a Docker restart starts all four at once
(measured: load average 355 and 68 MB free on a 7.5 GB VM - the WSL2 default ceiling), and a container
that lost its network endpoint to a VM restart makes k3s exit with code 0 - the node looks like it
started and vanished, the load balancer cannot resolve it, and `kubectl` times out while the server is
fine. `stand.ps1` handles all of it: it sets the policy to `no`, starts the server first, the agents one
at a time and the load balancer last, reconnects a lost endpoint, deletes a `Node` object whose recorded
address no longer matches the container and retries the start (the case that used to end in a rebuild),
and only then falls back to `deploy.ps1` (FIXES.md, section 22).

Verified on this stand: `-Stop` then `-Start -NoRebuild` → endpoints reconnected, three nodes `Ready`,
30/30 pods `Running`, `http://localhost:8080/` answered 200 - no rebuild.

`k3d cluster delete sock-shop` still exists, but `.\deploy.ps1 -Down` is the supported way to remove the
cluster, because terraform keeps the state.

If the stand does not come back - `kubectl` times out, `docker ps` hangs, the machine is slow - do
not debug it in place. `deploy\diagnose-stand.ps1` tells apart the three failures that all look like
"cannot connect": the Docker engine is wedged, the WSL VM is thrashing, or the cluster is unhealthy.
It is read-only and prints the next command for whichever one it finds.

A wedged VM is killed with `wsl --shutdown` (`docker stop` hangs in that state). A cluster that will
not recover is replaced with `.\deploy.ps1 -Down` followed by `.\deploy.ps1 -WithAIOps`.

What the stand accumulates while it runs is a different problem, and it does not need a rebuild.
Prometheus and Loki keep their data in `emptyDir` and the bot only ever reads the last 15 minutes of
logs, so what grows is data nobody asked for: log chunks, TSDB blocks, containerd image layers and
finished pods. The one thing that does survive a restart is the triage bot's journal on the
`triage-bot-journal` PVC (`local-path`): it is the audit trail, so it is kept - and because
`local-path` binds a volume to the node that created it, the bot's pod stays on that node for the life
of the cluster, and a rebuilt cluster starts with a new, empty journal.

```powershell
.\deploy\cleanup-stand.ps1                  # ~30 s: finished pods, unused images, Loki/Prometheus data
.\deploy\cleanup-stand.ps1 -KeepPrometheus  # keep the metric history
.\deploy\cleanup-stand.ps1 -KeepLogs        # keep the log chunks
.\deploy\cleanup-stand.ps1 -DryRun          # print what would be removed
```

It reports disk usage and image counts per node before and after, so growth is measured rather than
guessed, and it leaves the application, the Secrets and the bot alone.

That is a stand-only measure, not a production pattern: on a real cluster the answer is resource
limits, real volumes, retention and alerting on memory headroom. `FIXES.md`, section 17 has the
analysis and the mapping.

## License

See [LICENSE](./LICENSE). Distributed under the Apache 2.0 license.
