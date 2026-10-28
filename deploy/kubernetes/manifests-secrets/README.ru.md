# Секреты в режиме GitOps: Vault и External Secrets

В `deploy.ps1 -GitOps` три Kubernetes Secret стенда больше не создаются скриптом.
Их значения лежат в **Vault**, а **External Secrets Operator** (ESO) копирует их в
кластер. В git лежит только *откуда брать каждый Secret*, но ни одного значения.

Ничего ставить на свой ПК не нужно: Vault - это под внутри кластера, а скрипт
общается с ним через `kubectl exec`.

## Три части

| Часть | Что это | Где работает |
| ----- | ------- | ------------ |
| **Vault** (HashiCorp) | хранилище секретов: значения лежат в нём, доступ по политикам, каждое чтение аутентифицировано | под в namespace `vault`, **dev-режим** |
| **External Secrets Operator** | контроллер, который следит за объектами `ExternalSecret`, читает названные значения из Vault и создаёт обычные Kubernetes Secret | три пода в namespace `external-secrets` |
| **Объекты `ExternalSecret`** (этот каталог) | «собери Secret `grafana-admin` из `secret/sock-shop/grafana` в Vault» - рецепт, без значений | в git, синхронизируются Argo CD |

Приложения про это ничего не знают. Grafana, Alertmanager и triage-бот монтируют Secret
`grafana-admin`, `alertmanager-secrets` и `aiops-llm` ровно так же, как раньше.

## Как путешествует значение

    deploy/secrets.local.ps1                  (ваша машина, не в git)
            |
            |  deploy/vault-seed.ps1          пишет через stdin, не аргументами
            v
    Vault   secret/sock-shop/{grafana,alerting,aiops}
            ^
            |  каждую минуту: вход с токеном ServiceAccount самого пода (ниже),
            |  чтение значений
    External Secrets Operator   <--  объекты ExternalSecret (git, через Argo CD)
            |
            v
    Kubernetes Secret  grafana-admin / alertmanager-secrets / aiops-llm   (monitoring)
            |
            v
    Grafana, Job импорта, Alertmanager, triage-бот

Ротация значения - одна команда без передеплоя: поменять его в Vault (или поправить
`secrets.local.ps1` и запустить `deploy\vault-seed.ps1`), и в течение пары минут Secret
обновится, файл в поде догонит, а сайдкар Alertmanager перечитает конфиг
(`FIXES.ru.md`, раздел 31).

## Как ESO доказывает, кто он: токен нигде не хранится

Обычное слабое место хранилища секретов - учётные данные для входа в него. Здесь хранить
нечего:

1. у пода ESO есть **токен ServiceAccount** Kubernetes (его выдаёт и обновляет кластер);
2. он предъявляет этот токен **Kubernetes-методу аутентификации** Vault;
3. Vault спрашивает у API Kubernetes «токен настоящий и чей он?» (TokenReview) и сверяет
   ответ с **ролью**: пользоваться ей может только ServiceAccount `external-secrets` из
   namespace `external-secrets`;
4. Vault отвечает короткоживущим токеном Vault с одной **политикой**: только чтение
   `secret/sock-shop/*`.

Политику и роль создаёт `deploy/vault-seed.ps1`. Поэтому ESO читает эти три пути и больше
ничего, а писать не может вовсе.

## Dev-режим: что это значит, честно

Vault здесь запущен с `server.dev.enabled=true`: **один под, уже разблокированный, данные
в памяти, фиксированный корневой токен (`root`)**.

* Всё в нём **теряется при перезапуске пода** (перезапустили Docker Desktop, перезагрузили
  узел). Secret, которые уже есть в кластере, остаются - у объектов `ExternalSecret`
  `deletionPolicy: Retain`, так что работающий стенд продолжает работать; но их никто не
  обновляет, а новый под ждал бы их. Запустите `.\deploy\vault-seed.ps1` снова, и всё
  вернётся (он идемпотентен и переиспользует пароль Grafana, уже лежащий в Vault).
* Корневой токен публичный (он в Helm-значениях в git). Это допустимо только потому, что
  Vault одноразовый и доступен лишь изнутри кластера.
* Настоящий Vault устроен иначе: отказоустойчивое хранилище Raft на постоянных томах,
  авто-разблокировка через облачный KMS, аудит-лог и никакого статического корневого токена.
  Эта установка показывает **механизм** (хранилище, политика, роль, оператор, ротация), а
  не защищённое развёртывание.

## Как посмотреть

    kubectl port-forward -n vault svc/vault 8200:8200      # http://localhost:8200
    # войти токеном: root; значения лежат в secret/sock-shop

    kubectl -n monitoring get externalsecret               # STATUS SecretSynced, READY True
    kubectl get clustersecretstore vault                   # READY True

`ExternalSecret` со статусом `SecretSyncedError` - почти всегда одно из двух: Vault
перезапускали и он пуст (запустите скрипт наполнения), либо роли ещё нет (то же лекарство).

## Файлы

| Файл | Что говорит |
| ---- | ----------- |
| `00-cluster-secret-store.yaml` | где Vault и как в него входить (Kubernetes-аутентификация, роль `external-secrets`) |
| `10-grafana-admin.yaml` | `grafana-admin` <- `secret/sock-shop/grafana` |
| `11-alertmanager-secrets.yaml` | `alertmanager-secrets` <- `secret/sock-shop/alerting` |
| `12-aiops-llm.yaml` | `aiops-llm` <- `secret/sock-shop/aiops` (ключ необязателен: пустой значит «без модели») |
