# Архив стека автоскейлинга

> [English](README.md) | **Русский**

Хранится для истории, **ничем не применяется**: `deploy.ps1` накатывает только
верхний уровень `../`, а `kubectl apply -f <dir>` не рекурсивен. CI тоже
пропускает все пути `*/legacy/*` (`-ignore-filename-pattern '.*/legacy/.*'` в
`.github/workflows/main.yaml`).

## Что здесь лежит

| Файл(ы) | Чем был |
|---|---|
| `heapster-deployment.yaml`, `heapster-service.yaml`, `heapster-sa.yml`, `heapster-crb.yml` | Heapster — метрики кластера, предшественник metrics-server. Удалён из Kubernetes в 1.13; на кластере 1.35 эти объекты не поднимутся, а его ServiceAccount и ClusterRoleBinding — ровно те широкие права, про которые легко забыть. |
| `influxdb-deployment.yaml`, `influxdb-service.yaml` | База временных рядов, в которую писал Heapster. Заменена Prometheus. |
| `grafana-deployment.yaml`, `grafana-service.yaml` | Grafana той эпохи, подключённая к InfluxDB. Актуальная Grafana живёт в `../../manifests-monitoring/`, с datasource'ами Loki/Jaeger/Prometheus, импортируемыми Job'ом. |

## Почему их вынесли из родительского каталога

Они лежали рядом с восемью HPA, которые **применяются**, в каталоге, который
проверяет CI и использует `deploy.ps1`. Из-за этого стенд выглядел так, будто в
нём работает стек автоскейлинга 2018 года: читатель не мог понять, какие файлы
важны, а рекурсивное `kubectl apply -f ../` (или изменение glob в CI) развернуло
бы Heapster с ClusterRoleBinding на современном кластере.

Конвенция та же, что в `../../manifests-monitoring/legacy/`: неиспользуемые,
неприменимые версии хранятся для истории в явно названном каталоге, с пояснением,
что их заменило.

## Что что заменило

| Архив | Актуальное |
|---|---|
| Heapster (метрики) | `metrics-server` (идёт в комплекте k3s) — именно через него HPA в `../` читают `metrics.k8s.io` |
| InfluxDB (хранилище метрик) | Prometheus (`../../manifests-monitoring/06-prometheus-dep.yaml`) |
| Grafana + datasource InfluxDB | Grafana в `../../manifests-monitoring/` с datasource'ами Prometheus, Loki и Jaeger |
| HPA на `autoscaling/v1` | HPA на `autoscaling/v2` в `../` — что было не так со старыми, описано в `../README.ru.md` |
