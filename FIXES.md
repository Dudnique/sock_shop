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

Two small housekeeping changes closed this section. With `ci-legacy/` set aside, the workflow
file is the only thing CI runs, so its pins became the ones that matter: retiring the node20
runner had to be done by hand (commit `05cd3bb`), and `.github/dependabot.yml` now opens a
weekly PR whenever an action, a Terraform provider or one of the two helper images
(`openapi/`, `healthcheck/`) has a newer version. The scope is deliberately narrow and
**major** updates are excluded on purpose - a new major of an action or a provider changes
behaviour and is a decision, not a chore. Nothing is merged automatically: every PR runs this
same workflow.

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

- `deploy/docker-compose/docker-compose.monitoring.yml` hard-coded
  `GF_SECURITY_ADMIN_PASSWORD=foobar`, and `deploy/docker-compose/grafana/import.sh` posted
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
service here requests 100m); `maxReplicas: 5` is a three-node budget decision rather than a round
number; up 2 pods / 30 s and down 1 pod / 60 s behind a 300 s stabilization window is what stops
the classic flap. The 2018 stack that shipped next to these files - Heapster, InfluxDB, an old
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
VPA and no cluster capacity headroom, so `maxReplicas: 5 x 8 services` is a promise the three
nodes could not keep; `queue-master` is CPU-scaled although its real signal is queue depth (the
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

