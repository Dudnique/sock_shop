# ---------------------------------------------------------------------------
# Outputs are shown by `terraform apply` and can be read with
# `terraform output`. Handy values to connect to the cluster.
# ---------------------------------------------------------------------------

output "cluster_name" {
  description = "Name of the k3d cluster."
  value       = k3d_cluster.sock_shop.name
}

output "kubectl_context" {
  description = "kubectl context pointing at this cluster."
  value       = "k3d-${k3d_cluster.sock_shop.name}"
}

output "app_url" {
  description = "URL of the sock-shop front-end through the k3d load balancer (once the chart is deployed)."
  value       = "http://localhost:${var.ingress_http_port}"
}

output "kubeconfig_path" {
  description = "Path to the kubeconfig written by this config."
  value       = pathexpand(var.kubeconfig_path)
}
