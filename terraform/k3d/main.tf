# ---------------------------------------------------------------------------
# The whole local cluster is described declaratively below. `terraform apply`
# makes reality match this file; `terraform destroy` removes it again.
#
# The k3d provider is config-file driven: instead of exposing every k3d option
# as an argument, it takes a single `k3d_config` document written in k3d's own
# config format (https://k3d.io/v5/usage/configfile/). We build that document
# with yamlencode(), so the values still come from typed Terraform variables.
# ---------------------------------------------------------------------------
resource "k3d_cluster" "sock_shop" {
  name = var.cluster_name

  k3d_config = yamlencode({
    apiVersion = "k3d.io/v1alpha5"
    kind       = "Simple"

    metadata = {
      name = var.cluster_name
    }

    servers = var.servers
    agents  = var.agents
    image   = var.k3s_image

    # Publish the load balancer's 80/443 on the host, so the ingress (Traefik,
    # which k3d ships by default) is reachable at http://localhost:<port>.
    ports = [
      {
        port        = "${var.ingress_http_port}:80"
        nodeFilters = ["loadbalancer"]
      },
      {
        port        = "${var.ingress_https_port}:443"
        nodeFilters = ["loadbalancer"]
      },
    ]

    options = {
      k3d = {
        # Block until the cluster is up, so the next steps see a ready cluster.
        wait = true
      }
    }
  })
}

# The provider returns a ready-to-use kubeconfig. We persist it next to the
# user's other kubeconfigs (NOT in the repo: it contains client credentials),
# so kubectl and helm can reach the cluster.
resource "local_sensitive_file" "kubeconfig" {
  filename = pathexpand(var.kubeconfig_path)
  content  = k3d_cluster.sock_shop.kubeconfig
}
