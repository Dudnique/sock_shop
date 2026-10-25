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
  * the first container's `resources` (requests and limits, as whole dicts)

Deliberately not compared (each one is a documented decision in FIXES.md
section 23, and a gate that compared them would be red for reasons nobody intends
to change):

  * probes - the manifests rely on `availableReplicas`, the chart on container
    probes with upstream timings
  * `nodeSelector`, `env`, `command`/`args` - still drifting; the second column of
    section 23 lists them. They join the gate in the same commit that reconciles
    them, not before.

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
            first.get("resources") or {},
        )
    return out


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

    expected = summarise(manifests)
    actual = summarise(load_rendered(rendered_path))

    problems = []
    for key in sorted(set(expected) - set(actual)):
        problems.append(f"{key[0]}/{key[1]}: missing from the chart")
    for key in sorted(set(actual) - set(expected)):
        problems.append(
            f"{key[0]}/{key[1]}: rendered by the chart, absent from manifests/"
        )

    compared = 0
    for key in sorted(set(expected) & set(actual)):
        compared += 1
        name = key[1]
        exp_replicas, exp_containers, exp_image, exp_res = expected[key]
        act_replicas, act_containers, act_image, act_res = actual[key]

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

    if problems:
        print(f"chart and manifests/ disagree ({len(problems)} problem(s)):")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    allowlisted = len(
        [k for k in set(expected) & set(actual) if k[1] in KNOWN_IMAGE_DIFFS]
    )
    print(
        f"chart and manifests/ agree on {compared} workload(s): replicas, containers "
        f"(name + ports), resources and image "
        f"({allowlisted} allowlisted image difference(s))"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
