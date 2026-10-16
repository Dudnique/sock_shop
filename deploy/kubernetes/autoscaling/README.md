# Autoscaling for the sock-shop namespace

> **English** | [Русский](README.ru.md)

Eight `HorizontalPodAutoscaler` objects - one per stateless service - on the
modern `autoscaling/v2` API.

## What was wrong before

The directory existed, every file was valid YAML, and **not a single
autoscaler was running**. Four separate defects, three of them present in the
2018 manifests and one that only surfaced once the objects were actually
applied:

| Defect | Consequence |
|---|---|
| `apiVersion: autoscaling/v1` | No `behavior`: the controller reacts to every blip - adds a replica, sees the average drop, removes it 15 s later. v1 cannot express scaling policies at all. |
| `scaleTargetRef.apiVersion: apps/v1beta1` | Removed in Kubernetes 1.16, so on this 1.35 cluster the HPA cannot resolve its target. |
| never applied by `deploy.ps1` | `kubectl get hpa -A` was empty: the repository advertised autoscaling the cluster did not have. Worse than none, because it looks done. |
| the HPA `cart` pointed at `Deployment/cart`, the Deployment is `carts` | `kubectl get hpa` showed `cpu: <unknown>/50%` forever - an autoscaler that can never fire. Found by reading `TARGETS` instead of trusting the apply output. |

## Files

| File | Target | Replicas |
|---|---|---|
| `carts-hsc.yaml` | `carts` | 1..5 |
| `catalogue-hsc.yaml` | `catalogue` | 1..5 |
| `front-end-hsc.yaml` | `front-end` | 1..5 |
| `orders-hsc.yaml` | `orders` | 1..5 |
| `payment-hsc.yaml` | `payment` | 1..5 |
| `queue-master-hsc.yaml` | `queue-master` | 1..5 |
| `shipping-hsc.yaml` | `shipping` | 1..5 |
| `user-hsc.yaml` | `user` | 1..5 |

Databases (`*-db`, `rabbitmq`, `session-db`) are deliberately NOT autoscaled:
scaling a stateful store on this stand would mean multiple writers over an
`emptyDir`, i.e. data loss dressed up as elasticity.

## Why these numbers

- **CPU 50% of `requests.cpu`.** Every service here requests 100m, so replicas
  are added from ~50m of real usage. A percentage of the request is the only
  target that means the same thing across pods and nodes.
- **`minReplicas: 1`** - the stand runs on a laptop; a floor of 2 doubles the
  footprint for a demo nobody is watching.
- **`maxReplicas: 5`** - a node-budget decision, not a round number: eight
  services x 10 replicas would be 80 pods on three k3d nodes, which measures the
  nodes rather than the application.
- **`behavior`**: up 2 pods / 30 s, down 1 pod / 60 s behind a 300 s
  stabilization window. Without it you get the classic flap.

## Prerequisites (both checked by `deploy.ps1`)

1. **metrics-server** - HPAs read usage from `metrics.k8s.io`. k3s ships it
   (`kubectl top nodes` answering is the proof); the deploy script waits for the
   first HPA metric and fails loudly if it never arrives.
2. **`requests.cpu`** on the target pods - a percentage of nothing is nothing,
   and without requests the HPA reports `FailedGetResourceMetric`.

## How to watch it scale

Two terminals, both worth showing:

```powershell
kubectl -n sock-shop get hpa -w
kubectl -n sock-shop get pods -l name=front-end -w
```

Then start the optional load generator (`.\deploy.ps1 -WithLoadtest`). Those five
locust clients keep `front-end` at ~3m of CPU - honest traffic, but that is **3%
of its 100m request**, nowhere near the 50% target, so the HPA correctly stays at
one replica. To watch the mechanism work on that same real traffic, narrow the
*request* instead of inventing load (a demo aid only: the shipped manifest keeps
100m):

```powershell
# 1. temporary: let the real traffic exceed the target - no extra load is added
'{"spec":{"template":{"spec":{"containers":[{"name":"front-end","resources":{"requests":{"cpu":"1m"}}}]}}}}' |
  Set-Content -Encoding UTF8 $env:TEMP\fe-req-1m.json
kubectl -n sock-shop patch deploy front-end --type=strategic --patch-file $env:TEMP\fe-req-1m.json

# 2. watch the other terminal, then put the request back
'{"spec":{"template":{"spec":{"containers":[{"name":"front-end","resources":{"requests":{"cpu":"100m"}}}]}}}}' |
  Set-Content -Encoding UTF8 $env:TEMP\fe-req-100m.json
kubectl -n sock-shop patch deploy front-end --type=strategic --patch-file $env:TEMP\fe-req-100m.json
```

Measured on this stand with nothing but the load generator running:

```
t=40s    cpu: 300%/50%   replicas: 1   pods: 1
t=60s    cpu: 200%/50%   replicas: 3   pods: 3
t=80s    cpu: 300%/50%   replicas: 3   pods: 4
t=100s   cpu: 200%/50%   replicas: 4   pods: 4
```

The per-pod average falls as the load spreads over the new replicas - that is the
point of the exercise - and once the request is restored the HPA walks back down
to one replica (1 pod per 60 s, behind the 300 s stabilization window).

`kubectl -n sock-shop describe hpa front-end` says it in words:
`New size: 3; reason: cpu resource utilization (percentage of request) above target`.

The same data in Prometheus or Grafana (kube-state-metrics is already scraped):

```
kube_deployment_status_replicas{namespace="sock-shop", deployment="front-end"}
rate(container_cpu_usage_seconds_total{namespace="sock-shop", pod=~"front-end.*"}[1m])
```

None of this shows up in Docker Desktop: k3d runs three *node* containers and
every pod lives inside them.

## Traps

- **`kubectl apply` prints `created` even for an autoscaler that can never
  work.** Only `TARGETS` and the events of `kubectl describe hpa` show whether
  it is wired to a real workload; `<unknown>` means it is not.
- **A probe pod carrying a workload's labels joins that workload.** The first
  version of `manifests-policy/verify-netpol.ps1` ran a pod labelled
  `name=carts`, and the `carts` HPA counted it as one of its own pods:
  `FailedGetResourceMetric ... missing request for cpu in container
  netpol-check-3`. Labels are identity for everything that selects pods.
- **`kubectl apply -f <dir>` does not recurse**, so the archived stack under
  `legacy/` is never applied by accident - see `legacy/README.md`.
