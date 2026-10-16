# ---------------------------------------------------------------------------
# deploy.ps1 - one-command bring-up / teardown of the Sock Shop stand.
#
# Thin ORCHESTRATOR. It creates no infrastructure itself:
#   1. Terraform provisions the k3d cluster  (single source of truth for infra).
#   2. kubectl deploys the application        (manifests).
#   3. kubectl deploys the logging stack      (manifests-logging: Loki + Promtail).
#   4. kubectl deploys the tracing stack      (manifests-jaeger).
#   5. kubectl deploys the monitoring stack   (manifests-monitoring).
#   6. kubectl deploys the alerting stack     (manifests-alerting) + secrets.
#   7. (optional, -WithLoadtest) the load generator (manifests-loadtest).
#   It prints the resulting URLs.
#
# The cluster is deliberately NOT created with `k3d cluster create` here: that
# would duplicate terraform/k3d and let the two definitions drift apart.
#
# SECRETS: the alerting stack needs two secrets - a Telegram bot token and a
# Slack incoming-webhook URL. They are NEVER committed. Provide them either as
# environment variables (TELEGRAM_BOT_TOKEN, SLACK_WEBHOOK_URL) or in a local,
# git-ignored file: deploy\secrets.local.ps1
# The monitoring stack needs one more: the Grafana admin password. If
# GRAFANA_ADMIN_PASSWORD is not set the script generates a random one and reuses
# it on later runs (see the monitoring section).
#
# NOTE: this file is intentionally ASCII-only. On Windows PowerShell 5.1 a
# UTF-8-without-BOM script is decoded as the system ANSI codepage, which mangles
# non-ASCII text. Keep it ASCII to stay portable across PowerShell 5.1 / 7.
#
# Requirements: terraform, docker, kubectl on PATH (k3d is driven by Terraform).
#
# Usage:   .\deploy.ps1               # bring the stand up
#          .\deploy.ps1 -WithLoadtest # ...and start the load generator
#          .\deploy.ps1 -Down         # tear it down
# ---------------------------------------------------------------------------
[CmdletBinding()]
param(
    [switch]$Down,
    [string]$TfDir,
    # Optional: also deploy the locust load generator (manifests-loadtest).
    # Without traffic the dashboards stay empty and the alert rules have
    # nothing to fire on, so this is what makes the stand "alive" for a demo.
    [switch]$WithLoadtest
)

$ErrorActionPreference = "Stop"

# $PSScriptRoot is not reliably set inside a param() default block, so resolve
# the default here, in the script body (where it is always available).
if (-not $TfDir) { $TfDir = Join-Path $PSScriptRoot "terraform\k3d" }

foreach ($tool in @("terraform", "docker", "kubectl")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "Tool '$tool' not found in PATH. Install it and retry."
    }
}

# --- Native-command probes --------------------------------------------------
# Native commands are the one place where PowerShell 5.1 ignores `2>$null`:
# with $ErrorActionPreference = "Stop" a single byte on the tool's stderr turns
# into a TERMINATING error and the script dies on the spot. Verified on 5.1:
# `kubectl get secret <absent> -o jsonpath=... 2>$null` throws
# NativeCommandError. Two checks below probe for something that is ALLOWED to be
# missing (an existing Secret, the new rules file inside a running container),
# and `kubectl exec` itself reports a failing command on stderr - so those run
# through this helper: stderr is dropped, stdout is returned, and the exit code
# is handed to the caller instead of throwing.
function Invoke-QuietProbe {
    param([Parameter(Mandatory)][string[]] $Arguments)
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $stdout = & kubectl @Arguments 2>&1 |
            Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] }
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    [pscustomobject]@{
        ExitCode = $exitCode
        Output   = ($stdout | Out-String).Trim()
    }
}

# Reads one key of a Kubernetes Secret and decodes it: kubectl hands Secret values
# over base64-encoded, so its raw output is not usable as-is. This is how a
# credential is taken from the cluster itself, so a re-run never has to be handed one.
function Get-ClusterSecretValue {
    param(
        [Parameter(Mandatory)][string] $Namespace,
        [Parameter(Mandatory)][string] $SecretName,
        [Parameter(Mandatory)][string] $Key
    )
    $probe = Invoke-QuietProbe -Arguments @(
        "-n", $Namespace, "get", "secret", $SecretName, "-o", "jsonpath={.data.$Key}"
    )
    if ($probe.ExitCode -ne 0 -or -not $probe.Output) { return $null }
    return [System.Text.Encoding]::UTF8.GetString(
        [System.Convert]::FromBase64String($probe.Output))
}

