# Argo CD (режим GitOps)

`.\deploy.ps1 -GitOps` отдаёт деплой стенда Argo CD: он берёт состояние из этого
репозитория, а не скрипт применяет манифесты по одному каталогу. Больше ничего не
меняется: обычный путь и `-Helm` работают как раньше, а парити-гейт
(`../helm-chart/check-parity.py`) по-прежнему говорит, что чарт и `manifests/`
описывают одно и то же приложение.

## Что делает скрипт, а что Argo CD

| Кто | Что |
| --- | --- |
| `deploy.ps1` | кластер k3d (Terraform); три Secret, которых нет в git (`grafana-admin`, `alertmanager-secrets`, `aiops-llm`); с `-WithAIOps` собирает образ бота и импортирует его в ноду; ставит Argo CD и применяет **один** корневой Application |
| Argo CD | всё остальное: читает этот репозиторий и приводит кластер в соответствие |

    deploy.ps1 -GitOps
      |  helm install argo-cd (values.yaml)      версия закреплена: чарт 10.11.6 = Argo CD v3.5.6
      |  kubectl apply root-app.yaml
      v
    sock-shop-root  --рендерит-->  apps/ (Helm-чарт, по Application на запись values.yaml)
      |
      +- волна 0   sock-shop (Helm-чарт)  +  network-policies
      +- волна 1   autoscaling  +  logging  +  tracing
      +- волна 2   monitoring  +  alerting
      +- волна 3   aiops, loadtest   (только с -WithAIOps / -WithLoadtest)

Список - это данные (`apps/values.yaml`), а не код: добавить компонент - одна запись.

## Файлы

| Файл | Назначение |
| ---- | ---------- |
| `values.yaml` | значения чарта `argo-cd`: dex, notifications и ApplicationSet выключены, HTTP за Traefik, лимиты памяти под ноутбук, проверка здоровья дочерних Application |
| `root-app.yaml` | единственный Application, который применяется руками; `deploy.ps1` подставляет ревизию и два необязательных переключателя |
| `apps/` | чарт app-of-apps: `values.yaml` перечисляет Application, `templates/applications.yaml` их рендерит |
| `check-apps.py` | гейт в CI: `deploy.ps1` применяет каталог, для которого нет Application (или наоборот) -> красный |

## Решения и почему

* **Секреты остаются вне git и вне Argo CD.** Argo CD удаляет только то, что
  отслеживает, так что `prune: true` не тронет Secret, которого он не создавал. Поды,
  монтирующие Secret, созданный чуть позже, просто повторяют попытку (`retry` в
  каждом Application).
* **`ignoreDifferences` на `/spec/replicas` для `sock-shop`.** Число реплик ведут
  HPA; без этого `selfHeal` возвращал бы каждый Deployment обратно, воюя с
  автоскейлером (тот же конфликт, что был у Helm, `FIXES.md`, раздел 25). Следствие,
  которое стоит знать: ручное масштабирование Deployment Argo CD **не** отменяет -
  чтобы увидеть самовосстановление, меняйте образ, переменную окружения или метку.
* **Server-side apply.** Некоторые ConfigMap (дашборды Grafana, правила Prometheus)
  близки к лимиту 256 КиБ аннотации `last-applied`, которую пишет client-side apply.
* **Отслеживание ресурсов по аннотации.** Чарт сам ставит `app.kubernetes.io/instance`;
  отслеживание по метке заставило бы Argo CD и Helm драться за одну метку.
* **Проверка здоровья дочерних Application** (Lua в `values.yaml`). Без неё корень
  всегда Healthy, и волны игнорируются: на первом запуске Application бота стал
  Healthy, пока `sock-shop` ещё стартовал. С ней волна 1 ждёт здоровья волны 0 - тот
  порядок, который у `deploy.ps1` был всегда: приложение и политики до мониторинга.
* **Job импорта - Sync-хук** (`23-grafana-import-dash-batch.yaml`): спецификация Job
  неизменяема, поэтому Argo CD пересоздаёт его при каждой синхронизации - то же, что
  `deploy.ps1` делает руками через `kubectl delete job`.

## Как пользоваться

    .\deploy.ps1 -GitOps                              # читает main
    .\deploy.ps1 -GitOps -GitRevision my-branch       # проверить запушенную ветку на живом стенде
    .\deploy.ps1 -GitOps -WithAIOps -WithLoadtest

Интерфейс - `http://argocd.localhost:8080` (логин `admin`, пароль печатает скрипт, он
лежит в `argocd-initial-admin-secret`); без записи в hosts:

    kubectl port-forward -n argocd svc/argocd-server 8090:80   # http://localhost:8090

Изменение - это коммит: запушили, и Argo CD применяет его в течение трёх минут
(интервал опроса по умолчанию); на практике замерено 73 с. Чтобы увидеть
самовосстановление, измените то, чем владеет Argo CD:

    kubectl -n sock-shop set image deploy/payment payment=weaveworksdemos/payment:0.4.2
    kubectl -n argocd get application sock-shop      # OutOfSync, затем снова Synced

## Известные ограничения

* **Prometheus и Alertmanager сами не перечитывают конфиг.** После изменения правила
  или конфига Alertmanager Argo CD обновляет ConfigMap, но процесс держит старый файл
  до `POST /-/reload` (на обычном пути это делает `deploy.ps1`). Лечится sidecar'ом,
  перечитывающим конфиг, - он не входит в это изменение, см. `TODO.md`.
* Первая синхронизация занимает около пятнадцати минут: JVM-сервисы начинают свои
  readiness-проверки на 360-420 с.
* Argo CD добавляет около 0,35 ГБ к стенду, который и так занимает ~4,6 ГБ из 8 ГБ ВМ.
