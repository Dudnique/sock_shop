# ---------------------------------------------------------------------------
# Inputs. Everything here can be overridden without editing the code, either
# with -var, a terraform.tfvars file, or TF_VAR_<name> environment variables.
# ---------------------------------------------------------------------------

variable "cluster_name" {
  description = "Name of the local k3d cluster. Also drives the kubectl context name (k3d-<name>)."
  type        = string
  default     = "sock-shop"
}

variable "k3s_image" {
  description = "k3s node image. k3d runs k3s inside Docker, so this is a Docker image tag. Pinned for reproducibility."
  type        = string
  default     = "rancher/k3s:v1.35.5-k3s1"
}

variable "servers" {
  description = "Number of control-plane (server) nodes."
  type        = number
  default     = 1
}

variable "agents" {
  description = "Number of worker (agent) nodes."
  type        = number
  default     = 2
}

variable "ingress_http_port" {
  description = "Host port mapped to the cluster load balancer HTTP port (Traefik ingress)."
  type        = number
  default     = 8080
}

variable "ingress_https_port" {
  description = "Host port mapped to the cluster load balancer HTTPS port."
  type        = number
  default     = 8443
}

variable "exposed_node_ports" {
  description = <<-EOT
    Extra host-port : NodePort mappings published for the tooling
    (Prometheus, Grafana, Alertmanager, Jaeger, ...).

    Each KEY is a host port, each VALUE the Kubernetes NodePort it forwards to.
    The mapping is attached to the first server node, so the tool becomes
    reachable at http://localhost:<KEY> without kubectl port-forward.

    NodePorts must be within the Kubernetes range 30000-32767.
  EOT
  type        = map(number)
  default = {
    "9090"  = 31090 # Prometheus (manifests-monitoring/07-prometheus-svc.yaml)
    "3000"  = 31300 # Grafana    (manifests-monitoring/22-grafana-svc.yaml)
    "9093"  = 30903 # Alertmanager (manifests-alerting/alertmanager-svc.yaml)
    "16686" = 30686 # Jaeger UI  (manifests-jaeger/jaeger.yaml)
  }

  validation {
    condition     = alltrue([for np in values(var.exposed_node_ports) : np >= 30000 && np <= 32767])
    error_message = "Each NodePort must be within the Kubernetes range 30000-32767."
  }
}

variable "kubeconfig_path" {
  description = "Where to write the generated kubeconfig. Kept OUTSIDE the repo because it contains cluster credentials."
  type        = string
  default     = "~/.kube/sock-shop-k3d.yaml"
}
