# Docker Compose

> **English** | [Русский](README.ru.md)

See the [documentation](https://microservices-demo.github.io/deployment/docker-compose.html) on how to deploy Sock Shop using Docker Compose.

## Monitoring stack (`docker-compose.monitoring.yml`)

Grafana's admin password is **not** stored in the file: it is read from the
environment and `docker compose` refuses to start without it (the value used to
be `foobar`, hard-coded - a credential committed to git, see `FIXES.md`, section 12):

```bash
export GRAFANA_ADMIN_PASSWORD='<your password>'   # bash
$env:GRAFANA_ADMIN_PASSWORD = '<your password>'   # PowerShell

docker compose -f docker-compose.monitoring.yml up
```

Every published port binds to `127.0.0.1` on purpose: written as `9090:9090`
Docker publishes a port on **every** interface of the machine, and Prometheus,
Alertmanager, node-exporter and Grafana would be reachable from the local network
(the first three have no authentication at all). Grafana is then at
<http://127.0.0.1:3000>, user `admin`, password `$GRAFANA_ADMIN_PASSWORD`.

To import dashboards after Grafana has started:

```bash
docker compose -f docker-compose.monitoring.yml run --rm importer
```

This is the same reasoning - and the same fix - as the Kubernetes stand, where the
Service became `ClusterIP` and Grafana moved behind the Ingress
(`manifests-monitoring/30-grafana-ingress.yaml`).

