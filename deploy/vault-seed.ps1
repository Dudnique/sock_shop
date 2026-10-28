# ---------------------------------------------------------------------------
# vault-seed.ps1 - put the stand's secrets into Vault (GitOps mode).
#
# In -GitOps mode the Secrets are not created by deploy.ps1 any more. They live
# in Vault, and External Secrets copies them into Kubernetes Secrets. This script
# is the one place that writes them, from the same local, git-ignored file as
# before (deploy\secrets.local.ps1) or from the environment variables.
#
# Vault runs in the cluster, in DEV MODE: it keeps everything in memory, so a
# restart of its pod (Docker Desktop restarted, the node rebooted) empties it.
# The Secrets that are already in the cluster stay (External Secrets retains
# them), but nothing refreshes them until this script runs again. It is safe to
# run any number of times:
#
#     .\deploy\vault-seed.ps1
#
# What it does, in order:
#   1. enables the Kubernetes auth method and tells Vault where the API is;
#   2. writes a read-only policy for secret/sock-shop/* and a role that binds it
#      to the External Secrets ServiceAccount - so no token is stored anywhere;
#   3. writes the values to secret/sock-shop/{grafana,alerting,aiops}.
# Values travel through stdin, never as command-line arguments (those show up in
# process lists). Nothing secret is printed. The Grafana admin password is the
# one thing returned (as the last output), because deploy.ps1 prints it once.
#
# NOTE: ASCII-only on purpose, like deploy.ps1 (Windows PowerShell 5.1 decodes a
# UTF-8-without-BOM script as the ANSI codepage and mangles anything else).
# ---------------------------------------------------------------------------
[CmdletBinding()]
param(
    [string]$Namespace = "vault",
    [string]$Pod = "vault-0"
)

# Not "Stop": Windows PowerShell 5.1 turns one byte of stderr from a native
# command into a terminating error. Exit codes are checked explicitly instead.
$ErrorActionPreference = "Continue"

# What is piped into a native command is encoded with $OutputEncoding, and on
# Windows PowerShell 5.1 that is UTF-8 WITH a byte order mark: Vault then reads
# three invisible bytes at the start of the policy and answers "failed to parse
# policy: At 1:1: illegal char". Bash sends no BOM, which is why the same command
# worked there. Plain UTF-8 for everything this script pipes.
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)

$secretsFile = Join-Path $PSScriptRoot "secrets.local.ps1"
if (Test-Path $secretsFile) { . $secretsFile }

# The dev-mode root token is not a secret (dev mode prints it in its own log and
# the Helm values in git set it); it only exists to configure a throw-away Vault.
$rootToken = "root"

# Runs `vault <args>` inside the Vault pod. $Stdin, when given, is piped in.
function Invoke-Vault {
    param([string[]]$VaultArgs, [string]$Stdin, [switch]$AllowFailure)
    $base = @("-n", $Namespace, "exec")
    if ($PSBoundParameters.ContainsKey("Stdin")) { $base += "-i" }
    $base += @("-c", "vault", $Pod, "--", "env", "VAULT_TOKEN=$rootToken", "vault") + $VaultArgs
    if ($PSBoundParameters.ContainsKey("Stdin")) {
        $out = $Stdin | & kubectl @base 2>$null
    } else {
        $out = & kubectl @base 2>$null
    }
    if ($LASTEXITCODE -ne 0 -and -not $AllowFailure) {
        throw "vault $($VaultArgs[0..1] -join ' ') failed (exit $LASTEXITCODE) - is the pod $Pod in namespace $Namespace Ready?"
    }
    return $out
}

function ConvertTo-JsonLine {
    param([hashtable]$Data)
    # ConvertTo-Json on 5.1 is fine for flat string maps; -Compress keeps it on one line.
    return ($Data | ConvertTo-Json -Compress)
}

Write-Host "==> Seeding Vault ($Namespace/$Pod)" -ForegroundColor Cyan

