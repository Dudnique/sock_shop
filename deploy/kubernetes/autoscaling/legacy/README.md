# Archived autoscaling stack

> **English** | [Русский](README.ru.md)

Kept for history, **not applied by anything**: `deploy.ps1` applies only the top
level of `../`, and `kubectl apply -f <dir>` does not recurse. CI skips every
`*/legacy/*` path as well (`-ignore-filename-pattern '.*/legacy/.*'` in
`.github/workflows/main.yaml`).

## What is in here

| File(s) | What it was |
|---|---|
| `heapster-deployment.yaml`, `heapster-service.yaml`, `heapster-sa.yml`, `heapster-crb.yml` | Heapster - cluster metrics, the predecessor of metrics-server. Removed from Kubernetes in 1.13; these objects cannot start on the 1.35 cluster and its ServiceAccount plus ClusterRoleBinding are exactly the kind of broad permission that is easy to forget about. |
| `influxdb-deployment.yaml`, `influxdb-service.yaml` | The time-series database Heapster wrote into (its storage backend). Replaced by Prometheus. |
| `grafana-deployment.yaml`, `grafana-service.yaml` | The Grafana of that era, wired to the InfluxDB datasource. The current Grafana lives in `../../manifests-monitoring/` with Loki/Jaeger/Prometheus datasources imported by a Job. |

## Why they were moved out of the parent directory

They were sitting next to the eight HPAs that ARE applied, in a directory that the
CI validates and that `deploy.ps1` picks up. That made the stand look like it ran
an autoscaling stack from 2018: a reader could not tell which files matter, and
`kubectl apply -f ../` becoming recursive (or a CI glob change) would have
deployed Heapster with a ClusterRoleBinding on a modern cluster.

The convention is the same as in `../../manifests-monitoring/legacy/`: unused,
non-appliable versions are kept for history, in an explicitly named folder, with a
note explaining what replaced them.

## What replaced what

| Archived | Current |
|---|---|
| Heapster (metrics) | `metrics-server` (shipped by k3s) - it is what the HPAs in `../` read through `metrics.k8s.io` |
| InfluxDB (metric store) | Prometheus (`../../manifests-monitoring/06-prometheus-dep.yaml`) |
| Grafana + InfluxDB datasource | Grafana in `../../manifests-monitoring/` with Prometheus, Loki and Jaeger datasources |
| `autoscaling/v1` HPAs | `autoscaling/v2` HPAs in `../` - see `../README.md` for what was wrong with the old ones |
