# Sock Shop — рабочий стенд на k3d

> [English](README.md) | **Русский**

Готовый к запуску форк демо-приложения **Sock Shop**
(upstream: [microservices-demo/microservices-demo](https://github.com/microservices-demo/microservices-demo)).

В репозитории лежат **все файлы, необходимые для поднятия магазина**:
декларативное [Terraform](./terraform/k3d/)-описание локального кластера
[k3d](https://k3d.io), Kubernetes-манифесты всех сервисов и баз данных, Ingress,
стек мониторинга и алертинга, а также скрипт развёртывания в одну команду.
Сами Docker-образы публичные и автоматически скачиваются из Docker Hub, поэтому
в репозиторий их добавлять не нужно.

> Что именно было исправлено относительно upstream — см. [FIXES.ru.md](./FIXES.ru.md).

---

## Что нужно

- Windows (или Linux/macOS) с установленными:
  - [Terraform](https://developer.hashicorp.com/terraform/downloads) >= 1.5
  - [Docker](https://docs.docker.com/get-docker/) (запущен)
  - [kubectl](https://kubernetes.io/docs/tasks/tools/)

CLI `k3d` удобен для инспекции кластера, но не обязателен: провайдер Terraform
общается с Docker напрямую.

Проверить:

```powershell
terraform version
docker version
kubectl version --client
```

## Быстрый старт (одна команда)

```powershell
.\deploy.ps1
```

Скрипт — тонкий оркестратор, он делает **шесть** шагов плюс необязательный седьмой:

1. поднимает кластер k3d через **Terraform** (`terraform/k3d`) — единственный
   источник правды про инфраструктуру;
2. применяет манифесты приложения (`deploy/kubernetes/manifests`);
3. применяет стек логирования (`deploy/kubernetes/manifests-logging` — Loki + Promtail);
4. применяет стек трейсинга (`deploy/kubernetes/manifests-jaeger`);
5. применяет стек мониторинга (`deploy/kubernetes/manifests-monitoring`), а затем делает
   то, чего не умеет обычный `kubectl apply`: ждёт, пока ConfigMap с правилами алертов
   действительно обновится **внутри работающего пода**, проверяет смонтированный файл
   правил через `promtool check rules` и только после этого вызывает `POST /-/reload`.
   Prometheus перечитывает правила только при старте, а битый файл правил для него
   **не фатален** — он пишет ошибку в лог и продолжает работать **без этих алертов**.
   Тихая дыра в мониторинге хуже упавшего деплоя, поэтому невалидный файл прерывает скрипт;
6. применяет стек алертинга (`deploy/kubernetes/manifests-alerting`) и его секреты;
7. *(необязательно, `-WithLoadtest`)* применяет генератор нагрузки
   (`deploy/kubernetes/manifests-loadtest`).

После этого магазин откроется по адресу **http://localhost:8080**.

Поднять стенд сразу с трафиком — генератор нагрузки и есть то, что наполняет дашборды,
правила алертов и трейсы, поэтому для демо это рекомендуемый вариант:

```powershell
.\deploy.ps1 -WithLoadtest
```


Снести стенд целиком:

```powershell
.\deploy.ps1 -Down
```

### Секреты (никогда не коммитятся)

Стеку алертинга нужны два креденшела — токен Telegram-бота и URL Slack
incoming-webhook. В git они не попадают; задай их либо переменными окружения,
либо в отдельном git-игнорируемом файле, который `deploy.ps1` подхватывает
автоматически:

```powershell
# вариант A: переменные окружения
$env:TELEGRAM_BOT_TOKEN = '...'
$env:SLACK_WEBHOOK_URL  = 'https://hooks.slack.com/services/...'

# вариант B: локальный git-игнорируемый файл
Copy-Item deploy\secrets.local.ps1.example deploy\secrets.local.ps1
# затем открой deploy\secrets.local.ps1 и впиши реальные значения
```

## Ручное развёртывание (без скрипта)

```powershell
# 1. Кластер + kubeconfig (Terraform — единственный источник правды про инфру)
cd terraform\k3d
terraform init
terraform apply
$env:KUBECONFIG = (terraform output -raw kubeconfig_path)
cd ..\..

# 2. Приложение
kubectl apply -f deploy\kubernetes\manifests
kubectl wait --for=condition=available --timeout=300s deployment --all -n sock-shop

# 3. Логирование — Loki (хранилище) + Promtail (сборщик)
kubectl apply -f deploy\kubernetes\manifests-logging

# 4. Трейсинг — Jaeger
kubectl apply -f deploy\kubernetes\manifests-jaeger

# 5. Мониторинг
kubectl apply -f deploy\kubernetes\manifests-monitoring

# 5b. Prometheus перечитывает файлы правил только при старте, а битый файл правил
#     не фатален: он пишет ошибку и продолжает работать БЕЗ этих алертов. Поэтому
#     сначала валидируем смонтированный файл, потом перечитываем без рестарта
#     (рестарт уничтожил бы TSDB, а он на emptyDir):
kubectl -n monitoring exec deploy/prometheus-deployment -- `
    promtool check rules /etc/prometheus-rules/alert.rules
kubectl -n monitoring exec deploy/prometheus-deployment -- `
    wget -q -O- --post-data='' http://localhost:9090/-/reload
#     .\deploy.ps1 здесь ещё и ждёт, пока файл ConfigMap реально обновится внутри пода
#     (kubelet делает это асинхронно, до ~2 минут) — иначе reload тихо перечитает СТАРЫЙ файл.

# 6. Алертинг — сначала секрет, потом манифесты
kubectl -n monitoring create secret generic alertmanager-secrets `
    --from-literal=telegram_bot_token=$env:TELEGRAM_BOT_TOKEN `
    --from-literal=slack_webhook_url=$env:SLACK_WEBHOOK_URL `
    --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f deploy\kubernetes\manifests-alerting

# 7. (необязательно) генератор нагрузки — без трафика дашборды пустые,
#    а правилам алертов не на чем срабатывать
kubectl apply -f deploy\kubernetes\manifests-loadtest
```

Приложение можно применить и из одного файла:
`kubectl apply -f deploy/kubernetes/complete-demo.yaml`.

## Доступ к приложению и инструментам

Всё, кроме Grafana, доступно с хоста **без** `kubectl port-forward`: хост-порты
публикует `terraform/k3d`. Grafana отдаётся через Traefik на собственном имени
хоста (см. ниже).

| Порт  | URL | Что |
|-------|-----|-----|
| 8080  | <http://localhost:8080>  | Sock Shop (через Traefik/Ingress) |
| 8080  | <http://grafana.localhost:8080> | Grafana (тот же Traefik, свой заголовок `Host`) |
| 9090  | <http://localhost:9090>  | Prometheus |
| 9093  | <http://localhost:9093>  | Alertmanager |
| 16686 | <http://localhost:16686> | Jaeger UI (трейсы) |

Точка входа и для логов, и для трейсов — **Grafana**: в ней уже есть datasource
`loki` (логи) и datasource `jaeger` (трейсы) — они описаны в
`manifests-monitoring/20-grafana-configmap.yaml` и импортируются Job'ом
`grafana-import-dashboards`. У Loki **нет** ни UI, ни порта на хосте — намеренно:
это внутреннее хранилище, к которому обращается только Grafana
(`http://loki.logging.svc.cluster.local:3100`). Логи смотрят в **Explore** с
datasource `loki`, например запросом `{namespace="sock-shop"}`.

Сервис Grafana — `ClusterIP`, и на хост она больше **не** публикуется: как
`NodePort` она была доступна на **всех** интерфейсах машины, да ещё и с паролем по
умолчанию `admin`/`admin`. Теперь её отдаёт тот же вход Traefik, что и приложение,
а различает их заголовок HTTP `Host` — один порт, несколько бэкендов, та же идея,
что `server_name` в nginx:

```powershell
# один раз, в редакторе, запущенном от администратора, добавить строку в
#   C:\Windows\System32\drivers\etc\hosts
#   127.0.0.1 grafana.localhost
# затем открыть http://grafana.localhost:8080
# нет строки в hosts (или нет прав администратора)? используй туннель:
kubectl port-forward -n monitoring svc/grafana 3000:80   # http://localhost:3000
```

Пароля администратора в репозитории тоже нет: `deploy.ps1` создаёт Secret
`grafana-admin` — переиспользуя уже существующий, потому что Grafana хранит пароль
в своей базе — и из него читают данные и Deployment, и Job импорта. Чтобы задать
свой пароль, выставь `GRAFANA_ADMIN_PASSWORD`; иначе случайный будет сгенерирован и
напечатан в конце развёртывания.

### Мониторинг и алертинг одним взглядом

| Что | Где | Детали |
|---|---|---|
| Метрики | Prometheus <http://localhost:9090> | цели приложения находятся автоматически по аннотациям подов/Service; плюс `node-exporter` (3 ноды) и `kube-state-metrics` |
| Дашборды | Grafana <http://grafana.localhost:8080> | 7 дашбордов и 3 datasource импортирует Job `grafana-import-dashboards` |
| Алерты | правила Prometheus + Alertmanager <http://localhost:9093> | 11 правил в 4 группах — `availability`, `application`, `infrastructure`, `meta`; доставляются в Telegram и Slack |
| Пробы | `blackbox-exporter` | два маршрута с меткой `probe_scope`: `ingress` (Service Traefik) и `external` (опубликованный порт, `host.docker.internal:8080`) |
| Логи | Loki (только через Grafana **Explore**) | ни UI, ни хост-порта — намеренно |

Отдельно стоит объяснить группу `meta`: `ScrapeTargetDown` срабатывает, когда перестаёт
отвечать **сама цель сбора метрик** — правило наблюдает за мониторингом, а не за
приложением. Как только оно появилось, сразу нашлись три цели, которые в конфиге были
постоянно `DOWN` (неверный порт экспортёра, сервис, отдающий только Actuator JSON, и
неверный путь метрик у Alertmanager) — см. [FIXES.ru.md](./FIXES.ru.md), раздел 11.

Сервис front-end — `ClusterIP`: приложение доступно **только** через Ingress
(Traefik) на порту `8080`. Раньше, до появления ingress, это был `NodePort` на
`30001` (см. `FIXES.ru.md`). Для отладки по-прежнему можно сделать port-forward:

```powershell
kubectl port-forward -n sock-shop svc/front-end 8081:80
# затем http://localhost:8081
```

## Структура

```
.
├── deploy.ps1                              # развёртывание в одну команду (Terraform + kubectl)
├── FIXES.md                                # что исправлено и почему (EN)
├── FIXES.ru.md                             # что исправлено и почему (RU)
├── deploy/
│   └── kubernetes/
│       ├── manifests/                      # приложение, пофайлово (00 ... 29)
│       ├── manifests-logging/              # Loki + Promtail (логи подов)
│       ├── manifests-jaeger/               # Jaeger all-in-one (трейсы)
│       ├── manifests-monitoring/           # Prometheus, Grafana, экспортёры
│       ├── manifests-alerting/             # Alertmanager (Telegram + Slack)
│       ├── manifests-loadtest/             # опциональный генератор нагрузки (locust)
│       ├── complete-demo.yaml              # приложение одним файлом
│       └── helm-chart/                     # альтернатива: Helm-чарт
└── terraform/
    └── k3d/                                # декларативное описание кластера
```

## Остановить / удалить

```powershell
.\deploy.ps1 -Down             # снести стенд целиком (terraform destroy)
```

Или просто приостановить кластер:

```powershell
k3d cluster stop sock-shop     # остановить
k3d cluster start sock-shop    # запустить снова
k3d cluster delete sock-shop   # удалить полностью
```

## Лицензия

См. [LICENSE](./LICENSE). Проект распространяется под лицензией Apache 2.0.
