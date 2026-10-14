# legacy/

> [English](README.md) | **Русский**

Старые («до») версии манифестов, сохранены для сравнения и истории.
Файлы отсюда **не применяются**: `kubectl apply -f deploy/kubernetes/manifests-monitoring/`
не рекурсивный, поэтому этот подкаталог игнорируется.

## 04-prometheus-configmap.yaml

Исходный конфиг скрейпинга Prometheus. Локальный стенд ломали две вещи:

- jobs `kubernetes-services` / `kubernetes-ingresses` указывали `blackbox-exporter`
  на `blackbox-exporter.example.com:9115` — хост, которого в кластере нет, поэтому
  каждый blackbox-пробник падал;
- job `kubernetes-ingresses` строил цель пробы из хоста ингресса, а у локального
  ингресса хоста нет — получалась пустая (бесполезная) цель.

Заменён на `../04-prometheus-configmap.yaml`, где экспортёр адресуется своим
внутрикластерным именем (`blackbox-exporter.monitoring.svc.cluster.local:9115`), а
проба ингресса целится напрямую в точку входа Traefik
(`traefik.kube-system.svc.cluster.local`) с `metrics_path: /probe`.

## 06-prometheus-dep.yaml

Исходный Deployment Prometheus крутил старый образ `prom/prometheus:v2.26.0`.
Заменён на `../06-prometheus-dep.yaml`, где зафиксирован `prom/prometheus:v2.45.6` и
добавлен `--web.enable-lifecycle` (конфиг можно перезагружать без рестарта).

## 23-grafana-import-dash-batch.yaml

Исходный Job импорта Grafana. Он пытался дождаться Grafana через аннотацию
`pod.beta.kubernetes.io/init-containers` вместо реального поля `initContainers`.
Современный Kubernetes игнорирует эту аннотацию (фича init-контейнеров стала
настоящим полем, а аннотация удалена в 1.20), поэтому ожидание молча ничего не
делало. На чистой установке импортёр опережал Grafana и падал с
`curl: (7) ... Connection refused`, оставляя Grafana без datasource и без
дашбордов.

Заменён на `../23-grafana-import-dash-batch.yaml`, который использует настоящее
поле `initContainers` (`wait-for-grafana`) и `backoffLimit`.

В «исправленной» версии этого Job'а жил **второй, независимый дефект**, поэтому копия и
осталась здесь: его скрипт был **folded YAML-блоком (`>`)**. Folded-блок склеивает
строки пробелами, поэтому `fi` + `done` превращались в одно слово `fidone`, а
`#`-комментарий съедал остаток скрипта — при этом Job всё равно завершался `Completed`.
В рабочем файле теперь literal-блок (`|`) и **проверка результата**
(`/api/search?type=dash-db`, `/api/datasources`): Job падает, если ничего не импортировано.
Если что-то отсюда копируешь — не копируй folded shell-скрипт.

Про креды: раньше в URL этой архивной копии стоял дефолтный пароль
(`http://admin:admin@grafana/...`). Здесь он замаскирован как `admin:<password>`:
архив хранится ради структуры, а не ради паролей, и литеральный пароль в дереве —
ровно то, о чём раздел 12 в `FIXES.md`. В рабочем файле и пользователь, и пароль
берутся из Secret `grafana-admin`.

