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
