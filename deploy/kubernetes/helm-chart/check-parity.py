#!/usr/bin/env python3
"""Compare the chart with the raw manifests, object by object.

`manifests/` and `deploy/kubernetes/helm-chart` are the same application written
twice (see "The Helm path" in the root README). `helm lint` and `kubeconform`
prove that each side renders and validates on its own; neither says anything about
the two sides agreeing, and that is precisely how the chart drifted: it asked for
2000Mi of memory on each of the four JVM services - ~10 GiB of requests in total,
out of 8 GB - while the manifests, and everything measured on the stand, were
tuned to 200Mi-300Mi. CI was green the whole time.

Usage (exactly what the workflow runs). The release namespace is not cosmetic:
the chart builds service addresses from `.Release.Namespace` (catalogue's `-DSN`,
user's `-mongo-host`), so rendering without `--namespace sock-shop` compares a pod
that is never deployed against the one that is.

    helm template sock-shop deploy/kubernetes/helm-chart --namespace sock-shop \
        > rendered.yaml
    python3 deploy/kubernetes/helm-chart/check-parity.py rendered.yaml \
        deploy/kubernetes/manifests

What is compared, per workload:

  * `spec.replicas`
  * every container of the pod: its `name` and its `containerPort`s - every
    container, not `containers[0]`. The chart's RabbitMQ had lost its
    `rabbitmq-exporter` sidecar and the earlier version of this script could not
    have noticed, because it only ever looked at the first container.
  * the first container's `image`
  * the first container's `command`, `args` and `env` (name/value pairs, order
    ignored) - this is where `-decline=200`, `-mongo-host=...<namespace>...` and
    the `PermSize` flags lived, all invisible to every other check in CI
  * the first container's `resources` (requests and limits, as whole dicts)
  * every container's `volumeMounts` (`mountPath` + volume name) and the pod's
    `volumes` (name + the whole volume source, so `emptyDir` and
    `emptyDir: {medium: Memory}` are different pods). This is the field that
    section 25 needed: the `session-db` fix *is* a mount, and until this was
    compared the gate would happily have accepted the fix in one file and not the
    other - the same shape of blindness that let the read-only Redis live on.
  * every container's `livenessProbe` and `readinessProbe`, as written. Probes
    used to be excluded here on the theory that the manifests "rely on
    availableReplicas" - they do not, `availableReplicas` is Ready pods, and Ready
    is exactly what a probe decides. The chart declared probes for carts, orders,
    queue-master and shipping; the manifests declared none, so those four joined
    their Services while the JVM was still starting. On a fresh plain-path stand
    the load generator's `cart accepts item` check fell to 45% with
    `connect ECONNREFUSED 10.43.169.202:80` from the front-end, and the same
    requests passed 30 of 30 once the pods had warmed up. That asymmetry is what
    this field now catches.

Beyond the workloads, the objects that carry the app's network contract:

  * every `Service`: `type`, `labels`, `ports` (name, port, targetPort, nodePort) and
    `annotations` - the annotations are not decoration. Prometheus discovers targets
    through `prometheus.io/scrape` on the Service (role: endpoints in
    manifests-monitoring/04-prometheus-configmap.yaml), so a chart that drops the
    annotation silently drops the metrics. The chart used to drop it on seven
    Services and, on queue-master, to announce `prometheus.io/path: /prometheus` for
    a service that has no Prometheus endpoint at all.
  * the `Ingress`: `ingressClassName`, `labels`, `annotations` and every rule (host,
    path, pathType, backend service name and port).
  * the object inventory: a kind/name that exists on one side and not the other is a
    failure, except for the release `Namespace` - `deploy.ps1 -Helm` installs with
    `--create-namespace`, so the chart renders no Namespace and never should.

Deliberately not compared (each one is a documented decision in FIXES.md
section 23, and a gate that compared them would be red for reasons nobody intends
to change):

  * `nodeSelector` - every manifests/ pod pins `kubernetes.io/os: linux`, no chart
    template does. On a one-OS cluster the field is inert, so it is documented
    rather than copied into 14 templates.
  * the load generator - it is not an object of the application at all: a test
    harness in its own `loadtest` namespace, applied by `deploy.ps1 -WithLoadtest`
    on both paths (`manifests-loadtest/loadtest-dep.yaml`). The chart used to render
    a second copy behind `loadtest.enabled: false` that mounted no ConfigMap, so it
    could not have worked; it was removed rather than kept in sync.

Exit code is 1 on any mismatch, 0 when the two sides agree.
"""

import sys

try:
    import yaml
except ImportError:  # pragma: no cover - the workflow installs it explicitly
    sys.exit("check-parity: PyYAML is required (python3 -m pip install pyyaml)")


