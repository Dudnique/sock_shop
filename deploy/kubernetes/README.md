# Installing sock-shop on Kubernetes

> **English** | [Русский](README.ru.md)

See the [documentation](https://microservices-demo.github.io/deployment/kubernetes-minikube.html) on how to deploy Sock Shop using Minikube.

## Kubernetes manifests

There are 2 sets of manifests for deploying Sock Shop on Kubernetes: one in the [manifests directory](manifests/), and complete-demo.yaml. The complete-demo.yaml is a single file manifest
made by concatenating all the manifests from the manifests directory, so please regenerate it when changing files in the manifests directory:

```sh
awk '{print}' manifests/* > complete-demo.yaml
```

`awk` is exactly what the CI sync check uses as well. The old Docker + `make` wrapper for
this used to live in this directory; it is archived under `legacy/ci-legacy/` (see
`legacy/ci-legacy/README.md`).

## Monitoring

All monitoring is performed by prometheus. All services expose a `/metrics` endpoint. All services have a Prometheus Histogram called `request_duration_seconds`, which is automatically appended to create the metrics `_count`, `_sum` and `_bucket`.

The manifests for the monitoring are spread across the [manifests-monitoring](./manifests-monitoring) and [manifests-alerting](./manifests-alerting/) directories.

To use them, please run `kubectl create -f <path to directory>`.

### What's Included?

* Sock-shop grafana dashboards
* Alertmanager with 500 alert connected to slack
* Prometheus with config to scrape all k8s pods, connected to local alertmanager.

### Ports

Prometheus is exposed on the NodePort `31090`. If running on a real cluster, the easiest way to connect to it is by port forwarding in a ssh command:
```
ssh -i $KEY -L 9090:$NODE_IN_CLUSTER:31090 ubuntu@$BASTION_IP
```
Where all the pertinent information should be entered. Prometheus will be available on `http://localhost:9090`.

Grafana is a `ClusterIP` Service and is not published on a host port at all. As a NodePort `31300` it used to be reachable on every interface of the host with the default `admin`/`admin`, so it now goes through the Ingress instead: `http://grafana.localhost:8080` (see `manifests-monitoring/30-grafana-ingress.yaml`). Without a hosts entry use `kubectl port-forward -n monitoring svc/grafana 3000:80`.

If on Minikube, you can connect via the VM IP address and the NodePort.
