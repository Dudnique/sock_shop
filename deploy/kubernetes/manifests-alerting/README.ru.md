# Alerting (алертинг)

Правила алертов вычисляет сам Prometheus; **Alertmanager** берёт сработавшие
алерты и решает, *куда* их отправить. В этом каталоге - сам Alertmanager, его
конфиг маршрутизации и Service.

Образ зафиксирован: `prom/alertmanager:v0.34.1` (никакого `:latest` - стенд
воспроизводим).

## Файлы

| Файл | Назначение |
| ---- | ---------- |
| `alertmanager-configmap.yaml` | Дерево маршрутизации + receivers (Telegram, Slack). |
| `alertmanager-dep.yaml` | Deployment; монтирует конфиг и секреты. |
| `alertmanager-svc.yaml` | Service на порту 9093. |
| `examples/` | Примеры, которые `kubectl apply -f <dir>` НЕ должен подхватывать. |

## Два канала, и зачем `continue: true`

Каждый алерт приходит и в Telegram, и в Slack. Alertmanager обходит дерево
маршрутов и по умолчанию идёт **только по первому совпавшему child**, после
чего останавливается - из-за этого receiver корня был бы пропущен. Чтобы
разослать в оба, конфиг держит receiver корня плюс **два child-маршрута без
матчеров**, первый из которых несёт `continue: true`:

    route:
      receiver: 'telegram'        # fallback: только если ни один child не совпал
      routes:
        - receiver: 'telegram'
          continue: true          # продолжить к следующему sibling
        - receiver: 'slack'

## Секрет (никогда в git)

Конфиг ссылается на Kubernetes Secret **`alertmanager-secrets`** в namespace
`monitoring`. Его ключи монтируются файлами и читаются опциями `*_file`, так
что ни один креденшел не попадает ни в этот ConfigMap, ни в git:

| Ключ секрета | Читается как |
| ------------ | ------------ |
| `telegram_bot_token` | `telegram_configs.bot_token_file` |
| `slack_webhook_url`  | `slack_configs.api_url_file` |

`deploy.ps1` создаёт его идемпотентно из окружения (`TELEGRAM_BOT_TOKEN`,
`SLACK_WEBHOOK_URL`) либо из `deploy\secrets.local.ps1`.

## Правила алертов

Правила лежат рядом с Prometheus:
`../manifests-monitoring/05-prometheus-alertrules.yaml`.

## UI

Доступен без `port-forward` по **http://localhost:9093** - host-порт публикует
`terraform/k3d` через переменную `exposed_node_ports`.