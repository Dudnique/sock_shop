# Alerting (алертинг)

Правила алертов вычисляет сам Prometheus; **Alertmanager** берёт сработавшие
алерты и решает, *куда* их отправить. В этом каталоге - сам Alertmanager, его
конфиг маршрутизации и Service.

Образ зафиксирован: `prom/alertmanager:v0.34.1` (никакого `:latest` - стенд
воспроизводим).

## Файлы

| Файл | Назначение |
| ---- | ---------- |
| `alertmanager-configmap.yaml` | Дерево маршрутизации + receivers (Telegram, Slack, вебхук triage-бота). |
| `alertmanager-dep.yaml` | Deployment; монтирует конфиг и секреты. |
| `alertmanager-svc.yaml` | Service на порту 9093. |
| `examples/` | Примеры, которые `kubectl apply -f <dir>` НЕ должен подхватывать. |

## Три receiver'а, и зачем `continue: true`

Одни и те же сработавшие алерты видят Telegram, Slack и triage-бот
(`aiops-triage`). Alertmanager обходит дерево маршрутов и по умолчанию идёт
**только по первому совпавшему child**, после чего останавливается - поэтому
`continue: true` стоит на каждом маршруте, который нельзя пропустить:

    route:
      receiver: 'telegram'        # fallback: только если ни один child не совпал
      routes:
        - receiver: 'aiops-triage'
          matchers: ['aiops="true"']
          continue: true          # два человеческих канала ниже всё равно сработают
        - receiver: 'telegram'
          continue: true          # продолжить к следующему sibling
        - receiver: 'slack'

Маршрут бота закрыт матчером, поэтому до него доходят только правила, явно
согласившиеся через `aiops: "true"`; у человеческих маршрутов матчера нет, и они
совпадают со всем. Эта асимметрия и есть суть: автоматизация - это opt-in на
уровне правила, а уведомление - нет.

## Маршрут triage-бота

`aiops-triage` - обычный `webhook_configs`, указывающий на
`http://triage-bot.monitoring.svc.cluster.local:8080/alert` (разворачивается из
`../manifests-aiops`). Две детали сделаны намеренно:

* **Без аутентификации.** Endpoint - это ClusterIP, до которого дотягивается
  только pod из namespace `monitoring`, и именно это обеспечивает
  `../manifests-aiops/04-triage-bot-netpol.yaml`: сеть здесь и есть креденшел.
  Общий токен добавил бы второй секрет для защиты порта, недостижимого ниоткуда
  ещё.
* **`send_resolved: false`.** Бот разбирает *сработавшие* инциденты; resolved-вебхук
  получает `200` и записывается как `skipped: resolved`, так что отправлять их -
  значит просто раздувать `aiops_alerts_skipped{reason="resolved"}`. Про
  разрешение инцидента люди всё равно узнают из receiver'а `telegram`.

Если бот лежит, вебхук Alertmanager'а просто фейлится, и больше ничего не
меняется: человеческие маршруты - независимые sibling'и. Бот, оказавшийся
единственным нотификатором, был бы хуже, чем отсутствие бота.

## Секрет (никогда в git)

Конфиг ссылается на Kubernetes Secret **`alertmanager-secrets`** в namespace
`monitoring`. Его ключи монтируются файлами и читаются опциями `*_file`, так
что ни один креденшел не попадает ни в этот ConfigMap, ни в git:

| Ключ секрета | Читается как |
| ------------ | ------------ |
| `telegram_bot_token` | `telegram_configs.bot_token_file` |
| `slack_webhook_url`  | `slack_configs.api_url_file` |

`deploy.ps1` создаёт его идемпотентно из `deploy\secrets.local.ps1` (файл в
`.gitignore`), а если этого файла нет — читает оба значения обратно из уже
существующего в кластере Secret, поэтому токен никогда не вводится литералом в
командной строке; переменные окружения `TELEGRAM_BOT_TOKEN` и `SLACK_WEBHOOK_URL` —
путь для CI.

## Правила алертов

Правила лежат рядом с Prometheus:
`../manifests-monitoring/05-prometheus-alertrules.yaml`.

## UI

Доступен без `port-forward` по **http://localhost:9093** - host-порт публикует
`terraform/k3d` через переменную `exposed_node_ports`.