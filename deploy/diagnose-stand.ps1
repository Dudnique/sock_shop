<#
.SYNOPSIS
    Say what is actually wrong with the stand, in one command.

.DESCRIPTION
    Three different failures look identical from kubectl ("Unable to connect to
    the server"), and each one needs a different response:

      1. the Docker engine is wedged  - every docker call hangs, docker ps may
         answer 500 on the named pipe. Nothing about the cluster can be read.
      2. the WSL VM is thrashing     - the engine answers, but the VM is out of
         memory and the k3s API starves. docker exec hangs too.
      3. the cluster is unhealthy    - engine and VM are fine, the API answers,
         pods are simply not ready.

    This script measures the three layers in that order and stops at the first
    one that is broken, printing the next command to run. It is read-only: no
    stop, no start, no apply, no delete.

.NOTES
    Written for Windows PowerShell 5.1. ASCII only on purpose - see FIXES.md
    section 17 for what a BOM-less UTF-8 script costs on 5.1.
#>
[CmdletBinding()]
param(
    # Where terraform keeps the state (only used to find the generated kubeconfig).
    [string]$TfDir,
    # How long one probe may take before it counts as "not answering".
    [int]$TimeoutSeconds = 12
)

$ErrorActionPreference = "Stop"

if (-not $TfDir) { $TfDir = Join-Path (Split-Path -Parent $PSScriptRoot) "terraform\k3d" }

# Every probe below may HANG - that is the whole point of the script - so each
# one runs in a job with a deadline instead of blocking the shell. A native
# command cut off by the deadline is worth nothing to us, so its output is
# dropped rather than half-read.
function Invoke-Probe {
    param(
        [Parameter(Mandatory)][scriptblock] $Script,
        [object[]] $Arguments = @(),
        [int] $Seconds = $TimeoutSeconds
    )
    $job = Start-Job -ScriptBlock $Script -ArgumentList $Arguments
    try {
        if (Wait-Job $job -Timeout $Seconds) {
            $text = (Receive-Job $job 2>&1 | ForEach-Object { "$_" }) -join "`n"
            return [pscustomobject]@{ Answered = $true; Output = $text.Trim() }
        }
        return [pscustomobject]@{ Answered = $false; Output = "" }
    } finally {
        Remove-Job $job -Force -ErrorAction SilentlyContinue
    }
}

function Write-Step { param([string]$Text) Write-Host ""; Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Fail { param([string]$Text) Write-Host "    FAIL: $Text" -ForegroundColor Red }
function Write-Ok   { param([string]$Text) Write-Host "    OK: $Text" -ForegroundColor Green }

# --- 1. Is the engine answering at all? --------------------------------------
Write-Step "Docker engine"
$watch = [Diagnostics.Stopwatch]::StartNew()
$engine = Invoke-Probe -Script { docker info --format "{{.ServerVersion}}|{{.MemTotal}}|{{.NCPU}}" }
$watch.Stop()
$engineSeconds = [math]::Round($watch.Elapsed.TotalSeconds, 1)

if (-not $engine.Answered) {
    Write-Fail "docker info did not answer within ${TimeoutSeconds}s"
    Write-Host ""
    Write-Host "    The engine (or its WSL VM) is wedged. Recover with:" -ForegroundColor Yellow
    Write-Host "      wsl --shutdown          # the only way to kill a hung VM;" -ForegroundColor Gray
    Write-Host "                              # 'docker stop' will hang as well" -ForegroundColor Gray
    Write-Host "      start Docker Desktop, then: k3d cluster start sock-shop" -ForegroundColor Gray
    Write-Host "    If the stand still thrashes afterwards: .\deploy.ps1 -Down, then .\deploy.ps1." -ForegroundColor Gray
    Write-Host "    Full write-up: FIXES.md, section 17." -ForegroundColor Gray
    exit 2
}

$fields = $engine.Output -split "\|"
Write-Ok "docker info answered in ${engineSeconds}s (server $($fields[0].Trim()))"
if ($fields.Count -ge 3) {
    $memGb = [math]::Round([int64]$fields[1].Trim() / 1GB, 1)
    Write-Host "    Docker VM: $memGb GB RAM, $($fields[2].Trim()) CPUs" -ForegroundColor Gray
    if ($memGb -lt 8) {
        Write-Host "    NOTE: a fresh stand uses ~4.6 GB of this VM and grows day by day" -ForegroundColor Yellow
        Write-Host "          (it reached 7.4 GB before it failed once). That growth is discarded data:" -ForegroundColor Gray
        Write-Host "          .\deploy\cleanup-stand.ps1 drops it without a rebuild. Rebuild with" -ForegroundColor Gray
        Write-Host "          deploy.ps1 -Down + -WithAIOps only if the stand does not come back." -ForegroundColor Gray
    }
}
if ($engineSeconds -gt 5) {
    Write-Host "    NOTE: >5s for docker info means the VM is already under pressure." -ForegroundColor Yellow
}

# --- 2. Is the WSL VM alive, or just barely? ---------------------------------
# docker info is answered by the daemon, which can still reply while the VM it
# runs in is starving. The load average is the number that told the story during
# the incident: 577 on a 16-core VM, with 33 MB of RAM available.
Write-Step "Docker Desktop VM"
$vm = Invoke-Probe -Script {
    wsl -d docker-desktop sh -c "head -1 /proc/loadavg; free -m | sed -n 2p"
}
if (-not $vm.Answered) {
    Write-Fail "the VM shell did not answer within ${TimeoutSeconds}s"
    Write-Host ""
    Write-Host "    The engine answers but its VM is frozen - this is the thrashing case." -ForegroundColor Yellow
    Write-Host "    Recover with: wsl --shutdown, then .\deploy.ps1 -Down and .\deploy.ps1." -ForegroundColor Gray
    Write-Host "    Full write-up: FIXES.md, section 17." -ForegroundColor Gray
    exit 3
}

$vmLines = @($vm.Output -split "`n" | Where-Object { $_.Trim() })
$load = if ($vmLines.Count -ge 1) { ($vmLines[0].Trim() -split "\s+")[0] } else { "?" }
$loadValue = 0.0
[void][double]::TryParse($load, [ref]$loadValue)

# free -m prints: total used free shared buff/cache available
$freeMb = $null
if ($vmLines.Count -ge 2 -and $vmLines[1] -match '^Mem:\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+(\d+)') {
    $freeMb = [int]$matches[1]
}

Write-Host "    load average: $load" -ForegroundColor Gray
if ($null -ne $freeMb) { Write-Host "    available: $freeMb MB" -ForegroundColor Gray }

if ($loadValue -gt 50 -or ($null -ne $freeMb -and $freeMb -lt 200)) {
    Write-Fail "the VM is thrashing (load $load, available $freeMb MB)"
    Write-Host ""
    Write-Host "    In this state 'docker stop' and 'docker exec' hang, and the k3s API" -ForegroundColor Yellow
    Write-Host "    starves until kubectl times out at the TLS handshake. Recover with:" -ForegroundColor Gray
    Write-Host "      wsl --shutdown" -ForegroundColor Gray
    Write-Host "      .\deploy.ps1 -Down        # terraform destroy, ~20 s" -ForegroundColor Gray
    Write-Host "      .\deploy.ps1 -WithAIOps   # fresh cluster + manifests, ~5 min" -ForegroundColor Gray
    Write-Host "    Full write-up: FIXES.md, section 17." -ForegroundColor Gray
    exit 3
}

Write-Ok "the VM is not thrashing"

# --- 3. Is the cluster there and healthy? ------------------------------------
Write-Step "Cluster"
$kubeconfig = Join-Path $env:USERPROFILE ".kube\sock-shop-k3d.yaml"
if (Get-Command terraform -ErrorAction SilentlyContinue) {
    $probe = Invoke-Probe -Seconds 20 -Arguments @($TfDir) -Script {
        param($dir)
        Set-Location $dir
        terraform output -raw kubeconfig_path 2>$null
    }
    if ($probe.Answered -and $probe.Output) { $kubeconfig = $probe.Output }
}
if (-not (Test-Path $kubeconfig)) {
    Write-Fail "no kubeconfig at $kubeconfig"
    Write-Host "    The cluster was never created (or terraform state is gone): .\deploy.ps1" -ForegroundColor Gray
    exit 4
}
Write-Host "    kubeconfig: $kubeconfig" -ForegroundColor Gray

$nodes = Invoke-Probe -Seconds 25 -Arguments @($kubeconfig) -Script {
    param($kc)
    $env:KUBECONFIG = $kc
    kubectl get nodes --request-timeout=15s 2>&1
}
if (-not $nodes.Answered -or $nodes.Output -match 'Unable to connect|was refused|handshake timeout') {
    Write-Fail "kubectl cannot reach the API server"
    Write-Host "    The engine and the VM are fine, so this is not the wedged case: the API" -ForegroundColor Yellow
    Write-Host "    is down, or the kubeconfig points at a port that no longer exists (the" -ForegroundColor Gray
    Write-Host "    file is rewritten on every terraform apply). Rebuild:" -ForegroundColor Gray
    Write-Host "      .\deploy.ps1 -Down; .\deploy.ps1" -ForegroundColor Gray
    exit 4
}
$nodes.Output -split "`n" | Where-Object { $_.Trim() } | ForEach-Object { Write-Host "    $_" -ForegroundColor Gray }

$pods = Invoke-Probe -Seconds 30 -Arguments @($kubeconfig) -Script {
    param($kc)
    $env:KUBECONFIG = $kc
    kubectl get pods -A --no-headers --request-timeout=15s 2>&1
}
if ($pods.Answered) {
    $bad = @($pods.Output -split "`n" | Where-Object { $_ -match '\S' -and $_ -notmatch '\s(Running|Completed)\s' })
    if ($bad.Count -eq 0) {
        Write-Ok "every pod is Running or Completed"
    } else {
        Write-Fail "$($bad.Count) pod(s) not Running/Completed"
        $bad | Select-Object -First 10 | ForEach-Object { Write-Host "    $_" -ForegroundColor Gray }
        if ($bad.Count -gt 10) { Write-Host "    ... and $($bad.Count - 10) more" -ForegroundColor Gray }
        exit 5
    }
}

Write-Host ""
Write-Host "Verdict: engine, VM and cluster are healthy. If the bot misbehaves, the" -ForegroundColor Green
Write-Host "problem is in the bot, not in the stand: .\aiops\triage-bot\run-local.ps1" -ForegroundColor Green
exit 0

