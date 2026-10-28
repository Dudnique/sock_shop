# ---------------------------------------------------------------------------
# vault-seed.ps1 - put the stand's secrets into Vault (GitOps mode).
#
# In -GitOps mode the Secrets are not created by deploy.ps1 any more. They live
# in Vault, and External Secrets copies them into Kubernetes Secrets. This script
# is the one place that writes them, from the same local, git-ignored file as
# before (deploy\secrets.local.ps1) or from the environment variables.
#
# Vault runs in the cluster with its data on a volume (Raft storage), so the data
# survives a restart of its pod. What does not survive is the "unsealed" state:
# Vault starts sealed (locked) and gives nothing out until it is unsealed with a
# key. This script handles all of it and is safe to run any number of times:
#
#     .\deploy\vault-seed.ps1               # everything below
#     .\deploy\vault-seed.ps1 -UnsealOnly   # after a restart: just unlock, keep the data
#
# What it does, in order:
#   0. first start only: initialises Vault (it generates one unseal key and a root
#      token) and saves both to deploy\vault-init.local.json, which is git-ignored.
#      Then unseals it if it is sealed. If the cluster was rebuilt (the volume is
#      gone, the key file is stale) the old file is moved aside and Vault is
#      initialised afresh;
#   1. enables the Kubernetes auth method and tells Vault where the API is;
#   2. writes a read-only policy for secret/sock-shop/* and a role that binds it
#      to the External Secrets ServiceAccount - so no token is stored anywhere;
#   3. writes the values to secret/sock-shop/{grafana,alerting,aiops}.
# Secrets travel through stdin, never as command-line arguments (those show up in
# process lists) - the root token included. Nothing secret is printed. The Grafana
# admin password is the one thing returned (as the last output), because
# deploy.ps1 prints it once.
#
# NOTE: ASCII-only on purpose, like deploy.ps1 (Windows PowerShell 5.1 decodes a
# UTF-8-without-BOM script as the ANSI codepage and mangles anything else).
# ---------------------------------------------------------------------------
[CmdletBinding()]
param(
    [string]$Namespace = "vault",
    [string]$Pod = "vault-0",
    [switch]$UnsealOnly
)

$ErrorActionPreference = "Stop"

$secretsFile = Join-Path $PSScriptRoot "secrets.local.ps1"
if (Test-Path $secretsFile) { . $secretsFile }

# Written at the first start, never committed (see .gitignore): the unseal key and
# the root token of this Vault. Whoever holds this file owns the Vault.
$initFile = Join-Path $PSScriptRoot "vault-init.local.json"
$script:RootToken = $null