# Image differences that are known and deliberately not resolved, keyed by
# workload name: (what manifests/ ship, what the chart ships). Empty on purpose:
# the three entries that used to live here (`carts-db` and `orders-db` on untagged
# `mongo` vs `mongo:4.4.0`, `user-db` on 0.3.0 vs 0.4.0) were all closed by pointing
# the manifests at the version the chart deploys. That direction was chosen because
# the chart is what actually ran: the first `-Helm` run stored a cart item and an
# order in `mongo:4.4.0` and registered a customer against `user-db:0.4.0`, so the
# pinned side is the proven side, while the manifests' side had never been exercised
# on this stand. The dict stays so that a future difference has an explicit,
# reviewable place to live instead of being tolerated silently - anything NOT listed
# here is a hard failure.
KNOWN_IMAGE_DIFFS = {}

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet"}

# Objects the plain path creates that the chart legitimately does not: the release
# namespace itself, because `deploy.ps1 -Helm` passes `--create-namespace`.
ONLY_IN_MANIFESTS_OK = {("Namespace", "sock-shop")}


def load_rendered(path):
    """Helm writes UTF-8; PowerShell's `>` writes UTF-16. Accept both."""
    with open(path, "rb") as handle:
        raw = handle.read()
    text = None
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        sys.exit(f"check-parity: cannot decode {path} as UTF-8 or UTF-16")
    return [doc for doc in yaml.safe_load_all(text) if isinstance(doc, dict)]


def probe_signature(container):
    """`livenessProbe`/`readinessProbe` of one container, exactly as written.

    These are not health decoration: they decide when a pod receives traffic. The
    gate used to skip them ("the manifests rely on availableReplicas"), and that
    is how the two paths came to differ where it hurts most - the chart declared
    probes for carts, orders, queue-master and shipping, the manifests declared
    none, so on a fresh plain-path stand those four pods joined their Services
    while the JVM was still starting. The front-end answered `POST /cart` with
    500 `connect ECONNREFUSED 10.43.169.202:80` for roughly half the requests
    until the pods warmed up; the load generator's `checks` threshold caught it.
    """
    return tuple(
        (name, yaml.safe_dump(container.get(name), sort_keys=True))
        for name in ("livenessProbe", "readinessProbe")
        if container.get(name)
    )


def summarise(docs):
    """Map (kind, name) -> (replicas, containers, image, resources)."""
    out = {}
    for doc in docs:
        kind = doc.get("kind")
        if kind not in WORKLOAD_KINDS:
            continue
        spec = doc["spec"]
        containers = spec["template"]["spec"]["containers"]
        first = containers[0]
        out[(kind, doc["metadata"]["name"])] = (
            spec.get("replicas"),
            # Every container, by name and with its port numbers: a name change
            # or a lost sidecar is a different pod, not a formatting detail.
            tuple(
                (
                    container.get("name"),
                    tuple(
                        sorted(
                            port["containerPort"]
                            for port in (container.get("ports") or [])
                            if port.get("containerPort") is not None
                        )
                    ),
                )
                for container in containers
            ),
            first.get("image"),
            # command/args/env of the first container: these decide which binary
            # runs and which database it talks to. The chart used to pass
            # `-decline=200` where the manifests pass nothing, and no schema check
            # can see that; `env` is compared as a sorted (name, value) list, so
            # only content is compared, not the order it was written in.
            tuple(first.get("command") or ()),
            tuple(first.get("args") or ()),
            tuple(
                sorted(
                    (item.get("name"), str(item.get("value")))
                    for item in (first.get("env") or [])
                )
            ),
            first.get("resources") or {},
            # Mounts, per container, and the volumes behind them. A pod that mounts
            # a writable directory where the other side does not is a different pod
            # with a different runtime behaviour - `session-db` with a read-only
            # root filesystem and no `/data` mount answered every write with
            # `MISCONF` while both sides "agreed" on image, ports, env and limits.
            tuple(
                sorted(
                    (container.get("name"), mount.get("mountPath"), mount.get("name"))
                    for container in containers
                    for mount in (container.get("volumeMounts") or [])
                )
            ),
            tuple(
                sorted(
                    (volume.get("name"), yaml.safe_dump(volume, sort_keys=True))
                    for volume in (spec["template"]["spec"].get("volumes") or [])
                )
            ),
            # Probes, per container. Same argument as the mounts above: two pods
            # that advertise health differently are two different pods to the
            # Service, whatever the Deployment says.
            tuple(
                sorted(
                    (container.get("name"), probe_signature(container))
                    for container in containers
                )
            ),
        )
    return out


