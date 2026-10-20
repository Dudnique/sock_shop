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

output "api_port" {
  description = <<-EOT
    Host port of the Kubernetes API on the load balancer.

    Read by `deploy.ps1`. k3d writes its own random API port into the generated
    kubeconfig even when the config pins one (measured: the container published
    6443 -> 57443 while the kubeconfig said 50950), and every `kubectl` call then
    fails with "connection refused" right after a green `terraform apply`.
  EOT
  value       = var.api_port
}

output "tool_urls" {
  description = "Host URLs of the tooling exposed via NodePorts (valid once the matching manifests are deployed)."
  value = {
    for host_port in sort(keys(var.exposed_node_ports)) :
    host_port => "http://localhost:${host_port}"
  }
}
