# Argo CD (GitOps mode)

`.\deploy.ps1 -GitOps` lets Argo CD deploy the stand from this repository instead
of the script applying manifests one directory at a time. Nothing else changes:
the plain path and `-Helm` work exactly as before, and the parity gate
(`../helm-chart/check-parity.py`) still says the chart and `manifests/` describe
the same application.

## What the script does, and what Argo CD does

| Who | What |
| --- | ---- |
| `deploy.ps1` | the k3d cluster (Terraform); the three Secrets that are not in git (`grafana-admin`, `alertmanager-secrets`, `aiops-llm`); with `-WithAIOps` the bot image is built and imported into the node; installs Argo CD and applies **one** root Application |
| Argo CD | everything else: it reads this repository and makes the cluster match it |

    deploy.ps1 -GitOps
      |  helm install argo-cd (values.yaml)      pinned: chart 10.11.6 = Argo CD v3.5.6
      |  kubectl apply root-app.yaml
      v
    sock-shop-root  --renders-->  apps/ (a Helm chart, one Application per entry of values.yaml)
      |
      +- wave 0   sock-shop (Helm chart)  +  network-policies
      +- wave 1   autoscaling  +  logging  +  tracing
      +- wave 2   monitoring  +  alerting
      +- wave 3   aiops, loadtest   (only with -WithAIOps / -WithLoadtest)

The list is data (`apps/values.yaml`), not code: adding a component is one entry.

## Files

| File | Purpose |
| ---- | ------- |
| `values.yaml` | values for the `argo-cd` chart: dex, notifications and the ApplicationSet controller are off, HTTP behind Traefik, memory limits for a laptop |
| `root-app.yaml` | the one Application applied by hand; `deploy.ps1` fills in the revision and the two optional toggles |
| `apps/` | the app-of-apps chart: `values.yaml` lists the Applications, `templates/applications.yaml` renders them |
| `check-apps.py` | CI gate: `deploy.ps1` applies a directory with no Application (or the reverse) -> red |

## Design decisions

* **Secrets stay outside git and outside Argo CD.** Argo CD deletes only what it
  tracks, so `prune: true` cannot touch a Secret it never created. Pods that mount
  a Secret created a moment later simply retry (`retry` in each Application).
* **`ignoreDifferences` on `/spec/replicas` for `sock-shop`.** The HPAs own the
  replica count; without this `selfHeal` would scale every Deployment back in a
  fight with the autoscaler (the same conflict Helm hit, `FIXES.md` section 25).
  Consequence worth knowing: scaling a Deployment by hand is *not* undone by Argo
  CD - change an image, an env var or a label to see self-heal work.
* **Server-side apply.** Some ConfigMaps (Grafana dashboards, Prometheus rules)
  are close to the 256 KiB limit of the `last-applied` annotation that client-side
  apply writes.
* **Annotation resource tracking.** The chart sets `app.kubernetes.io/instance`
  itself; label tracking would make Argo CD and Helm fight over one label.
* **The import Job is a Sync hook** (`23-grafana-import-dash-batch.yaml`): a Job
  spec is immutable, so Argo CD recreates it on each sync, which is what
  `deploy.ps1` does by hand with `kubectl delete job`.

## Using it

    .\deploy.ps1 -GitOps                              # reads main
    .\deploy.ps1 -GitOps -GitRevision my-branch       # try a pushed branch on a live stand
    .\deploy.ps1 -GitOps -WithAIOps -WithLoadtest

The UI is `http://argocd.localhost:8080` (login `admin`, the password is printed
by the script and lives in `argocd-initial-admin-secret`); without a hosts entry:

    kubectl port-forward -n argocd svc/argocd-server 8090:80   # http://localhost:8090

A change is a commit: push it, and Argo CD applies it within about three minutes
(its default polling interval). To see self-heal, change something Argo CD owns:

    kubectl -n sock-shop set image deploy/payment payment=weaveworksdemos/payment:0.4.2
    kubectl -n argocd get application sock-shop      # OutOfSync, then Synced again

## Known limits

* **Prometheus and Alertmanager do not reload their config by themselves.** After
  a change to a rule or to the Alertmanager config Argo CD updates the ConfigMap,
  but the process keeps the old file until `POST /-/reload`
  (`deploy.ps1` does this itself on the plain path). A config-reload sidecar is
  the fix and is not part of this change; see `TODO.md`.
* A first sync takes about fifteen minutes: the JVM services start their
  readiness checks at 360-420 s.
* Argo CD adds about 0.8 GB to a stand that already uses ~4.6 GB of an 8 GB VM.
