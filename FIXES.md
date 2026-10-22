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

The original workflow is kept for history in `legacy/ci-legacy/main.yaml`.

The Docker + `make` helper that used to generate and check `complete-demo.yaml` (the root
`Makefile`, `deploy/kubernetes/Makefile` and `deploy/kubernetes/Dockerfile`) was archived
in `legacy/ci-legacy/` for the same reason: the generation is a one-liner and CI performs the
check itself, so `make` - and the purpose-built image it needed - were pure overhead. The
regeneration command is documented in `legacy/ci-legacy/README.md` and in
`deploy/kubernetes/README.md`.

Two small housekeeping changes closed this section. With `legacy/ci-legacy/` set aside, the workflow
file is the only thing CI runs, so its pins became the ones that matter: retiring the node20
runner had to be done by hand (commit `05cd3bb`), and `.github/dependabot.yml` now opens a
weekly PR whenever an action, a Terraform provider or one of the two helper images
(`legacy/openapi/`, `legacy/healthcheck/`) has a newer version. The scope is deliberately narrow and
**major** updates are excluded on purpose - a new major of an action or a provider changes
behaviour and is a decision, not a chore. Nothing is merged automatically: every PR runs this
same workflow. Pointing those two entries at the archived paths mattered: while they still said
`/openapi` and `/healthcheck`, every weekly run failed with "couldn't find a Dockerfile" and no PR
was opened, so the base-image pins froze without a single visible error (commit `cb1f876`).

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
9. **PowerShell 5.1 buffers redirected output - and an empty `*.err.log` proves nothing.**
   A long run started as `Start-Process ... -RedirectStandardOutput run.log` can sit at
   **0 bytes** while it is still working, because the host flushes that stream lazily.
   Measured on 5.1 with a script that prints, sleeps 6 s and prints again: at t=3 s the
   redirected file held **7 B** while the same output through `Start-Transcript` already
   held **975 B**; it only reached its final 26 B after the process exited. (The detached
   child itself survived the exit of the shell that launched it - checked by pid.) Two
   conclusions: to watch a run live, use `-NoNewWindow` (the child shares the console and
   lines appear immediately); to keep a log, wrap the run in `Start-Transcript`. And never
   read a 0-byte `*.err.log` as "it died": `deploy.ps1` writes everything with `Write-Host`,
   so stderr stays empty on a fully successful run - the real signals are the exit code and
   the final `SCRIPT-FINISHED-WITHOUT-ERROR` line.
10. **PowerShell 5.1 does not escape `"` inside an argument passed to a native command.** An
    otherwise valid `git commit -m '... a "Guard Grafana access" step ...'` reached git
    mangled and failed with `error: pathspec 'Grafana' did not match any file(s) known to
    git`. For multi-line messages containing quotes, write the message to a file and use
    `git commit -F`.
11. **Personal notes sitting next to published documentation.** `internal-docs/interview.md`
    accumulated real paths, commands and the credentials used while building the stand - one
    `git add -A` away from being published. It is in `.gitignore` now, while the project
    documentation (`internal-docs/design.md`, `internal-docs/testing.md`) stays tracked: the
    line is drawn between "documents the stand" and "documents the process". A line in
    `.gitignore` is the cheapest possible guard, and "be careful with `git add -A`" is not a
    guard at all.

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

---

## 11. Monitoring: 11 alert rules, the promtool gate, and the three dead targets they exposed

In `HEAD`, `manifests-monitoring/05-prometheus-alertrules.yaml` held **one** group with
**one** rule (`HighErrorRate`). It now holds **11 rules in 4 groups** - and that is not
"rules for the sake of rules", it is closing concrete blind spots of this stand.

| Group | Rules | What it catches |
|---|---|---|
| `availability` | `ProbeFailed`, `ProbeSlow` | storefront down / answering slower than 2s |
| `application` | `HighErrorRate`, `DeploymentReplicasUnavailable`, `PodCrashLooping`, `PodRestartFlapping` | 5xx, zero available replicas, CrashLoopBackOff, restart flapping |
| `infrastructure` | `NodeNotReady`, `NodeDiskPressure`, `NodeHighCPU`, `NodeHighMemory` | node gone, disk/CPU/RAM pressure |
| `meta` | `ScrapeTargetDown` | **monitoring itself**: a scrape target stopped answering |

- **Why a broken rule file is so dangerous.** Prometheus treats a malformed rule file as
  **non-fatal**: it logs an error and keeps running **without those alerts**. That is the
  worst possible outcome - silence looks exactly like "everything is fine". CI therefore
  gained a `promtool check rules` step: the rule file is extracted from the ConfigMap
  (`yq`) and validated. `kubeconform` can never catch this: a rule file has no
  `apiVersion/kind`, it is not a Kubernetes object at all.
- **Rules are not re-read on their own.** kubelet updates the file inside the mounted
  ConfigMap volume, but Prometheus reads it at startup only. Proven live: the file in the
  pod already contained all 11 rules while `GET /api/v1/rules` still showed the single old
  one. The fix is not a restart (that would wipe the TSDB on `emptyDir` and lose metric
  history) but `POST /-/reload`. After applying the monitoring manifests, `deploy.ps1`
  waits for the file to land, triggers the reload and prints the loaded rules.
- **The `ScrapeTargetDown` rule immediately found three dead targets** that had been
  silently broken in the config:

  | Target | Error | Root cause | Fix |
  |---|---|---|---|
  | `rabbitmq:9090` | `connection refused` | the `kbudde/rabbitmq-exporter` sidecar listens on **9419** (`PUBLISH_PORT`) while the Service and its annotation promised 9090 | pinned `PUBLISH_PORT: "9090"` |
  | `queue-master:80` | `expected a valid start token, got "{"` | this service serves Spring Boot **Actuator JSON** on `/metrics` and has no Prometheus endpoint at all (`/actuator/prometheus` -> 404) | dropped `prometheus.io/scrape` (otherwise a permanently DOWN target) |
  | `alertmanager:9093/alertmanager/metrics` | `HTTP 404` | wrong `prometheus.io/path`; Alertmanager serves its own metrics on `/metrics` | path corrected |

- **The storefront probe now covers two paths instead of one.** Blackbox used to probe
  `http://traefik.kube-system.svc.cluster.local/` - a path that never leaves the cluster.
  It catches a dead application (the Ingress has no `host`, so everything routes to
  `front-end`; verified: `200`, `X-Powered-By: Express`, 8688 bytes of HTML) but it can
  never catch a broken **published port** (k3d port mapping / a dead loadbalancer
  container) - the very first layer a user hits. A `blackbox-external` job was added with
  the target `http://host.docker.internal:8080/`, which from inside a pod is exactly the
  path a browser on the host takes (verified: `200 OK`). Both probes carry `probe_scope`
  (`ingress` / `external`), so an alert tells you which layer broke. Note:
  `host.docker.internal` is a Docker Desktop convenience - on a plain Linux host it has to
  be replaced with the host gateway address.
- Verified that **all 11 rules rely on metrics that actually exist** instead of invented
  ones: `request_duration_seconds_count` (10 series, real `status_code` label),
  `kube_deployment_status_replicas_available` (14 `sock-shop` deployments), `probe_success`,
  node-exporter metrics. A rule whose metric does not exist is the same silent gap from the
  other side: it sits in the list and can never fire.
- Bottom line in numbers: `promtool check rules` -> `SUCCESS: 11 rules found`; live
  Prometheus holds 4 groups / 11 rules; targets in `down` state: **0**; probe paths: **2**.

---

## 12. Grafana access: no host port, no `admin/admin` - a Secret, an Ingress and a Job that lied

Three problems around Grafana were really one problem: convenience quietly replacing a
security control. All three are now closed, and each fix was verified on the live stand.

- **A `NodePort` published on every host interface.** `22-grafana-svc.yaml` was
  `type: NodePort` with `nodePort: 31300`, and Terraform mapped it to host port 3000, so
  Grafana listened on **every** interface of the machine (office Wi-Fi included) with the
  default `admin`/`admin`. It is now a plain `ClusterIP` served by the same Traefik
  entrypoint as the application and distinguished by the HTTP `Host` header
  (`30-grafana-ingress.yaml`) - one port, several backends, the same idea as
  `server_name` in nginx. Reach it as `http://grafana.localhost:8080` (one line in the
  hosts file) or, without admin rights, `kubectl port-forward -n monitoring svc/grafana
  3000:80`. The matching host-port entry was removed from `terraform/k3d/variables.tf`.
  With the Service already `ClusterIP`, the leftover mapping answered nothing
  (`curl: (52) Empty reply from server` - Docker's proxy accepted the TCP connection while
  the NodePort behind it was gone).
  Dropping the mapping itself needs an explicit recreate, and that is worth knowing before
  changing any published port: the k3d provider **cannot update a running cluster**
  (`Error: Updating clusters is not supported by k3d`), and because the same apply replaces
  `local_sensitive_file.kubeconfig` (destroy, then create), a failed apply additionally
  leaves the machine **without a kubeconfig file**. Hence
  `terraform apply -replace=k3d_cluster.sock_shop`; verified after the recreate:
  `docker ps` no longer lists `3000->31300` and nothing listens on host port 3000.
