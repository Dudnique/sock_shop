# legacy/

> **English** | [Русский](README.ru.md)

Old ("before") versions of manifests, kept for comparison and history.
Files here are **not** applied: `kubectl apply -f deploy/kubernetes/manifests-monitoring/`
is not recursive, so it ignores this sub-directory.

## 04-prometheus-configmap.yaml

The original Prometheus scrape config. Two things kept the local stand broken:

- the `kubernetes-services` / `kubernetes-ingresses` jobs pointed `blackbox-exporter`
  at `blackbox-exporter.example.com:9115` - a host that does not exist in the cluster,
  so every blackbox probe failed;
- the `kubernetes-ingresses` job built the probe target from the ingress host, and a
  local ingress has no host, producing an empty (useless) target.

Superseded by `../04-prometheus-configmap.yaml`, where the exporter is addressed by its
in-cluster name (`blackbox-exporter.monitoring.svc.cluster.local:9115`) and the ingress
probe targets the Traefik entrypoint directly
(`traefik.kube-system.svc.cluster.local`) with `metrics_path: /probe`.

## 06-prometheus-dep.yaml

The original Prometheus Deployment ran the old image `prom/prometheus:v2.26.0`.
Superseded by `../06-prometheus-dep.yaml`, which pins `prom/prometheus:v2.45.6` and adds
`--web.enable-lifecycle` (so the config can be reloaded without a restart).

## 23-grafana-import-dash-batch.yaml

The original Grafana import Job. It tried to wait for Grafana using the
annotation `pod.beta.kubernetes.io/init-containers` instead of a real
`initContainers` field. Modern Kubernetes ignores that annotation (the
init-container feature graduated to a real field and the annotation was removed
in 1.20), so the wait silently did nothing. On a fresh install the importer
raced Grafana and failed with `curl: (7) ... Connection refused`, leaving
Grafana with no datasource and no dashboards.

Superseded by `../23-grafana-import-dash-batch.yaml`, which uses a proper
`initContainers` entry (`wait-for-grafana`) plus `backoffLimit`.

A **second, independent defect** lived on in the "fixed" version of this Job, which is why
that copy is kept here too: its script was a **folded YAML block (`>`)**. Folded blocks
join the lines with spaces, so `fi` + `done` became the single word `fidone` and the
leading `#` comment swallowed the rest of the script - while the Job still finished as
`Completed`. The live file now uses a literal block (`|`) and asserts the **outcome**
(`/api/search?type=dash-db`, `/api/datasources`), failing the Job when nothing was
imported. If you copy anything from this directory, do not copy a folded shell script.

A note on credentials: the archived copy used to carry the default credential inside the
URLs (`http://admin:admin@grafana/...`). It is masked to `admin:<password>` here - an
archive is kept for structure, not for credentials, and a literal password in the tree is
exactly what section 12 of `FIXES.md` is about. The live file takes both the user and the
password from the `grafana-admin` Secret.
