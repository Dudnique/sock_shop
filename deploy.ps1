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
#   It prints the resulting URLs.
#
# The cluster is deliberately NOT created with `k3d cluster create` here: that
# would duplicate terraform/k3d and let the two definitions drift apart.
#
# SECRETS: the alerting stack needs two secrets - a Telegram bot token and a
# Slack incoming-webhook URL. They are NEVER committed. Provide them either as
# environment variables (TELEGRAM_BOT_TOKEN, SLACK_WEBHOOK_URL) or in a local,
# git-ignored file: deploy\secrets.local.ps1
#
# NOTE: this file is intentionally ASCII-only. On Windows PowerShell 5.1 a
# UTF-8-without-BOM script is decoded as the system ANSI codepage, which mangles
# non-ASCII text. Keep it ASCII to stay portable across PowerShell 5.1 / 7.
#
# Requirements: terraform, docker, kubectl on PATH (k3d is driven by Terraform).
#
# Usage:   .\deploy.ps1          # bring the stand up
#          .\deploy.ps1 -Down    # tear it down
# ---------------------------------------------------------------------------
[CmdletBinding()]
param(
    [switch]$Down,
    [string]$TfDir
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

# --- Secrets (never in git) -------------------------------------------------
# Prefer environment variables; also honour a local, git-ignored file so the
# stand can be brought up with a single command without exporting anything.
$secretsFile = Join-Path $PSScriptRoot "deploy\secrets.local.ps1"
if (Test-Path $secretsFile) { . $secretsFile }

if (-not $Down -and (-not $env:TELEGRAM_BOT_TOKEN -or -not $env:SLACK_WEBHOOK_URL)) {
    throw "Alerting secrets missing. Set TELEGRAM_BOT_TOKEN and SLACK_WEBHOOK_URL (env vars) or create deploy\secrets.local.ps1 that sets both."
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
        return
    }

    # --- 1. Cluster (Terraform) ---------------------------------------------
    Write-Host "==> 1/6 Cluster via Terraform" -ForegroundColor Cyan
    terraform init -input=false
    terraform apply -auto-approve

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
Write-Host "==> 2/6 Application (sock-shop)" -ForegroundColor Cyan
kubectl apply -f (Join-Path $PSScriptRoot "deploy\kubernetes\manifests")
kubectl wait --for=condition=available --timeout=300s deployment --all -n sock-shop

# --- 3. Logging (Loki + Promtail) -------------------------------------------
# Applied after the application on purpose: Promtail tails the log files of the
# pods that are already running, so Loki starts collecting immediately.
Write-Host "==> 3/6 Logging (Loki + Promtail)" -ForegroundColor Cyan
kubectl apply -f (Join-Path $PSScriptRoot "deploy\kubernetes\manifests-logging")

# --- 4. Tracing (Jaeger) ----------------------------------------------------
Write-Host "==> 4/6 Tracing (Jaeger)" -ForegroundColor Cyan
kubectl apply -f (Join-Path $PSScriptRoot "deploy\kubernetes\manifests-jaeger")

# --- 5. Monitoring ----------------------------------------------------------
Write-Host "==> 5/6 Monitoring" -ForegroundColor Cyan

# The dashboard/datasource import is a one-shot Job and a Job spec is immutable,
# so a plain re-apply of an existing Job fails. Delete the finished Job first:
# the import then always runs against the current configmap.
kubectl -n monitoring delete job grafana-import-dashboards --ignore-not-found | Out-Null

kubectl apply -f (Join-Path $PSScriptRoot "deploy\kubernetes\manifests-monitoring")

# --- 6. Alerting ------------------------------------------------------------
Write-Host "==> 6/6 Alerting" -ForegroundColor Cyan

# The secret is created BEFORE the manifests: the Alertmanager pod mounts it as
# a volume, so it must exist or the pod will not start.
# Created idempotently: render the manifest client-side, then apply it (a plain
# `kubectl create` would fail on the second run because the secret already exists).
kubectl -n monitoring create secret generic alertmanager-secrets `
    --from-literal=telegram_bot_token=$env:TELEGRAM_BOT_TOKEN `
    --from-literal=slack_webhook_url=$env:SLACK_WEBHOOK_URL `
    --dry-run=client -o yaml | kubectl apply -f -

# note: examples/ is a sub-directory, and `kubectl apply -f <dir>` is NOT
# recursive, so the *.example.yaml files inside it are intentionally ignored.
kubectl apply -f (Join-Path $PSScriptRoot "deploy\kubernetes\manifests-alerting")

# --- Summary ----------------------------------------------------------------
Write-Host ""
Write-Host "Done. URLs:" -ForegroundColor Green
Write-Host ("  Sock Shop   {0}" -f $appUrl)
$toolUrls = $toolUrlsJson | ConvertFrom-Json
foreach ($p in $toolUrls.PSObject.Properties) {
    Write-Host ("  Port {0,-6} {1}" -f $p.Name, $p.Value)
}