# --- Secrets (never in git) -------------------------------------------------
# The values live in a local, git-ignored file - that is the source of truth for
# anyone working on the stand; CI has no such file and passes the two environment
# variables instead. The file is sourced LAST on purpose: it wins over the
# environment, so a half-exported shell cannot silently deploy a stale token.
$secretsFile = Join-Path $PSScriptRoot "deploy\secrets.local.ps1"
if (Test-Path $secretsFile) { . $secretsFile }

# Missing credentials are not fatal here: the cluster may already hold the Secret
# (a re-run, or a fresh clone next to a live stand). Section 6 reads the values back
# out of that Secret, so nobody is asked to type a token into a command line.
if (-not $Down -and (-not $env:TELEGRAM_BOT_TOKEN -or -not $env:SLACK_WEBHOOK_URL)) {
    Write-Host "    No alerting credentials in the environment or in $secretsFile - they will be read from the cluster Secret if it exists" -ForegroundColor Yellow
}

# All terraform calls run inside the module directory. We deliberately avoid
# the `-chdir=` flag here: on Windows PowerShell it is passed to the native
# binary unexpanded, so we change the working directory for the duration of
# the calls instead.
Push-Location $TfDir
try {
    if ($Down) {
        Write-Host "==> Tearing down the stand (terraform destroy)" -ForegroundColor Cyan
        terraform destroy -auto-approve
        if ($LASTEXITCODE -ne 0) {
            throw "terraform destroy failed (exit $LASTEXITCODE)."
        }
        return
    }

    # --- 1. Cluster (Terraform) ---------------------------------------------
    Write-Host "==> 1/8 Cluster via Terraform" -ForegroundColor Cyan
    terraform init -input=false
    if ($LASTEXITCODE -ne 0) {
        throw "terraform init failed (exit $LASTEXITCODE). Fix the configuration and re-run."
    }
    terraform apply -auto-approve
    # A failed apply MUST stop the script. A native command's non-zero exit does
    # not terminate PowerShell on its own, and the damage compounds: observed
    # live, the k3d provider refuses to change a running cluster
    # ("Error: Updating clusters is not supported by k3d"), while the SAME apply
    # had already destroyed the generated kubeconfig file
    # (local_sensitive_file.kubeconfig is replaced on every apply). Everything
    # below then ran against a kubeconfig that no longer existed and produced a
    # screenful of misleading "openapi: the server could not find the requested
    # resource" errors instead of one clear failure.
    if ($LASTEXITCODE -ne 0) {
        $hint = "NOTE: the k3d provider cannot update a running cluster in place, " +
                "so a change to exposed_node_ports needs an explicit recreate: " +
                "terraform apply -replace=k3d_cluster.sock_shop"
        throw "terraform apply failed (exit $LASTEXITCODE). Nothing was deployed. $hint"
    }

    # The k3d Terraform provider does NOT merge the kubeconfig into
    # ~/.kube/config (the k3d CLI does), so capture the file it produced and
    # point kubectl at it.
    $env:KUBECONFIG = (terraform output -raw kubeconfig_path)
    $appUrl         = terraform output -raw app_url
    $toolUrlsJson   = terraform output -json tool_urls
}
finally {
    Pop-Location
}

Write-Host "    KUBECONFIG=$env:KUBECONFIG"

Write-Host "    Waiting for nodes to be ready..." -ForegroundColor Cyan
kubectl wait --for=condition=Ready node --all --timeout=180s | Out-Null

# --- 2. Application ---------------------------------------------------------
Write-Host "==> 2/8 Application (sock-shop)" -ForegroundColor Cyan
kubectl apply -f (Join-Path $PSScriptRoot "deploy\kubernetes\manifests")
kubectl wait --for=condition=available --timeout=300s deployment --all -n sock-shop

