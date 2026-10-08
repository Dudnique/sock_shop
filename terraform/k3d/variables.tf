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

variable "kubeconfig_path" {
  description = "Where to write the generated kubeconfig. Kept OUTSIDE the repo because it contains cluster credentials."
  type        = string
  default     = "~/.kube/sock-shop-k3d.yaml"
}