def summarise_services(docs):
    """Map ('Service', name) -> (type, labels, ports, annotations)."""
    out = {}
    for doc in docs:
        if doc.get("kind") != "Service":
            continue
        meta = doc["metadata"]
        spec = doc.get("spec") or {}
        out[("Service", meta["name"])] = (
            spec.get("type"),
            meta.get("labels") or {},
            # Written as a sorted tuple so a reordering in the YAML is not a diff,
            # while a changed port or targetPort is. `targetPort` is stringified
            # because the chart renders it either as 80 or as a port *name*.
            tuple(
                sorted(
                    (
                        port.get("name"),
                        port.get("port"),
                        str(port.get("targetPort")),
                        port.get("nodePort"),
                    )
                    for port in (spec.get("ports") or [])
                )
            ),
            meta.get("annotations") or {},
        )
    return out


def summarise_ingress(docs):
    """Map ('Ingress', name) -> (className, labels, annotations, rules)."""
    out = {}
    for doc in docs:
        if doc.get("kind") != "Ingress":
            continue
        meta = doc["metadata"]
        spec = doc.get("spec") or {}
        rules = []
        for rule in spec.get("rules") or []:
            for path in ((rule.get("http") or {}).get("paths") or []):
                service = (path.get("backend") or {}).get("service") or {}
                rules.append(
                    (
                        rule.get("host"),
                        path.get("path"),
                        path.get("pathType"),
                        service.get("name"),
                        (service.get("port") or {}).get("number"),
                    )
                )
        out[("Ingress", meta["name"])] = (
            spec.get("ingressClassName"),
            meta.get("labels") or {},
            meta.get("annotations") or {},
            tuple(rules),
        )
    return out


def inventory(docs):
    """Every (kind, name) a side renders, for the presence check."""
    return {
        (doc.get("kind"), doc["metadata"]["name"])
        for doc in docs
        if doc.get("kind") and doc.get("metadata", {}).get("name")
    }


