terraform {
  # Pin a minimum Terraform version. `required_version` lets Terraform refuse to
  # run on an old CLI instead of failing later with a confusing error.
  required_version = ">= 1.5.0"

  # Providers are plugins that know how to talk to an external system.
  # `source` is the registry address, `version` follows semantic versioning
  # (~> 1.0 means "any 1.x, but not 2.0"). Pinning keeps builds reproducible.
  required_providers {
    k3d = {
      source  = "SneakyBugs/k3d"
      version = "~> 1.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.4"
    }
  }
}