# --- 1. Kubernetes auth ------------------------------------------------------
$auth = Invoke-Vault -VaultArgs @("auth", "list", "-format=json")
if (-not (($auth | Out-String) -match '"kubernetes/"')) {
    Invoke-Vault -VaultArgs @("auth", "enable", "kubernetes") | Out-Null
    Write-Host "    Kubernetes auth method enabled."
}
# Vault reviews tokens with its own ServiceAccount (the chart binds it to
# system:auth-delegator), so only the API address is needed.
Invoke-Vault -VaultArgs @("write", "auth/kubernetes/config",
    "kubernetes_host=https://kubernetes.default.svc:443") | Out-Null

# --- 2. Policy and role ------------------------------------------------------
$policy = @'
path "secret/data/sock-shop/*" {
  capabilities = ["read"]
}
path "secret/metadata/sock-shop/*" {
  capabilities = ["read", "list"]
}
'@
Invoke-Vault -VaultArgs @("policy", "write", "external-secrets", "-") -Stdin $policy | Out-Null
Invoke-Vault -VaultArgs @("write", "auth/kubernetes/role/external-secrets",
    "bound_service_account_names=external-secrets",
    "bound_service_account_namespaces=external-secrets",
    "policies=external-secrets", "ttl=1h") | Out-Null
Write-Host "    Policy and role for External Secrets are in place."

# --- 3. The values -----------------------------------------------------------
# Grafana: an explicit GRAFANA_ADMIN_USER / GRAFANA_ADMIN_PASSWORD wins; otherwise
# the value already in Vault is kept (Grafana stores the admin password in its own
# database, so regenerating it on every run would lock us out of a running one);
# otherwise a new one is generated. A plain UUID, safe to embed in a URL.
$grafanaUser = if ($env:GRAFANA_ADMIN_USER) { $env:GRAFANA_ADMIN_USER } else { "admin" }
$grafanaPassword = $env:GRAFANA_ADMIN_PASSWORD
$generated = $false
if (-not $grafanaPassword) {
    $existing = Invoke-Vault -VaultArgs @("kv", "get", "-mount=secret", "-field=admin-password",
        "sock-shop/grafana") -AllowFailure
    if ($LASTEXITCODE -eq 0 -and $existing) {
        $grafanaPassword = ($existing | Out-String).Trim()
        Write-Host "    Reusing the Grafana admin password already in Vault."
    } else {
        $grafanaPassword = [Guid]::NewGuid().ToString("N")
        $generated = $true
    }
}

if (-not $env:TELEGRAM_BOT_TOKEN -or -not $env:SLACK_WEBHOOK_URL) {
    throw ("TELEGRAM_BOT_TOKEN and SLACK_WEBHOOK_URL are needed: set them in $secretsFile " +
           "(git-ignored; see README, section Secrets) or in the environment.")
}

Invoke-Vault -VaultArgs @("kv", "put", "-mount=secret", "sock-shop/grafana", "-") -Stdin (
    ConvertTo-JsonLine @{ "admin-user" = $grafanaUser; "admin-password" = $grafanaPassword }) | Out-Null
Invoke-Vault -VaultArgs @("kv", "put", "-mount=secret", "sock-shop/alerting", "-") -Stdin (
    ConvertTo-JsonLine @{ "telegram_bot_token" = $env:TELEGRAM_BOT_TOKEN
                          "slack_webhook_url" = $env:SLACK_WEBHOOK_URL }) | Out-Null

# The model key is optional: an empty string means "no model", and the bot falls
# back to its ruleset and says so on every card.
$deepseek = if ($env:DEEPSEEK_API_KEY) { $env:DEEPSEEK_API_KEY } else { "" }
Invoke-Vault -VaultArgs @("kv", "put", "-mount=secret", "sock-shop/aiops", "-") -Stdin (
    ConvertTo-JsonLine @{ "deepseek_api_key" = $deepseek }) | Out-Null
$modelNote = if ($deepseek) { "model key present" } else { "no model key: the bot runs on its ruleset" }
Write-Host "    Values written: sock-shop/grafana, sock-shop/alerting, sock-shop/aiops ($modelNote)." -ForegroundColor Green

if ($generated) {
    Write-Host "    Grafana admin password generated in this run (kept in Vault)." -ForegroundColor Yellow
}
return $grafanaPassword
