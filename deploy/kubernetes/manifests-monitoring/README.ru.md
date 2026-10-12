# Мониторинг

> [English](README.md) | **Русский**

Сначала создай namespace мониторинга из файла `00-monitoring-ns.yaml`:

`$ kubectl create -f 00-monitoring-ns.yaml`


### Prometheus

Для развёртывания просто примени все манифесты prometheus (01-10) в любом порядке:

`kubectl apply $(ls *-prometheus-*.yaml | awk ' { print " -f " $1 } ')`

Сервер Prometheus публикуется на NodePort `31090`.

### Grafana

Сначала примени манифесты grafana с 20 по 22:

`kubectl apply $(ls *-grafana-*.yaml | awk ' { print " -f " $1 }'  | grep -v grafana-import)`

Когда pod grafana перейдёт в состояние Running, примени манифест
`23-grafana-import-dash-batch.yaml`, чтобы импортировать дашборды:

`kubectl apply -f 23-grafana-import-dash-batch.yaml`

Grafana публикуется на NodePort `31300`.