# Runs `vault <args>` inside the Vault pod and returns its stdout.
#
# Two Windows PowerShell 5.1 traps, both ending in Vault's "failed to parse policy:
# At 1:1: illegal char" (three invisible bytes, EF BB BF, at the start of stdin):
#  1. text piped into a native command gets a byte order mark and a stray line
#     break, whatever $OutputEncoding says - so kubectl is started as a process and
#     its stdin is written as raw UTF-8 bytes;
#  2. .NET builds the redirected stdin stream with the CONSOLE's input encoding and
#     flushes it at once, which writes a BOM when the console code page is UTF-8
#     (65001; plain cmd.exe and most terminals use 437 or 866, which is why the
#     same script worked in one window and failed in another) - so the console
#     input encoding is switched to BOM-less UTF-8 just for the Process.Start call.
# Bash sends neither, which is why the same command worked there. A process also
# gives the real stderr, so a failure says why instead of "exit 2".
#
# The root token is not put on the command line (kubectl's arguments are visible in
# the process list): it is the first line of stdin, and a tiny `sh` wrapper inside
# the pod reads it into VAULT_TOKEN before running vault. The rest of stdin is
# left for vault itself (`policy write ... -`, `kv put ... -`). -NoToken is for the
# calls made before a token exists (status, init, unseal).
$script:VaultExit = 0
function Invoke-Vault {
    param([string[]]$VaultArgs, [string]$Stdin, [switch]$AllowFailure, [switch]$NoToken, [switch]$UnsealKeyOnStdin)
    $withToken = (-not $NoToken) -and $script:RootToken
    if ($withToken) {
        $Stdin = $script:RootToken + "`n" + $(if ($PSBoundParameters.ContainsKey("Stdin")) { $Stdin } else { "" })
    }
    $hasStdin = $withToken -or $UnsealKeyOnStdin -or $PSBoundParameters.ContainsKey("Stdin")
    $all = @("-n", $Namespace, "exec")
    if ($hasStdin) { $all += "-i" }
    if ($withToken) {
        $all += @("-c", "vault", $Pod, "--", "sh", "-c",
            'IFS= read -r VAULT_TOKEN; export VAULT_TOKEN; exec vault "$@"', "sh") + $VaultArgs
    } elseif ($UnsealKeyOnStdin) {
        # `vault operator unseal` takes the key as an argument (it does not read "-"),
        # so it is read from stdin by a wrapper inside the pod: on this side the key
        # is never on a command line.
        $all += @("-c", "vault", $Pod, "--", "sh", "-c",
            'IFS= read -r K; exec vault operator unseal "$K"')
    } else {
        $all += @("-c", "vault", $Pod, "--", "vault") + $VaultArgs
    }

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "kubectl"
    $psi.Arguments = (($all | ForEach-Object {
        if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
    }) -join " ")
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.RedirectStandardInput = $hasStdin

    $previousInput = [Console]::InputEncoding
    try {
        [Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
        $proc = [System.Diagnostics.Process]::Start($psi)
    } finally {
        [Console]::InputEncoding = $previousInput
    }
    if ($hasStdin) {
        # Bytes straight into the stream: no encoder in between, so no byte order mark.
        $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes(($Stdin -replace "`r`n", "`n"))
        $proc.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
        $proc.StandardInput.BaseStream.Flush()
        $proc.StandardInput.Close()
    }
    $out = $proc.StandardOutput.ReadToEnd()
    $err = $proc.StandardError.ReadToEnd()
    $proc.WaitForExit()
    $script:VaultExit = $proc.ExitCode
    if ($proc.ExitCode -ne 0 -and -not $AllowFailure) {
        throw ("vault " + ($VaultArgs[0..1] -join " ") + " failed (exit " + $proc.ExitCode + "): " + $err.Trim())
    }
    return $out
}

Write-Host "==> Seeding Vault ($Namespace/$Pod)" -ForegroundColor Cyan

# --- 0. Initialise (first start) and unseal ----------------------------------
# `vault status` exits 0 when unsealed, 2 when sealed or not initialised, 1 on error;
# the JSON is on stdout in the first two cases.
function Get-VaultStatus {
    $deadline = (Get-Date).AddMinutes(3)
    while ($true) {
        $raw = Invoke-Vault -VaultArgs @("status", "-format=json") -NoToken -AllowFailure
        if ($script:VaultExit -in 0, 2 -and $raw.Trim()) { return ($raw | ConvertFrom-Json) }
        if ((Get-Date) -gt $deadline) { throw "Vault does not answer in $Namespace/$Pod (is the pod running?)" }
        Start-Sleep -Seconds 3
    }
}

$status = Get-VaultStatus
if (-not $status.initialized) {
    if (Test-Path $initFile) {
        # The cluster was rebuilt: the volume is new and empty, the file is from the old one.
        $aside = $initFile + ".stale-" + (Get-Date -Format "yyyyMMdd-HHmmss")
        Move-Item $initFile $aside
        Write-Host "    Vault is new but a key file from an old cluster exists: moved to $(Split-Path $aside -Leaf)." -ForegroundColor Yellow
    }
    $initJson = Invoke-Vault -VaultArgs @("operator", "init", "-key-shares=1", "-key-threshold=1", "-format=json") -NoToken
    # Saved before anything else: this output is the only copy of the key.
    [System.IO.File]::WriteAllText($initFile, $initJson, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "    Vault initialised. Unseal key and root token saved to deploy\vault-init.local.json (git-ignored: keep a copy, losing it loses the data)." -ForegroundColor Yellow
    $status = Get-VaultStatus
}
if (-not (Test-Path $initFile)) {
    throw ("Vault is initialised but $initFile is missing, so there is no key to unseal it with. " +
           "If the data does not matter, delete the volume: kubectl -n $Namespace delete pvc --all; " +
           "kubectl -n $Namespace delete pod $Pod  - and run this script again.")
}
$init = Get-Content $initFile -Raw | ConvertFrom-Json
$script:RootToken = $init.root_token
if ($status.sealed) {
    Invoke-Vault -VaultArgs @("operator", "unseal") -Stdin ($init.unseal_keys_b64[0] + "`n") -NoToken -UnsealKeyOnStdin | Out-Null
    Write-Host "    Vault unsealed."
}
# After the unseal a single Raft node needs a few seconds to elect itself leader;
# until then writes answer "Vault is sealed" or "no active node".
$deadline = (Get-Date).AddMinutes(2)
while ($true) {
    Invoke-Vault -VaultArgs @("auth", "list", "-format=json") -AllowFailure | Out-Null
    if ($script:VaultExit -eq 0) { break }
    if ((Get-Date) -gt $deadline) { throw "Vault was unsealed but did not become active within 2 minutes" }
    Start-Sleep -Seconds 3
}
if ($UnsealOnly) {
    Write-Host "    Unsealed; data kept (-UnsealOnly): nothing was rewritten." -ForegroundColor Green
    return
}

# --- 1. Secrets engine and Kubernetes auth -----------------------------------
# Dev mode mounted a key-value store (version 2) at secret/ by itself; a real
# Vault starts with none.
$mounts = Invoke-Vault -VaultArgs @("secrets", "list", "-format=json")
if ($mounts -notmatch '"secret/"') {
    Invoke-Vault -VaultArgs @("secrets", "enable", "-path=secret", "-version=2", "kv") | Out-Null
    Write-Host "    Key-value store (v2) enabled at secret/."
}
$auth = Invoke-Vault -VaultArgs @("auth", "list", "-format=json")
if ($auth -notmatch '"kubernetes/"') {
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
    if ($script:VaultExit -eq 0 -and $existing.Trim()) {
        $grafanaPassword = $existing.Trim()
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

# Flat string maps; ConvertTo-Json -Compress keeps each on one line.
Invoke-Vault -VaultArgs @("kv", "put", "-mount=secret", "sock-shop/grafana", "-") -Stdin (
    @{ "admin-user" = $grafanaUser; "admin-password" = $grafanaPassword } | ConvertTo-Json -Compress) | Out-Null
Invoke-Vault -VaultArgs @("kv", "put", "-mount=secret", "sock-shop/alerting", "-") -Stdin (
    @{ "telegram_bot_token" = $env:TELEGRAM_BOT_TOKEN
       "slack_webhook_url" = $env:SLACK_WEBHOOK_URL } | ConvertTo-Json -Compress) | Out-Null

# The model key is optional: an empty string means "no model", and the bot falls
# back to its ruleset and says so on every card.
$deepseek = if ($env:DEEPSEEK_API_KEY) { $env:DEEPSEEK_API_KEY } else { "" }
Invoke-Vault -VaultArgs @("kv", "put", "-mount=secret", "sock-shop/aiops", "-") -Stdin (
    @{ "deepseek_api_key" = $deepseek } | ConvertTo-Json -Compress) | Out-Null
$modelNote = if ($deepseek) { "model key present" } else { "no model key: the bot runs on its ruleset" }
Write-Host "    Values written: sock-shop/grafana, sock-shop/alerting, sock-shop/aiops ($modelNote)." -ForegroundColor Green

if ($generated) {
    Write-Host "    Grafana admin password generated in this run (kept in Vault)." -ForegroundColor Yellow
}
return $grafanaPassword
