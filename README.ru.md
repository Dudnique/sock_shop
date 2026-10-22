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

Скрипт — тонкий оркестратор, он делает **восемь** шагов плюс два необязательных:

1. поднимает кластер k3d через **Terraform** (`terraform/k3d`) — единственный
   источник правды про инфраструктуру;
2. применяет манифесты приложения (`deploy/kubernetes/manifests`);
3. применяет **сетевые политики** (`deploy/kubernetes/manifests-policy`): настоящий
   default-deny и на вход, и на выход, плюс по одному разрешению на сервис. До этого
   шага каталог проверялся в CI и не применялся никем, а его «default deny» был
   аннотацией namespace из версий до 1.7, которую современные кластеры игнорируют —
   то есть любой под мог достучаться до любого, включая все базы;
4. применяет **автоскейлеры** (`deploy/kubernetes/autoscaling`, `autoscaling/v2`) и
   затем ждёт, пока хотя бы один HPA реально начнёт отдавать метрики CPU. Объекты
   существуют в любом случае, но без metrics-server (идёт в комплекте k3s) и
   `requests.cpu` они никогда не сработают — ровно в таком состоянии репозиторий и был;
5. применяет стек логирования (`deploy/kubernetes/manifests-logging` — Loki + Promtail);
6. применяет стек трейсинга (`deploy/kubernetes/manifests-jaeger`);
7. применяет стек мониторинга (`deploy/kubernetes/manifests-monitoring`), а затем делает
   то, чего не умеет обычный `kubectl apply`: ждёт, пока ConfigMap с правилами алертов
   действительно обновится **внутри работающего пода**, проверяет смонтированный файл
   правил через `promtool check rules` и только после этого вызывает `POST /-/reload`.
   Prometheus перечитывает правила только при старте, а битый файл правил для него
   **не фатален** — он пишет ошибку в лог и продолжает работать **без этих алертов**.
   Тихая дыра в мониторинге хуже упавшего деплоя, поэтому невалидный файл прерывает скрипт;
8. применяет стек алертинга (`deploy/kubernetes/manifests-alerting`) и его секреты;
9. *(необязательно, `-WithLoadtest`)* применяет генератор нагрузки
   (`deploy/kubernetes/manifests-loadtest`);
10. *(необязательно, `-WithAIOps`)* разворачивает **AIOps-триаж-бот** (`aiops/triage-bot`):
    собирает образ `triage-bot:0.1.0-m1`, импортирует его в ноду k3d (`k3d image import` — образ
    нигде не опубликован, и именно поэтому в Deployment стоит
    `imagePullPolicy: IfNotPresent`), создаёт опциональный Secret `aiops-llm`, если доступен
    `DEEPSEEK_API_KEY`, применяет `deploy/kubernetes/manifests-aiops` и ждёт окончания rollout.
    Бот разбирает алерты с меткой `aiops="true"`, которые ему шлёт Alertmanager, и отвечает
    карточкой в Telegram; у него нет ни токена сервис-аккаунта, ни RBAC, ни `kubectl`, а в M1 он
    ничего не исполняет (`FIXES.md` §16).

После этого магазин откроется по адресу **http://localhost:8080**.

Поднять стенд сразу с трафиком — генератор нагрузки и есть то, что наполняет дашборды,
правила алертов и трейсы, поэтому для демо это рекомендуемый вариант:

```powershell
.\deploy.ps1 -WithLoadtest
```

Поставить AIOps-бота на тот же стенд (ему нужен стек алертинга, а ключ модели он берёт из того же
`deploy\secrets.local.ps1`):

```powershell
.\deploy.ps1 -WithAIOps
```


Снести стенд целиком:

```powershell
.\deploy.ps1 -Down
```

### Секреты (никогда не коммитятся)

Стеку алертинга нужны два креденшела — токен Telegram-бота и URL Slack
incoming-webhook. Они лежат в `deploy\secrets.local.ps1` — git-игнорируемом файле,
который `deploy.ps1` подхватывает на каждом прогоне. Создай его один раз и один раз
заполни: больше ни один шаг не попросит эти значения:

```powershell
Copy-Item deploy\secrets.local.ps1.example deploy\secrets.local.ps1
# затем открой deploy\secrets.local.ps1 и впиши реальные значения
```

Переменные окружения `TELEGRAM_BOT_TOKEN` и `SLACK_WEBHOOK_URL` тоже работают — это
путь для CI, — но задавать их руками здесь ничего не требует.

Если файла нет вовсе — это тоже не ошибка: когда стенд уже поднят, `deploy.ps1` читает
оба значения обратно из кластерного Secret `alertmanager-secrets` (kubectl отдаёт
значения Secret в base64) и применяет его заново. Так или иначе, ничего никуда не
вставляется руками и ничего не вводится в командной строке.

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

