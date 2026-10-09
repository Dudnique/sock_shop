# legacy/

Old ("before") versions of manifests, kept for comparison and history.
Files here are **not** applied: `kubectl apply -f deploy/kubernetes/manifests-monitoring/`
is not recursive, so it ignores this sub-directory.

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
