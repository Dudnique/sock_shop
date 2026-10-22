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
  → the one job that was carried over: it still exists in
  `.github/workflows/main.yaml` and runs on a `v*` tag, with the build context
  following the sources into `legacy/openapi` and `legacy/healthcheck`.

## Archived tooling (`Makefile`, `manifests-Makefile`, `manifests-Dockerfile`)

The same reasoning applies to the Docker + make machinery that used to live in the
repository root and in `deploy/kubernetes/`:

| File here | Was | What it did |
|-----------|-----|-------------|
| `Makefile` | `./Makefile` | thin wrapper: `make gen-complete-demo` / `make check-generated-files` |
| `manifests-Makefile` | `deploy/kubernetes/Makefile` | `awk '{print}' manifests/* > complete-demo.yaml`, the reverse check, and the `docker-%` targets that ran them inside an image |
| `manifests-Dockerfile` | `deploy/kubernetes/Dockerfile` | the `alpine:3.12.0` + `bash`/`make` image those `docker-%` targets built |

Nothing live depends on them:

- the generation is a single command and does not need `make`, let alone an image
  built for it;
- the check is run by CI directly as
  `awk '{print}' deploy/kubernetes/manifests/* | diff deploy/kubernetes/complete-demo.yaml -`
  (`complete-demo-sync-check` in `.github/workflows/main.yaml`), so the
  `make check-complete-demo` path is dead weight;
- the stand itself is brought up by `deploy.ps1` + Terraform, never by `make`, and on
  Windows the Docker image was the only reason to have `make`/`awk` at all.

To regenerate `complete-demo.yaml` after touching `manifests/`, run this from the
repository root (Linux, Git Bash, WSL or CI - `awk` is what the check itself uses):

```sh
awk '{print}' deploy/kubernetes/manifests/* > deploy/kubernetes/complete-demo.yaml
```

Watch out for one upstream quirk that this command hides: `manifests/18-queue-master-svc.yaml`
has no trailing newline, and `awk '{print}'` adds one for it, which is why the
concatenation matches the committed file character for character.

## Restoring it

Copy this file back into the workflows folder:

```powershell
Copy-Item ci-legacy/main.yaml .github/workflows/main.yaml -Force
```

The archived Makefiles and the Dockerfile would have to be moved back to `./Makefile`,
`deploy/kubernetes/Makefile` and `deploy/kubernetes/Dockerfile` respectively: the paths
hard-coded inside them (`make -C deploy/kubernetes ...`) only resolve from there.
