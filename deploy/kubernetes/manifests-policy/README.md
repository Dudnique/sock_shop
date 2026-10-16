# Network policies for the `sock-shop` namespace

> **English** | [Русский](README.ru.md)

These manifests are a firewall for pods, not a schema exercise: they decide who
may talk to whom inside the namespace. Before the rewrite this directory was
the perfect example of "security that is not there": it contained a Namespace
object with the pre-1.7 annotation `net.beta.kubernetes.io/network-policy`,
which modern clusters ignore, plus a set of per-service rules with no
`policyTypes` and no `egress` at all. The stand looked segmented in `kubectl
get -f` and was wide open in reality - any pod could reach any pod, including
every database and Redis, which has no password here.

## Files

| File | What it protects | May call |
|---|---|---|
| `00-default-deny.yaml` | the namespace baseline | nobody - until a rule below allows it |
| `01-platform-traffic.yaml` | platform egress for all pods | DNS (`kube-system:53/udp+tcp`), Jaeger (`jaeger:9411`) |
| `02-observability-and-probes.yaml` | metrics | Prometheus (`monitoring`) on 80/8079/9090 |
| `10-front-end.yaml` | `front-end` | in: Traefik (`kube-system`), `loadtest` - out: carts, catalogue, orders, user, session-db |
| `11-carts.yaml` | `carts` + `carts-db` | in: front-end, orders - out: carts-db:27017 |
| `12-catalogue.yaml` | `catalogue` + `catalogue-db` | in: front-end - out: catalogue-db:3306 |
| `13-orders.yaml` | `orders` + `orders-db` | in: front-end - out: orders-db:27017, payment, shipping, user |
| `14-user.yaml` | `user` + `user-db` | in: front-end, orders - out: user-db:27017 |
| `15-payment.yaml` | `payment` | in: orders - out: nothing |
| `16-shipping.yaml` | `shipping` | in: orders - out: rabbitmq:5672 |
| `17-queue-master.yaml` | `queue-master` | in: nothing - out: rabbitmq:5672 |
| `18-session-db.yaml` | `session-db` (Redis) | in: front-end:6379 |
| `19-rabbitmq.yaml` | `rabbitmq` | in: shipping, queue-master:5672 |

Policy objects are **additive**: Kubernetes unions all rules that select a pod.
Two consequences worth remembering:

- one more policy never makes the namespace stricter, it only opens holes, so
  the old upstream set could not be kept "just in case" while the new one was
  written;
- `default-deny` works because it denies everything and every other file adds a
  specific hole back.

## How it is applied

`deploy.ps1` applies this directory as its own step (after the application
manifests, before monitoring), so a plain `.\deploy.ps1` gives a segmented
namespace. CI validates the syntax of every file with `kubeconform`.

## How it is verified

`verify-netpol.ps1` in this directory runs all the checks below in one go: it
creates throwaway probe pods, reads their verdict and deletes them again, so
nothing is left behind. Measured on this stand:

```
[OK  ] default namespace -> front-end.sock-shop:80 (must NOT be reachable)
       expected blocked, observed blocked   (rc=1)
[OK  ] monitoring (Prometheus) -> front-end.sock-shop:80 (scrape path, MUST work)
       expected reachable, observed reachable   (rc=0)
[OK  ] monitoring (Prometheus) -> catalogue.sock-shop:80/metrics (application metrics, MUST work)
       expected reachable, observed reachable   (rc=0)
[OK  ] front-end -> carts:80 (the UI must reach the basket)
       expected reachable, observed reachable   (OPEN)
[OK  ] front-end -> session-db:6379 (session store, SESSION_REDIS=true)
       expected reachable, observed reachable   (OPEN)
[OK  ] front-end -> user-db:27017 (a database it has no business in, must NOT be reachable)
       expected blocked, observed blocked   (ERROR ECONNREFUSED)

All 6 checks passed.
```

The two pairs are the point: a namespace with no business in `sock-shop` cannot
even open an HTTP session to the UI, while the front-end pod cannot open a TCP
session to somebody else's database - and the same pod still reaches its own
dependencies, so the policy cuts exactly one lane and not the application.

Metrics are the third check: if a policy forgot the scrape path, `up == 0`
appears for the application jobs within a minute and `ScrapeTargetDown` fires.

## Traps this rewrite ran into

- **DNS is egress.** With `policyTypes: [Egress]` and no DNS rule every service
  lookup fails with "no such host" and it reads as an application bug.
- **kubelet probes are NOT blocked here** - and that is a measurement, not a
  guess. The first version of `02-observability-and-probes.yaml` allowed the
  node network (`172.19.0.0/16`) "because probes cannot be expressed with a
  podSelector". The rule was then removed and the namespace watched: every pod
  stayed `1/1 Ready`, no `Unhealthy` events, `up == 0` targets still zero.
  k3s runs kube-router, which keeps node-originated traffic out of policy
  evaluation, so the rule was deleted instead of kept "just in case". On a CNI
  that does police kubelet traffic (some Calico setups) it must come back - the
  comment in the file says so.
- **A wrong port is a silent outage**: `catalogue-db` is MySQL (3306) while the
  other three databases are Mongo (27017). Every port in these files is copied
  from a Service definition.
- **`namespaceSelector` needs `kubernetes.io/metadata.name`**, the label
  Kubernetes adds automatically to every namespace since 1.21 - it is how
  `monitoring`, `kube-system`, `jaeger` and `loadtest` are matched.
