# Local k3d cluster (Terraform)

> **English** | [Русский](README.ru.md)

Declarative definition of the local [k3d](https://k3d.io) cluster used to run the
sock-shop stack. One `terraform apply` creates the cluster; `terraform destroy`
removes it.

## Why Terraform for a local cluster?

The cluster becomes **reproducible infrastructure as code** instead of a sequence
of shell commands someone has to remember. Delete it, `apply` again, and you get
the exact same cluster. It also demonstrates the same workflow used for real
cloud infrastructure (see `terraform/aws-state/` for the AWS state backend).

## Requirements

- [Terraform](https://developer.hashicorp.com/terraform/downloads) >= 1.5
- [Docker](https://www.docker.com/) (k3d runs k3s in Docker containers)
- The `k3d` CLI is convenient for inspection, but the provider talks to Docker
  directly, so it is not strictly required.

## Usage

```bash
cd terraform/k3d

# Download the providers declared in versions.tf
terraform init

# Show what would change
terraform plan

# Create the cluster
terraform apply

# Point kubectl/helm at the new cluster (see the output `kubeconfig_path`)
export KUBECONFIG="$HOME/.kube/sock-shop-k3d.yaml"
kubectl get nodes

# Remove everything
terraform destroy
```

## Published ports

The cluster publishes two kinds of host ports:

| Host port | Container port | Served by | Purpose |
|---|---|---|---|
| `ingress_http_port` (default `8080`) | `80` | load balancer | Sock Shop through Traefik/Ingress |
| `ingress_https_port` (default `8443`) | `443` | load balancer | HTTPS (reserved for TLS) |
| every key of `exposed_node_ports` | matching NodePort | first server node | tooling: Prometheus `9090`, Grafana `3000`, Alertmanager `9093`, Jaeger UI `16686` |

Why two mechanisms: the app is routed by the ingress controller (Traefik) on the
cluster's single load balancer, while the tools are exposed directly through their
NodePorts. NodePorts are served by nodes, not by the load balancer, hence the
`server:0` node filter.

To expose a new tool, append one line to `exposed_node_ports` in `variables.tf`
(or override it in `terraform.tfvars`) and re-`apply`. Nothing else changes.

## Layout

| File | Purpose |
|---|---|
| `versions.tf` | Terraform + provider version pins |
| `variables.tf` | Inputs (cluster name, node counts, ports, ...) |
| `main.tf` | The cluster resource and kubeconfig file |
| `outputs.tf` | Useful values printed after apply |
| `terraform.tfvars.example` | Example of local overrides |

> The generated kubeconfig is written outside the repository (default
> `~/.kube/sock-shop-k3d.yaml`) because it contains cluster credentials.
