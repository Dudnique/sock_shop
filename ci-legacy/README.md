# Legacy CI (kept for reference)

`main.yaml` in this folder is the **original** CI workflow inherited from
upstream ([microservices-demo/microservices-demo](https://github.com/microservices-demo/microservices-demo)).

It is kept here **only as a reference** and is **not** executed by GitHub —
Actions only runs files that live directly in `.github/workflows/`.

## What it did

- **`complete-demo-sync-check`** — built a Docker image and ran
  `make check-complete-demo` (`awk '{print}' manifests/* | diff complete-demo.yaml -`).
  → still done today, but directly via `awk | diff` (no Docker build).

- **`deployments-tests`** — spun up a `kind` cluster (`kindest/node:v1.20.0`
  from 2020) and deployed the whole demo, waiting for every pod to become
  `Ready`. On modern runners this regularly exceeded the `timeout-minutes: 10`
  limit and the job was cancelled.
  → replaced by a fast, offline schema check with `kubeconform`.

- **`build-test-images`** — built and pushed the `openapi` / `healthcheck`
  images to Docker Hub (needs the `DOCKER_USER` / `DOCKER_PASS` secrets).

## Restoring it

Copy this file back into the workflows folder:

```powershell
Copy-Item ci-legacy/main.yaml .github/workflows/main.yaml -Force
```
