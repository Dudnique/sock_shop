# Sock Shop — рабочий стенд на k3d

Готовый к запуску форк демо-приложения **Sock Shop**
(upstream: [microservices-demo/microservices-demo](https://github.com/microservices-demo/microservices-demo)).

В репозитории лежат **все файлы, необходимые для поднятия магазина** —
Kubernetes-манифесты всех сервисов и баз данных, Ingress и скрипт развёртывания.
Сами Docker-образы публичные и автоматически скачиваются из Docker Hub, поэтому
в репозиторий их добавлять не нужно.

> Что именно было исправлено относительно upstream — см. [FIXES.md](./FIXES.md).
> Кратко: у фронтенда был закреплён старый образ `front-end:0.3.1`, из-за
> которого регистрация пользователя возвращала `500`. Теперь везде стоит
> корректный `front-end:0.3.12`.

---

## Что нужно

- Windows (или Linux/macOS) с установленными:
  - [Docker](https://docs.docker.com/get-docker/)
  - [k3d](https://k3d.io/) — лёгкий Kubernetes в Docker
  - [kubectl](https://kubernetes.io/docs/tasks/tools/)

Проверить:

```powershell
docker version
k3d version
kubectl version --client
```

## Быстрый старт (одна команда)

```powershell
.\deploy.ps1
```

Скрипт создаст кластер k3d, применит все манифесты и дождётся запуска подов.
После этого магазин откроется по адресу **http://localhost:8079**.

Параметры (необязательно):

```powershell
.\deploy.ps1 -ClusterName sock-shop -HttpPort 8079
```

## Ручное развёртывание

Если не хочешь использовать скрипт:

```powershell
# 1. Кластер с Traefik и пробросом хост-порта 8079 -> 80 (loadbalancer)
k3d cluster create sock-shop -p 8079:80@loadbalancer

# 2. Применить манифесты (порядок задаётся числовыми префиксами имён файлов)
kubectl apply -f deploy/kubernetes/manifests

# 3. Дождаться готовности
kubectl wait --for=condition=available --timeout=300s deployment --all -n sock-shop
```

Либо всё сразу из одного файла:

```powershell
kubectl apply -f deploy/kubernetes/complete-demo.yaml
```

## Доступ к приложению

- Через Ingress (нужен Traefik, ставится в k3d по умолчанию): **http://localhost:8079**
- Через NodePort (если пробрасывать порт вручную):

  ```powershell
  kubectl port-forward -n sock-shop svc/front-end 8070:80
  # затем http://localhost:8070
  ```

## Структура

```
.
├── deploy.ps1                              # развёртывание в один клик
├── FIXES.md                                # что исправлено и почему
└── deploy/
    ├── kubernetes/
    │   ├── manifests/                      # пофайловые манифесты (00 ... 29)
    │   ├── complete-demo.yaml              # всё в одном файле
    │   └── helm-chart/                     # альтернатива: Helm-чарт
    └── docker-compose/                     # альтернатива: docker-compose
```

## Остановить / удалить

```powershell
k3d cluster stop sock-shop     # остановить
k3d cluster start sock-shop    # запустить снова
k3d cluster delete sock-shop   # удалить полностью
```

## Лицензия

См. [LICENSE](./LICENSE). Проект распространяется под лицензией Apache 2.0.
