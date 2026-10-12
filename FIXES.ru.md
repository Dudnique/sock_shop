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
