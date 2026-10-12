# Установка sock-shop в Kubernetes

> [English](README.md) | **Русский**

См. [документацию](https://microservices-demo.github.io/deployment/kubernetes-minikube.html)
по развёртыванию Sock Shop через Minikube.

## Манифесты Kubernetes

Есть 2 набора манифестов для развёртывания Sock Shop в Kubernetes: каталог
[manifests](manifests/) и `complete-demo.yaml`. Файл `complete-demo.yaml` — это
единый манифест, полученный конкатенацией всех манифестов из каталога `manifests`,
поэтому при изменении файлов в `manifests` его нужно пересобрать.

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

Grafana публикуется на NodePort `31300`, Prometheus — на `31090`. Если кластер
«настоящий», проще всего дотянуться до этих портов пробросом через ssh:

```
ssh -i $KEY -L 3000:$NODE_IN_CLUSTER:31300 -L 9090:$NODE_IN_CLUSTER:31090 ubuntu@$BASTION_IP
```

куда подставляются нужные значения. Grafana и Prometheus будут доступны на
`http://localhost:3000` и `:9090`.

На Minikube можно подключаться по IP виртуалки и NodePort.