# --- 3. Network policies ----------------------------------------------------
# Before the monitoring stack on purpose: the policies open the scrape path for
# Prometheus by namespace, and a policy applied after the first scrape would
# leave the monitoring stack with dead targets until the next rollout.
# `kubectl apply -f <dir>` does not recurse, so nothing under legacy/ is ever
# applied by accident. Verification is a separate, explicit step:
#   .\deploy\kubernetes\manifests-policy\verify-netpol.ps1
Write-Host "==> 3/8 Network policies (manifests-policy)" -ForegroundColor Cyan
kubectl apply -f (Join-Path $PSScriptRoot "deploy\kubernetes\manifests-policy")

# --- 4. Autoscaling (HPA v2) ------------------------------------------------
# Two prerequisites, and both are worth stating because the stand used to have
# neither in effect: `requests.cpu` on the target pods (the HPA scales on a
# PERCENTAGE of the request) and metrics-server (the usage comes from the
# metrics.k8s.io API). k3s ships metrics-server, so this is one line here - and
# the wait below turns a missing metrics-server into a clear failure instead of
# leaving eight dead autoscalers behind, which is how this repository looked
# before: 8 manifests, 0 HPAs in the cluster.
Write-Host "==> 4/8 Autoscaling (HPA v2)" -ForegroundColor Cyan
kubectl apply -f (Join-Path $PSScriptRoot "deploy\kubernetes\autoscaling")

