# legacy/ - what is not part of the stand any more

Everything in this folder is kept **for history only**. None of it is deployed,
tested or supported by the stand in the repository root, and none of it is
referenced by `deploy.ps1`, `deploy\stand.ps1` or `.github\workflows\main.yaml`.
It is here so that the decisions in `FIXES.md` can be checked against the code
they were about - not so that it can be run.

| Folder | What it is | Why it is not used |
| --- | --- | --- |
| `ci-legacy/` | The original CI workflow inherited from upstream `microservices-demo` (`kind` cluster, Docker Hub image builds) plus its `Makefile` helpers. Its own `README.md` explains what replaced each job. | The `kind` job timed out on modern runners; replaced by offline `kubeconform` validation in `.github/workflows/main.yaml`. |
| `docker-compose/` | The original compose files for the application + monitoring stack, the pre-provisioned Grafana dashboards and the alert rules. | The stand is Kubernetes. Two of the findings in `FIXES.md` were in these files (hard-coded Grafana admin password, dashboards posted by an init script), so they stay as the evidence of what was fixed where. |
| `install/` | Upstream "install utilities": `aws-minimesos` (Mesos cluster on AWS EC2 with Terraform + `provision.sh`). | Nothing in the project needs Mesos, and the local stand has no cloud dependency. |
| `staging/` | Upstream Terraform for a staging cluster on AWS from a bastion host. | Same: cloud staging was never part of this repository's story; `terraform/k3d` is the supported cluster. |
| `aws-ec2-cluster/` | Upstream Terraform for a Kubernetes cluster on AWS EC2 (master + node + ELB, previously `deploy/kubernetes/terraform`). | The repo's cluster is local (k3d). Kept because CI once referred to it; the `terraform validate` job is scoped to `terraform/k3d` only. |
| `graphs/` | `grafanalib` generators for the Grafana dashboards. | The dashboards are provisioned from `deploy/kubernetes/manifests-monitoring` (ConfigMaps), so a generator would be a second source of truth. |
| `openapi/`, `healthcheck/` | Helper images (a Swagger UI for the API spec, a Ruby health-checker) built by `ci-legacy/main.yaml`. | No manifest in `deploy/kubernetes` references either image, so both would be built and never deployed. |

Rules of thumb for this folder:

- Nothing here may be added to a `kubectl apply -f` path. `kubectl apply -f <dir>`
  is not recursive, which is what keeps this folder out of the cluster.
- New work goes in the root of the repository, not here. If something in here is
  needed again, it is restored and modernised, not resurrected as is.
- `kubeconform` in CI skips `*/legacy/*` by filename pattern, and
  `deploy/kubernetes/autoscaling/legacy/` keeps that same convention for
  archived API versions.
