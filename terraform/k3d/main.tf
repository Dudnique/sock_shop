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

    # The API host port is pinned, and pinned the way k3d understands it: the
    # `kubeAPI` block (`--api-port` on the CLI). Do NOT put `6443` into `ports`:
    # measured on this stand, that made the load balancer route the API to every
    # node - agents included - so two requests out of three hit a node with no
    # API server and `kubectl` failed intermittently with `TLS handshake
    # timeout`/`connection refused` while the cluster itself was healthy.
    kubeAPI = {
      host     = "127.0.0.1"
      hostIP   = "0.0.0.0"
      hostPort = var.api_port
    }

    # Two kinds of published ports:
    #
    # 1) Ingress (Traefik, shipped by k3d) on the load balancer -> 80/443.
    #    The app is reachable at http://localhost:<ingress_http_port>.
    # 2) Tooling exposed directly via NodePorts, attached to the first server
    #    node (NodePorts are served by nodes, NOT by the load balancer).
    #    Each tool is reachable at http://localhost:<host_port>.
    #
    # There is exactly ONE load balancer (part of every k3d cluster) - it is
    # NOT something we add per port. NodePorts simply don't exist on it, which
    # is why the app goes through the load balancer and the tools via server:0.
    ports = concat(
      [
        {
          port        = "${var.ingress_http_port}:80"
          nodeFilters = ["loadbalancer"]
        },
        {
          port        = "${var.ingress_https_port}:443"
          nodeFilters = ["loadbalancer"]
        },
      ],
      [
        for host_port, node_port in var.exposed_node_ports : {
          port        = "${host_port}:${node_port}"
          nodeFilters = ["server:0"]
        }
      ],
    )

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