$hpaDeadline = (Get-Date).AddMinutes(2)
$hpaMetricsReady = $false
while ((Get-Date) -lt $hpaDeadline) {
    $hpaMetrics = kubectl -n sock-shop get hpa `
        -o jsonpath='{range .items[*]}{.status.currentMetrics}{"\n"}{end}' 2>$null
    if ($hpaMetrics -match 'averageUtilization') { $hpaMetricsReady = $true; break }
    Start-Sleep -Seconds 5
}
if (-not $hpaMetricsReady) {
    throw "No HPA reported CPU metrics within 2 minutes. HPA needs metrics-server (k3s ships it) and requests.cpu on the target pods; without both, the objects exist but never scale."
}
Write-Host "    HPA metrics are flowing (metrics-server reachable)." -ForegroundColor Green

# --- 5. Logging (Loki + Promtail) -------------------------------------------
# Applied after the application on purpose: Promtail tails the log files of the
# pods that are already running, so Loki starts collecting immediately.
Write-Host "==> 5/8 Logging (Loki + Promtail)" -ForegroundColor Cyan
kubectl apply -f (Join-Path $PSScriptRoot "deploy\kubernetes\manifests-logging")

# --- 6. Tracing (Jaeger) ----------------------------------------------------
Write-Host "==> 6/8 Tracing (Jaeger)" -ForegroundColor Cyan
kubectl apply -f (Join-Path $PSScriptRoot "deploy\kubernetes\manifests-jaeger")

# --- 7. Monitoring ----------------------------------------------------------
Write-Host "==> 7/8 Monitoring" -ForegroundColor Cyan

# The dashboard/datasource import is a one-shot Job and a Job spec is immutable,
# so a plain re-apply of an existing Job fails. Delete the finished Job first:
# the import then always runs against the current configmap.
kubectl -n monitoring delete job grafana-import-dashboards --ignore-not-found | Out-Null

# Grafana's admin credentials are NOT in the manifests any more: both the
# Deployment and the import Job read them from the `grafana-admin` Secret. Two
# details that make this non-obvious:
#  1. the namespace must exist before the Secret, and the manifest directory is
#     applied two lines below - so the namespace is applied explicitly here;
#  2. an EXISTING Secret is reused. Grafana stores the admin password in its own
#     database (emptyDir on this stand), so regenerating the Secret on every run
#     would silently lock us out of an instance that is already running.
# The generated password is safe to embed in a URL (unlike a password made of
# shell/URL metacharacters), which is why a plain UUID is used.
kubectl apply -f (Join-Path $PSScriptRoot "deploy\kubernetes\manifests-monitoring\00-monitoring-ns.yaml") | Out-Null

$grafanaUser = if ($env:GRAFANA_ADMIN_USER) { $env:GRAFANA_ADMIN_USER } else { "admin" }
$grafanaPassword = $env:GRAFANA_ADMIN_PASSWORD
$grafanaPasswordGenerated = $false
if (-not $grafanaPassword) {
    $probe = Invoke-QuietProbe -Arguments @(
        "-n", "monitoring", "get", "secret", "grafana-admin",
        "-o", "jsonpath={.data.admin-password}")
    if ($probe.ExitCode -eq 0 -and $probe.Output) {
        $grafanaPassword = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($probe.Output))
        Write-Host "    Reusing the existing grafana-admin Secret."
    } else {
        $grafanaPassword = [Guid]::NewGuid().ToString("N")
        $grafanaPasswordGenerated = $true
    }
}

kubectl -n monitoring create secret generic grafana-admin `
    --from-literal=admin-user=$grafanaUser `
    --from-literal=admin-password=$grafanaPassword `
    --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f (Join-Path $PSScriptRoot "deploy\kubernetes\manifests-monitoring")

# --- 8. Alerting ------------------------------------------------------------
Write-Host "==> 8/8 Alerting" -ForegroundColor Cyan

# The secret is created BEFORE the manifests: the Alertmanager pod mounts it as
# a volume, so it must exist or the pod will not start.
# Where the values come from, in order: the local, git-ignored secrets file (or the
# CI environment variables), and if neither has them - the cluster itself. The Secret
# is usually already deployed there, so its keys are read back out of it (see
# Get-ClusterSecretValue: kubectl returns them base64-encoded) and applied again
# idempotently - rendered client-side, because a plain `kubectl create` fails on the
# second run. Nothing is asked of a human and nothing is typed on a command line.
$alertingSecretExists = (Invoke-QuietProbe -Arguments @(
    "-n", "monitoring", "get", "secret", "alertmanager-secrets", "-o", "name")).ExitCode -eq 0

if ($alertingSecretExists -and (-not $env:TELEGRAM_BOT_TOKEN -or -not $env:SLACK_WEBHOOK_URL)) {
    if (-not $env:TELEGRAM_BOT_TOKEN) {
        $env:TELEGRAM_BOT_TOKEN = Get-ClusterSecretValue -Namespace monitoring `
            -SecretName alertmanager-secrets -Key telegram_bot_token
    }
    if (-not $env:SLACK_WEBHOOK_URL) {
        $env:SLACK_WEBHOOK_URL = Get-ClusterSecretValue -Namespace monitoring `
            -SecretName alertmanager-secrets -Key slack_webhook_url
    }
    if ($env:TELEGRAM_BOT_TOKEN -and $env:SLACK_WEBHOOK_URL) {
        Write-Host "    Alerting credentials taken from the cluster Secret - nothing to type"
    }
}

if ($env:TELEGRAM_BOT_TOKEN -and $env:SLACK_WEBHOOK_URL) {
    kubectl -n monitoring create secret generic alertmanager-secrets `
        --from-literal=telegram_bot_token=$env:TELEGRAM_BOT_TOKEN `
        --from-literal=slack_webhook_url=$env:SLACK_WEBHOOK_URL `
        --dry-run=client -o yaml | kubectl apply -f -
} elseif ($alertingSecretExists) {
    Write-Host "    alertmanager-secrets is in the cluster but its values are not readable - leaving it untouched"
} else {
    throw ("alertmanager-secrets is missing and no credentials are available. " +
           "Create deploy\secrets.local.ps1 from deploy\secrets.local.ps1.example (it is git-ignored) " +
           "and put the token and the webhook URL in it - there is no need to type a token on a command line.")
}

# note: examples/ is a sub-directory, and `kubectl apply -f <dir>` is NOT
# recursive, so the *.example.yaml files inside it are intentionally ignored.
kubectl apply -f (Join-Path $PSScriptRoot "deploy\kubernetes\manifests-alerting")

# --- Prometheus rules: wait for the file, validate it, reload ---------------
# Three facts make this non-trivial:
#  1. Prometheus runs with --web.enable-lifecycle, so POST /-/reload re-reads
#     the rule files without a restart. That matters here: this stand has no
#     PVC for /prometheus, so a restart throws the whole TSDB (metric history)
#     away - which is why there is deliberately NO restart fallback below.
#  2. A ConfigMap volume is refreshed by the kubelet ASYNCHRONOUSLY (up to
#     ~2 minutes), so an immediate reload after `kubectl apply` quietly
#     re-reads the OLD file: the classic "I changed the rule and nothing
#     happened" bug. So wait until the new content is really in the container.
#  3. A malformed rule file is NOT fatal for Prometheus: it logs an error and
#     keeps running WITHOUT those alerts - a silent monitoring gap. So the
#     mounted file is validated with promtool (the binary of the very same
#     image/version that runs the server) before anything is reloaded.
$rulesMarker = 'name: availability'   # a group name only the new file has
$rulesDeadline = (Get-Date).AddMinutes(3)
$rulesReady = $false
while ((Get-Date) -lt $rulesDeadline) {
    # Quiet probe: while the volume still holds the old file, `grep -c` exits 1
    # and kubectl reports that on stderr - exactly the case the loop exists for.
    $probe = Invoke-QuietProbe -Arguments @(
        "-n", "monitoring", "exec", "deploy/prometheus-deployment", "--",
        "grep", "-c", $rulesMarker, "/etc/prometheus-rules/alert.rules")
    if ($probe.ExitCode -eq 0 -and $probe.Output -match '^[1-9]') { $rulesReady = $true; break }
    Start-Sleep -Seconds 5
}
if (-not $rulesReady) {
    throw "Prometheus rule file did not refresh on the ConfigMap volume within 3 minutes. Re-run the script, or check the alertrules-volume mount (06-prometheus-dep.yaml)."
}

Write-Host "    Validating Prometheus rules (promtool)..." -ForegroundColor Cyan
kubectl -n monitoring exec deploy/prometheus-deployment -- `
    promtool check rules /etc/prometheus-rules/alert.rules
if ($LASTEXITCODE -ne 0) {
    throw "Prometheus rules are INVALID - refusing to reload: Prometheus would silently run without them."
}

kubectl -n monitoring exec deploy/prometheus-deployment -- `
    wget -q -O- --post-data='' http://localhost:9090/-/reload
if ($LASTEXITCODE -ne 0) {
    throw "POST /-/reload failed - check --web.enable-lifecycle in 06-prometheus-dep.yaml."
}
Write-Host "    Rules reloaded (no restart: metric history kept)." -ForegroundColor Green

# --- 7. Load generator (optional) -------------------------------------------
# Deliberately optional: two locust pods hammering front-end are noise in a
# training cluster, but they are exactly what makes the dashboards, the alert
# rules and the traces non-empty (see manifests-loadtest/loadtest-dep.yaml).
if ($WithLoadtest) {
    Write-Host "==> load generator (manifests-loadtest)" -ForegroundColor Cyan
    kubectl apply -f (Join-Path $PSScriptRoot "deploy\kubernetes\manifests-loadtest")
    kubectl -n loadtest rollout status deploy/load-test --timeout=120s | Out-Null
}

# --- Summary ----------------------------------------------------------------
Write-Host ""
Write-Host "Done. URLs:" -ForegroundColor Green
Write-Host ("  Sock Shop   {0}" -f $appUrl)
if ($WithLoadtest) { Write-Host "  Load test   2 locust pods hammering front-end (namespace loadtest)" }
$toolUrls = $toolUrlsJson | ConvertFrom-Json
foreach ($p in $toolUrls.PSObject.Properties) {
    Write-Host ("  Port {0,-6} {1}" -f $p.Name, $p.Value)
}

# Grafana has no published host port any more (it used to be a NodePort with the
# default admin/admin, reachable from every interface of the host). It is served
# by the same Traefik entrypoint as the application, distinguished by the HTTP
# Host header.
$ingressPort = ([Uri]$appUrl).Port
Write-Host ("  Grafana     http://grafana.localhost:{0}   (login: {1})" -f $ingressPort, $grafanaUser)
Write-Host  "              needs '127.0.0.1 grafana.localhost' in your hosts file; without it:"
Write-Host  "              kubectl port-forward -n monitoring svc/grafana 3000:80  ->  http://localhost:3000"
if ($grafanaPasswordGenerated) {
    Write-Host ("    Grafana admin password generated in this run: {0}" -f $grafanaPassword) -ForegroundColor Yellow
} else {
    # Printed on purpose. The value already lives in the grafana-admin Secret and
    # this is a single-user training stand - and "the password is in the Secret,
    # look it up" turned out to be exactly the step people get stuck on (the
    # Secret is base64, the Service is ClusterIP, and the UI needs a hosts entry).
    # Set GRAFANA_ADMIN_PASSWORD to pick your own value instead.
    Write-Host ("    Grafana admin password (from the existing grafana-admin Secret): {0}" -f $grafanaPassword) -ForegroundColor Yellow
}