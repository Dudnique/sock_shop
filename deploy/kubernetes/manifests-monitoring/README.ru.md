# Мониторинг

> [English](README.md) | **Русский**

Сначала создай namespace мониторинга из файла `00-monitoring-ns.yaml`:

`$ kubectl create -f 00-monitoring-ns.yaml`


### Prometheus

Для развёртывания просто примени все манифесты prometheus (01-10) в любом порядке:

`kubectl apply $(ls *-prometheus-*.yaml | awk ' { print " -f " $1 } ')`

Сервер Prometheus публикуется на NodePort `31090`.

### Grafana

Сначала создай Secret `grafana-admin`: из него читают данные администратора и
Deployment, и Job импорта, так что без него поды не запустятся. Готовый шаблон —
`examples/grafana-admin-secret.example.yaml`.

`kubectl -n monitoring create secret generic grafana-admin --from-literal=admin-user=admin --from-literal=admin-password='<длинная-случайная-строка>'`

Затем примени манифесты grafana с 20 по 22:

`kubectl apply $(ls *-grafana-*.yaml | awk ' { print " -f " $1 }'  | grep -v grafana-import)`

Когда pod grafana перейдёт в состояние Running, примени манифест
`23-grafana-import-dash-batch.yaml`, чтобы импортировать дашборды:

`kubectl apply -f 23-grafana-import-dash-batch.yaml`

Сервис — `ClusterIP`, на хост Grafana **не** публикуется: как NodePort `31300` она
была доступна на всех интерфейсах машины с паролем по умолчанию `admin`/`admin`.
Теперь её отдаёт тот же вход Traefik, что и приложение, а различает их заголовок
HTTP `Host` (см. `30-grafana-ingress.yaml`) — один порт, несколько бэкендов, та же
идея, что `server_name` в nginx:

строка `127.0.0.1 grafana.localhost` в `C:\Windows\System32\drivers\etc\hosts` → `http://grafana.localhost:8080`

Нет строки в hosts (или нет прав администратора)? Используй туннель:

`kubectl port-forward -n monitoring svc/grafana 3000:80` → `http://localhost:3000`
