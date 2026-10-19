# AIOps triage bot manifests

The Kubernetes objects of the triage bot - the M1 advisor whose code lives in
`../../../aiops/triage-bot`. Read that README first; this one only says how the
component is deployed and why each line is there.

Namespace: `monitoring`. The bot *consumes* alerts, so it lives next to the
alerting stack rather than inside `sock-shop`, and in M1 it is the only workload in
this repository that does not touch `sock-shop` at all.

## Files

| File | Purpose |
| ---- | ------- |
| `01-triage-bot-configmap.yaml` | Every behaviour knob (`MODEL_ADAPTER`, `REQUIRE_LABEL`, source URLs, journal dir). No secret is ever here. |
| `02-triage-bot-dep.yaml` | The Deployment: one replica, no cluster credentials, read-only root, `Recreate` strategy. |
| `03-triage-bot-svc.yaml` | ClusterIP on 8080 plus the three `prometheus.io/*` annotations that make the bot scrape itself. |
| `04-triage-bot-netpol.yaml` | Exhaustive egress allow-list (DNS, Prometheus, Loki, 443) and ingress from Alertmanager and Prometheus only. |
| `examples/` | A sample that must NOT reach the cluster: `triage-bot-secret.example.yaml`. What keeps it out is that `kubectl apply -f <dir>` is not recursive. |

Applied by `.\deploy.ps1 -WithAIOps`, which also builds the image and imports it
into the k3d node.

## The guardrail is in this YAML, not in a document

Three lines and one absence, each of them named again by CI:

* `automountServiceAccountToken: false` and **no** `serviceAccountName`: the pod is
  handed no token, so a bug in the image still has nothing to authenticate with;
* there is no Role, ClusterRole, RoleBinding or ClusterRoleBinding in this
  directory, and the `aiops-tests` job fails if one ever appears;
* `readOnlyRootFilesystem: true` with a single writable `emptyDir` at `/data`: even
  a compromised process cannot rewrite its own code.

These are properties of the artifact. A prompt that says "do not run commands" is a
wish; a pod with no token, no `kubectl` in the image and a read-only root is a fact.

## Secrets: one shared, one optional

| Secret | Mounted as | Why |
| ------ | ---------- | --- |
| `alertmanager-secrets` (already exists) | `/etc/aiops/telegram`, narrowed with `items: [telegram_bot_token]` | The bot gets no copy of its own: the token lives in one Secret and is rotated in one place. `items` keeps `slack_webhook_url` invisible to it. |
| `aiops-llm` (new, `optional: true`) | `/etc/aiops/secrets` | `DEEPSEEK_API_KEY`, created idempotently by `.\deploy.ps1 -WithAIOps` and never committed. |

Both mounts are `optional: true` on purpose: a stand with no model key must still
come up and *say so* (`/healthz` and the card both report
`no key in /etc/aiops/secrets/deepseek_api_key`). A pod that refuses to start
because a key is absent turns a degraded bot into an outage.

## The NetworkPolicy is the credential

`monitoring` has no default-deny (only `sock-shop` does), so this policy is the only
thing between the bot and the rest of the cluster. It is written as an exhaustive
allow-list:

* **in** - `app: alertmanager` on 8080 (the webhook) and `app: prometheus` on 8080
  (the scrape). That is exactly why `/alert` needs no token: the network *is* the
  credential;
* **out** - DNS (53), Prometheus (9090), Loki (3100, across namespaces) and 443 to
  the internet for `api.telegram.org` and `api.deepseek.com`. A NetworkPolicy
  cannot name a DNS host, so 443 is the narrowest thing it can express - stated
  openly instead of hidden behind a default-allow;
* **no egress rule to Alertmanager (9093)**, because in M1 the bot only *receives*
  from it. A rule for a connection nobody has ever observed would make the policy
  look stricter than it is.

`podSelector` with labels rather than `podSelector: {}`: a policy that names its pod
cannot silently widen when a second workload appears in the namespace.

## Why `Recreate`, and why `IfNotPresent`

* `strategy: Recreate` - the dedupe window and the audit trail live in an
  `emptyDir`, so a surge pod would start with an empty window and could notify
  twice for one incident. Seconds of downtime cost nothing here: Alertmanager
  retries the webhook.
* `imagePullPolicy: IfNotPresent` - the image is published nowhere and arrives via
  `k3d image import`. With `Always` the kubelet would go to Docker Hub and the pod
  would never start.

## Verifying a deployment

```powershell
kubectl -n monitoring rollout status deploy/triage-bot --timeout=90s
kubectl -n monitoring port-forward svc/triage-bot 8080:8080
curl http://localhost:8080/healthz      # adapter, model_tier_available, telegram_enabled
kubectl -n monitoring logs deploy/triage-bot --tail=40
```

A synthetic alert proves the whole path without waiting for a real incident: the
same POST `run-local.ps1` makes, but aimed at the in-cluster bot (see the component
README). The card should appear in Telegram within seconds, and `/metrics` should
show `aiops_alerts_received` plus one `aiops_incidents_tier` counter - the proof
that the alert travelled the whole path.

## What is deliberately absent

No ServiceAccount, no RBAC object, no PVC (the journal is an `emptyDir`, and losing
it costs a dedupe window, not data), no Ingress (nothing outside the cluster talks
to the bot), no HPA (one replica, no load), no PodDisruptionBudget (with a single
replica there is no useful form of one).

