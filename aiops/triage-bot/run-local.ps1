<#
.SYNOPSIS
    Run the AIOps triage bot locally, offline, against the live stand.

.DESCRIPTION
    Starts the bot on http://localhost:8080 with the rules-only tier-0 path
    (`MODEL_ADAPTER=mock`) and Telegram disabled, then POSTs a realistic
    Alertmanager payload to it and prints what the bot decided.

    Why this script exists: the same pipeline that runs in the cluster can be
    demonstrated without Docker, without a cluster rebuild and without a model
    key. Everything it needs is Python and kubectl - and Prometheus/Loki are
    reached through `kubectl port-forward`, so no secret leaves the machine.

.NOTES
    The bot writes its journal to %TEMP%\aiops-triage-journal; delete that
    directory to start from a clean page.
#>
param(
    # Which model tier to use locally: mock | deepseek | openai | ollama.
    [string]$Model = "mock",
    # Alert to simulate when no payload file is given.
    [string]$Alertname = "DeploymentReplicasUnavailable",
    # 8090, NOT 8080: on this stand the host port 8080 belongs to the front-end
    # Ingress (Traefik). Windows happily lets a second process bind a port that is
    # already in use, so the POST then lands on the *application* and answers
    # "Cannot POST /alert" from Node - measured exactly that way.
    [int]$Port = 8090,
    # Start from an empty journal. Without it, a second run of the same simulated
    # alert inside the 30-minute DEDUPE window is (correctly) reported as
    # "duplicate" - that is the duplicate filter working, not the bot failing.
    # Run once with -Fresh (full triage) and once without (you see the dedupe).
    [switch]$Fresh
)

$ErrorActionPreference = "Stop"

# --- Console encoding -------------------------------------------------------
# This script prints Russian section headers. A .ps1 saved as UTF-8 *without a
# BOM* is read by PowerShell 5.1 in the ANSI code page, so every Cyrillic
# literal was already mojibake before it reached the console ("--- СЂРѕС‚ ...").
# The file now carries a UTF-8 BOM; these two lines make the console itself
# speak UTF-8 as well, so the text also survives redirection and the VS Code
# terminal instead of turning back into garbage there.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$botDir = $PSScriptRoot
$journalDir = Join-Path $env:TEMP "aiops-triage-journal"
if ($Fresh -and (Test-Path $journalDir)) {
    Remove-Item -Recurse -Force $journalDir
    Write-Host "==> journal wiped (-Fresh): $journalDir" -ForegroundColor Yellow
}

function Test-Endpoint {
    param([string]$Url)
    try {
        Invoke-WebRequest -Uri $Url -TimeoutSec 3 -UseBasicParsing | Out-Null
        return $true
    } catch {
        return $false
    }
}

# --- 1. Reach Prometheus and Loki from the host ------------------------------
# Prometheus is already published by the k3d port mapping (host 9090 -> NodePort),
# so port-forwarding it is unnecessary - and binding an occupied port is exactly
# the trap this script documents above. Loki is a ClusterIP with no host mapping,
# so it does get a port-forward.
Write-Host "==> checking prometheus and loki reachability" -ForegroundColor Cyan
$prom = $null
# `/-/healthy` and not a PromQL query: the first series query on this stand can
# take tens of seconds (WSL2 + a cold TSDB), and a health probe that times out
# looks exactly like "Prometheus is down" - measured that way, too.
if (Test-Endpoint "http://localhost:9090/-/healthy") {
    Write-Host "    Prometheus is already reachable on host port 9090 (k3d mapping) - no port-forward needed"
} else {
    $prom = Start-Process kubectl -PassThru -NoNewWindow -ArgumentList @(
        "-n", "monitoring", "port-forward", "svc/prometheus", "9090:9090")
    Start-Sleep -Seconds 3
}

$loki = $null
if (Test-Endpoint "http://localhost:3100/ready") {
    Write-Host "    Loki is already reachable on host port 3100"
} else {
    $loki = Start-Process kubectl -PassThru -NoNewWindow -ArgumentList @(
        "-n", "logging", "port-forward", "svc/loki", "3100:3100")
    Start-Sleep -Seconds 3
}

# --- 2. Start the bot ---------------------------------------------------------
$env:PROMETHEUS_URL = "http://localhost:9090"
$env:LOKI_URL = "http://localhost:3100"
$env:MODEL_ADAPTER = $Model
$env:JOURNAL_DIR = $journalDir
$env:TELEGRAM_CHAT_ID = ""
$env:LISTEN_PORT = "$Port"
$env:LOG_LEVEL = "INFO"

