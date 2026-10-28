# Секреты в режиме GitOps: Vault и External Secrets

В `deploy.ps1 -GitOps` три Kubernetes Secret стенда больше не создаются скриптом.
Их значения лежат в **Vault**, а **External Secrets Operator** (ESO) копирует их в
кластер. В git лежит только *откуда брать каждый Secret*, но ни одного значения.

Ничего ставить на свой ПК не нужно: Vault - это под внутри кластера, а скрипт
общается с ним через `kubectl exec`.

## Три части

| Часть | Что это | Где работает |
| ----- | ------- | ------------ |
| **Vault** (HashiCorp) | хранилище секретов: значения лежат в нём, доступ по политикам, каждое чтение аутентифицировано | под в namespace `vault`, данные на томе (Raft), после каждого запуска запечатан |
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

## Хранилище, печать и корневой токен: что это значит

Три свойства отличают этот Vault от одноразового «dev-режима» (на нём стенд жил до раздела 34
в `FIXES.ru.md`):

* **Данные на диске.** Vault хранит их сам (встроенное хранилище называется Raft) на томе в
  1 Gi, поэтому перезапуск пода их не теряет. Ничего не нужно записывать заново.
* **Vault после запуска «запечатан».** Запечатан значит заперт: данные у него есть, но прочитать
  их и что-либо отдать он не может, пока его не «распечатают» ключом. Ключ создаётся один раз, при
  первом запуске, скриптом `deploy/vault-seed.ps1` и сохраняется вместе с корневым токеном в
  `deploy/vault-init.local.json` - **в git не попадает, лежит рядом с `secrets.local.ps1`; у кого
  этот файл, у того и Vault, а потеряв его, вы теряете данные**. После перезапуска (перезапустили
  Docker Desktop, перезагрузили узел) одна команда отпирает его и больше ничего не трогает:

      .\deploy\vault-seed.ps1 -UnsealOnly

  Пока он запечатан, External Secrets не может его читать: Secret, уже лежащие в кластере,
  продолжают работать (`deletionPolicy: Retain`), а через 5 минут срабатывает
  `ExternalSecretNotReady`. `vault-seed.ps1` без флага тоже распечатывает и дополнительно
  переписывает значения из `secrets.local.ps1`.
* **Публичного корневого токена нет.** Он создаётся при первом запуске и существует только в этом
  локальном файле. Скрипт передаёт его в под через stdin, а не в командной строке.

Если кластер пересоздан, том новый, а файл с ключом устарел: скрипт это замечает (Vault не
инициализирован, а файл есть), переименовывает старый файл в `vault-init.local.json.stale-<время>`
и инициализирует Vault заново. Если файл потерян, а том нет, данные открыть нельзя: скрипт говорит
об этом и что удалить, если данные не важны.

Что осталось «ноутбучным»: **один под, один ключ**. Боевой Vault - это три или пять подов (Raft
реплицирует данные между ними), ключ разделён на несколько частей у разных людей, а в AWS его
распечатывает KMS сам (auto-unseal), так что вручную не распечатывает никто. Это и есть шаг AWS;
показанный здесь механизм (хранилище, политика, роль, оператор, ротация, сохранность данных) тот же.

## Как посмотреть

    kubectl port-forward -n vault svc/vault 8200:8200      # http://localhost:8200
    # войти токеном root_token из deploy\vault-init.local.json; значения: secret/sock-shop

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
