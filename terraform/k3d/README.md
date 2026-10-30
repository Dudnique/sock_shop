# Local k3d cluster (Terraform)

> **English** | [Русский](README.ru.md)

Declarative definition of the local [k3d](https://k3d.io) cluster used to run the
sock-shop stack. One `terraform apply` creates the cluster; `terraform destroy`
removes it.

## Why Terraform for a local cluster?

The cluster becomes **reproducible infrastructure as code** instead of a sequence
of shell commands someone has to remember. Delete it, `apply` again, and you get
the exact same cluster. It is also the same workflow that real
cloud infrastructure would use; the AWS part (remote state in S3 with DynamoDB locking, EKS)
is planned, not written yet (see `TODO.md`).

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
| `api_port` (default `57443`) | `6443` | load balancer | `kubectl` - pinned, see below |
| every key of `exposed_node_ports` | matching NodePort | first server node | tooling: Prometheus `9090`, Alertmanager `9093`, Jaeger UI `16686` |

Why two mechanisms: the app is routed by the ingress controller (Traefik) on the
cluster's single load balancer, while the tools are exposed directly through their
NodePorts. NodePorts are served by nodes, not by the load balancer, hence the
`server:0` node filter.

Grafana is deliberately **not** in that list any more. It used to be NodePort `31300`
published as host port `3000`, which means "reachable on every interface of the machine",
and it answered with the default `admin`/`admin`. Its Service is `ClusterIP` now and Grafana
is served through the same Traefik entrypoint as the application, distinguished by the HTTP
`Host` header: `deploy/kubernetes/manifests-monitoring/30-grafana-ingress.yaml`, host
`grafana.localhost` (see `FIXES.md`, section 12).

To expose a new tool, append one line to `exposed_node_ports` in `variables.tf`
(or override it in `terraform.tfvars`) and re-`apply`. Nothing else changes - with one
caveat that costs time if you do not know it: the k3d provider **cannot modify an existing
cluster** (`Error: Updating clusters is not supported by k3d`), so a change to the published
ports needs an explicit recreate:

```bash
terraform apply -replace=k3d_cluster.sock_shop
```

A plain `terraform apply` fails on that change, and since the same apply also replaces the
generated kubeconfig file (destroy, then create), the failure leaves you **without a
kubeconfig**. `deploy.ps1` now stops on terraform's non-zero exit instead of carrying on.

## Recreating a stand that will not come back

The cluster is disposable: `deploy.ps1 -Down` removes it with `terraform destroy`, and the next
`deploy.ps1` recreates it from this configuration. That is the first thing to try when Docker
Desktop's VM has been through an unclean shutdown and `kubectl` times out: a fresh cluster starts at
load ~0.5 under the same 7.5 GB WSL limit, while the old one (three days of containerd snapshots,
Prometheus TSDB and Loki chunks) does not.

```powershell
.\deploy.ps1 -Down           # terraform destroy, ~20 s
.\deploy.ps1 -WithAIOps      # terraform apply + the manifests, ~5 min
```

The kubeconfig this configuration writes (`~/.kube/sock-shop-k3d.yaml`, output `kubeconfig_path`) is
replaced on every apply, so a rebuild also repairs a kubeconfig that points at an API port which no
longer exists. What is lost is history - Prometheus data, Loki chunks and the bot's journal live
inside the nodes - not configuration: everything that matters is in git.

Before rebuilding, `deploy\diagnose-stand.ps1` tells the three failures apart that all look like
"kubectl cannot connect": engine wedged, VM thrashing, cluster unhealthy. It is read-only and prints
the next command for whichever one it finds. The full analysis of the incident, and the two
`deploy.ps1` bugs the rebuild uncovered, are in `FIXES.md`, section 17.

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
