#!/usr/bin/env python3
"""Run every query of every Grafana dashboard and report the empty ones.

A dashboard can import cleanly and still show "No data": a renamed metric, a label
that does not exist, a service that gets no traffic. Nothing in CI sees that,
because CI has no cluster. This script is the check for a live stand:

  1. port-forwards Grafana (svc/grafana in `monitoring`) and reads the admin
     credentials from the `grafana-admin` Secret - the password is never printed;
  2. lists the dashboards and takes the targets of every panel, including panels
     nested in rows, fills in the template variables and the $__interval family;
  3. sends each query through Grafana's own datasource proxy - Prometheus targets
     as instant queries, Loki targets as a one-hour range query - so the check
     follows exactly the path the browser takes;
  4. prints one line per empty or failing query and a summary; exit code 1 if any.

Run it after a load test, not before: on a cold stand a panel for `carts` is empty
because nobody has called `carts`, which is not a regression.

    python deploy/grafana_check.py
"""
import argparse
import base64
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

INTERVALS = {"__interval": "1m", "__rate_interval": "5m", "__range": "1h",
             "__interval_ms": "60000", "__range_s": "3600", "__range_ms": "3600000"}


def kubectl(*args):
    return subprocess.run(["kubectl", *args], check=True, capture_output=True, text=True).stdout


def secret_field(name, field):
    out = kubectl("get", "secret", "-n", "monitoring", name, "-o", f"jsonpath={{.data.{field}}}")
    return base64.b64decode(out).decode()


def http_json(url, auth=None):
    req = urllib.request.Request(url)
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def variable_values(dashboard):
    """Template variable -> the value Grafana would use; "All" or unset means match-all."""
    values = {}
    for var in dashboard.get("templating", {}).get("list", []):
        cur = var.get("current", {}).get("value", "")
        if isinstance(cur, list):
            cur = "|".join(cur)
        if cur in (None, "", "$__all", "All"):
            cur = ".*"
        values[var["name"]] = str(cur)
    return values


def expand(expr, values):
    """Substitute variables. A regex value needs `=~`, and Grafana does that rewrite
    itself for multi-value variables, so do the same for `label="$var"` selectors."""
    for name, val in {**INTERVALS, **values}.items():
        n = re.escape(name)
        if re.search(r"[.*+|\[\]]", val):
            expr = re.sub(r'(\w+)\s*=\s*"(?:\$\{%s(?::[a-z]+)?\}|\$%s\b|\[\[%s\]\])"' % (n, n, n),
                          lambda m: f'{m.group(1)}=~"{val}"', expr)
        expr = re.sub(r"\$\{%s(?::[a-z]+)?\}|\$%s\b|\[\[%s\]\]" % (n, n, n), lambda m: val, expr)
    return expr


def all_panels(dashboard):
    def walk(items):
        for p in items:
            yield p
            yield from walk(p.get("panels", []))
    yield from walk(dashboard.get("panels", []))
    for row in dashboard.get("rows", []):
        yield from walk(row.get("panels", []))


def datasource_of(panel, by_name, by_uid, default):
    ds = panel.get("datasource")
    if isinstance(ds, dict):
        return by_uid.get(ds.get("uid")) or default
    if isinstance(ds, str) and not ds.startswith("$"):
        return by_name.get(ds, default)
    return default


def run_query(grafana, auth, ds, expr, now):
    base = f"{grafana}/api/datasources/proxy/{ds['id']}"
    if ds["type"] == "prometheus":
        url = f"{base}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
        return bool(http_json(url, auth)["data"]["result"])
    if ds["type"] == "loki":
        url = f"{base}/loki/api/v1/query_range?" + urllib.parse.urlencode(
            {"query": expr, "start": int((now - 3600) * 1e9), "end": int(now * 1e9), "limit": 1})
        return bool(http_json(url, auth)["data"]["result"])
    return True  # not a metrics/logs source (jaeger): nothing to evaluate


def main():
    argparse.ArgumentParser(description=__doc__,
                            formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    auth = f"{secret_field('grafana-admin', 'admin-user')}:{secret_field('grafana-admin', 'admin-password')}"
    port = 13000
    pf = subprocess.Popen(["kubectl", "port-forward", "-n", "monitoring", "svc/grafana", f"{port}:80"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    graf = f"http://localhost:{port}"
    try:
        for _ in range(30):
            try:
                http_json(graf + "/api/health")
                break
            except (urllib.error.URLError, OSError):
                time.sleep(1)
        else:
            sys.exit("Grafana did not answer on the port-forward")

        sources = http_json(graf + "/api/datasources", auth)
        by_name = {d["name"]: d for d in sources}
        by_uid = {d["uid"]: d for d in sources}
        default = next(d for d in sources if d["type"] == "prometheus")
        now = time.time()

        total = empty = 0
        for item in http_json(graf + "/api/search?type=dash-db", auth):
            dash = http_json(graf + "/api/dashboards/uid/" + item["uid"], auth)["dashboard"]
            values = variable_values(dash)
            count = bad = 0
            for panel in all_panels(dash):
                ds = datasource_of(panel, by_name, by_uid, default)
                for target in panel.get("targets", []):
                    if not target.get("expr"):
                        continue
                    count += 1
                    q = expand(target["expr"], values)
                    try:
                        ok = run_query(graf, auth, ds, q, now)
                    except (urllib.error.URLError, KeyError) as exc:
                        print(f"ERROR  [{dash['title']}] {panel.get('title')!r}: {exc}")
                        bad += 1
                        continue
                    if not ok:
                        bad += 1
                        print(f"EMPTY  [{dash['title']}] {panel.get('title')!r}: {q[:110]}")
            print(f"  {dash['title']}: {count} queries, {bad} empty")
            total += count
            empty += bad
        print(f"\n{total} queries, {empty} empty")
        return 1 if empty else 0
    finally:
        pf.terminate()


if __name__ == "__main__":
    sys.exit(main())
