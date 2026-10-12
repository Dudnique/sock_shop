# Локальный кластер k3d (Terraform)

> [English](README.md) | **Русский**

Декларативное описание локального кластера [k3d](https://k3d.io), на котором
запускается стек sock-shop. Один `terraform apply` создаёт кластер; `terraform
destroy` удаляет его.

## Зачем Terraform для локального кластера?

Кластер становится **воспроизводимой инфраструктурой как код** вместо
последовательности shell-команд, которые нужно помнить. Удали его, сделай
`apply` снова — получишь тот же кластер. Это ещё и демонстрация того же рабочего
процесса, что используется для реальной облачной инфраструктуры (см.
`terraform/aws-state/` — backend состояния в AWS).

## Требования

- [Terraform](https://developer.hashicorp.com/terraform/downloads) >= 1.5
- [Docker](https://www.docker.com/) (k3d запускает k3s в контейнерах Docker)
- CLI `k3d` удобен для инспекции, но провайдер общается с Docker напрямую,
  поэтому строго обязательным он не является.

## Использование

```bash
cd terraform/k3d

# Скачать провайдеры, объявленные в versions.tf
terraform init

# Посмотреть, что изменится
terraform plan

# Создать кластер
terraform apply

# Направить kubectl/helm на новый кластер (см. вывод `kubeconfig_path`)
export KUBECONFIG="$HOME/.kube/sock-shop-k3d.yaml"
kubectl get nodes

# Удалить всё
terraform destroy
```

## Публикуемые порты

Кластер публикует два вида хост-портов:

| Хост-порт | Порт контейнера | Кем обслуживается | Назначение |
|---|---|---|---|
| `ingress_http_port` (по умолчанию `8080`) | `80` | load balancer | Sock Shop через Traefik/Ingress |
| `ingress_https_port` (по умолчанию `8443`) | `443` | load balancer | HTTPS (зарезервирован под TLS) |
| каждый ключ `exposed_node_ports` | соответствующий NodePort | первая server-нода | тулы: Prometheus `9090`, Grafana `3000`, Alertmanager `9093`, Jaeger UI `16686` |

Почему два механизма: приложение маршрутизируется ingress-контроллером (Traefik)
на единственном load balancer кластера, а тулы публикуются напрямую через свои
NodePort'ы. NodePort'ы обслуживаются нодами, а не балансировщиком, отсюда
node-фильтр `server:0`.

Чтобы опубликовать новый тул, добавь одну строку в `exposed_node_ports`
в `variables.tf` (или переопредели в `terraform.tfvars`) и сделай `apply` заново.
Больше ничего менять не нужно.

## Структура

| Файл | Назначение |
|---|---|
| `versions.tf` | Версии Terraform + провайдеров |
| `variables.tf` | Входные параметры (имя кластера, число нод, порты, ...) |
| `main.tf` | Ресурс кластера и файл kubeconfig |
| `outputs.tf` | Полезные значения, выводимые после apply |
| `terraform.tfvars.example` | Пример локальных переопределений |

> Сгенерированный kubeconfig пишется вне репозитория (по умолчанию
> `~/.kube/sock-shop-k3d.yaml`), потому что содержит учётные данные кластера.
