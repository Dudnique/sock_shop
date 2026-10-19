# Манифесты AIOps triage-бота

Kubernetes-объекты triage-бота - того самого советника M1, код которого лежит в
`../../../aiops/triage-bot`. Сначала прочитайте тот README: здесь только о том,
как компонент разворачивается и почему каждая строка стоит именно так.

Namespace - `monitoring`. Бот *потребляет* алерты, поэтому живёт рядом со
стеком алертинга, а не внутри `sock-shop`, и в M1 он единственная нагрузка в
этом репозитории, которая `sock-shop` не касается вообще.

## Файлы

| Файл | Назначение |
| ---- | ---------- |
| `01-triage-bot-configmap.yaml` | Все поведенческие ручки (`MODEL_ADAPTER`, `REQUIRE_LABEL`, адреса источников, каталог журнала). Секретов здесь нет и быть не может. |
| `02-triage-bot-dep.yaml` | Deployment: одна реплика, никаких кластерных креденшелов, read-only корень, стратегия `Recreate`. |
| `03-triage-bot-svc.yaml` | ClusterIP на 8080 плюс три аннотации `prometheus.io/*`, из-за которых бот скрейпит сам себя. |
| `04-triage-bot-netpol.yaml` | Исчерпывающий allow-list на egress (DNS, Prometheus, Loki, 443) и ingress только от Alertmanager и Prometheus. |
| `examples/` | Пример, который НЕ должен попасть в кластер: `triage-bot-secret.example.yaml`. Его выручает то, что `kubectl apply -f <dir>` не рекурсивный. |

Всё это применяет `.\deploy.ps1 -WithAIOps` - он же собирает образ и импортирует
его в k3d.

## Guardrail живёт в этом YAML, а не в документе

Три строки и одно отсутствие, каждое из которых названо ещё раз в CI:

* `automountServiceAccountToken: false` и **нет** `serviceAccountName`: поду не
  выдают токен, поэтому баг в образе всё равно останется без права
  аутентифицироваться;
* в этом каталоге нет ни Role, ни ClusterRole, ни RoleBinding, ни
  ClusterRoleBinding - и джоба `aiops-tests` упадёт, если они когда-нибудь
  появятся;
* `readOnlyRootFilesystem: true` и единственный writable-том `emptyDir` в
  `/data`: даже скомпрометированный процесс не может переписать собственный код.

Это свойства артефакта. Промпт «не выполняй команды» - пожелание; под без
токена, без `kubectl` в образе и с read-only корнем - факт.

## Секреты: один общий, один опциональный

| Секрет | Куда монтируется | Зачем |
| ------ | ---------------- | ----- |
| `alertmanager-secrets` (уже существует) | `/etc/aiops/telegram`, сужен через `items: [telegram_bot_token]` | Бот не получает собственной копии: токен живёт в одном Secret и ротируется в одном месте. `items` оставляет `slack_webhook_url` невидимым для него. |
| `aiops-llm` (новый, `optional: true`) | `/etc/aiops/secrets` | `DEEPSEEK_API_KEY`; создаётся идемпотентно скриптом `.\deploy.ps1 -WithAIOps` и в git не попадает. |

Оба монтирования `optional: true` намеренно: стенд без ключа модели обязан
подняться и **сказать об этом** (`/healthz` и карточка одинаково сообщают
`no key in /etc/aiops/secrets/deepseek_api_key`). Под, который отказывается
стартовать из-за отсутствующего ключа, превращает деградировавшего бота в аварию.

## NetworkPolicy - это и есть креденшел

В `monitoring` нет default-deny (он есть только в `sock-shop`), поэтому эта
политика - единственное, что стоит между ботом и остальным кластером. Она написана
как исчерпывающий allow-list:

* **внутрь** - `app: alertmanager` на 8080 (вебхук) и `app: prometheus` на 8080
  (скрейп). Именно поэтому `/alert` не нужен токен: сеть *и есть* креденшел;
* **наружу** - DNS (53), Prometheus (9090), Loki (3100, через namespace'ы) и 443 в
  интернет для `api.telegram.org` и `api.deepseek.com`. NetworkPolicy не умеет
  называть DNS-хост, поэтому 443 - самое узкое, что она способна выразить; это
  сказано открыто, а не спрятано за default-allow;
* **правила на egress к Alertmanager (9093) нет**, потому что в M1 бот от него
  только *принимает*. Правило для соединения, которого никто никогда не наблюдал,
  делало бы политику строже, чем она есть.

`podSelector` по меткам, а не `podSelector: {}`: политика, которая называет свой
под, не может молча расшириться, когда в namespace появится вторая нагрузка.

## Почему `Recreate` и почему `IfNotPresent`

* `strategy: Recreate` - окно дедупа и audit trail живут в `emptyDir`, поэтому
  surge-под стартовал бы с пустым окном и мог уведомить дважды про один инцидент.
  Секунды простоя здесь ничего не стоят: Alertmanager повторит вебхук.
* `imagePullPolicy: IfNotPresent` - образ нигде не опубликован и приезжает через
  `k3d image import`. С `Always` kubelet пошёл бы в Docker Hub и под не стартовал
  бы никогда.

## Как проверить развёртывание

```powershell
kubectl -n monitoring rollout status deploy/triage-bot --timeout=90s
kubectl -n monitoring port-forward svc/triage-bot 8080:8080
curl http://localhost:8080/healthz      # adapter, model_tier_available, telegram_enabled
kubectl -n monitoring logs deploy/triage-bot --tail=40
```

Синтетический алерт проверяет весь путь, не дожидаясь настоящего инцидента: тот же
POST, который делает `run-local.ps1`, но нацеленный на бота в кластере (см. README
компонента). Карточка должна появиться в Telegram в течение секунд, а `/metrics` -
показать `aiops_alerts_received` и один счётчик `aiops_incidents_tier`, то есть
доказательство, что алерт прошёл весь путь.

## Чего здесь намеренно нет

Ни ServiceAccount, ни RBAC-объекта, ни PVC (журнал - это `emptyDir`, и его потеря
стоит окна дедупа, а не данных), ни Ingress (снаружи кластера с ботом никто не
разговаривает), ни HPA (одна реплика, нагрузки нет), ни PodDisruptionBudget (при
одной реплике нет его полезной формы).