def main(argv):
    import glob
    import os

    if len(argv) != 3:
        sys.exit(f"usage: {argv[0]} <rendered.yaml> <manifests-dir>")
    rendered_path, manifests_dir = argv[1], argv[2]

    manifests = []
    for path in sorted(glob.glob(os.path.join(manifests_dir, "*"))):
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                manifests.extend(
                    doc
                    for doc in yaml.safe_load_all(handle)
                    if isinstance(doc, dict)
                )

    rendered = load_rendered(rendered_path)

    expected = summarise(manifests)
    actual = summarise(rendered)

    problems = []

    # Presence, for every kind the two sides produce - not just the workloads, so a
    # Service that only one side creates is caught too. The release Namespace is the
    # one allowed asymmetry (`--create-namespace`).
    only_plain = inventory(manifests) - inventory(rendered) - ONLY_IN_MANIFESTS_OK
    for kind, name in sorted(only_plain):
        problems.append(f"{kind}/{name}: exists in manifests/, the chart renders no such object")
    for kind, name in sorted(inventory(rendered) - inventory(manifests)):
        problems.append(f"{kind}/{name}: rendered by the chart, absent from manifests/")

    compared = 0
    for key in sorted(set(expected) & set(actual)):
        compared += 1
        name = key[1]
        (
            exp_replicas,
            exp_containers,
            exp_image,
            exp_command,
            exp_args,
            exp_env,
            exp_res,
            exp_mounts,
            exp_volumes,
            exp_probes,
        ) = expected[key]
        (
            act_replicas,
            act_containers,
            act_image,
            act_command,
            act_args,
            act_env,
            act_res,
            act_mounts,
            act_volumes,
            act_probes,
        ) = actual[key]

        if exp_replicas != act_replicas:
            problems.append(
                f"{key[0]}/{name}: replicas {act_replicas} in the chart, "
                f"{exp_replicas} in manifests/"
            )

        if exp_containers != act_containers:
            problems.append(
                f"{key[0]}/{name}: containers differ (name, containerPorts)\n"
                f"    manifests/: {exp_containers}\n"
                f"    chart     : {act_containers}"
            )

        if exp_command != act_command:
            problems.append(
                f"{key[0]}/{name}: command differs\n"
                f"    manifests/: {list(exp_command)}\n"
                f"    chart     : {list(act_command)}"
            )

        if exp_args != act_args:
            problems.append(
                f"{key[0]}/{name}: args differ\n"
                f"    manifests/: {list(exp_args)}\n"
                f"    chart     : {list(act_args)}"
            )

        if exp_env != act_env:
            problems.append(
                f"{key[0]}/{name}: env differs\n"
                f"    manifests/: {list(exp_env)}\n"
                f"    chart     : {list(act_env)}"
            )

        if exp_res != act_res:
            problems.append(
                f"{key[0]}/{name}: resources differ\n"
                f"    manifests/: {exp_res}\n"
                f"    chart     : {act_res}"
            )

        if exp_mounts != act_mounts:
            problems.append(
                f"{key[0]}/{name}: volumeMounts differ (container, mountPath, volume)\n"
                f"    manifests/: {list(exp_mounts)}\n"
                f"    chart     : {list(act_mounts)}"
            )

        if exp_volumes != act_volumes:
            problems.append(
                f"{key[0]}/{name}: volumes differ (name, source)\n"
                f"    manifests/: {[n for n, _ in exp_volumes]}\n"
                f"    chart     : {[n for n, _ in act_volumes]}\n"
                f"    manifests/ sources:\n{chr(10).join(source for _, source in exp_volumes)}\n"
                f"    chart sources:\n{chr(10).join(source for _, source in act_volumes)}"
            )

        if exp_probes != act_probes:
            problems.append(
                f"{key[0]}/{name}: probes differ (container, livenessProbe/readinessProbe) - "
                f"this is what decides when the pod receives traffic\n"
                f"    manifests/: {list(exp_probes)}\n"
                f"    chart     : {list(act_probes)}"
            )

        if exp_image != act_image:
            known = KNOWN_IMAGE_DIFFS.get(name)
            if known is None:
                problems.append(
                    f"{key[0]}/{name}: image {act_image!r} in the chart, "
                    f"{exp_image!r} in manifests/"
                )
            elif (exp_image, act_image) != known:
                problems.append(
                    f"{key[0]}/{name}: image {act_image!r} in the chart, "
                    f"{exp_image!r} in manifests/ - the allowlisted difference is "
                    f"{known[1]!r} vs {known[0]!r}, so this is a new one"
                )

    # --- Services: the network contract -------------------------------------
    exp_svc = summarise_services(manifests)
    act_svc = summarise_services(rendered)

    compared_svc = 0
    for key in sorted(set(exp_svc) & set(act_svc)):
        compared_svc += 1
        exp_type, exp_labels, exp_ports, exp_annotations = exp_svc[key]
        act_type, act_labels, act_ports, act_annotations = act_svc[key]
        name = key[1]

        if exp_type != act_type:
            problems.append(
                f"Service/{name}: type {act_type!r} in the chart, {exp_type!r} in manifests/"
            )
        if exp_labels != act_labels:
            problems.append(
                f"Service/{name}: labels differ\n"
                f"    manifests/: {exp_labels}\n"
                f"    chart     : {act_labels}"
            )
        if exp_ports != act_ports:
            problems.append(
                f"Service/{name}: ports differ (name, port, targetPort, nodePort)\n"
                f"    manifests/: {list(exp_ports)}\n"
                f"    chart     : {list(act_ports)}"
            )
        if exp_annotations != act_annotations:
            problems.append(
                f"Service/{name}: annotations differ - for this stack they decide what "
                f"Prometheus scrapes\n"
                f"    manifests/: {exp_annotations}\n"
                f"    chart     : {act_annotations}"
            )

    # --- Ingress: the only way into the app ---------------------------------
    exp_ing = summarise_ingress(manifests)
    act_ing = summarise_ingress(rendered)

    compared_ing = 0
    for key in sorted(set(exp_ing) & set(act_ing)):
        compared_ing += 1
        name = key[1]
        for field, exp_value, act_value in zip(
            ("ingressClassName", "labels", "annotations", "rules"),
            exp_ing[key],
            act_ing[key],
        ):
            if exp_value != act_value:
                problems.append(
                    f"Ingress/{name}: {field} differ\n"
                    f"    manifests/: {exp_value}\n"
                    f"    chart     : {act_value}"
                )

    if problems:
        print(f"chart and manifests/ disagree ({len(problems)} problem(s)):")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    allowlisted = len(
        [k for k in set(expected) & set(actual) if k[1] in KNOWN_IMAGE_DIFFS]
    )
    print(
        f"chart and manifests/ agree on {compared} workload(s), {compared_svc} "
        f"Service(s) and {compared_ing} Ingress(es) - workloads: replicas, containers "
        f"(name + ports), image, command, args, env, resources, volumeMounts, "
        f"volumes, probes; Services: type, labels, ports, annotations; Ingress: class, "
        f"labels, annotations, rules "
        f"({allowlisted} allowlisted image difference(s), "
        f"{len(ONLY_IN_MANIFESTS_OK)} object(s) only the plain path creates)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
