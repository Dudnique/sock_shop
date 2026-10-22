# Docker Compose

> [English](README.md) | **Русский**

См. [документацию](https://microservices-demo.github.io/deployment/docker-compose.html)
по развёртыванию Sock Shop через Docker Compose.

## Стек мониторинга (`docker-compose.monitoring.yml`)

Пароль администратора Grafana **не** хранится в файле: он читается из окружения,
и `docker compose` отказывается стартовать без него (раньше на этом месте стоял
литерал `foobar`, то есть пароль, закоммиченный в git — см. `FIXES.md`, раздел 12):

```bash
export GRAFANA_ADMIN_PASSWORD='<ваш пароль>'   # bash
$env:GRAFANA_ADMIN_PASSWORD = '<ваш пароль>'   # PowerShell

docker compose -f docker-compose.monitoring.yml up
```

Все опубликованные порты привязаны к `127.0.0.1` намеренно: записанные как
`9090:9090`, они публикуются на **всех** интерфейсах машины, и Prometheus,
Alertmanager, node-exporter и Grafana становились доступны из локальной сети
(у первых трёх аутентификации нет вообще). Grafana — на
<http://127.0.0.1:3000>, пользователь `admin`, пароль `$GRAFANA_ADMIN_PASSWORD`.

Импорт дашбордов после старта Grafana:

```bash
docker compose -f docker-compose.monitoring.yml run --rm importer
```

Ровно та же логика и тот же фикс, что и в Kubernetes-стенде, где Service стал
`ClusterIP`, а Grafana переехала за Ingress
(`manifests-monitoring/30-grafana-ingress.yaml`).

