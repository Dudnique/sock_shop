# What was fixed in this fork

> **English** | [Русский](FIXES.ru.md)

A working fork of the **Sock Shop** demo application
(upstream: https://github.com/microservices-demo/microservices-demo),
prepared to run on a local **k3d** (k3s) cluster on Windows/WSL2.

Below are the differences from upstream and the reasons for them.

---

## 1. User registration failed with `Internal Server Error (500)`

### Symptom
`POST /register` from the web UI returned **500**, even though the `user` service
did create the customer.

### Real cause (NOT in `user`/`user-db`)
The `user` and `user-db` services were perfectly healthy:

- `GET /health` on `user` answered `user-db: OK`;
- a direct `POST /register` to `svc/user` returned `200` + a customer id.

The error was produced by the **front-end** while merging the cart right after
registration. The cluster was running an old image `weaveworksdemos/front-end:0.3.1`,
whose `api/endpoints.js` pointed at a non-existent host `cart` (singular):

```js
cartsUrl: "http://cart/carts"   // BUG: the service is called "carts"
```

while the Service in the cluster is called `carts` (plural). Front-end logs:

```
Posting Customer: {...}
{ id: '6ac649390cc5ef00019d4254' }          <- user created the customer OK
Merging carts for customer id: 6ac649390cc5ef00019d4254 ...
Error with log in: Error: getaddrinfo EAI_AGAIN cart:80
POST /register 500
```

### Fix
Use the image that matches the manifests — `weaveworksdemos/front-end:0.3.12`,
where `endpoints.js` contains the correct address `http://carts/carts`.

```diff
- image: weaveworksdemos/front-end:0.3.1
+ image: weaveworksdemos/front-end:0.3.12
```

Applied in `deploy/kubernetes/manifests/09-front-end-dep.yaml` and
`deploy/kubernetes/complete-demo.yaml`.

After rollout, `POST /register` returns `200` and the logs show `Carts merged.`.

> Why the bug appeared at all: the `0.3.1` image had previously been deployed
> **by hand**, and the manifests from this repository were not applied. Deploying
> strictly through the manifests avoids the problem — the fix is recorded here
> explicitly so the version cannot silently drift again.

---

## 2. Added an Ingress for the front-end

Upstream ships an Ingress only inside the Helm chart. For k3d a separate manifest
`deploy/kubernetes/manifests/29-front-end-ingress.yaml` was added, so the app is
reachable from the host at `http://localhost:<port>` without `kubectl port-forward`
and without NodePort.

---

## 3. Outdated node selectors updated

In every manifest the deprecated node label `beta.kubernetes.io/os` was replaced
with the stable `kubernetes.io/os`, so the manifests also deploy on new clusters
(k3s, GKE, EKS) where the beta label may be absent.

---

## 4. CI simplified

The heavy `deployments-tests` job spun up a `kind` cluster running k8s `v1.20.0`
(2020) and waited for all pods; on modern runners it could not finish within
`timeout-minutes: 10` and was cancelled consistently.

Now CI runs:

- `complete-demo-sync-check` — the same check, but directly via `awk | diff`,
  without building a Docker image;
- `manifests-validate` — fast offline validation of all manifests
  (`kubeconform -strict`), no cluster required.

The original workflow is kept for history in `ci-legacy/main.yaml`.

The Docker + `make` helper that used to generate and check `complete-demo.yaml` (the root
`Makefile`, `deploy/kubernetes/Makefile` and `deploy/kubernetes/Dockerfile`) was archived
in `ci-legacy/` for the same reason: the generation is a one-liner and CI performs the
check itself, so `make` - and the purpose-built image it needed - were pure overhead. The
regeneration command is documented in `ci-legacy/README.md` and in
`deploy/kubernetes/README.md`.

---

## 5. Alerting stack added (Alertmanager: Telegram + Slack)

The fork adds a fourth deployment step - an **Alertmanager** (`manifests-alerting/`)
that fans every firing/resolved alert out to **two channels at once** (Telegram and
Slack). Nothing in upstream ships this.

Decisions worth recording:

- the image is **pinned** (`prom/alertmanager:v0.34.1`) - a reproducible stand, no `:latest`;
- the credentials live in a Kubernetes Secret **`alertmanager-secrets`** (namespace
  `monitoring`) with the keys `telegram_bot_token` and `slack_webhook_url`, mounted as
  files and read with `*_file` options, so no token ever appears in a ConfigMap or in git;
- `deploy.ps1` creates the secret **idempotently** (render client-side with
  `--dry-run=client -o yaml`, then `kubectl apply -f -`), because a plain
  `kubectl create` fails on the second run;
- to reach **both** channels the routing tree keeps the root receiver plus **two child
  routes without matchers**, the first carrying `continue: true` - otherwise
  Alertmanager follows only the first matching child and the root receiver is skipped.

---

## 6. Tooling gotchas hit while building this stand

Recorded because they cost real time and are easy to hit again:

1. **k3d has no in-place update.** Changing the cluster definition is not applied to a
   running cluster; it has to be recreated (`terraform apply -replace` or destroy +
   apply). Treat the cluster as immutable.
2. **PowerShell 5.1 and non-ASCII scripts.** A `.ps1` saved as UTF-8 **without BOM** is
   decoded by PS 5.1 with the system ANSI codepage, which mangles Cyrillic text.
   `deploy.ps1` is therefore kept **ASCII-only**.
3. **`$PSScriptRoot` is not reliable inside a `param()` default.** It is resolved in the
   script body instead, where it is always set.
4. **`-chdir=$var` is not expanded** for native commands under Windows PowerShell, so the
   script uses `Push-Location`/`Pop-Location` around every Terraform call.
5. **`kubectl apply -f <dir>` is not recursive and picks up `*.example.yaml`.** Sample
   manifests were moved into an `examples/` sub-directory, which `apply -f <dir>` ignores.
6. **Manifests without a `namespace:`** landed in `default` and did nothing useful;
   `namespace: monitoring` was added explicitly to every monitoring/alerting manifest.
7. **Alertmanager routing surprise.** When a child route matches, the root receiver is
   ignored - hence the two matcherless children with `continue: true` (see section 5).
8. **`kubectl apply` never removes a field.** `serviceAccountName: promtail` was
   deleted from the Promtail DaemonSet manifest, and the field **stayed** in the
   cluster: `apply` is a three-way merge, so a key that is *absent* from the new
   manifest keeps whatever the live object has. The object had to be deleted and
   recreated from scratch (`kubectl delete -f ...` then `apply`) for the removal to
   take effect - see section 8.

---

## 7. `front-end` Service: `NodePort` dropped in favour of the Ingress

Upstream's `front-end` Service was `type: NodePort` with a hard-coded
`nodePort: 30001`. That was the only way in **before the ingress existed**: it pins
the stand to one fixed port and requires the node IP (not `localhost`) to be
reachable. Once the Traefik Ingress (section 2) routes to `service/front-end:80`,
the NodePort is a pure leftover - it duplicates the ingress path and adds a second,
unmanaged entry point.

The Service is now a plain `ClusterIP` (the `type` / `nodePort` lines are gone, like
every other service in `manifests/`), so the application is reached **only** through
Traefik on `http://localhost:8080`.

Changed in `deploy/kubernetes/manifests/10-front-end-svc.yaml`,
`deploy/kubernetes/complete-demo.yaml` and
`deploy/kubernetes/helm-chart/templates/front-end-svc.yaml`.

---

## 8. Logging stack added (Loki + Promtail)

The stand had metrics and alerts but no logs: pod logs only existed on the node and
disappeared with the container. This fork adds the fourth manifest directory,
`deploy/kubernetes/manifests-logging/`, built from two pieces:

- **Loki** - the store. One replica, filesystem storage, no host port and no UI: it is
  an internal service that only Grafana queries.
- **Promtail** - the collector. A DaemonSet (one pod per node), because log files live
  on each node's disk and a pod on node A cannot see the files of node B.

Reading the logs is done in **Explore** in Grafana with the `loki` datasource, e.g.
the query `{namespace="sock-shop"}` - Loki deliberately has no UI of its own.

### Symptom
Loki was up and reachable from Grafana, but every query was empty.
`GET /loki/api/v1/label/namespace/values` answered with a single empty string, i.e.
"no data" - and, the confusing part, **nothing complained**: Promtail's own log was
clean, no push errors, no RBAC warnings.

### Real cause (neither Loki nor Grafana)
Promtail was configured the "official" way, with `kubernetes_sd_configs`. That
configuration asks the Kubernetes API which pods exist and derives log targets from
the answers; the first version even carried a ServiceAccount plus a ClusterRole.
Inside a single-container DaemonSet the discovery result was **empty** - silently:
Promtail had nothing to tail, pushed nothing, and Loki stayed empty while looking
perfectly healthy.

### Fix
Promtail no longer talks to the Kubernetes API at all - it just reads files:

- **a static file glob instead of service discovery**: `__path__:
  /var/log/pods/*/*/*.log` in a plain `static_configs` entry;
- the node's `/var/log` is mounted read-only (hostPath), so the CRI log files
  (`/var/log/pods/<ns>_<pod>_<uid>/<container>/<n>.log`) are visible inside the pod;
- the labels Grafana filters on are cut **out of the path** by a `regex` stage on
  `filename` (`namespace`, `pod_name`, `pod_uid`, `container_name`) and promoted by a
  `labels` stage. `pod_uid` is deliberately **not** made a label: it changes on every
  restart, and in Loki each new label combination is a new stream;
- a `cri: {}` pipeline stage parses the containerd line format
  (`<timestamp> stdout F <message>`), so the timestamp and the stdout/stderr stream are
  real instead of being part of the message text;
- `positions.yaml` lives on the node (`hostPath: /run/promtail`), so a Promtail restart
  does not re-send whole files and duplicate lines in Loki;
- **no RBAC at all**: ServiceAccount, ClusterRole and ClusterRoleBinding are gone. A
  file-based collector needs no cluster-level permissions, and with them removed there
  is nothing left that can silently return an empty result;
- `tolerations: [{operator: Exists}]`, because the k3d server node carries a taint;
  without the toleration that node's logs (kube-system, etc.) would never be collected;
- `prometheus.io/scrape: "true"` + `prometheus.io/port: "3101"`, so Promtail's own
  metrics are scraped without touching the Prometheus config;
- the legacy Elasticsearch/Fluentd/Kibana manifests were moved to
  `manifests-logging/legacy/`: kept for history, ignored by `kubectl apply -f <dir>`.

### Verification
```
GET /loki/api/v1/label/namespace/values  -> ["kube-system","logging","monitoring","sock-shop", ...]
GET /loki/api/v1/query_range?query={namespace="sock-shop"}
```
returns real application lines labelled `container_name`, `job`, `namespace`,
`pod_name`, `stream` - and the same data is visible in Grafana Explore.

### Trap worth remembering
The first attempt merely patched the running DaemonSet, and the old
`serviceAccountName: promtail` **survived**: `kubectl apply` is a three-way merge, so
a field that is absent from the new manifest keeps its live value. The object had to be
deleted and recreated (section 6, item 8).

---

## 9. Tracing stack added (Jaeger) - the "Phase 2" the README used to promise

The port table already reserved `16686` for the Jaeger UI ("added in Phase 2") and
Terraform already published `host:16686 -> node:30686`. This fork finishes that job:
`deploy/kubernetes/manifests-jaeger/` deploys Jaeger and the instrumented services are
pointed at it.

- `00-jaeger-ns.yaml`, `01-jaeger-dep.yaml`, `02-jaeger-svc.yaml`;
- image pinned to `jaegertracing/all-in-one:1.57.0`; `SPAN_STORAGE_TYPE=memory` - a demo
  stand needs no PVC, traces are intentionally lost on restart;
- `strategy: Recreate`, because a single in-memory instance must never run twice at once;
- `COLLECTOR_ZIPKIN_HOST_PORT=:9411`: the sock-shop services speak **Zipkin**, not the
  native Jaeger protocol, so Jaeger has to serve the Zipkin-compatible collector;
- the `ZIPKIN` env var is set on the three services that are instrumented upstream
  (`catalogue`, `payment`, `user`):
  `http://zipkin.jaeger.svc.cluster.local:9411/api/v1/spans`
  (`manifests/05-catalogue-dep.yaml`, `15-payment-dep.yaml`, `25-user-dep.yaml` and
  `complete-demo.yaml`);
- a Service literally named **`zipkin`** in namespace `jaeger` exists purely for that
  DNS name - the hostname has to resolve before a service can POST spans;
- `jaeger-query` is a **NodePort** (`30686`) so the host can open the UI, while
  `jaeger-collector` stays `ClusterIP`: only in-cluster clients send spans.

Why `NodePort` and not `type: LoadBalancer`: k3s `servicelb` would spawn another
`svclb-*` DaemonSet that binds **hostPort 80 on every node** - the port `svclb-traefik`
already owns - which would break `http://localhost:8080`; and it would still not publish
`16686`.

Verification: after the shop has served some traffic, `http://localhost:16686` and
`GET /api/services` list `["jaeger-all-in-one","payment","catalogue","user"]`. Grafana
also gets a `jaeger` datasource, so traces can be searched from Grafana as well.

---

## 10. Grafana: Loki/Jaeger datasources, and a dashboard JSON that failed silently

- `manifests-monitoring/20-grafana-configmap.yaml` gained two more files for the import
  Job: `loki-datasource.json` (`"type": "loki"`,
  `"url": "http://loki.logging.svc.cluster.local:3100"`, `"access": "proxy"`) and
  `jaeger-datasource.json` (`"type": "jaeger"`,
  `"url": "http://jaeger-query.jaeger.svc.cluster.local"`). The Job's script already
  loops over `*-datasource.json` and `*-dashboard.json`, so the Job itself needed no
  structural change. `access: proxy` matters: Grafana reaches both stores from inside
  the cluster, so the browser never has to resolve a `*.svc.cluster.local` name.
- the datasource loop is now **idempotent**: it ends with
  `|| echo "(datasource already exists - kept as is)"`. On a second deployment the API
  answers `409 Conflict` ("data source with the same name already exists") once per
  datasource - that is expected state, not a failure, and the log now says so instead of
  printing a bare curl error.
- **a pre-existing bug was found and fixed**: `prometheus-stats-dashboard.json`
  contained `process_start_time_seconds{name="prometheus"}` with **unescaped** double
  quotes inside a JSON string embedded in a ConfigMap, so that file was not valid JSON
  and Grafana rejected it with `400`. The import loop swallowed it - every iteration
  ends with `echo ""`, so the container always exited `0` and the Job reported
  `Complete`. After escaping the quotes all **6** dashboards and **3** datasources
  import, and every dashboard payload carries `"overwrite": true`, so a re-import
  updates a dashboard instead of duplicating it.
- because a Job spec is immutable, `deploy.ps1` now deletes the finished
  `grafana-import-dashboards` Job **before** applying `manifests-monitoring`, so a
  redeploy re-imports the current configmap instead of failing on an immutable field.
- `deploy.ps1` grew from four steps to **six** (cluster, application, logging, tracing,
  monitoring, alerting) and the README gained the matching manual commands, so a
  from-scratch `.\deploy.ps1` reproduces the whole stand - logs and traces included.
