# Что осталось сделать

Состояние на момент паузы: чарт и `manifests/` описывают **один и тот же pod spec** -
это проверяется гейтом `deploy/kubernetes/helm-chart/check-parity.py`, локально зелёным
(`helm lint --strict`, `helm template`, `kubeconform -strict`, parity - все exit 0).
История правок и измерения - в `FIXES.md` §23 (и `FIXES.ru.md` §23).

## 1. Прогон `-Helm` на чистом кластере (единственное, что осталось непроверенным)

Чарт ни разу не устанавливался целиком: `helm` не принимает объекты, созданные ранее
`kubectl apply`, поэтому нужен чистый кластер:

```powershell
.\deploy.ps1 -Down
.\deploy.ps1 -Helm
```

Прогон должен подтвердить именно то, что чинилось вслепую:

- [ ] `payment` слушает 80, а Service бьёт в `targetPort: 80` (было 8080 без бэкенда);
- [ ] `rabbitmq` отдаёт метрики на `:9090` через `rabbitmq-exporter` (в чарте сайдкара
      не было вообще), `kubectl -n sock-shop get servicemonitor`/`TARGETS` в Prometheus
      показывает `rabbitmq` в состоянии UP;
- [ ] `ZIPKIN` у `catalogue`/`payment`/`user` доходит до Jaeger (`zipkin.jaeger.svc...:9411`),
      трассы видны в Jaeger UI;
- [ ] `user` поднимается с переменной `mongo: user-db:27017` и без флагов `-mongo-host`;
- [ ] `carts`/`orders`/`shipping` стартуют на `ENTRYPOINT` образа (`--port=80`), пробы
      зелёные, `order` проходит end-to-end (checkout не отвечает `-decline`);
- [ ] `helm uninstall` + повторный `install` не ломает `sock-shop` (идемпотентность).

## 2. После прогона - закрыть долг по образам

В `check-parity.py` остались три allowlisted различия, снятие которых требует решения
(и, скорее всего, того же `-Helm`-прогона):

- [ ] `carts-db`, `orders-db`: `mongo` (манифесты) против `mongo:4.4.0` (чарт) - выбрать
      одну сторону и выровнять **оба** файла, затем убрать запись из `IMAGE_EXCEPTIONS`;
- [ ] `user-db`: `0.3.0` (манифесты) против `0.4.0` (чарт) - то же самое.

## 3. Осознанно НЕ выравнивается (записано, чтобы не «чинили» снова)

- Пробы: манифесты живут на `availableReplicas`, чарт - на контейнерных пробах с
  upstream-таймингами. Гейт по пробам был бы красным всегда.
- `nodeSelector`: все 14 подов в `manifests/` пинят `kubernetes.io/os: linux`, шаблоны
  чарта - нет. На одноосном кластере поле ни на что не влияет. Если стенд когда-нибудь
  станет разноосным - добавить в чарт и в гейт одновременно.

## 4. Гигиена репозитория

- [ ] `rendered.yaml` в корне - артефакт локальной проверки (в `.gitignore`); удалять при
      необходимости, в коммиты не попадает.
- [ ] После пуша коммитов проверить, что CI (`helm chart validation` в
      `.github/workflows/main.yaml`) зелёный: шаг сравнения рендерит с
      `--namespace sock-shop` и запускает `check-parity.py` на том же файле.
