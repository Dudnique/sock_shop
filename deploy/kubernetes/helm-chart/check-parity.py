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

  * probes - the manifests rely on `availableReplicas`, the chart on container
    probes with upstream timings
  * `nodeSelector` - every manifests/ pod pins `kubernetes.io/os: linux`, no chart
    template does. On a one-OS cluster the field is inert, so it is documented
    rather than copied into 14 templates.

Exit code is 1 on any mismatch, 0 when the two sides agree.
"""

import sys

try:
    import yaml
except ImportError:  # pragma: no cover - the workflow installs it explicitly
    sys.exit("check-parity: PyYAML is required (python3 -m pip install pyyaml)")


# Workloads the plain path deploys from a different directory, so they legitimately
# differ: `manifests-loadtest/loadtest-dep.yaml` (2 replicas, applied as its own
# step) and the chart's copy, which is off by default (`loadtest.enabled: false`).
SKIP = {"load-test"}

# Image differences that are known and deliberately not resolved yet, keyed by
# workload name: (what manifests/ ship, what the chart ships). Both sides run, they
# just run different images; deciding which one is right needs a full `-Helm` run on
# a fresh cluster, which has not been done. Everything NOT listed here is a hard
# failure, so the drift cannot grow.
KNOWN_IMAGE_DIFFS = {
    # manifests/ say `mongo` (untagged = whatever `latest` resolves to; right now
    # that is a floating tag on the live stand), the chart pins 4.4.0. Aligning the
    # chart would put `latest` into a chart; aligning the manifests would restart a
    # working carts-db on a different MongoDB major.
    "carts-db": ("mongo", "mongo:4.4.0"),
    "orders-db": ("mongo", "mongo:4.4.0"),
    # The user database schema version, not just a patch: 0.3.0 is what the `user`
    # service has been tested against here.
    "user-db": ("weaveworksdemos/user-db:0.3.0", "weaveworksdemos/user-db:0.4.0"),
}

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


def summarise(docs):
    """Map (kind, name) -> (replicas, containers, image, resources)."""
    out = {}
    for doc in docs:
        kind = doc.get("kind")
        if kind not in WORKLOAD_KINDS or doc["metadata"]["name"] in SKIP:
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
        ) = expected[key]
        (
            act_replicas,
            act_containers,
            act_image,
            act_command,
            act_args,
            act_env,
            act_res,
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
        f"(name + ports), image, command, args, env, resources; Services: type, "
        f"labels, ports, annotations; Ingress: class, labels, annotations, rules "
        f"({allowlisted} allowlisted image difference(s), "
        f"{len(ONLY_IN_MANIFESTS_OK)} object(s) only the plain path creates)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