- **`GF_SECURITY_ADMIN_PASSWORD: admin` in the manifest.** Both the Deployment and the
  import Job now read `GF_SECURITY_ADMIN_USER` / `GF_SECURITY_ADMIN_PASSWORD` from the
  `grafana-admin` Secret (`secretKeyRef`, `optional: false`), and the Job uses
  `curl --user "$GRAFANA_USER:$GRAFANA_PASSWORD"`, so the password is not in the argument
  list of the process either. `deploy.ps1` creates the Secret idempotently: it **reuses**
  an existing one - Grafana keeps the admin password in its own database, so regenerating
  the Secret on every run would silently lock you out of a running instance - generates a
  random password when there is none, and `GRAFANA_ADMIN_PASSWORD` overrides both.
  Template: `manifests-monitoring/examples/grafana-admin-secret.example.yaml`.
  Proven from scratch: delete the Secret -> the pod stays `0/1` with
  `GF_SECURITY_ADMIN_USER: <set to the key 'admin-user' in secret 'grafana-admin'>`,
  `Optional: false`; recreate it -> rollout completes, the Ingress login works with the
  new password and `admin`/`admin` answers `401`.
- **The import Job reported success while importing nothing.** Two independent bugs:
  1. its shell script was a **folded YAML block (`>`)**. A folded block joins the lines
     with spaces, so `fi` + `done` turned into the single word `fidone` and the leading
     `#` comment swallowed the rest of the script - and with no outcome check the Job
     still finished as `Completed`. It is a literal block (`|`) now, with a comment in the
     manifest saying why it must stay that way;
  2. the `wait-for-grafana` init container waited for `/api/health`, which answers even
     from the **previous** pod - the one still running with the previous password. On a
     redeploy the importer authenticated with the new password against the old pod and got
     `401` for every file (observed live: `importing ... 401`, Job `Completed`).
     It now waits until the credentials from the Secret are actually accepted
     (`curl -f --user ... /api/datasources`, up to ~4 minutes), and after the loops it
     **verifies the outcome** instead of trusting exit codes: it asks Grafana for the
     number of dashboards and datasources and fails the Job when the answer is zero. The
     datasource loop also separates `409 Conflict` ("already exists in grafana - kept as
     is") from a real error, so a healthy redeploy no longer prints bare `curl: (22)`.

  Live proof: a redeploy logs `grafana now reports 7 dashboards and 3 datasources` +
  `import finished`, Job `Complete 1/1` in ~8s; on a *fresh* Grafana database all three
  datasources are created and the import still ends with 7 dashboards;
  `curl -H 'Host: grafana.localhost' http://localhost:8080/api/health` ->
  `{"commit":"b5190ee547","database":"ok","version":"7.5.5"}`; `GET /api/datasources` ->
  `200` for the Secret password, `401` for `admin`/`admin`, `401` without auth.

  The lesson worth keeping: a one-shot Job needs an **outcome assertion**, otherwise
  "green" only means "the shell exited 0". Here the fix for the 401 bug and the fix for
  the silent-success bug were the same edit.

Two leftovers of the same class lived outside the Kubernetes path, and they were closed in
the same pass: "no credentials in files" is a property of the repository, not of one
directory.

- `legacy/docker-compose/docker-compose.monitoring.yml` hard-coded
  `GF_SECURITY_ADMIN_PASSWORD=foobar`, and `legacy/docker-compose/grafana/import.sh` posted
  to `http://admin:foobar@grafana:3000/...` - the very same default-credential pattern, in
  the stand's second (compose) entry point. The password now comes from
  `${GRAFANA_ADMIN_PASSWORD:?...}` (compose fails with an explicit message when it is
  unset, instead of starting with a well-known one) and `import.sh` reads
  `GRAFANA_USER` / `GRAFANA_PASSWORD` from the environment. Every published port in that
  file also binds to `127.0.0.1`: written as `3000:3000` a port is published on **every**
  interface of the host, exactly like the NodePort that was removed here.
- the archived copy `manifests-monitoring/legacy/23-grafana-import-dash-batch.yaml` still
  carried `http://admin:admin@grafana/...` in its script. It is masked to
  `admin:<password>` now: an archive is kept for structure, not for credentials, and the
  legacy README says so in both languages.

Finally, the fix is protected in CI. The `manifests-validate` job runs a **`Guard Grafana
access`** step that fails on (1) a literal credential on an active line of an applyable
manifest, (2) `GF_SECURITY_ADMIN_*` that is not followed by `valueFrom`, (3) the Grafana
Service turning back into a `NodePort` or losing its Ingress, (4) a folded YAML block (`>`)
in the import Job. None of these is a schema error, so `kubeconform` cannot see any of
them - and all four were real defects at some point in this section.

The pass ended with one more defect, in the stand's own orchestrator, and it is worth writing
down because it only appears in a specific combination. `deploy.ps1` probes twice for
something that is *allowed* to be absent: an existing `grafana-admin` Secret, and the new
rules file inside the running Prometheus container. Both probes were written the obvious way
- `kubectl ... 2>$null` and then look at `$LASTEXITCODE`. On Windows PowerShell 5.1 that is
not enough: with `$ErrorActionPreference = "Stop"` (line 45 of the script) a single byte on a
native command's stderr becomes a TERMINATING error, and the redirection does not prevent it.
Verified in isolation on 5.1: `kubectl get secret <absent> -o jsonpath=... 2>$null` throws
`NativeCommandError`. The consequence on a freshly created cluster was that the run stopped
dead in section 5/6 with `Error from server (NotFound): secrets "grafana-admin" not found` -
that is, exactly on the "first run against a new cluster" path, while every re-run stayed
green because by then the Secret existed. Both probes now go through a small
`Invoke-QuietProbe` helper: it lowers `$ErrorActionPreference` to `Continue` for the duration
of the call, merges stderr into the pipeline and drops the error records, and hands the exit
code back as data - so "the Secret is not there yet" is an answer, not a crash. Verified: the
probe returns `exit=1, output=''` for an absent Secret without throwing, and a full run on a
fresh cluster now reaches 6/6.


## 13. The alerting Secret: no token is ever typed, and the cluster hands its own back

The stand needs two credentials (a Telegram bot token and a Slack incoming-webhook URL), and
the first version of the tooling treated them as something a human has to paste into a
command line. That is wrong twice over: a value typed as an argument stays in the shell
history, and it demands a secret that the stand may already hold.

Three things changed.

- **The git-ignored file is the source, the environment is the CI fallback.**
  `deploy\secrets.local.ps1` is sourced on every run, and it is sourced *after* the
  environment - so it wins, and a shell with a stale exported `TELEGRAM_BOT_TOKEN` cannot
  silently deploy a token that differs from the file. CI has no such file and passes the two
  environment variables instead. The README no longer presents
  `$env:TELEGRAM_BOT_TOKEN = '...'` as the normal path, and the manual step 6 dot-sources
  that same file instead of asking for the value.
- **When neither has the values, the cluster supplies them.** The early check used to `throw`
  before Terraform had even created the cluster, so a re-run (or a fresh clone sitting next
  to a live stand) died over credentials that were already deployed and did not need to
  change. Now the missing values are a printed note, and section 6 reads them back out of
  `alertmanager-secrets` itself - `kubectl` hands Secret values over base64-encoded, so the
  new `Get-ClusterSecretValue` helper decodes them - and then re-applies the Secret
  idempotently. If the Secret is missing as well, the `throw` names the one thing to do
  (create `deploy\secrets.local.ps1` from the `.example` sample) and still never asks anybody
  to paste a token. Every probe goes through `Invoke-QuietProbe`, for the PowerShell 5.1
  reason from section 6.9: an absent Secret is an answer, not a crash.
- **No paste-me placeholders left in the deployment docs.**
  `examples/alertmanager-secret.example.yaml` used to advertise a
  `--from-literal=telegram_bot_token='…'` recipe and carried `PASTE_BOT_TOKEN_HERE`; both are
  gone, and the key is now `REPLACE_ME` next to a note that the file is a shape reference,
  not a template. `manifests-alerting/README.md` no longer calls the environment variables
  the primary path.

Verified on the live stand, each run through `Start-Transcript` (the transcript holds the
script's own notes; the `... unchanged` lines are quoted from the run output, because a
PowerShell 5.1 transcript does not capture a native command's stdout):

- **without** `deploy\secrets.local.ps1` and without environment variables, against a cluster
  that already had the Secret: the run prints the note, then
  `Alerting credentials taken from the cluster Secret - nothing to type`, then
  `secret/alertmanager-secrets unchanged`, and reaches 6/6 with no `Error` line - the values
  travelled out of the Secret and back into it, and the exact case that used to abort now
  completes;
- **with** the file: 6/6 again and `secret/alertmanager-secrets unchanged`, i.e. the
  idempotent apply of the real values still works;
- **with the Secret deleted** and no `deploy\secrets.local.ps1`, but with the two environment
  variables present (this machine has them set at Windows level), the run recreated the Secret
  from those variables: `secret/alertmanager-secrets created`, exit code 0 - the branch a CI
  shell takes;
- the last branch (no Secret and no credentials anywhere) was not exercised live for exactly
  that reason: the machine has both variables exported, so the run always found something to
  deploy. Its `throw` is therefore quoted from the code - it names `deploy\secrets.local.ps1`
  (created from the `.example` sample) as the one thing to do and still contains no request to
  paste a token anywhere.

After those runs the Secret is in place again and the stand is intact: both keys are present
(`telegram_bot_token=64`, `slack_webhook_url=108` base64 characters). The `with the file` run
reporting `unchanged` is what proves the values in the file and in the Secret are the same.

Checked alongside: `deploy/secrets.local.ps1` is not tracked, is ignored by
`.gitignore:148`, and has never been committed - the token the stand runs on has never been
in git.


---

## 14. Autoscaling: eight HPAs that could not scale, and the defect only `TARGETS` could show

`deploy/kubernetes/autoscaling/` had been in the repository since the upstream fork and looked
finished: eight `*-hsc.yaml` files, valid YAML, believable numbers. Measured state: **not one
autoscaler was running, and none of them could have.** Four independent defects:

| Defect | How it showed up | Consequence |
|---|---|---|
| `apiVersion: autoscaling/v1` | reading the files | no `behavior` at all - v1 cannot express scaling policies, so the controller reacts to every blip: adds a replica, sees the average fall, removes it 15 s later |
| `scaleTargetRef.apiVersion: apps/v1beta1` | `kubectl describe hpa` | removed in Kubernetes 1.16 - on this 1.35 cluster the HPA cannot resolve its target at all |
| the directory was applied by nobody | `kubectl get hpa -A` was empty | the repository advertised elasticity the cluster did not have; worse than absent, because it looks done |
| the HPA named `cart` pointed at `Deployment/cart`, the Deployment is `carts` | `kubectl get hpa` showed `cpu: <unknown>/50%`, forever | an autoscaler that can never fire |

The last row is the reason this section exists: **`kubectl apply` prints `created` for an HPA
whose target does not exist.** The only proof that an autoscaler works is the `TARGETS` column
(`cpu: 3%/50%`) or the events in `kubectl describe hpa`; a permanent `<unknown>` is a dead
autoscaler, not a slow one. Renaming the object to `carts` was not cosmetic - it is what makes
the target resolve.

**Fix.** All eight files are `autoscaling/v2` with `target: Utilization / averageUtilization:
50` and an explicit `behavior`; `deploy.ps1` applies the directory and then **waits until an HPA
reports a CPU metric**, failing the run if it never arrives (the objects exist either way - that
is exactly the trap being closed). The rationale for the numbers is in `autoscaling/README.md`:
50% of `requests.cpu` is the only target that means the same thing across pods and nodes (every
service here requests 100m); `maxReplicas` is a budget decision rather than a round
number - 5 for the four Go/Node services, **2 for the four JVM ones** (§18 has the measurement:
20 JVMs at ~320 MiB is 6.4 GB, more than the Docker VM owns). Upstream asked for `10` everywhere
and the first rewrite cut that to 5 by counting *pods* (8 x 10 = 80 on three nodes); a pod budget
is the wrong unit when four of the eight services are JVMs. Up 2 pods / 30 s and down 1 pod / 60 s
behind a 300 s stabilization window is what stops the classic flap. The 2018 stack that shipped next to these files - Heapster, InfluxDB, an old
Grafana - was moved to `autoscaling/legacy/` with a README saying what replaced it
(metrics-server), because Heapster was removed from Kubernetes in 1.13 and applied by nobody.

**Verification (live, on this stand).** The honest problem is that locust drives `front-end` to
~3m of CPU - 3% of its 100m request - so real traffic is nowhere near the 50% target and the
HPA correctly stays at one replica. Inventing CPU load would prove that the laptop gets hot, not
that the autoscaler works, so the *request* was narrowed instead (1m, temporary, reverted; the
shipped manifest keeps 100m):

```
t=40s    cpu: 300%/50%   replicas: 1   pods: 1
t=60s    cpu: 200%/50%   replicas: 3   pods: 3      <- New size: 3; reason: cpu resource
t=80s    cpu: 300%/50%   replicas: 3   pods: 4         utilization (percentage of request)
t=100s   cpu: 200%/50%   replicas: 4   pods: 4         above target
```

and the reverse direction, after the request was restored to 100m: `4 -> 2 -> 1` replica, one
pod per 60 s behind the 300 s window, `cpu: 2%/50%`. Both directions were measured on the same
HPA with the same traffic and no synthetic pressure. Scale-down is the half people skip, and it
is the half that decides whether an autoscaler is usable: a controller that never comes back
down only moves the capacity problem somewhere else.

**What is honestly still missing** (kept in the notes' weak-points section): no
PodDisruptionBudget, so draining a node can still take every replica of a service at once; no
VPA and no cluster capacity headroom, so even `maxReplicas: 2 x 4 JVM services + 5 x 4 light
services` is a promise the three nodes could not keep at once; `queue-master` is CPU-scaled although its real signal is queue depth (the
honest next step is KEDA on RabbitMQ metrics, not a lower CPU target); and there is no node-level
autoscaling, which on a laptop is a deliberate omission rather than an oversight.

---

## 15. Network policies: the segmentation that was a namespace annotation

`manifests-policy/` was in the same state as the autoscaling directory, and for the same reason:
**validated by CI, applied by nobody.** Three defects, each of them hidden by the others:

- `netpol-default-deny.yaml` was not a NetworkPolicy at all. It was a `kind: Namespace` object
  with the pre-1.7 annotation
  `net.beta.kubernetes.io/network-policy: {"ingress":{"isolation":"DefaultDeny"}}`. Modern
  clusters ignore that annotation completely, so the file called "default deny" denied nothing.
- the twelve per-service files carried neither `policyTypes` nor a single `egress` rule. They
  listed ingress sources and left the rest to defaults, so nothing in the repository said what a
  pod is allowed to *send*.
- `session-db` (Redis, `SESSION_REDIS=true` in `09-front-end-dep.yaml`) was covered by no file
  at all - and that Redis has no password on this stand.

Measured consequence: any pod could reach any other pod, every Mongo/MySQL store included.
`kubectl get netpol` printed thirteen objects, which is exactly the kind of security that shows
up in a screenshot and not in a threat model.

**Fix.** The upstream set was deleted rather than kept: NetworkPolicy objects are **additive**
(Kubernetes unions every rule that selects a pod), so an old permissive rule cannot be "backed
up" by a new strict one - it only adds holes back. The replacement is 17 objects: a real
`default-deny` for ingress *and* egress, platform egress that every pod needs (DNS, Jaeger), the
scrape path for Prometheus, and one file per service naming its callers and its own
dependencies. Ports are copied from the Service definitions, which is how `catalogue-db:3306`
(MySQL) is told apart from the three Mongo stores on 27017.

**Verification** - `verify-netpol.ps1` creates throwaway probe pods, reads their verdict and
deletes them again, so nothing is left behind. 6/6 green:

```
[OK] default ns -> front-end.sock-shop:80          blocked, rc=1
[OK] monitoring (Prometheus) -> front-end:80       reachable, rc=0
[OK] monitoring (Prometheus) -> catalogue/metrics  reachable, rc=0
[OK] front-end -> carts:80                         reachable (OPEN)
[OK] front-end -> session-db:6379                  reachable (OPEN)
[OK] front-end -> user-db:27017                    blocked (ERROR ECONNREFUSED)
```

The two blocked lanes are the point, and so is the fact that the same `front-end` pod still
reaches its own dependencies: the policy cuts one lane, not the application. Metrics are the
third check - a forgotten scrape path shows up as `up == 0` and `ScrapeTargetDown` within a
minute.

**Traps measured here** (every one of them cost time):

- **DNS is egress.** With `policyTypes: [Egress]` and no DNS rule every service lookup fails
  with "no such host", which reads as an application bug rather than a firewall.
- **kubelet probes are not policed on this CNI - and that is a measurement, not an assumption.**
  The first version of `02-observability-and-probes.yaml` allowed the node network
  `172.19.0.0/16` "because probes cannot be expressed with a podSelector". The rule was then
  removed and the namespace watched: every pod stayed `1/1 Ready`, no `Unhealthy` events, zero
  `up == 0` targets. k3s runs kube-router, which keeps node-originated traffic out of policy
  evaluation, so the rule was deleted instead of kept "just in case" - an unused `ipBlock` on
  the node CIDR is a hole with no benefit. On a CNI that does police kubelet traffic (some
  Calico setups) it must come back; the comment in the file says so.
- **`namespaceSelector` needs `kubernetes.io/metadata.name`**, the label Kubernetes adds to
  every namespace since 1.21 - matching on a hand-written label silently selects nothing.
- **A probe pod carrying a workload's labels joins that workload.** The first version of the
  verification script ran a pod labelled `name=carts`, and the `carts` HPA counted it as one of
  its own: `FailedGetResourceMetric ... missing request for cpu in container netpol-check-3`.
  Labels are identity for everything that selects pods.
- **k3s REJECTs, it does not drop.** Denied connections come back as `ECONNREFUSED`
  immediately instead of timing out, so "it failed fast, therefore the policy cannot be there"
  is the wrong conclusion in the other direction. And `nc` inside busybox hangs on an
  *established* connection, so its exit code lies; the verdict has to come from a real client -
  here, `node` inside the existing front-end pod.
- **PowerShell 5.1 and `\"` inside single quotes**: the escape is a literal backslash, so JSON
  written that way reaches `kubectl -p` / `curl -d` broken. Payloads go through files
  (`--patch-file`, `--data-binary`), the same lesson as section 6.10.

**What this does not give you**: NetworkPolicy is L3/L4 only - no HTTP path or method filtering
and no TLS inspection are possible at this layer; the DNS rule is a legitimate exfiltration
channel (it is also a required one); and pod configuration hardening (Pod Security Admission,
RBAC scoping) is a different axis, still open in section 10.

The lesson worth keeping: the control *and* the deployment step were both missing, and either
one alone would have produced a false sense of security - a policy that is never applied and a
policy that is applied but wrong look identical in a repository.

---

## 16. An AIOps triage bot, and the two bugs its first live run found

This section is different from the others: the component is new rather than an upstream defect. It
belongs in this file anyway, because both of its real bugs were found the way every other fix here
was found - by running it against the live stand. The unit suite was green before and after each one.

### What was added

`aiops/triage-bot`: Alertmanager POSTs a firing alert, the bot collects read-only evidence from
Prometheus and Loki, the cascade answers - a ruleset first, a model only when the ruleset is itself
unsure - the answer is validated against a whitelist, and a Telegram card with **approve / details /
report / ignore** buttons appears. Nothing is executed. Delivered with it:

| Piece | Where |
| ----- | ----- |
| The bot: ten stdlib-only modules plus `run-local.ps1` and 120 tests | `aiops/triage-bot/` |
| ConfigMap, Deployment, Service, NetworkPolicy | `deploy/kubernetes/manifests-aiops/` |
| The Alertmanager route (`aiops-triage`, `continue: true`) | `manifests-alerting/alertmanager-configmap.yaml` |
| One deploy switch: build, `k3d image import`, optional Secret, apply, wait | `.\deploy.ps1 -WithAIOps` |
| A CI job that runs the suite, greps the guardrails and import-smokes the image | `.github/workflows/main.yaml` (`aiops-tests`) |
| 15 contract checks between the YAML and the code | `tests/test_manifests.py` |

"My bot cannot act" is stated in four independent places, so a mistake in one of them is not enough
to break the boundary: the pod has `automountServiceAccountToken: false` and no `serviceAccountName`
(nothing to authenticate with); there is no RBAC object anywhere in the directory (and CI fails if
one appears); the image holds neither `kubectl` nor a client library nor a shell; and the action
whitelist lives in `triage_bot/model.py`, not in the ConfigMap, with `TriagePipeline.execute()`
raising `NotImplementedError` whose docstring is the M2 specification.

### Bug 1: the Loki selector asked for a label that does not exist

**Symptom.** A card for the front-end said `missing: loki: no log lines in the window` - while
front-end was logging continuously.

**Cause.** Loki's labels are cut by Promtail out of the *log file path* on the node
(`manifests-logging/04-promtail-configmap.yaml`), so the label set is
`container_name, filename, job, namespace, pod_name, stream`. The first version reused Prometheus
names (`pod`, `deployment`), which Loki has no such label for. The query was valid, matched nothing,
and came back empty.

**Why it was invisible.** An empty answer is not an error. Prometheus handed over its metrics, Loki's
`/query_range` answered `200` with `"result": []`, and the only trace of the defect was one word in a
list the code fills in exactly so that silence stays visible. A response of the wrong shape would
have failed loudly; this one failed politely.

**Fix.** The selector uses `pod_name` / `container_name`, and the label names are pinned by
`tests/test_context.py`. Measured on the live stand: `GET /loki/api/v1/labels` answers exactly
`container_name, filename, job, namespace, pod_name, stream`, and sock-shop had **673,191 log lines
over the last 6 hours** - the logs were always there.

### Bug 2: tier 0 trusted the alert

**Symptom.** A synthetic POST of `DeploymentReplicasUnavailable` describing a perfectly healthy
front-end produced a confident `restart_deployment`.

**Cause.** The ruleset keyed on `alertname` and checked nothing else, and the alert was the only
input it ever verified. That inverts the job: the alert is a *claim* produced by a rule that can be
stale, mislabelled or synthetic (all three were true here - the payload had just been written by
hand). Acting on the claim without reading the series the claim was computed from is precisely the
failure mode a first responder must not have.

**Fix.** `MockAdapter.PREMISES` plus `_premise_conflict()`: for every answer whose alert has a
premise, the value is re-read from the series it must come from (`replicas_available` against
`replicas_desired`), and a contradiction turns the answer into an escalation that carries **both
numbers**:

```
Алерт DeploymentReplicasUnavailable не подтверждается метриками
(replicas_available=1 >= replicas_desired=1): состояние уже соответствует
желаемому, автоматический шаг ничего не исправит - решение за человеком.
```

The asymmetry is the whole policy: only a **contradiction** stops the bot. A **missing** metric is
not a conflict - silence must not block a safe step, or the bot would go inert on any stand with one
broken exporter, which is worse than being wrong about a single alert.

### Bug 3 (small): a health field that lied

`/healthz` reported `model_tier_reason: "the provider has no base_url"` on a stand running
`MODEL_ADAPTER=mock`, where nothing was missing at all. `mock` *is* the tier-0 ruleset, so the
honest answer is an empty string. A misleading field in a health endpoint costs more than the three
lines it takes to remove it, and the live run is what made it visible.

### Bug 4: the card named the validator instead of the cause

**Symptom.** A card whose model answer had been rejected showed, as its reason, `params ignored:
escalate_human takes none` - the validator's note - and said nothing about why the model had chosen
that action. The evidence collected a moment earlier was gone, and so was the model's free-text
proposal.

**Cause.** Two losses in one path. `escalate()` built a fresh `Decision` and carried only `reason`,
`raw` and the adapter name, so `evidence` and `confidence` were dropped at the exact moment they were
most needed. The pipeline then put the validator's note into that same `reason`, letting a bookkeeping
sentence displace the explanation a human was reading for.

**Fix.** `escalate()` now carries `evidence`, `confidence` and `proposal` through; the validator's
remark travels in its own field (`validation_note`) and is rendered as its own line; and the card was
re-laid out to answer, in order, "why", "what did you see", "what do I do" - including the alert's own
runbook when nothing is executable. The tier-by-tier comparison left the card entirely: it is a
measurement for the journal, not news for a human at 3 a.m.

### The follow-up buttons: advice that is advice, and a report that survives the chat

`📝 без действий` promised a consultation and wrote one journal line. A label a human learns not to
trust is worse than a missing button, so it is now `💡 подробнее` - and it does what it says: a
**second model call** over the recorded incident, answered in prose.

The prose path is deliberate (`Adapter.answer()`): no `response_format`, no JSON schema, no
validator, no whitelist. An answer that looked like a decision while bypassing the validation that
makes a decision trustworthy would be the worst of both shapes - so this one is text a human reads,
and the pipeline never parses it. The call is built from the incident already in the journal, so
pressing the button cannot query the cluster; a second press within 30 seconds is refused instead of
being paid for twice.

`📄 отчёт` (`triage_bot/report.py` plus `sendDocument`) sends the incident as a Markdown file: cause,
evidence, the step, the cascade the card hides on purpose, what data was missing, and every human
press with its timestamp and user. The chat scrolls and dies with the chat; the file does not. Both
buttons leave the keyboard in place - after reading them a human can still press `approve` or
`ignore` - and neither executes anything.

### Verification

| Check | Result |
| ----- | ------ |
| `python -m unittest discover -s tests -t . -v` | **120 tests, green**, no network, a few seconds |
| `kubeconform -strict` over `manifests-aiops/` | 5/5 objects valid |
| `amtool check-config alertmanager-configmap.yaml` | SUCCESS (the new `aiops-triage` route included) |
| `run-local.ps1` against the live stand | one alert in, one card in Telegram, one incident in the journal |
| `run-local.ps1 -Model deepseek -Alertname ProbeFailed -Fresh` | `deepseek-chat`, **1.64 s / 907 tokens**, then **1.85 s / 899 tokens** on a repeat run; the model answered `escalate_human` *with* `params`, the validator rejected it (`bad_params`) and kept the escalation |
| the `💡 подробнее` / `📄 отчёт` buttons | covered by `test_pipeline.py` and `test_report.py`: a prose call, a Markdown file, and no execution path in either |
| `kubectl -n monitoring get pods` | `triage-bot` `1/1 Running`, 0 restarts, no service-account token in the pod |

What the live run proves that the suite cannot: the pydantic-free payload path survives a real
Alertmanager (its `alerts` array, `fingerprint`, `status`, the `annotations` it actually sends), the
Loki selectors match the labels Promtail really produces, the Prometheus queries return numbers
rather than `"result": []`, the NetworkPolicy allows exactly the three conversations the bot needs,
and the Telegram card renders without a parse error on real text. All five of those were wrong or
unproven somewhere along the way.

The live model run is worth reading on its own, because it produced the first real `bad_params`.
Tier 0 escalated by its own confidence (0.60 against the 0.70 threshold - by design, not by
failure), `deepseek-chat` was asked, and it answered `escalate_human` - the right action - with
`deployment`, `namespace` and `replicas` attached to it. `escalate_human` is a param-less action,
so the validator threw the answer away, kept the escalation and recorded why
(`escalate_human needs exactly no params, got [...]`). Two runs, 1.6-1.9 s and ~900 tokens each,
the same outcome. A model that is right about the action and wrong about its shape is exactly the
case a schema check exists for: a reviewer who reads only `action_id` would have seen nothing wrong.

### What this does not give you

M1 is an advisor, and that is a deliberate stopping point rather than a limitation discovered late:

- no execution and no Kubernetes access at all - events, node conditions and `describe` output are
  not on the card but in `missing`, which is the honest version of the same thing;
- one replica, a `Recreate` strategy and an `emptyDir` journal - the dedupe window and the audit
  trail survive a process restart, not a pod restart;
- no authentication on the webhook, as in the rest of the alerting stack: the NetworkPolicy is the
  credential, and it is worth saying out loud rather than implying a token that is not there;
- the ruleset knows only the alerts this stand fires, and a new alert is a new row plus a test that
  fails first;
- no eval set yet - `EVAL_MODE=1` measures a model against the ruleset on the live alerts, which is a
  baseline, not a golden set of incidents with known causes.

The lesson worth keeping: the suite of 94 tests passed before *and* after both bugs, and it would
have passed forever. A green unit suite says the code does what its author meant; a live run is what
asks whether the author meant the right thing. The two defects were also of the same shape - a
component answering confidently from insufficient data - which is the failure mode this whole
component was built to remove, committed first inside it.

---

## 17. A stand that would not come back, and the two deploy bugs the rebuild found

Section 16 ends with a live run against a healthy stand. This one is about the day that stand
refused to start. It is here because the first three explanations were wrong, and because the fix
that finally worked uncovered two defects in `deploy.ps1` that had been sitting behind a deploy
that looked green.

### The symptom

`docker ps` and `kubectl get nodes` both hung; sometimes `docker ps` answered `500 Internal Server
Error` on the `dockerDesktopLinuxEngine` named pipe, sometimes `kubectl` gave `net/http: TLS
handshake timeout`. `docker stop`, `docker exec` and `docker inspect` hung as well, and
`wsl -d docker-desktop` began failing at the service level (`WsaETimeout`), so the VM could not
even be inspected from inside.

All four k3d containers were listed as `Up`. The k3s log was frozen. From the outside it looked
like a k3d or Kubernetes problem, which is what made it expensive: k3d was fine.

### What the measurements said

| Layer | Measurement |
| ----- | ----------- |
| Host | 15.2 GB RAM, 49 GB free on `C:`, no `.wslconfig` - so the WSL VM gets ~7.5 GB |
| VM | load average **577**, `Mem 7383/7554 MB`, **33 MB** available, swap 1562/2048, `57% sys / 18% io / 2% idle` |
| Host disk | not full - the "vhdx ran out of space" theory is ruled out |
| Engine, empty | after a forced `wsl --shutdown`, `docker ps` answered in **0.3 s** |

The last two rows decide the diagnosis. Docker and WSL are healthy and the host has space; what
is broken is the *workload*. The VM is alive, out of memory and I/O-bound, and every process that
touches the filesystem sits in uninterruptible sleep - which is why `docker stop` hangs instead of
stopping a container: the runtime cannot make progress either.

### What did not work

1. **Restarting Docker Desktop** (twice). All four k3d containers come back on their own and start
   at the same moment, so the first minutes are the heaviest the stand ever gets - straight back
   into swap.
2. **`wsl --shutdown`, then starting the nodes by hand.** Server alone: fine (load 2.6).
   `+agent-0`: fine (load 4.6). `+agent-1`: unresponsive within 50 s.
3. **Disabling the suspect DaemonSet.** That API call hung too, because `docker exec` was in the
   same uninterruptible state as everything else. You cannot repair a wedged VM from inside it -
   the one lesson here that generalises to every "the cluster is hung" situation.
4. **`docker update --restart=no` followed by `docker stop`.** The update succeeded, the stop hung.
   (The policy itself is worth knowing: `unless-stopped` on all four containers is what turns a
   Docker Desktop restart into a stampede.)

### What worked

Throw the cluster away and rebuild it:

```powershell
.\deploy.ps1 -Down            # terraform destroy, 18 s
.\deploy.ps1 -WithAIOps       # terraform apply + 8 deploy steps, ~5 min
```

The stand is infrastructure as code, so this is the *cheap* path, not the last resort - it is the
reason `terraform/` is in the repository at all. The same 7.5 GB VM that could not answer
`docker ps` now runs the whole stack at load 0.2-0.9 with 2.7 GB free and 0 pods not `Running`.

Why the old cluster was unusable is a hypothesis, and it is worth labelling as one: three days of
containerd snapshots, Prometheus TSDB and Loki chunks, plus an unclean shutdown in the middle of a
write, make the first minutes after a cold start the heaviest the stand ever gets. One DaemonSet
adds to exactly that - `manifests-monitoring/08-prometheus-exporter-disk-usage-ds.yaml` walks the
whole `/var` of every node with `du` every 300 s (`--threshold` limits the output, not the
traversal) - but the control experiment that would have proven it needed the API that was already
gone. What is *proven* is the shape of the fix: a fresh cluster on the same memory limit is
comfortable.

### Bug 1: `deploy.ps1` died at step 4 of 8 and said nothing

The HPA wait loop checked its metrics with:

```powershell
kubectl -n sock-shop get hpa -o jsonpath='{range .items[*]}{.status.currentMetrics}{"\n"}{end}'
```

Windows PowerShell 5.1 drops those embedded double quotes when it hands the argument to a native
command, so kubectl receives a bare `\n` and refuses it:

```
error parsing jsonpath {range .items[*]}{.status.currentMetrics}{\n}{end},
unrecognized character in action: U+005C '\'
```

The loop retries for two minutes and then throws, so the deploy stopped before steps 5-8: no
logging, no tracing, no monitoring, no alerting - and the only trace was one line on stderr. The
fix removes `{"\n"}` (the output is pattern-matched for `averageUtilization`, so a concatenated
list is enough) and writes the reason next to it, because the next person will otherwise "fix" it
back.

### Bug 2: `-WithAIOps` ran `kubectl image import`

`Invoke-QuietProbe` - the helper that exists because a native command's stderr becomes a
terminating error under `$ErrorActionPreference = "Stop"` - hard-coded `kubectl`. The AIOps step
calls it with `image import ...`, so it ran `kubectl image import`, got exit 1 and printed an empty
message, because the same helper filters `ErrorRecord`s (that is, stderr) out of its output. Two
fixes: the helper takes `-Command` (default `kubectl`) and `-KeepStderr`, and the call site passes
`-Command k3d -KeepStderr`. Same class of defect as section 6.10 - a failure hidden by a stream
nobody read.

### Bug 3 (cosmetic, and still worth a fix): mojibake in the terminal

`run-local.ps1` prints Russian section headers and was saved as UTF-8 **without a BOM**. PowerShell
5.1 reads a BOM-less file in the ANSI code page, so the literals were already broken before anything
printed them (`--- СЂРѕС‚ ...`). Fixed by adding the BOM and setting `[Console]::OutputEncoding` and
`$OutputEncoding` to UTF-8. Exactly one file in the repository contained non-ASCII bytes, and a byte
scan found it in a minute - which is the point: this is not a mystery, it is a check.

### How to avoid it next time

The incident produced four changes. In order of how much time they would have saved:

1. **`deploy.ps1` asks the engine before it asks terraform.** A `docker info` with a 15-second
   deadline: on timeout the script says what is wrong and prints the recovery commands, instead of
   letting terraform fail for a reason that names none of it. It also prints the VM's memory, because
   a fresh stand uses ~4.6 GB and the failed one had grown to 7.4 GB of the same 7.55 GB - the number
   that matters is not the ceiling, it is the distance to it.
2. **`deploy\diagnose-stand.ps1`** separates the three failures that look identical from `kubectl`:
   engine wedged, VM thrashing, cluster unhealthy. It measures engine latency, the VM's load average
   and free memory, then nodes and pods, and stops at the first broken layer with the command to run
   next. It is read-only, so "run this first" is always safe advice.
3. **The `scripts-guard` CI job.** The scripts are never executed on CI - there is no Docker Desktop
   on a Linux runner - but both bug classes are static properties of the files: an embedded double
   quote in a `jsonpath` argument fails the build, and so does a non-ASCII `.ps1` without a BOM
   (checked byte-wise, not by eye). Every `.ps1` is also parsed, so a syntax error cannot wait for
   a deploy to appear.
4. **Documentation instead of folklore.** The recovery path is in this section, in
   `terraform/k3d/README.md` and in the root README, whose "Stop / remove" section now says what to
   do when the stand does not come back.
5. **Bounded growth, at the level this stand can afford.** Nothing that matters lives on a volume -
   Prometheus, Loki and the bot's journal all use `emptyDir` - and the bot reads only the last 15
   minutes of logs, so everything that grows is data nobody asked for. `deploy\cleanup-stand.ps1`
   removes exactly that: pods in `Succeeded`/`Failed` together with their sandboxes and logs, images
   no container references (`crictl rmi --prune` on every node), and the Loki and Prometheus data
   (`rollout restart` - with an `emptyDir`, restarting the pod *is* the delete). It takes about 30
   seconds against a live stand, leaves the application, the Secrets and the bot in place, reports
   disk usage and image counts per node before and after, and has `-KeepPrometheus`, `-KeepLogs` and
   `-DryRun`. A rebuild stays what it is: the last resort for a stand that does not come back. The
   journal is reported, never truncated - it is the audit trail. Also removed: the last
   `imagePullPolicy: Always` (on the `du` DaemonSet, which runs on every node), so a restart no longer
   re-pulls an image at the busiest possible moment.

What is deliberately *not* automated: raising the WSL memory limit. It is a host-level setting
(`%UserProfile%\.wslconfig`), it changes every WSL distribution on the machine, and the stand works
without it - so it is a documented recommendation with a warning, not something a deploy script
edits behind the user's back.

### Why neither the cleanup nor the rebuild is a production pattern

It is worth being explicit, because "recreate the infrastructure every night" is exactly the kind of
idea that gets copied somewhere it does not belong. On a real cluster the answer to the same symptom
is different, and it does not involve rebuilding anything:

| Symptom here | Production equivalent |
| ------------ | --------------------- |
| one VM, no memory limit per component | `resources.requests/limits` + QoS, so a leak OOM-kills its own pod instead of starving the node |
| `emptyDir` everywhere, data disposable | PersistentVolumes with real retention (TSDB, Loki compactor) |
| nobody notices the VM at 97% | an alert on `MemAvailable` headroom, measured in days of runway, not percent |
| containerd layers grow for weeks | scheduled image pruning (`kubelet --image-gc-high-threshold`, registry policy) |
| the whole stand is rebuilt | a rolling restart of the one component that leaks, once monitoring says which |

Neither the cleanup nor the rebuild would be acceptable on a production system, and for the same
reason: the cleanup exists because nobody configured retention, and the rebuild exists because there
is no volume to keep. Both are safe here only because of three properties this stand has and a real
system does not: no state worth keeping, no users, and a cost measured in minutes. Both scripts carry
that caveat in their own help text, so neither can be lifted into a real environment without the
warning travelling with it.

### Verification

| Check | Result |
| ----- | ------ |
| `docker ps` after a forced `wsl --shutdown` | answered in 0.3 s - the engine was never the problem |
| `.\deploy.ps1 -Down` | `terraform destroy`, 18 s |
| `.\deploy.ps1 -WithAIOps`, run twice | 8/8 steps both times, image imported, `triage-bot is Ready` |
| `kubectl get nodes` / `get pods -A` | 3/3 Ready, 0 pods not `Running`/`Completed` |
| VM after the rebuild | load 0.2-0.9, ~2.7 GB available, same 7.5 GB ceiling as before |
| `deploy\diagnose-stand.ps1` | exit 0 on a healthy stand; each of the three failures prints its own recovery command |
| `deploy\cleanup-stand.ps1`, live stand | 3 finished pods deleted, images 18→15 / 18→15 / 15→14, Loki and Prometheus back to `Ready` in under a minute, the VM's disk 23→21 GB |
| `deploy\cleanup-stand.ps1 -DryRun` | prints the plan, exits 0, changes nothing |
| `aiops` unit tests / every `.ps1` parsed | 94 tests green, no parse errors |
| the two `scripts-guard` checks, run locally | 0 jsonpath hits, 0 non-ASCII files without a BOM |
| `run-local.ps1` again | Russian headers render, DeepSeek answered, the same `bad_params` → `escalate_human` |

The lesson worth keeping: a deploy script is code, and code that only ever runs when a human
double-clicks it is code without tests. Both bugs were in behaviour that had not been executed
since it was written - the deploy "worked" because it was demonstrated from the middle. A green run
of steps 1-4 says nothing about steps 5-8.

---

## 18. The stand that came back with the wrong API port, and a cold start that looked like a dead cluster

**Symptom.** After a Docker/WSL restart `k3d cluster start sock-shop` failed with `ports are not
available ... access permissions` on host port **56910**; `kubectl` was unreachable while all three
nodes claimed to be healthy, and the agent containers exited with code **0** a minute after starting.

**What the measurements said.**

- `netsh interface ipv4 show excludedportrange protocol=tcp` showed **56897-56996** reserved, with the
  cluster's random API port 56910 inside it. k3d publishes the API through the load balancer, so a
  load balancer that cannot start means no `kubectl` at all - even with every node running.
- `docker inspect` showed `k3d-sock-shop=invalid IP` on both agents: after the WSL restart they had
  lost their network endpoints, and k3s exited with `failed to start networking: ... failed to find
  interface with specified node ip`. A clean `exit(0)`, which is why the container looked "started
  and then gone" instead of crashed.
- `docker logs k3d-sock-shop-serverlb` showed the API proxied to `172.19.0.2/0.3/0.4` - agents
  included - because `6443` had been pinned in the general `ports` list.

**Fixes.**

| What | Why |
| ---- | --- |
| terraform: `kubeAPI = { host = "127.0.0.1", hostIP = "0.0.0.0", hostPort = var.api_port }`, `api_port` default `"57443"` | `--api-port` in a k3d config is the `kubeAPI` block. A `6443` entry in `ports` makes the LB balance the API across agents, which run no API server - two requests out of three then fail with `TLS handshake timeout`. `hostPort` must be a **string**: the schema rejects a number (`Expected: string, given: integer`). |
| `host = "127.0.0.1"` | A kubeconfig with `server: https://0.0.0.0:57443` hangs `kubectl` on Windows, while `curl -k https://127.0.0.1:57443/healthz` answers 401 on the same port. |
| `deploy.ps1` normalises the kubeconfig server to `https://127.0.0.1:<api_port>` | k3d can still write its own address into the file. The cluster is healthy; the file is not. |
| `deploy.ps1` runs `docker build` through `Invoke-QuietProbe` | `docker build` writes progress to stderr, and `$ErrorActionPreference = "Stop"` turns one stderr byte into a terminating error *after* the image is built - the deploy died before `k3d image import` and `kubectl apply` and left a stand with no bot. |
| HPA `scaleUp.stabilizationWindowSeconds: 30` -> **120** | Cold start: every JVM warms up at once, `carts` spends a minute at 183% of its 100m request, and a 30 s window is short enough for the HPA to take `carts` and `shipping` to five replicas each. Measured: the 7.5 GB Docker VM fell to **64 MB free**, load average **12**, `kubectl` timed out and `docker ps` stalled - a healthy cluster that looked dead. With 120 s both stay at one replica: ~3 GB available, load ~1. |
| HPA `maxReplicas: 5` -> **2** for the four JVM services (`carts`, `orders`, `queue-master`, `shipping`) | The 120 s window covers a *deploy*, where pods appear a few at a time while images import. It does **not** cover a stand restart: all 30 pods start together, the four JVMs stay above target for longer than two minutes, and the HPA walks all four to 5 replicas - 20 JVMs at ~320 MiB RSS = ~6.4 GB. Measured after `stand.ps1 -Stop` then `-Start -NoRebuild`: **8989 MB used of 9946, 802 MB available**, load average **16-22**, held for ~8 minutes until the 300 s downscale window walked it back; `describe hpa` logged `FailedGetResourceMetric` throughout - a VM too busy to answer the metrics API cannot scale itself back down. With the cap at 2 the same restart peaks at 8 JVM pods ~2.6 GB. The four light Go/Node services keep 1..5, so the demo still shows a real 1 -> 2 rescale. |

The ordering matters: the port problem made the cluster unreachable, the HPA stampede made the
Docker VM unresponsive, and each looked like the other. What separated them was measuring inside the
node (`docker exec ... uptime`, `free -m`, `kubectl get hpa`) instead of asking the API that was
already gone.

---

## 19. The bot that triaged itself: one bad line of text, and a journal that outlived its data

**Symptom.** Telegram received an endless stream: Alertmanager's own message "Prometheus cannot
scrape `kubernetes-service-endpoints` (10.42.2.14:8080)", then an AI triage card for it, then the same
pair again after every restart. Pressing "📄 отчёт" on an older card did nothing visible - the toast
said "инцидент не найден: журнал пода эфемерный" and the report never arrived.

**What the measurements said.**

- `api/v1/targets` gave the whole answer in one field: `lastError: expected metric name after HELP,
  got "(" ... "# HELP ("`. The bot's `/metrics` served `# HELP ('aiops_alerts_received', ()) …`,
  because `render()` unpacked `(name, labels), value` as `(name, labels)`: the dictionary key is
  itself a tuple, so `name` became the entire key.
- Prometheus rejects the **whole scrape** on the first invalid line. So `up{job=...,kubernetes_name=
  "triage-bot"}` was 0, and the `ScrapeTargetDown` rule (`aiops: "true"`) fired on the bot's own
  endpoint. The bot triaged its own scrape failure, which is a closed loop that no amount of dedupe
  can fix: the card is produced by the very thing that is broken.
- 120 tests and not one called `Metrics.render()`. `snapshot()` was tested; the wire format - the
  only part Prometheus sees - was not.
- The journal (`/data`) was an `emptyDir`, and so were the dedupe table and the snooze list. Every
  pod recreate wiped all three: cards stayed in the chat, their buttons no longer resolved an
  incident id, and Alertmanager's retry was triaged as a fresh incident.

**Fixes.**

| What | Why |
| ---- | --- |
| `metrics.py`: flatten to `(name, labels, value)` once, then render | The bug was an unpacking shape assumed twice. Stating it once makes the invalid line impossible to write. |
| `_GAUGES` as an explicit set; `aiops_build_info{tier1,version} 1` at startup | `TYPE` was decided by a string comparison against one timestamp name; a second gauge would have been exported as a counter. Build info also gives an idle `/metrics` one real sample, so "empty" and "broken" stop looking identical. |
| `tests/test_metrics.py` (9 tests) parses the exposition line by line | Substring assertions passed on the broken output - the metric names were all there. The test now fails on `# HELP (` the way Prometheus does. |
| PVC `triage-bot-journal` (256Mi, `local-path`) instead of `emptyDir` | The chat outlives the pod, so the journal must too: dedupe, snooze, `/incidents` and every button on an older card read from it. |
| missing incident now answers in the chat, not only in a toast | A three-second toast is indistinguishable from "the button is broken"; the message names the journal and where the live cards are. |

Measured after the rebuild: `up{kubernetes_name="triage-bot"} = 1`, `scrape_samples_scraped = 1`,
`aiops_build_info{tier1="deepseek",version="0.1.0-m1"} = 1`, and **zero** active alerts. The lesson is
narrower than "write tests": a metrics endpoint is an interface with a grammar, and an untested
interface is a guess - the guess was wrong for 40 minutes of cluster time and every restart after it.





## 20. A restart must not answer yesterday's buttons

**Symptom (latent).** Nothing was observed - the landmine was found by reading the restart path instead
of waiting for the next demo. With `/data` on a PVC the journal survives a pod recreate, but a rebuilt
cluster (`deploy.ps1 -Down`) starts from an empty volume: `state.json`, and with it `telegram_offset`,
is gone.

**Why that matters.** Telegram keeps unconfirmed updates for 24 hours. A fresh `state.json` means
`offset = 0`, so the first poll after a rebuild receives every button press made while the bot was down -
and each one points at an incident the fresh journal has never seen. The bot would greet its first
second of life with a burst of "инцидент не найден" messages.

**Fix.** `TelegramClient.drain_pending()` calls `getUpdates` with `offset = -1` (the newest pending update
only) and returns `update_id + 1`; confirming that id makes Telegram drop everything older. `__main__.py`
calls it **only when `state.json` does not exist**, because an existing journal means the offset is real
and late presses must still be answered. If Telegram cannot be reached the method returns `None` and
`state.json` is left unwritten, so the next start tries again - writing 0 would silently give up the
protection.

**Measured.** After the rebuild `state.json` exists on the PVC, owned by `uid 65534` (`drwxrwxrwx` comes
from `local-path`). That also proves the journal is writable by the non-root container: "the pod is
Running" only ever proved that `mkdir` had succeeded.

**Trade-off.** A rebuilt cluster loses the journal, and that is the right default - the journal is
history, not configuration. Cards that outlive it answer with the explanation instead of a report.

## 21. "No log lines" about a deployment that has no pods

**Symptom (live, the M1 demo).** `front-end` was scaled to `0` and the card's `missing` list said
`loki: no log lines in the window` - the exact sentence the Loki selector bug produced when the query
itself was broken. Two very different states, one wording, and it pointed the reader at the wrong one.

**What was actually true.** Loki was healthy and the selector was right: there was no process to log,
because `desired=0`. The context already carried `replicas_desired[front-end]=0`, so the card withheld
evidence it had.

**Fix.** `context._is_scaled_to_zero()` looks for that series, and `collect_context` uses it only when
the value is a known zero: `loki: no pods - the deployment is scaled to 0 (desired=0), so there is
nothing to log`. A `desired=1` and a missing series both keep the old sentence - turning silence into
"scaled to zero" would be the same mistake in the other direction.

**Tests.** `tests/test_context.py` asserts both directions: at zero the card must say "scaled to 0" and
must not say "no log lines"; a live-but-silent deployment and an unanswered query keep the plain
wording. Measured live on the rebuilt stand: a deployment at `replicas=0` printed `loki: no pods - the
deployment is scaled to 0 (desired=0), so there is nothing to log`, while the live `sock-shop/front-end`
(`replicas_desired=1`) and a deployment that does not exist (no series) kept the plain wording.

---

## 22. The stand that did not survive `docker stop` + `docker start`, and how to stop it now

**Symptom.** After the four k3d containers were stopped and Docker Desktop restarted (or `wsl
--shutdown` run), `kubectl` stopped answering (`TLS handshake timeout` / `connection refused`), `docker
ps` sometimes answered `500 Internal Server Error`, the load balancer exited with code **1** and the
agent containers with code **0**.

**Measurements (all on this stand).**

| Layer | Measurement |
| ----- | ----------- |
| VM | load average **355**, `Mem 7389/7554`, **68 MB** available - the same shape as section 17 |
| `docker inspect agent-0` | `EndpointID: ""`, `IPAddress: ""` - the endpoint was lost with the VM restart |
| `docker logs agent-0` | `failed to start networking: ... failed to find interface with specified node ip`, then a clean `exit(0)` |
| `docker logs serverlb` | `host not found in upstream "k3d-sock-shop-agent-0:443"` - nginx resolves **every** node while building its config |
| `docker inspect -f '{{.NetworkSettings.Networks.k3d-sock-shop.IPAddress}}'` | `bad character U+002D '-'` - a Go template identifier cannot contain a hyphen, so the obvious way to read the address does not work at all |
| `docker info` on the host | `MemTotal 7554 MB` - WSL2 defaults to **half** of the 15.2 GB host, so the ceiling was 7.6 GB while the stand had grown to 7.4 GB |
| `kubectl get nodes` vs `docker inspect` | etcd still said `agent-1 = 172.19.0.3`, while Docker had restarted the container on `172.19.0.4`: the agent exits with `failed to find interface with specified node ip` even though its own endpoint is perfectly fine |

**Cause.** Three things at once. First: k3d creates the containers with `unless-stopped`, so Docker starts
all four together, and the 7.6 GB VM that WSL2 hands out by default does not survive three k3s nodes
coming up while they replay their snapshots. Second: after a VM restart a stopped container comes back
with an empty network endpoint, k3s does not find its interface and exits with code 0 - it looks like it
started and vanished - and without the agents the load balancer refuses to start, so `127.0.0.1:57443` is
never published and `kubectl` stays silent while the server is perfectly alive. Third: even with the
endpoint repaired, the agent may come back on a **different address** than the one its `Node` object in
etcd recorded, and k3s refuses to start networking on an address that is not on any interface - so the
node dies again, this time with a healthy endpoint.

**What was changed.**

| Change | Why |
| ------ | --- |
| `deploy.ps1` step 1b: `docker update --restart=no` on the four containers | A Docker restart no longer starts a stampede: the stand stays stopped until somebody asks for it |
| `deploy\stand.ps1 -Stop` | Stops the agents, then the server, then the load balancer - by SIGTERM (`Exited (143)`), so k3s writes its snapshot |
| `deploy\stand.ps1 -Start` | Load balancer, server, a `/readyz` gate asked inside the server, then one agent at a time with a `Ready` gate and a "still running after 10 s" check, then the load balancer last |
| Why the balancer is last | Its nginx resolves every node name at startup: started before the agents it dies with `host not found in upstream`, and `127.0.0.1:57443` never appears |
| Why the API is asked inside the server | Before the balancer runs the kubeconfig is useless (it publishes the port), while the server container carries `kubectl` and its own credentials |
| Endpoint repair: `network disconnect -f` + `network connect` | An empty endpoint is the only reason for the `exit(0)`; the address is deliberately **not** pinned: k3s accepts whatever address the interface has, while pinning the old one collides with whichever container took it in the meantime |
| Start retried once on `Address already in use` | `docker start` restores the endpoint recorded in the container config, and that record can name an address another node owns by now; rebuilding the endpoint clears it |
| `%USERPROFILE%\.wslconfig`: `memory=8GB`, `swap=2GB`, `autoMemoryReclaim=gradual` | Removing the real ceiling instead of shrinking the stand: `docker info` reports `MemTotal 8327696384` (7.76 GB) and a fresh stand uses ~4.6 GB of it. Small swap on purpose - enough for a spike, not enough to thrash. `gradual` because WSL2 otherwise keeps what it took: measured with `memory=10GB`, `vmmemWSL` held a 9.66 GB commit while only 2.63 GB of it was resident, Windows had 0.5 GB free and 2.56 GB parked in Memory Compression |
| `Reset-NodeRegistration` in `stand.ps1` | Deletes the stale `Node` object (`kubectl delete node`, asked **inside** the server) and retries the start, instead of declaring the cluster unrevivable. Only the `Node` object goes: the datastores keep their emptyDir contents inside their containers, and the pods reschedule |
| `Start-NodeWithRetry` | Starts one agent, waits for it to stay running and reach `Ready`; on the stale-address failure it resets the registration exactly once and tries again - the case that used to end in a rebuild |
| `-NoRebuild` and an automatic rebuild | If a node still refuses to come back, the honest path is to recreate the cluster (`deploy.ps1 -Down`, then `-WithAIOps`); `-Start` does that by itself, because "start it and have it work" matters more than "start it cheaply" |

**Verified on this stand.** `.\\deploy\\stand.ps1 -Stop` then `.\\deploy\\stand.ps1 -Start -NoRebuild`:
all four containers reconnected their endpoints, the three nodes were `Ready` within ~40 s, all 30 pods
`Running`, the bot `Ready`, `http://localhost:8080/` answered **200** with 8688 bytes - **no rebuild**. The
addresses came back identical (`172.19.0.2/.3/.4` for container and `Node` object alike), so the reset
path did not have to fire; it is the safety net for the run where they do not match.

**What the same restart cost in memory.** The pods came back, but the cold-start CPU spike (load average
16-22) sent the four JVM services to 5 replicas each: **8989 MB of 9946 used, 802 MB available**, held for
~8 minutes until the 300 s downscale window walked it back. The stand survived because the ceiling was
raised, not because it fit. `maxReplicas` for `carts`, `orders`, `queue-master` and `shipping` is now **2**
(§18 has the measurement that justifies it); the same steady state after the cap measures **4409 MB used /
5362 MB available**, load ~0.8.

**So the ceiling went back down to 8 GB.** With the cap in place, 10 GB was headroom nobody used - 4.4 GB
steady, 5.9 GB at the worst restart - so `.wslconfig` became `memory=8GB`, `swap=2GB`,
`autoMemoryReclaim=gradual` and `docker info` reports `MemTotal 8327696384` (7.76 GB). The `gradual` line
is not decoration: WSL2 does not hand memory back on its own, and on this host that showed up as
`vmmemWSL` holding a **9.66 GB commit while only 2.63 GB of it was resident**, Windows down to **0.5 GB
free** and **2.56 GB** already parked in Memory Compression. Verified with the same restart:
`wsl --shutdown`, then `stand.ps1 -Start` - endpoints reconnected, three nodes `Ready` in ~40 s, 42 pods
`Running`, `http://localhost:8080/` answered **200** with 8688 bytes. The cold start with the cap peaked at
**5868 MB of 7942 used, 1894 MB available**, load back to 0.28 within four minutes, swap down to 6 MB (the
old run left 692 MB in swap). The 8989 MB peak from the paragraph above would not fit in 8 GB: the cap,
not the ceiling, is what makes this size enough - and anything below ~8 GB is the 7.5 GB default that
wedged in the first place.

**How to stop and start now.**

```powershell
.\deploy\stand.ps1 -Stop -Free   # optionally drop logs/TSDB before stopping
.\deploy\stand.ps1 -Status       # engine, VM, containers, nodes - changes nothing
.\deploy\stand.ps1 -Start        # in order; rebuilds by itself if a node does not return
```

**The limitation worth knowing.** A k3d node on Docker Desktop may not survive a VM restart: the
endpoint is gone and k3s does not always agree to start again. `-Start` therefore not only starts
containers, it can also rebuild the stand; the bot's journal lives on a PVC of the old cluster, so a
rebuilt stand starts with an empty journal. That is written down here so that "empty journal after a
rebuild" does not look like a bug.

## 23. The Helm chart that asked for 10 GiB on an 8 GB stand, and the CI gate that could not see it

**Symptom.** None, which is the problem. The stand was healthy, `helm lint --strict`
was green, `helm template | kubeconform` was green, and the chart's values had drifted
away from `manifests/` to the point where the two deploy paths no longer described the
same pods. It would have surfaced on the first `.\deploy.ps1 -Helm` - the one path this
repository has never run end-to-end.

**Measurements.** `helm template` against `manifests/`, workload by workload:

| Workload | `manifests/` (requests) | chart, before (requests) |
| -------- | ----------------------- | ------------------------ |
| `carts` | 100m / 200Mi | 300m / **2000Mi** |
| `orders` | 100m / 300Mi | 200m / **2000Mi** |
| `shipping` | 100m / 300Mi | 300m / **2000Mi** |
| `queue-master` | 100m / 300Mi | 300m / **1000Mi** |
| `user` | 100m / 100Mi (limit 300m / 200Mi) | 100m / **400Mi** (limit 1000Mi) |
| `catalogue` | 100m / 100Mi, limit 200m | limit **100m** |
| `payment` | 99m / 100Mi, limit 200m | limit **100m** |
| `carts-db`, `orders-db` | nothing | 500m / 512Mi |
| `catalogue-db`, `user-db` | nothing | 100m / 300Mi, limit 300m / 1000Mi |
| `session-db` | nothing | 250m / 256Mi |
| `rabbitmq` | nothing | 350m / 256Mi, limits **1 CPU** / 512Mi |

13 of 14 workloads disagreed, and the total was **10036 MiB of memory requests in the
chart against 1700 MiB in `manifests/`** - on a VM whose ceiling is 7942 MiB. Four
images had drifted too: `mongo` vs `mongo:4.4.0` (twice), `rabbitmq:3.6.8-management`
vs `rabbitmq:3.6.8`, and `user-db:0.3.0` vs `0.4.0`.

**Cause.** Two independent histories in one repository. The chart is upstream's
Weaveworks chart, kept as shipped; `manifests/` is where every measurement of §14, §18
and §22 landed - the `availableReplicas` probes, the `maxReplicas: 2` cap, the
200-300Mi requests that make 8 GB enough. The two never met, and nothing compared them:
`helm lint` and `kubeconform` ask whether each side renders and validates **on its
own**, and both answers were yes. The README even documented the hole - "CI catches
rendering and schema errors on both sides, not semantic drift between them" - which is
honest, but honesty is not a gate.

`requests == limits` on those four services also means Guaranteed QoS with a 2 GiB
memory ceiling each, and the CPU requests change autoscaling itself: an HPA scales on a
percentage of the request, so `300m` instead of `100m` makes the same autoscaler three
times slower to react to the same load (§14 is where that arithmetic was measured).

**What was changed.**

| Change | Why |
| ------ | --- |
| `values.yaml`: `carts`, `orders`, `shipping`, `queue-master`, `user`, `catalogue` and `payment` aligned with `manifests/` | The two paths have to describe the same pod, and the manifests are the numbers measured on this stand |
| `values.yaml`: the 7 datastores got `resources: {}` | `manifests/` sets nothing on them, and a request is not free - the scheduler admits pods on it |
| `payment`'s `99m` copied verbatim, with a comment | "The same objects" includes the oddities; a checker that tolerated 99m vs 100m would tolerate the next 2000Mi |
| New `deploy/kubernetes/helm-chart/check-parity.py` | Compares replicas, resources and image per workload, exits 1 on any disagreement |
| CI: `Compare the rendered chart with manifests/` in `helm-chart-validate` (`pip install pyyaml`, then the script on the file the previous step already rendered) | Turns "the two paths describe the same pods" into a gate instead of a promise |
| The 4 known image differences allowlisted **inside the script, each with its reason** | They are real disagreements that need a `-Helm` run to settle; the checker still fails on any *new* one |
| `yamllint` removed from the CI-gates list in `README.md` | `git grep yamllint` finds it only in the two READMEs - no workflow ever ran it. A gate that does not exist is the same failure class as a chart that does not describe the same pods |

**Verified on this checkout.**

| Check | Result |
| ----- | ------ |
| `helm lint deploy/kubernetes/helm-chart --strict` | exit 0, `1 chart(s) linted, 0 chart(s) failed` |
| `helm template sock-shop deploy/kubernetes/helm-chart` | exit 0, 29 resources |
| `kubeconform -strict -summary` on the rendered file, CI's own image, run locally | `29 resources found ... Valid: 29, Invalid: 0, Errors: 0` |
| `check-parity.py rendered.yaml deploy/kubernetes/manifests` | exit 0, `agree on 14 workload(s): replicas, resources and image (4 allowlisted image difference(s))` |
| The same script against a deliberately broken render (`--set carts.resources.requests.memory=2000Mi`) | exit 1, `Deployment/carts: resources differ` with both dicts - a checker that cannot fail is a rubber stamp |
| Total memory requests, both paths | 1700 MiB each, down from 10036 MiB in the chart |

**The limitation worth knowing.** The chart path still has never been installed
end-to-end: `helm` refuses to adopt objects that an earlier `kubectl apply` created, so
`.\deploy.ps1 -Helm` needs a fresh cluster, and that run is what would settle the four
image questions. The checker also compares only replicas, resources and image - probes,
env and ports are not compared, because the chart deliberately keeps the upstream 360 s
readiness timings while the manifests do not.