# 3. Сетевые политики - default-deny (и на вход, и на выход) плюс по одному
#    разрешению на сервис. Доказательство, что они работают:
#      .\deploy\kubernetes\manifests-policy\verify-netpol.ps1
kubectl apply -f deploy\kubernetes\manifests-policy

# 4. Автоскейлеры (HPA v2). Нужны metrics-server (идёт в комплекте k3s) и requests.cpu;
#    в колонке TARGETS должен быть процент — `<unknown>` означает, что автоскейлер мёртв:
kubectl apply -f deploy\kubernetes\autoscaling
kubectl -n sock-shop get hpa

# 5. Логирование — Loki (хранилище) + Promtail (сборщик)
kubectl apply -f deploy\kubernetes\manifests-logging

# 6. Трейсинг — Jaeger
kubectl apply -f deploy\kubernetes\manifests-jaeger

# 7. Мониторинг
kubectl apply -f deploy\kubernetes\manifests-monitoring

# 7b. Prometheus перечитывает файлы правил только при старте, а битый файл правил
#     не фатален: он пишет ошибку и продолжает работать БЕЗ этих алертов. Поэтому
#     сначала валидируем смонтированный файл, потом перечитываем без рестарта
#     (рестарт уничтожил бы TSDB, а он на emptyDir):
kubectl -n monitoring exec deploy/prometheus-deployment -- `
    promtool check rules /etc/prometheus-rules/alert.rules
kubectl -n monitoring exec deploy/prometheus-deployment -- `
    wget -q -O- --post-data='' http://localhost:9090/-/reload
#     .\deploy.ps1 здесь ещё и ждёт, пока файл ConfigMap реально обновится внутри пода
#     (kubelet делает это асинхронно, до ~2 минут) — иначе reload тихо перечитает СТАРЫЙ файл.

# 8. Алертинг — сначала секрет, потом манифесты.
#    Значения читаются из того же файла, что использует deploy.ps1, поэтому в
#    командную строку ничего вписывать не надо (там оно осталось бы в history):
. .\deploy\secrets.local.ps1
kubectl -n monitoring create secret generic alertmanager-secrets `
    --from-literal=telegram_bot_token=$env:TELEGRAM_BOT_TOKEN `
    --from-literal=slack_webhook_url=$env:SLACK_WEBHOOK_URL `
    --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f deploy\kubernetes\manifests-alerting

# 9. (необязательно) генератор нагрузки — без трафика дашборды пустые,
#    а правилам алертов не на чем срабатывать
kubectl apply -f deploy\kubernetes\manifests-loadtest

# 10. (необязательно) AIOps-триаж-бот. Образ собирается здесь и не опубликован
#     нигде, поэтому `imagePullPolicy: IfNotPresent` в Deployment — не пожелание:
#     с `Always` kubelet пошёл бы за ним в Docker Hub и под не стартовал бы.
docker build -t triage-bot:0.1.0-m1 aiops\triage-bot
k3d image import triage-bot:0.1.0-m1 -c sock-shop
#     Ключ модели опционален (`optional: true` на монтировании): без него каскад
#     откатывается на таблицу правил, а /healthz говорит, какого файла ключа нет.
. .\deploy\secrets.local.ps1
kubectl -n monitoring create secret generic aiops-llm `
    --from-literal=deepseek_api_key=$env:DEEPSEEK_API_KEY `
    --dry-run=client -o yaml | kubectl apply -f -
#     Никогда `examples\`: `kubectl apply -f <dir>` не рекурсивный, и именно это
#     не даёт каталогу `manifests-aiops\examples\` попасть в кластер.
kubectl apply -f deploy\kubernetes\manifests-aiops
kubectl -n monitoring rollout status deploy/triage-bot --timeout=90s
kubectl -n monitoring port-forward svc/triage-bot 8080:8080
#     затем: curl http://localhost:8080/healthz
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

**Как войти.** Логин `admin` (или `GRAFANA_ADMIN_USER`), а пароль всегда можно
прочитать из Secret — тем же приёмом, что использует `deploy.ps1`; он же печатает
его в итоговой сводке:

```powershell
kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' |
  ForEach-Object { [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($_)) }
