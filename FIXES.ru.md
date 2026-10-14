# Что починено в этом форке

> [English](FIXES.md) | **Русский**

Рабочий форк демо-приложения **Sock Shop**
(upstream: https://github.com/microservices-demo/microservices-demo),
подготовленный для запуска на локальном кластере **k3d** (k3s) на Windows/WSL2.

Ниже — отличия от upstream и их причина.

---

## 1. Регистрация пользователя падала с `Internal Server Error (500)`

### Симптом
`POST /register` через веб-интерфейс возвращал **500**, хотя сервис `user`
пользователя создавал.

### Настоящая причина (НЕ в `user`/`user-db`)
Сервисы `user` и `user-db` были полностью здоровы:

- `GET /health` у `user` отвечал `user-db: OK`;
- прямой `POST /register` в `svc/user` возвращал `200` + id покупателя.

Ошибку порождал **front-end** на этапе слияния корзины сразу после регистрации.
В кластере крутился старый образ `weaveworksdemos/front-end:0.3.1`, в котором
`api/endpoints.js` указывал на несуществующий хост `cart` (единственное число):

```js
cartsUrl: "http://cart/carts"   // BUG: сервис называется "carts"
```

а Service в кластере называется `carts` (множественное число). В логах front-end:

```
Posting Customer: {...}
{ id: '6ac649390cc5ef00019d4254' }          <- user создал покупателя OK
Merging carts for customer id: 6ac649390cc5ef00019d4254 ...
Error with log in: Error: getaddrinfo EAI_AGAIN cart:80
POST /register 500
```

### Исправление
Использовать образ, соответствующий манифестам, — `weaveworksdemos/front-end:0.3.12`,
в котором `endpoints.js` содержит корректный адрес `http://carts/carts`.

```diff
- image: weaveworksdemos/front-end:0.3.1
+ image: weaveworksdemos/front-end:0.3.12
```

Применено в `deploy/kubernetes/manifests/09-front-end-dep.yaml` и
`deploy/kubernetes/complete-demo.yaml`.

После раскатки `POST /register` возвращает `200`, в логах — `Carts merged.`.

> Почему баг вообще возникал: в кластер ранее был задеплоен образ `0.3.1`
> **вручную**, а манифесты из этого репозитория не применялись. При развёртывании
> строго через манифесты проблема не возникает — здесь фикс зафиксирован явно,
> чтобы версия не «уехала» снова.

---

## 2. Добавлен Ingress для front-end

В upstream Ingress есть только внутри helm-чарта. Для k3d добавлен отдельный
манифест `deploy/kubernetes/manifests/29-front-end-ingress.yaml`, чтобы приложение
было доступно с хоста по `http://localhost:<port>` без `kubectl port-forward` и
без NodePort.

---

## 3. Устаревшие node-селекторы обновлены

Во всех манифестах устаревший (deprecated) лейбл ноды `beta.kubernetes.io/os`
заменён на стабильный `kubernetes.io/os`, чтобы манифесты разворачивались и на
новых кластерах (k3s, GKE, EKS), где бета-лейбл может отсутствовать.

---

## 4. Упрощён CI

Тяжёлый job `deployments-tests` поднимал `kind`-кластер с k8s `v1.20.0` (2020 г.)
и ждал готовности всех подов; на современных раннерах он не укладывался в
`timeout-minutes: 10` и стабильно отменялся.

Теперь в CI:

- `complete-demo-sync-check` — та же проверка, но напрямую через `awk | diff`,
  без сборки Docker-образа;
- `manifests-validate` — быстрая оффлайн-валидация всех манифестов
  (`kubeconform -strict`), кластер не нужен.

Оригинальный workflow сохранён для истории в `ci-legacy/main.yaml`.

Docker + `make`-хелпер, который генерировал и проверял `complete-demo.yaml` (корневой
`Makefile`, `deploy/kubernetes/Makefile` и `deploy/kubernetes/Dockerfile`), по той же
причине отправлен в `ci-legacy/`: генерация — это одна строка, а проверку CI выполняет
сам, так что `make` (и специально собранный под него образ) были чистым оверхедом.
Команда пересборки описана в `ci-legacy/README.md` и в `deploy/kubernetes/README.md`.

---

## 5. Добавлен стек алертинга (Alertmanager: Telegram + Slack)

Форк добавляет четвёртый шаг развёртывания — **Alertmanager**
(`manifests-alerting/`), который рассылает каждый сработавший/погашенный алерт
сразу в **два канала** (Telegram и Slack). В upstream этого нет.

Решения, которые стоит зафиксировать:

- образ **зафиксирован** (`prom/alertmanager:v0.34.1`) — воспроизводимый стенд, без `:latest`;
- креденшелы лежат в Kubernetes Secret **`alertmanager-secrets`** (namespace
  `monitoring`) с ключами `telegram_bot_token` и `slack_webhook_url`; они монтируются
  файлами и читаются опциями `*_file`, поэтому токен не попадает ни в ConfigMap, ни в git;
- `deploy.ps1` создаёт секрет **идемпотентно** (рендерит клиентски через
  `--dry-run=client -o yaml`, затем `kubectl apply -f -`), потому что обычный
  `kubectl create` падает на втором запуске;
- чтобы достать **оба** канала, дерево маршрутизации держит receiver корня плюс
  **два child-маршрута без матчеров**, первый из которых несёт `continue: true` —
  иначе Alertmanager идёт только по первому совпавшему child и receiver корня пропускается.

---

## 6. Инструментальные грабли при сборке стенда

Зафиксировано потому, что они съели реальное время и легко повторяются:

1. **У k3d нет in-place update.** Изменение описания кластера не применяется к
   работающему кластеру; его нужно пересоздавать (`terraform apply -replace` или
   destroy + apply). Считай кластер неизменяемым.
2. **PowerShell 5.1 и не-ASCII скрипты.** `.ps1`, сохранённый в UTF-8 **без BOM**,
   PS 5.1 декодирует системной ANSI-кодировкой, из-за чего кириллица портится.
   Поэтому `deploy.ps1` держится **строго ASCII**.
3. **`$PSScriptRoot` ненадёжен в `param()`-default.** Он вычисляется в теле скрипта,
   где всегда установлен.
4. **`-chdir=$var` не разворачивается** для нативных команд в Windows PowerShell,
   поэтому скрипт оборачивает каждый вызов Terraform в `Push-Location`/`Pop-Location`.
5. **`kubectl apply -f <dir>` не рекурсивный и подхватывает `*.example.yaml`.**
   Примеры-манифесты вынесены в подкаталог `examples/`, который `apply -f <dir>` игнорирует.
6. **Манифесты без `namespace:`** улетали в `default` и не делали ничего полезного;
   `namespace: monitoring` добавлен явно в каждый манифест мониторинга/алертинга.
7. **Сюрприз роутинга Alertmanager.** Когда совпадает child-маршрут, receiver корня
   игнорируется — отсюда два маршрута без матчеров с `continue: true` (см. раздел 5).
8. **`kubectl apply` не удаляет поля.** Из манифеста DaemonSet'а Promtail убрали
   `serviceAccountName: promtail`, а в кластере поле **осталось**: `apply` — это
   трёхсторонний merge, поэтому ключ, которого **нет** в новом манифесте, сохраняет
   живое значение. Чтобы удаление сработало, объект пришлось удалить и создать заново
   (`kubectl delete -f ...`, затем `apply`) — см. раздел 8.

---

## 7. Сервис `front-end`: убран `NodePort` в пользу Ingress

В upstream сервис `front-end` был `type: NodePort` с жёстко прописанным
`nodePort: 30001`. Это был единственный способ попасть внутрь **до появления
ingress**: он привязывает стенд к одному фиксированному порту и требует
доступности IP ноды (а не `localhost`). Как только Traefik Ingress (раздел 2)
маршрутизирует на `service/front-end:80`, NodePort становится чистым легаси —
он дублирует ingress-путь и добавляет вторую, неуправляемую точку входа.

Теперь сервис — обычный `ClusterIP` (строки `type` / `nodePort` убраны, как и у
всех остальных сервисов в `manifests/`), поэтому приложение доступно **только**
через Traefik на `http://localhost:8080`.

Изменено в `deploy/kubernetes/manifests/10-front-end-svc.yaml`,
`deploy/kubernetes/complete-demo.yaml` и
`deploy/kubernetes/helm-chart/templates/front-end-svc.yaml`.

---

## 8. Добавлен стек логирования (Loki + Promtail)

В стенде были метрики и алерты, но **не было логов**: логи подов жили только на ноде
и умирали вместе с контейнером. Форк добавляет четвёртый каталог манифестов —
`deploy/kubernetes/manifests-logging/`, и он собран из двух частей:

- **Loki** — хранилище. Одна реплика, файловое хранилище, без порта на хосте и без
  UI: это внутренний сервис, к которому обращается только Grafana.
- **Promtail** — сборщик. DaemonSet (по одному поду на ноду), потому что файлы логов
  лежат на диске каждой ноды, и под с ноды A файлы ноды B не видит.

Логи читают в **Explore** в Grafana с datasource `loki`, например запросом
`{namespace="sock-shop"}` — своего UI у Loki намеренно нет.

### Симптом
Loki был поднят и доступен из Grafana, но любой запрос возвращал пусто.
`GET /loki/api/v1/label/namespace/values` отвечал одной пустой строкой, т.е. «данных
нет», и — самое сбивающее — **никто не жаловался**: лог самого Promtail был чистым,
ошибок push нет, предупреждений про RBAC нет.

### Настоящая причина (ни Loki, ни Grafana)
Promtail был настроен «официально» — через `kubernetes_sd_configs`. Такая
конфигурация спрашивает Kubernetes API, какие поды есть, и выводит из ответов
цели для чтения логов; в первой версии под это даже были заведены ServiceAccount и
ClusterRole. Внутри одноконтейнерного DaemonSet'а результат discovery оказался
**пустым** — молча: Promtail ничего не читал, ничего не отправлял, а Loki оставался
пустым, выглядя полностью здоровым.

### Исправление
Promtail больше вообще не общается с Kubernetes API — он просто читает файлы:

- **статичный file-glob вместо service discovery**: `__path__:
  /var/log/pods/*/*/*.log` в обычной секции `static_configs`;
- каталог ноды `/var/log` примонтирован read-only (hostPath), поэтому CRI-файлы логов
  (`/var/log/pods/<ns>_<pod>_<uid>/<container>/<n>.log`) видны внутри пода;
- метки, по которым фильтруют в Grafana, вырезаются **из пути** стадией `regex` по
  `filename` (`namespace`, `pod_name`, `pod_uid`, `container_name`) и превращаются в
  лейблы стадией `labels`. `pod_uid` намеренно **не** становится лейблом: он меняется
  при каждом рестарте, а в Loki каждая новая комбинация лейблов — это новый stream;
- стадия `cri: {}` разбирает формат строк containerd (`<timestamp> stdout F <сообщение>`),
  поэтому время и поток stdout/stderr настоящие, а не часть текста сообщения;
- `positions.yaml` лежит на ноде (`hostPath: /run/promtail`), поэтому рестарт Promtail
  не пересылает файлы целиком и не дублирует строки в Loki;
- **RBAC вырезан полностью**: ServiceAccount, ClusterRole и ClusterRoleBinding убраны.
  Сборщику, читающему файлы, не нужны права на уровне кластера, а вместе с ними
  исчезает и то, что может молча вернуть пустой результат;
- `tolerations: [{operator: Exists}]`: на server-ноде k3d висит taint, и без
  tolerate логи этой ноды (kube-system и т.п.) не собирались бы вовсе;
- `prometheus.io/scrape: "true"` + `prometheus.io/port: "3101"` — метрики самого
  Promtail скребутся без правки конфига Prometheus;
- легаси-манифесты Elasticsearch/Fluentd/Kibana перенесены в
  `manifests-logging/legacy/`: сохранены для истории и игнорируются `kubectl apply -f <dir>`.

### Проверка
```
GET /loki/api/v1/label/namespace/values  -> ["kube-system","logging","monitoring","sock-shop", ...]
GET /loki/api/v1/query_range?query={namespace="sock-shop"}
```
возвращает реальные строки приложения с лейблами `container_name`, `job`, `namespace`,
`pod_name`, `stream` — те же данные видны в Grafana Explore.

### Грабли, которые стоит запомнить
Первая попытка была просто патчем работающего DaemonSet'а, и старый
`serviceAccountName: promtail` **выжил**: `kubectl apply` — трёхсторонний merge, поэтому
поле, отсутствующее в новом манифесте, сохраняет живое значение. Объект пришлось удалить
и создать заново (раздел 6, пункт 8).

---

## 9. Добавлен стек трейсинга (Jaeger) — та самая «Phase 2» из README

В таблице портов `16686` уже был зарезервирован под Jaeger UI («появится в Phase 2»),
и Terraform уже публиковал `host:16686 -> node:30686`. Форк доводит это до конца:
`deploy/kubernetes/manifests-jaeger/` разворачивает Jaeger, а инструментированные
сервисы на него нацелены.

- `00-jaeger-ns.yaml`, `01-jaeger-dep.yaml`, `02-jaeger-svc.yaml`;
- образ зафиксирован: `jaegertracing/all-in-one:1.57.0`; `SPAN_STORAGE_TYPE=memory` —
  демо-стенду не нужен PVC, трейсы намеренно теряются при рестарте;
- `strategy: Recreate`: единственный инстанс с in-memory хранилищем нельзя запускать
  дважды одновременно;
- `COLLECTOR_ZIPKIN_HOST_PORT=:9411`: сервисы sock-shop говорят на **Zipkin**, а не на
  родном протоколе Jaeger, поэтому Jaeger обязан поднять Zipkin-совместимый коллектор;
- переменная `ZIPKIN` выставлена трём сервисам, которые инструментированы в upstream
  (`catalogue`, `payment`, `user`):
  `http://zipkin.jaeger.svc.cluster.local:9411/api/v1/spans`
  (`manifests/05-catalogue-dep.yaml`, `15-payment-dep.yaml`, `25-user-dep.yaml` и
  `complete-demo.yaml`);
- Service с именем ровно **`zipkin`** в namespace `jaeger` существует только ради этого
  DNS-имени — пока хостнейм не резолвится, сервис не может отправить спаны;
- `jaeger-query` — **NodePort** (`30686`), чтобы UI был доступен с хоста, а
  `jaeger-collector` остаётся `ClusterIP`: спаны шлют только клиенты внутри кластера.

Почему `NodePort`, а не `type: LoadBalancer`: k3s-овский `servicelb` поднял бы ещё один
DaemonSet `svclb-*`, который занимает **hostPort 80 на каждой ноде** — тот же порт, что
уже держит `svclb-traefik`, — и сломал бы `http://localhost:8080`; при этом `16686` он
всё равно не опубликовал бы.

Проверка: после того как магазин поработает под нагрузкой, `http://localhost:16686` и
`GET /api/services` показывают `["jaeger-all-in-one","payment","catalogue","user"]`.
В Grafana появляется datasource `jaeger`, так что трейсы можно искать и оттуда.

---

## 10. Grafana: datasources Loki/Jaeger и JSON дашборда, который молча не импортировался

- в `manifests-monitoring/20-grafana-configmap.yaml` добавились два файла для Job'а
  импорта: `loki-datasource.json` (`"type": "loki"`,
  `"url": "http://loki.logging.svc.cluster.local:3100"`, `"access": "proxy"`) и
  `jaeger-datasource.json` (`"type": "jaeger"`,
  `"url": "http://jaeger-query.jaeger.svc.cluster.local"`). Скрипт Job'а уже перебирает
  `*-datasource.json` и `*-dashboard.json`, так что структурно Job менять не пришлось.
  `access: proxy` здесь принципиален: Grafana ходит в оба хранилища изнутри кластера,
  поэтому браузеру вообще не нужно резолвить имя `*.svc.cluster.local`.
- цикл импорта datasource'ов теперь **идемпотентный**: он завершается
  `|| echo "(datasource already exists - kept as is)"`. На втором развёртывании API
  отвечает `409 Conflict` («data source with the same name already exists») по одному
  разу на каждый datasource — это ожидаемое состояние, а не сбой, и в логе теперь так и
  написано вместо голой ошибки curl.
- **найден и исправлен пре-существующий баг**: в `prometheus-stats-dashboard.json`
  строка `process_start_time_seconds{name="prometheus"}` содержала **неэкранированные**
  двойные кавычки внутри JSON-строки, вложенной в ConfigMap, поэтому файл не был валидным
  JSON, а Grafana отвечала `400`. Цикл импорта это проглотил: каждая итерация
  заканчивается `echo ""`, поэтому контейнер всегда выходил с кодом `0`, а Job
  отчитывался `Complete`. После экранирования кавычек импортируются все **6** дашбордов и
  **3** datasource'а, а в каждом дашборде есть `"overwrite": true` — повторный импорт
  обновляет дашборд, а не создаёт дубль.
- спецификация Job'а неизменяема, поэтому `deploy.ps1` теперь **удаляет** завершённый Job
  `grafana-import-dashboards` **до** применения `manifests-monitoring`: повторное
  развёртывание заново импортирует актуальный configmap, а не падает на immutable-поле.
- `deploy.ps1` вырос с четырёх шагов до **шести** (кластер, приложение, логирование,
  трейсинг, мониторинг, алертинг), а README получил соответствующие ручные команды, так
  что `.\deploy.ps1` с нуля воспроизводит весь стенд — вместе с логами и трейсами.

---

## 11. Мониторинг: 11 правил алертов, promtool-гейт и три «мёртвых» таргета

В `HEAD` файл `manifests-monitoring/05-prometheus-alertrules.yaml` содержал **одну**
группу и **одно** правило (`HighErrorRate`). Теперь там **11 правил в 4 группах** —
и это не «добавим правил для красоты», а закрытие конкретных слепых зон стенда.

| Группа | Правила | Что ловит |
|---|---|---|
| `availability` | `ProbeFailed`, `ProbeSlow` | витрина недоступна / отвечает дольше 2 с |
| `application` | `HighErrorRate`, `DeploymentReplicasUnavailable`, `PodCrashLooping`, `PodRestartFlapping` | 5xx, 0 доступных реплик, CrashLoopBackOff, «петля рестартов» |
| `infrastructure` | `NodeNotReady`, `NodeDiskPressure`, `NodeHighCPU`, `NodeHighMemory` | нода выпала, давление по диску/CPU/RAM |
| `meta` | `ScrapeTargetDown` | **сам мониторинг**: цель перестала отвечать |

- **Почему плохой файл правил так опасен.** Prometheus относится к битому rule-файлу
  **не фатально**: пишет ошибку в лог и продолжает работать **без этих алертов**.
  Это худший сценарий: тишина выглядит как «всё хорошо». Поэтому в CI появился шаг
  `promtool check rules`: файл правил извлекается из ConfigMap (`yq`) и проверяется
  синтаксически. `kubeconform` этого не увидит принципиально — в rule-файле нет
  `apiVersion/kind`, он не Kubernetes-объект.
- **Правила не перечитываются сами.** kubelet обновляет файл в томе ConfigMap, но
  Prometheus читает его при старте. Доказано на живом стенде: файл в поде уже содержал
  все 11 правил, а `GET /api/v1/rules` показывал **одно** старое. Решение — не рестарт
  (он убил бы TSDB на emptyDir и потерял бы историю метрик), а `POST /-/reload`.
  `deploy.ps1` после применения манифестов мониторинга сам дожидается обновления файла,
  делает reload и показывает, что правила реально загрузились.
- **Правило `ScrapeTargetDown` немедленно нашло три «мёртвых» таргета**, которые жили
  в конфиге молча годами:

  | Таргет | Ошибка | Причина | Фикс |
  |---|---|---|---|
  | `rabbitmq:9090` | `connection refused` | sidecar `kbudde/rabbitmq-exporter` слушает **9419** (`PUBLISH_PORT`), а Service/аннотация обещали 9090 | задали `PUBLISH_PORT: "9090"` |
  | `queue-master:80` | `expected a valid start token, got "{"` | у сервиса на `/metrics` Spring Boot **Actuator JSON**, а Prometheus-эндпоинта (`/actuator/prometheus`) нет вообще (404) | убрали `prometheus.io/scrape` (иначе таргет вечно DOWN) |
  | `alertmanager:9093/alertmanager/metrics` | `HTTP 404` | неверный `prometheus.io/path`; Alertmanager отдаёт метрики на `/metrics` | путь исправлен |

- **Probe витрины: теперь два маршрута, а не один.** Раньше blackbox проверял
  `http://traefik.kube-system.svc.cluster.local/` — это **внутрикластерный** путь.
  Он ловит упавшее приложение (Ingress без `host` отправляет всё на `front-end`,
  проверено: ответ `200`, `X-Powered-By: Express`, 8688 байт HTML), но не поймает сломанный
  **опубликованный порт** (маппинг k3d / упавший контейнер балансировщика) — тот слой,
  в который пользователь упирается первым. Добавлен job `blackbox-external` с целью
  `http://host.docker.internal:8080/` — из пода это ровно тот путь, которым идёт браузер
  с хоста (проверено: `200 OK`). Оба probe помечены `probe_scope` (`ingress` / `external`),
  поэтому в алерте сразу видно, какой уровень лёг. Замечание: `host.docker.internal` —
  удобство Docker Desktop; на чистом Linux-хосте имя заменяется на адрес шлюза хоста.
- Проверено, что **все 11 правил опираются на существующие метрики**, а не на выдуманные:
  `request_duration_seconds_count` (10 серий, метка `status_code` реальная),
  `kube_deployment_status_replicas_available` (14 деплойментов `sock-shop`),
  `probe_success`, метрики node-exporter. Правило, у которого нет метрики, — такой же
  silent gap, только наоборот: оно «стоит в списке» и никогда не сработает.
- Итог по цифрам: `promtool check rules` → `SUCCESS: 11 rules found`; в живом Prometheus
  4 группы / 11 правил; таргетов в состоянии `down` — **0**; probe-маршрутов — **2**.

---

## 12. Доступ к Grafana: без порта на хосте и без `admin/admin` — Secret, Ingress и Job, который врал

Три проблемы вокруг Grafana на самом деле одна: удобство тихо подменило собой контроль
безопасности. Все три закрыты, и каждый фикс проверен на живом стенде.

- **`NodePort` на всех интерфейсах хоста.** `22-grafana-svc.yaml` был `type: NodePort`
  с `nodePort: 31300`, а Terraform мапил его на хост-порт 3000, то есть Grafana слушала
  **все** интерфейсы машины (включая офисный Wi-Fi) с паролем по умолчанию
  `admin`/`admin`. Теперь это обычный `ClusterIP`, который отдаёт тот же вход Traefik,
  что и приложение, а различает их заголовок HTTP `Host` (`30-grafana-ingress.yaml`) —
  один порт, несколько бэкендов, та же идея, что `server_name` в nginx. Адрес:
  `http://grafana.localhost:8080` (одна строка в hosts-файле) либо, если прав
  администратора нет, `kubectl port-forward -n monitoring svc/grafana 3000:80`.
  Соответствующая запись убрана из `terraform/k3d/variables.tf`. С уже ставшим
  `ClusterIP` Service'ом оставшийся маппинг ничего не отдавал
  (`curl: (52) Empty reply from server` — прокси Docker'а принимал TCP-соединение, а
  стоящего за ним NodePort'а уже не было).
  Само удаление маппинга требует явного пересоздания, и это стоит знать до любой правки
  публикуемых портов: провайдер k3d **не умеет менять существующий кластер**
  (`Error: Updating clusters is not supported by k3d`), а так как тот же `apply` сначала
  пересоздаёт `local_sensitive_file.kubeconfig` (destroy → create), неудачный apply
  оставляет машину ещё и **без файла kubeconfig**. Отсюда
  `terraform apply -replace=k3d_cluster.sock_shop`; проверено после пересоздания:
  в `docker ps` строки `3000->31300` больше нет, хост-порт 3000 закрыт.
- **`GF_SECURITY_ADMIN_PASSWORD: admin` в манифесте.** Теперь и Deployment, и Job
  импорта читают `GF_SECURITY_ADMIN_USER` / `GF_SECURITY_ADMIN_PASSWORD` из Secret
  `grafana-admin` (`secretKeyRef`, `optional: false`), а Job ходит через
  `curl --user "$GRAFANA_USER:$GRAFANA_PASSWORD"`, то есть пароля нет и в списке
  аргументов процесса. `deploy.ps1` создаёт Secret идемпотентно: **переиспользует**
  существующий — Grafana хранит пароль администратора в своей базе, так что регенерация
  Secret на каждом запуске молча запирала бы нас в уже работающем инстансе — а если его
  нет, генерирует случайный; `GRAFANA_ADMIN_PASSWORD` перекрывает оба варианта. Шаблон:
  `manifests-monitoring/examples/grafana-admin-secret.example.yaml`. Доказано с нуля:
  удаляем Secret → под остаётся `0/1` с
  `GF_SECURITY_ADMIN_USER: <set to the key 'admin-user' in secret 'grafana-admin'>`,
  `Optional: false`; создаём заново → rollout завершается, вход через Ingress работает
  с новым паролем, а `admin`/`admin` даёт `401`.
- **Job импорта рапортовал об успехе, ничего не импортировав.** Два независимых дефекта:
  1. его shell-скрипт был **folded YAML-блоком (`>`)**. Folded-блок склеивает строки
     пробелами, поэтому `fi` + `done` превращались в одно слово `fidone`, а `#`-комментарий
     съедал остаток скрипта — и без проверки результата Job всё равно завершался
     `Completed`. Теперь это literal-блок (`|`), и в манифесте стоит комментарий, почему
     так и должно остаться;
  2. init-контейнер `wait-for-grafana` ждал `/api/health`, а его отдаёт и **предыдущий**
     под — тот, что ещё живёт со старым паролем. При повторном развёртывании импортёр
     аутентифицировался новым паролем в старый под и получал `401` на каждый файл
     (наблюдалось вживую: `importing ... 401`, Job `Completed`).
     Теперь он дожидается, что креденшелы из Secret реально принимаются
     (`curl -f --user ... /api/datasources`, до ~4 минут), а после циклов импорта
     **проверяет результат**, а не коды возврата: спрашивает у Grafana число дашбордов и
     датасорсов и валит Job, если ответ ноль. Цикл датасорсов ещё и отделяет
     `409 Conflict` («already exists in grafana - kept as is») от настоящей ошибки, поэтому
     здоровое повторное развёртывание больше не печатает голые `curl: (22)`.

  Доказательство на живом стенде: повторное развёртывание пишет
  `grafana now reports 7 dashboards and 3 datasources` + `import finished`, Job
  `Complete 1/1` за ~8 секунд; на **чистой** базе Grafana создаются все три датасорса, и
  импорт всё равно заканчивается 7 дашбордами;
  `curl -H 'Host: grafana.localhost' http://localhost:8080/api/health` →
  `{"commit":"b5190ee547","database":"ok","version":"7.5.5"}`; `GET /api/datasources` →
  `200` для пароля из Secret, `401` для `admin`/`admin`, `401` без аутентификации.

  Вывод, который стоит запомнить: одноразовому Job'у нужна **проверка результата**, иначе
  «зелёный» означает лишь «shell вышел с кодом 0». Здесь фикс 401-бага и фикс
  «тихого успеха» оказались одной и той же правкой.

Два остатка того же класса жили вне Kubernetes-пути, и они закрыты в том же проходе:
«в файлах нет креденшелов» — это свойство репозитория, а не одного каталога.

- `deploy/docker-compose/docker-compose.monitoring.yml` держал литерал
  `GF_SECURITY_ADMIN_PASSWORD=foobar`, а `deploy/docker-compose/grafana/import.sh` ходил на
  `http://admin:foobar@grafana:3000/...` — тот же самый паттерн «пароль по умолчанию», но
  во втором (compose) входе стенда. Теперь пароль берётся из
  `${GRAFANA_ADMIN_PASSWORD:?...}` (compose падает с внятным сообщением, если переменная не
  задана, вместо старта с общеизвестным паролем), а `import.sh` читает
  `GRAFANA_USER` / `GRAFANA_PASSWORD` из окружения. Все публикуемые порты в том файле
  привязаны к `127.0.0.1`: записанные как `3000:3000`, они публикуются на **всех**
  интерфейсах хоста — ровно как тот NodePort, что убран здесь.
- архивная копия `manifests-monitoring/legacy/23-grafana-import-dash-batch.yaml` всё ещё
  несла в скрипте `http://admin:admin@grafana/...`. Теперь это `admin:<password>`: архив
  хранится ради структуры, а не ради паролей, и в legacy-README это сказано на двух
  языках.

И наконец, фикс защищён в CI. В джобе `manifests-validate` появился шаг **`Guard Grafana
access`**, который падает на: (1) литеральном креденшеле на активной строке применямого
манифеста, (2) `GF_SECURITY_ADMIN_*`, за которым нет `valueFrom`, (3) Grafana-Service'е,
снова ставшем `NodePort`, или Ingress'е, который пропал, (4) folded YAML-блоке (`>`) в
Job'е импорта. Ни одна из этих проверок не является ошибкой схемы — `kubeconform` не
видит ни одну из них, а все четыре были реальными дефектами в этом разделе.

Проход закончился ещё одним дефектом — в оркестраторе самого стенда, и его стоит записать,
потому что он проявляется лишь в определённом сочетании. `deploy.ps1` дважды делает «пробу»
того, чего **может законно не быть**: существующего Secret `grafana-admin` и нового файла
правил внутри работающего контейнера Prometheus. Обе пробы были написаны очевидным
образом — `kubectl ... 2>$null`, а потом взгляд на `$LASTEXITCODE`. В Windows PowerShell 5.1
этого недостаточно: при `$ErrorActionPreference = "Stop"` (строка 45 скрипта) один байт в
stderr native-команды становится **терминирующей** ошибкой, и редирект этому не мешает.
Проверено изолированно на 5.1: `kubectl get secret <отсутствующий> -o jsonpath=... 2>$null`
выбрасывает `NativeCommandError`. На чистом кластере следствие было такое: прогон намертво
вставал в разделе 5/6 с `Error from server (NotFound): secrets "grafana-admin" not found` —
то есть ровно на пути «первый запуск на новом кластере», тогда как каждый повторный прогон
оставался зелёным: к тому моменту Secret уже есть. Теперь обе пробы идут через небольшой
хелпер `Invoke-QuietProbe`: на время вызова он опускает `$ErrorActionPreference` до
`Continue`, вливает stderr в конвейер и выбрасывает error-записи, а код возврата отдаёт как
данные — то есть «Secret'а ещё нет» становится ответом, а не падением. Проверено: проба
возвращает `exit=1, output=''` для отсутствующего Secret и не падает, а полный прогон на
чистом кластере теперь доходит до 6/6.

