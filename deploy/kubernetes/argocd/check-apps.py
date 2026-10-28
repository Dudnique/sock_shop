#!/usr/bin/env python3
"""Fail when deploy.ps1 and the Argo CD app-of-apps describe different stands.

deploy.ps1 applies a list of directories; apps/values.yaml lists the Argo CD
Applications for the GitOps mode. They are two descriptions of one stand, and
two descriptions drift: somebody adds `manifests-foo` to the script and forgets
the Application, and the stand deployed with -GitOps silently lacks a component.

Checks (every failure is printed, the exit code is 1 if there is any):
  1. every directory deploy.ps1 applies has an Application with that path;
  2. every Application path is applied by the script (no orphans) - except the
     entries marked `gitopsOnly` (Vault, External Secrets, the ExternalSecret
     objects), which exist only in this mode and which the script never applies;
  3. every path exists and holds YAML;
  4. an entry with a `chart` (a Helm chart from a repository) names its repo, its
     chart and an exact version - never a range and never `latest`;
  5. names are unique, waves are integers, `flag` names a top-level toggle.

`manifests/` and `helm-chart/` are the two interchangeable ways to create the
same application (deploy.ps1 -Helm picks one); the Application tracks the chart,
and the parity gate (helm-chart/check-parity.py) is what says the two are equal.

    python3 deploy/kubernetes/argocd/check-apps.py
"""
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "deploy.ps1"
VALUES = ROOT / "deploy" / "kubernetes" / "argocd" / "apps" / "values.yaml"

#: A directory the script applies on one of the two interchangeable paths.
EQUIVALENT = {"deploy/kubernetes/manifests": "deploy/kubernetes/helm-chart"}

#: Directories the script reads but that are not part of the stand: Argo CD itself
#: (its values and the app-of-apps live here) is the thing that deploys the rest.
NOT_APPLICATIONS = {"deploy/kubernetes/argocd"}

#: `kubectl apply -f (Join-Path $PSScriptRoot "deploy\kubernetes\<dir>")` and the
#: Helm chart path. A path ending in .yaml is one file (a namespace), not a step.
APPLIED = re.compile(r'Join-Path \$PSScriptRoot "(deploy\\kubernetes\\[A-Za-z0-9_-]+)"')


def applied_directories(text: str) -> set:
    found = set()
    for match in APPLIED.finditer(text):
        path = match.group(1).replace("\\", "/")
        if path not in NOT_APPLICATIONS:
            found.add(EQUIVALENT.get(path, path))
    return found


def main() -> int:
    errors: list = []
    script_dirs = applied_directories(SCRIPT.read_text(encoding="utf-8"))
    values = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
    apps = values.get("apps") or []

    app_paths = {app["path"] for app in apps if "path" in app and not app.get("gitopsOnly")}
    for path in sorted(script_dirs - app_paths):
        errors.append(f"deploy.ps1 applies {path} but no Application has that path")
    for path in sorted(app_paths - script_dirs):
        errors.append(f"Application path {path} is not applied by deploy.ps1")

    names = [app["name"] for app in apps]
    for name in sorted({n for n in names if names.count(n) > 1}):
        errors.append(f"Application name {name!r} is used more than once")
    for app in apps:
        if "path" in app:
            directory = ROOT / app["path"]
            if not directory.is_dir() or not any(directory.glob("*.y*ml")):
                errors.append(f"{app['name']}: {app['path']} does not exist or holds no YAML")
        elif "chart" in app:
            chart = app["chart"]
            for key in ("repoURL", "name", "version"):
                if not chart.get(key):
                    errors.append(f"{app['name']}: chart.{key} is missing")
            if not re.fullmatch(r"\d+\.\d+\.\d+", str(chart.get("version", ""))):
                errors.append(f"{app['name']}: chart.version {chart.get('version')!r} is not an exact x.y.z")
        else:
            errors.append(f"{app['name']}: needs either `path` or `chart`")
        if app.get("gitopsOnly") is not True and "chart" in app:
            errors.append(f"{app['name']}: a chart entry is not applied by deploy.ps1, so it must be gitopsOnly")
        if not isinstance(app.get("wave"), int):
            errors.append(f"{app['name']}: wave must be an integer")
        flag = app.get("flag")
        if flag and not isinstance(values.get(flag), dict):
            errors.append(f"{app['name']}: flag {flag!r} has no toggle at the top of values.yaml")

    for line in errors:
        print("FAIL:", line)
    print(f"{len(apps)} Applications, {len(script_dirs)} directories applied by deploy.ps1, "
          f"{len(errors)} problem(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
