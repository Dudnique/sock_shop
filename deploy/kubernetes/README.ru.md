# Установка sock-shop в Kubernetes

> [English](README.md) | **Русский**

См. [документацию](https://microservices-demo.github.io/deployment/kubernetes-minikube.html)
по развёртыванию Sock Shop через Minikube.

## Манифесты Kubernetes

Есть 2 набора манифестов для развёртывания Sock Shop в Kubernetes: каталог
[manifests](manifests/) и `complete-demo.yaml`. Файл `complete-demo.yaml` — это
единый манифест, полученный конкатенацией всех манифестов из каталога `manifests`,
поэтому при изменении файлов в `manifests` его нужно пересобрать:

```sh
awk '{print}' manifests/* > complete-demo.yaml
```

Это ровно та команда, которой пользуется и CI-проверка синхронности. Старая обёртка
Docker + `make` для этого жила в этом каталоге; она отправлена в `legacy/ci-legacy/`
(см. `legacy/ci-legacy/README.md`).

## Мониторинг

Весь мониторинг выполняет Prometheus. Все сервисы отдают эндпоинт `/metrics`.
У всех сервисов есть гистограмма Prometheus `request_duration_seconds`, из которой
автоматически получаются метрики `_count`, `_sum` и `_bucket`.

Манифесты мониторинга разложены по каталогам
[manifests-monitoring](./manifests-monitoring) и
[manifests-alerting](./manifests-alerting/).

Чтобы их применить, выполни `kubectl create -f <путь к каталогу>`.

### Что включено?

* Grafana-дашборды для Sock-shop
* Alertmanager с алертом на 500, подключённый к Slack
* Prometheus с конфигом для сбора метрик со всех pod'ов кластера, подключённый
  к локальному alertmanager.

### Порты

Prometheus публикуется на NodePort `31090`. Если кластер «настоящий», проще всего
дотянуться до него пробросом через ssh:

```
ssh -i $KEY -L 9090:$NODE_IN_CLUSTER:31090 ubuntu@$BASTION_IP
```

куда подставляются нужные значения. Prometheus будет доступен на
`http://localhost:9090`.

Grafana — это `ClusterIP`-сервис, на хост-порт она не публикуется вовсе: как
NodePort `31300` она была доступна на всех интерфейсах машины с паролем по
умолчанию `admin`/`admin`, поэтому теперь идёт через Ingress:
`http://grafana.localhost:8080` (см. `manifests-monitoring/30-grafana-ingress.yaml`).
Без строки в hosts используй `kubectl port-forward -n monitoring svc/grafana 3000:80`.

На Minikube можно подключаться по IP виртуалки и NodePort.
