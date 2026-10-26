# Что осталось сделать

Состояние: **путь через чарт впервые развёрнут целиком** (26.10.2026, `-Down` затем `-Helm`
на чистом кластере) и обслуживает трафик; заказ проходит end-to-end. Гейт
`deploy/kubernetes/helm-chart/check-parity.py` сравнивает 14 workload'ов (реплики, каждый
контейнер с портами, образ, `command`, `args`, `env`, `resources`), 14 Service (тип, метки,
порты, аннотации), Ingress и инвентарь объектов обеих сторон. История и измерения -
`FIXES.md` §23 (рендер) и §24 (прогон), то же в `FIXES.ru.md`.

## 1. Прогон `-Helm` - сделано, что он подтвердил

- [x] `payment` слушает 80 (`/proc/net/tcp6`: только `:0050`), Service бьёт в тот же порт,
      `Authorise result=true` в логе payment при заказе;
- [x] `rabbitmq` с `-management` и сайдкаром `rabbitmq-exporter`, цель на `:9090` в Prometheus
      `up=1` (но см. пункт 2а: сам management API отдаёт 500);
- [x] в `TARGETS` все восемь прикладных сервисов: шесть на `:80`, `front-end` на `:8079`,
      `rabbitmq` на `:9090`, все `up=1`;
- [x] `queue-master` в `TARGETS` отсутствует, и `ScrapeTargetDown` не горит;
- [x] `ZIPKIN` доходит до Jaeger: в `/api/services` есть `payment`, `user`, `catalogue` со
      спанами этого прогона;
- [x] `user` поднимается на `mongo=user-db:27017` без флагов;
- [x] `carts`/`orders`/`shipping`/`queue-master` без `command`/`args` (образ сам себе ENTRYPOINT),
      `JAVA_OPTS` без `PermSize` и с `-Dspring.zipkin.enabled=false`;
- [x] checkout: `POST /orders` -> 201, `data.customerOrder` = 2, `queue-master` получил обе
      задачи доставки. **Для этого пришлось починить NetworkPolicy**: в
      `manifests-policy/13-orders.yaml` в egress не было `carts` (при том, что `carts` уже
      пускал `orders` к себе) - `kube-router` отвечает `REJECT`, и checkout падал 500 за
      миллисекунды (§24);
- [x] `helm uninstall` + `install` на том же кластере: релиз снесён и поставлен заново,
      14/14 подов вернулись в Ready (данные приложений при этом, разумеется, теряются).

## 2. Что осталось

а) **Метрики RabbitMQ.** Экспортёр скрейпится (`up=1`), но `rabbitmq_up` = 0 всё время:
management-плагин RabbitMQ `3.6.8-management` падает в
`rabbit_mgmt_stats:rate_from_difference/2` с `badarith`, `/api/overview` отдаёт 500. AMQP при
этом здоров (`shipping-task` обрабатывается). Нужно решение: поднять брокер (и перепроверить
`queue-master`) либо честно объявить, что метрик RabbitMQ на стенде нет. К чарту отношения не
имеет - контейнеры у обоих путей идентичны.

б) **Долг по образам** (`KNOWN_IMAGE_DIFFS` в `check-parity.py`):

- [ ] `carts-db`, `orders-db`: `mongo` (манифесты) против `mongo:4.4.0` (чарт) - выбрать
      сторону и выровнять **оба** файла, затем убрать запись;
- [ ] `user-db`: `0.3.0` (манифесты) против `0.4.0` (чарт) - то же самое.

в) **Генератор нагрузки.** `weaveworksdemos/load-test:0.1.1` (Python 2) на этом ядре падает в
`Segmentation fault`, поэтому `-WithLoadtest` не создаёт ни одного заказа. Нужен пересобранный
образ (locust на Python 3) или замена на k6.

г) **CI.** После пуша проверить, что `helm chart validation` в `.github/workflows/main.yaml`
зелёный: шаг сравнения рендерит с `--namespace sock-shop` и запускает `check-parity.py` на том
же файле.

## 3. Осознанно НЕ выравнивается (записано, чтобы не «чинили» снова)

- Пробы: манифесты живут на `availableReplicas`, чарт - на контейнерных пробах с
  upstream-таймингами (`periodSeconds: 3`, `timeoutSeconds: 1`, `failureThreshold: 3`).
  Гейт по пробам был бы красным всегда. Но помнить: три пропущенные секундные пробы выводят
  JVM из эндпоинтов, и клиент получает `ECONNREFUSED`, а не медленный ответ - на пути через
  манифесты такого режима отказа нет (§24).
- `nodeSelector`: все 14 подов в `manifests/` пинят `kubernetes.io/os: linux`, шаблоны
  чарта - нет. На одноосном кластере поле ни на что не влияет.

## 4. Гигиена репозитория

- [ ] `rendered.yaml` в корне - артефакт локальной проверки (в `.gitignore`); удалять при
      необходимости, в коммиты не попадает.