```

Анонимный вход выключен **намеренно** (`GF_AUTH_ANONYMOUS_ENABLED=false` в
`21-grafana-dep.yaml`): раньше Grafana была `NodePort` без аутентификации с парой
`admin`/`admin`, доступная на всех интерфейсах хоста. Если на локальном стенде
хочется открывать UI без логина — поставь эту переменную в `true`, а
`GF_AUTH_ANONYMOUS_ORG_ROLE` в `Viewer`, тогда анонимные посетители останутся
только для чтения, а правка дашбордов по-прежнему требует входа администратора.

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
├── aiops/
│   └── triage-bot/                         # AIOps-советник M1: код, 94 теста, run-local.ps1
├── deploy/
│   └── kubernetes/
│       ├── manifests/                      # приложение, пофайлово (00 ... 29)
│       ├── manifests-logging/              # Loki + Promtail (логи подов)
│       ├── manifests-jaeger/               # Jaeger all-in-one (трейсы)
│       ├── manifests-monitoring/           # Prometheus, Grafana, экспортёры
│       ├── manifests-alerting/             # Alertmanager (Telegram + Slack)
│       ├── manifests-loadtest/             # опциональный генератор нагрузки (locust)
│       ├── manifests-policy/               # сетевые политики: default-deny + разрешения по сервисам
│       ├── manifests-aiops/                # AIOps-триаж-бот (M1): ConfigMap, Deployment, Service, NetworkPolicy
│       ├── autoscaling/                    # HPA v2 для stateless-сервисов (+ legacy/)
│       ├── complete-demo.yaml              # приложение одним файлом
│       └── helm-chart/                     # альтернатива: Helm-чарт
└── terraform/
    └── k3d/                                # декларативное описание кластера
```

## Остановить / удалить

```powershell
.\deploy.ps1 -Down             # снести стенд целиком (terraform destroy)
```

Или просто приостановить стенд:

```powershell
.\deploy\stand.ps1 -Stop -Free   # остановить ноды по порядку; -Free сначала освобождает логи/TSDB
.\deploy\stand.ps1 -Status       # движок, ВМ, контейнеры, ноды - ничего не меняет
.\deploy\stand.ps1 -Start        # запустить по одному узлу, дождавшись API и каждого Ready
```

Не надо делать `docker stop k3d-sock-shop-*` и ждать, что `docker start` вернёт стенд. k3d создаёт
эти контейнеры с политикой `unless-stopped`, поэтому перезапуск Docker поднимает все четыре сразу
(измерено: load average 355 и 68 МБ свободных на ВМ 7,5 ГБ), а контейнер, потерявший сетевой endpoint
при перезапуске ВМ, заставляет k3s выйти с кодом 0 — нода выглядит «стартовала и исчезла»,
балансировщик не может её зарезолвить, и `kubectl` уходит в таймаут при живом сервере. `stand.ps1`
закрывает оба случая: ставит политику `no`, поднимает сервер первым, агентов по одному, балансировщик
последним, а если нода всё равно не возвращается — пересобирает кластер через `deploy.ps1`
(FIXES.ru.md, раздел 22).

`k3d cluster delete sock-shop` тоже существует, но поддерживаемый путь удаления — `.\deploy.ps1 -Down`,
потому что состояние хранит terraform.

Если стенд не возвращается — `kubectl` уходит в таймаут, `docker ps` висит, машина тормозит — не надо
чинить это на месте. `deploy\diagnose-stand.ps1` различает три отказа, которые выглядят одинаково как
«не подключается»: зависший движок Docker, трэшинг ВМ WSL или больной кластер. Скрипт read-only и для
найденного случая печатает следующую команду.

Зависшая ВМ убивается через `wsl --shutdown` (`docker stop` в этом состоянии тоже висит). Кластер,
который не восстанавливается, заменяется: `.\deploy.ps1 -Down`, затем `.\deploy.ps1 -WithAIOps`.

То, что стенд накапливает за время работы, — отдельная проблема, и пересборки она не требует.
Prometheus и Loki держат данные в `emptyDir`, а бот читает только последние 15 минут логов, поэтому
растёт то, что никто не просил: чанки логов, блоки TSDB, слои образов в containerd и завершённые
поды. Единственное, что переживает перезапуск, — журнал триаж-бота на PVC `triage-bot-journal`
(`local-path`): это аудит-след, поэтому он сохраняется. Из-за того, что `local-path` привязывает том к
создавшей его ноде, под бота живёт на этой ноде всё время жизни кластера, а пересобранный кластер
начинается с нового, пустого журнала.

```powershell
.\deploy\cleanup-stand.ps1                  # ~30 с: завершённые поды, неиспользуемые образы, данные Loki/Prometheus
.\deploy\cleanup-stand.ps1 -KeepPrometheus  # сохранить историю метрик
.\deploy\cleanup-stand.ps1 -KeepLogs        # сохранить чанки логов
.\deploy\cleanup-stand.ps1 -DryRun          # показать, что будет удалено
```

Скрипт печатает занятое место и число образов на каждой ноде до и после, так что рост измеряется, а не
угадывается; приложение, Secret'ы и бота он не трогает.

Это мера только для стенда, а не продовый паттерн: на реальном кластере ответ — лимиты ресурсов,
настоящие тома, retention и алерты на запас памяти. Разбор и таблица соответствий — в `FIXES.md`,
раздел 17.

## Лицензия

См. [LICENSE](./LICENSE). Проект распространяется под лицензией Apache 2.0.
