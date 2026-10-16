## Как это увидеть

Два терминала, оба стоят показа:

```powershell
kubectl -n sock-shop get hpa -w
kubectl -n sock-shop get pods -l name=front-end -w
```

Дальше поднимаем нагрузочный генератор (`.\deploy.ps1 -WithLoadtest`). Эти пять
клиентов locust держат `front-end` на ~3m CPU — честный трафик, но это **3% от
запроса 100m**, то есть до цели 50% далеко, и HPA правильно остаётся на одной
реплике. Чтобы увидеть механизм в работе на том же реальном трафике, сужаем
*запрос* — и **не добавляем никакой нагрузки** (это только демо-приём; в репозитории
остаётся 100m):

```powershell
# 1. временно: реальный трафик начинает превышать цель — доп. нагрузки нет
'{"spec":{"template":{"spec":{"containers":[{"name":"front-end","resources":{"requests":{"cpu":"1m"}}}]}}}}' |
  Set-Content -Encoding UTF8 $env:TEMP\fe-req-1m.json
kubectl -n sock-shop patch deploy front-end --type=strategic --patch-file $env:TEMP\fe-req-1m.json

# 2. смотрим во втором терминале, затем возвращаем запрос обратно
'{"spec":{"template":{"spec":{"containers":[{"name":"front-end","resources":{"requests":{"cpu":"100m"}}}]}}}}' |
  Set-Content -Encoding UTF8 $env:TEMP\fe-req-100m.json
kubectl -n sock-shop patch deploy front-end --type=strategic --patch-file $env:TEMP\fe-req-100m.json
```

Измерено на этом стенде, при работающем только нагрузочном генераторе:

```
t=40s    cpu: 300%/50%   реплик: 1   подов: 1
t=60s    cpu: 200%/50%   реплик: 3   подов: 3
t=80s    cpu: 300%/50%   реплик: 3   подов: 4
t=100s   cpu: 200%/50%   реплик: 4   подов: 4
```

Средняя по поду падает, потому что нагрузка размазывается по новым репликам — в
этом и смысл. После возврата запроса HPA спускается назад к одной реплике (1 под
за 60 с, за окном стабилизации 300 с).

`kubectl -n sock-shop describe hpa front-end` говорит то же словами:
`New size: 3; reason: cpu resource utilization (percentage of request) above target`.

Те же данные в Prometheus или Grafana (kube-state-metrics уже скрейпится):

```
kube_deployment_status_replicas{namespace="sock-shop", deployment="front-end"}
rate(container_cpu_usage_seconds_total{namespace="sock-shop", pod=~"front-end.*"}[1m])
```

В Docker Desktop ничего из этого не видно: k3d поднимает три контейнера-*ноды*, а
все поды живут внутри них.

## Грабли

- **`kubectl apply` печатает `created` даже для автоскейлера, который не может
  работать.** Только `TARGETS` и события `kubectl describe hpa` показывают,
  привязан ли он к реальной нагрузке; `<unknown>` означает «нет».
- **Обычный трафик этого стенда не пробивает цель.** Пять клиентов locust — это
  2–4% от запроса; в проде такую цель выставляют под реальные RPS, а здесь для
  демонстрации приходится сужать запрос (см. выше).
- **Пробник с метками рабочей нагрузки становится её частью.** Первая версия
  `manifests-policy/verify-netpol.ps1` запускала под с меткой `name=carts`, и HPA
  `carts` посчитал его своим подом: `FailedGetResourceMetric ... missing request
  for cpu in container netpol-check-3`. Метки — это идентичность для всего, что
  выбирает поды.
- **`kubectl apply -f <dir>` не рекурсивен**, поэтому архивный стек в `legacy/`
  никогда не применяется случайно — см. `legacy/README.md`.
- **Формат аргумента с JSON — источник ошибок.** `\"` внутри одинарных кавычек
  PowerShell — это литеральный обратный слэш (kubectl ответит `invalid character
  's'`), а `-d '{\"user\":...}'` уедет в API слэшами. Надёжнее писать патч в файл
  и использовать `--patch-file`.