# The key for a hosted model is read from the git-ignored secrets file and copied
# into a temp file, because config.py reads secrets from FILES (`LLM_API_KEY_FILE`)
# and never from the environment. The temp file is deleted in the `finally` block:
# it is never printed, never passed on a command line and never reaches the journal.
$keyFile = $null
if ($Model -ne "mock") {
    $secretsFile = Join-Path $botDir "..\..\deploy\secrets.local.ps1"
    if (Test-Path $secretsFile) { . $secretsFile }
    if (-not $env:DEEPSEEK_API_KEY) {
        Write-Host "==> $Model needs a key: put DEEPSEEK_API_KEY into $secretsFile" -ForegroundColor Yellow
    } else {
        $keyFile = Join-Path $env:TEMP "aiops-$Model-api-key"
        Set-Content -Path $keyFile -Value $env:DEEPSEEK_API_KEY -NoNewline -Encoding ascii
        $env:LLM_API_KEY_FILE = $keyFile
        Write-Host "    key read from $secretsFile into $keyFile (deleted on exit)" -ForegroundColor Cyan
    }
}

Write-Host "==> starting the bot (adapter=$Model, telegram off)" -ForegroundColor Cyan
$bot = Start-Process python -PassThru -NoNewWindow -WorkingDirectory $botDir -ArgumentList @(
    "-m", "triage_bot")
Start-Sleep -Seconds 3

try {
    Write-Host "==> POST /alert with a simulated $Alertname" -ForegroundColor Cyan
    $payload = @{
        version = "4"
        status  = "firing"
        alerts  = @(@{
            status      = "firing"
            labels      = @{
                alertname  = $Alertname
                severity   = "critical"
                namespace  = "sock-shop"
                deployment = "front-end"
                aiops      = "true"
            }
            annotations = @{
                summary     = "$Alertname on front-end (simulated locally)"
                description = "posted by run-local.ps1, not by Alertmanager"
                runbook     = "kubectl -n sock-shop describe deploy/front-end"
            }
            startsAt    = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
            fingerprint = "local-simulation-0001"
        })
    } | ConvertTo-Json -Depth 6

    $response = Invoke-RestMethod -Method Post -Uri "http://localhost:$Port/alert" `
        -ContentType "application/json" -Body $payload
    Write-Host "--- ответ бота ---" -ForegroundColor Green
    $response | ConvertTo-Json -Depth 6

    if ($response.triaged.Count -eq 0) {
        Write-Host "`nбот разобрал 0 алертов. Если причина 'duplicate' или 'snoozed' - это" -ForegroundColor Yellow
        Write-Host "работающий дедуп/игнор из журнала прошлого прогона: запусти с -Fresh." -ForegroundColor Yellow
    }

    Write-Host "`n--- последний инцидент (как его увидит человек) ---" -ForegroundColor Green
    $incidents = Invoke-RestMethod "http://localhost:$Port/incidents?limit=1"
    $incidents.incidents[0].incident | ConvertTo-Json -Depth 8

    Write-Host "`n--- метрики бота (только aiops_*) ---" -ForegroundColor Green
    # -UseBasicParsing is mandatory on PowerShell 5.1: without it Invoke-WebRequest
    # tries to parse the response with the IE engine and asks a human a security
    # question - inside a script that is supposed to be non-interactive.
    $metrics = (Invoke-WebRequest "http://localhost:$Port/metrics" -UseBasicParsing).Content
    ($metrics -split "`n") | Where-Object { $_ -match '^aiops_' } | ForEach-Object { $_ }

    Write-Host "`nжурнал: $journalDir" -ForegroundColor Yellow
    Write-Host "чтобы сравнить с моделью: .\run-local.ps1 -Model deepseek (нужен ключ в окружении LLM_API_KEY_FILE)" -ForegroundColor Yellow
    Write-Host "to compare with a real model: .\run-local.ps1 -Model deepseek  (key comes from deploy\secrets.local.ps1)" -ForegroundColor Yellow
} finally {
    if ($keyFile -and (Test-Path $keyFile)) {
        Remove-Item $keyFile -Force -ErrorAction SilentlyContinue
        Write-Host "temporary key file removed: $keyFile" -ForegroundColor Cyan
    }
    foreach ($process in @($bot, $prom, $loki)) {
        if ($process -and -not $process.HasExited) {
            # -Force because a port-forward child can ignore a polite request.
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Host "порты закрыты, бот остановлен" -ForegroundColor Cyan
}
