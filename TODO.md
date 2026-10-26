# Что осталось сделать

Состояние: **путь через чарт впервые развёрнут целиком** (26.10.2026, `-Down` затем `-Helm`
на чистом кластере) и обслуживает трафик; заказ проходит end-to-end. **Нагрузочный тест
работает**: `-WithLoadtest` даёт `checks` 100.00% (505 из 505) и `http_req_failed` 0 из 820,
заказы реально создаются. **Под нагрузкой стенд держится**: после ослабления проб чарта ни
один под не рестартовал, четыре прогона k6 подряд - снова 100.00%. Гейт
`deploy/kubernetes/helm-chart/check-parity.py` сравнивает 14
workload'ов (реплики, каждый контейнер с портами, образ, `command`, `args`, `env`, `resources`,
`volumeMounts` и тома), 14 Service (тип, метки, порты, аннотации), Ingress и инвентарь объектов
обеих сторон. История и измерения - `FIXES.md` §23 (рендер), §24 (прогон) и §25 (нагрузочный
тест, session-db, Helm и HPA), то же в `FIXES.ru.md`.

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
      14/14 подов вернулись в Ready (данные приложений при этом, разумеется, теряются);
- [x] нагрузочный тест: k6 `0.54.0` вместо locust на Python 2, `sleep(1)` на итерацию,
      `noCookiesReset`, Basic auth на `GET /login`, товары не дороже 40 - `checks` 100.00%,
      `http_req_failed` 0 из 820, `data.customerOrder` растёт, `queue-master` получает задачи;
- [x] метрики RabbitMQ: `rabbitmq:3.13.7-management` + `kbudde/rabbitmq-exporter:1.0.0` на обоих
      путях, `rabbitmq_up=1`, `rabbitmq_queues=1`, `rabbitmq_connections=2` (было `0` и пусто);
- [x] `session-db`: `emptyDir` на `/data` - read-only корень не давал Redis сохранить RDB,
      `stop-writes-on-bgsave-error` блокировал **все** записи, то есть логин и checkout;
- [x] образы выровнены (`mongo:4.4.0`, `user-db:0.4.0`, `rabbitmq:3.13.7-management` на обеих
      сторонах), `KNOWN_IMAGE_DIFFS` пуст, строка отчёта parity - 0 различий.
- [x] пробы ослаблены до `periodSeconds: 10`, `timeoutSeconds: 5`, `failureThreshold: 6` во всех
      восьми шаблонах чарта и в четырёх манифестах, где пробы есть (`catalogue`, `front-end`,
      `payment`, `user`; `complete-demo.yaml` перегенерирован): под нагрузкой upstream-тайминги
      (3 с период, 1 с таймаут, 3 провала) убивали здоровые JVM - `catalogue`, `payment`, `user`,
      `queue-master` и два пода `front-end` рестартовали в пределах трёх минут, k6 показывал
      `catalogue 200` 0/100 и `http_req_waiting` до 35 с при 26% занятой памяти на нодах (§25).
      После правки: ревизия 6 - `deployed`, у всех подов `0` рестартов, шесть прогонов k6
      подряд - `checks` 100.00% (505 из 505), `http_req_failed` 0 из 820;
- [x] `--timeout` в `deploy.ps1` поднят с `10m` до `15m`: апгрейд стенда, который уже
      обслуживает трафик, занимает около пятнадцати минут, потому что каждая реплика, которую
      HPA добавляет посреди раскатки, ждёт свои 360 с `initialDelaySeconds` (§25).

## 2. Что осталось

а) **CI.** После пуша проверить, что `helm chart validation` в `.github/workflows/main.yaml`
зелёный: шаг сравнения рендерит с `--namespace sock-shop` и запускает `check-parity.py` на том
же файле. Гейт теперь сравнивает и `volumeMounts`/тома (иначе фикс `session-db` мог попасть в
один файл и не попасть в другой при зелёном CI), так что шаблоны обязаны совпадать и по ним.

б) **Повторный `-Helm` на загруженном стенде** проверен одним прогоном (`--force-conflicts`,
`REVISION: 5`). Полный `deploy.ps1 -Helm` после суток нагрузки стоит прогнать ещё раз - но это
уже дешёвая проверка, а не открытый вопрос: конфликт с HPA воспроизводился стабильно (§25).

в) **Метрики RabbitMQ на дашбордах.** Экспортёр отдаёт `rabbitmq_up=1`,
`rabbitmq_queues=1`, `rabbitmq_connections=2`; стоит глазами посмотреть, что панели Grafana и
правила алертов ожидаемого набора метрик с новым брокером не потеряли.

## 3. Осознанно НЕ выравнивается (записано, чтобы не «чинили» снова)

- Пробы: манифесты и чарт покрывают разные наборы сервисов - манифесты только четыре
  (`catalogue`, `front-end`, `payment`, `user`), чарт все восемь прикладных плюс `availableReplicas`
  через Deployment. Гейт по пробам был бы красным всегда. Тайминги на обеих сторонах теперь
  одинаковые и ослабленные: `periodSeconds: 10`, `timeoutSeconds: 5`, `failureThreshold: 6`
  (§1, §25). Upstream-набор (`3`/`1`/`3`) под нагрузкой перезапускал здоровые поды - и в чарте,
  и в тех четырёх манифестах, где пробы есть.
- `nodeSelector`: все 14 подов в `manifests/` пинят `kubernetes.io/os: linux`, шаблоны
  чарта - нет. На одноосном кластере поле ни на что не влияет.
- Апстримный баг в образе `front-end` (0.6.0): `helpers.sessionMiddleware` объявлен с четырьмя
  аргументами (`err, req, res, next`), поэтому Express считает его error-handler'ом, и любая
  ошибка в POST превращается в `500 Cannot read property 'logged_in' of undefined` - исходная
  ошибка теряется. Образ не наш, править нечем; помнить при отладке и не верить телам ошибок
  (§25).
- `helm upgrade` на стенде, где HPA уже менял реплики, требует
  `--server-side=true --force-conflicts`; `deploy.ps1` их передаёт, ручные команды в README тоже
  (§25).

## 4. Гигиена репозитория

- [ ] `rendered.yaml` в корне - артефакт локальной проверки (в `.gitignore`); удалять при
      необходимости, в коммиты не попадает.

