<#
.SYNOPSIS
    Stop and start the stand so that a Docker restart cannot wedge it.

.DESCRIPTION
    k3d creates its containers with the `unless-stopped` restart policy, so the
    moment Docker Desktop comes back all four of them start at the same instant
    while k3s replays its snapshot. Measured on this stand: load average 355 on
    the Docker VM, 68 MB of 7554 MB free, `docker ps` answering 500 on the named
    pipe and every `kubectl` call dying at the TLS handshake - the cluster looked
    dead while it was only starving (FIXES.md, section 22).

    Three things stop that from happening again:

      1. the restart policy is `no`, so starting Docker leaves the stand stopped
         until somebody asks for it explicitly;
      2. -Start brings it up in the only order that fits in memory: the load
         balancer, then the server, then the API has to answer, then one agent at
         a time with a Ready gate between them - and it refuses to start another
         agent while the VM is short of memory or swapping;
      3. -Stop stops the agents first and the server last, gracefully, so k3s
         writes its snapshot instead of being killed and replayed on next start.

    One case cannot be repaired: a stopped node container that lost its network
    endpoint to the VM restart may refuse to come up at all (k3s exits with code
    0 before the interface it wants exists). -Start detects that and rebuilds the
    cluster with deploy.ps1, because recreating it is the only step known to work
    from there; -NoRebuild reports instead of rebuilding.

    -Status answers "what is the stand doing" and changes nothing.

.PARAMETER Stop
    Stop the cluster: agents, then the server, then the load balancer.

.PARAMETER Start
    Start it in order, waiting for the API and for each node to become Ready.

.PARAMETER Status
    Report the engine, the VM, the containers and the nodes. The default.

.PARAMETER Free
    With -Stop: run deploy\cleanup-stand.ps1 first, so the log chunks and the
    TSDB that grew are dropped before the next start has to replay them.

.PARAMETER NoRebuild
    With -Start: report a node that will not come back instead of rebuilding the
    cluster. Without it, -Start finishes the job the only way that is known to
    work at that point: deploy.ps1 -Down followed by deploy.ps1 -WithAIOps.

.EXAMPLE
    .\deploy\stand.ps1 -Stop -Free
    .\deploy\stand.ps1 -Start
    .\deploy\stand.ps1 -Status

.NOTES
    ASCII only and Windows PowerShell 5.1 compatible, like deploy.ps1: every
    probe runs in a job with a deadline, because the failure it reports is
    exactly the one where a plain `docker` or `kubectl` call never returns.
#>
[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$Start,
    [switch]$Status,
    [switch]$Free,
    [switch]$NoRebuild
)

$ErrorActionPreference = "Stop"

$server = "k3d-sock-shop-server-0"
$agents = @("k3d-sock-shop-agent-0", "k3d-sock-shop-agent-1")
$balancer = "k3d-sock-shop-serverlb"

function Write-Step { param([string]$Text) Write-Host ""; Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Text) Write-Host "    OK: $Text" -ForegroundColor Green }
function Write-Bad  { param([string]$Text) Write-Host "    FAIL: $Text" -ForegroundColor Red }
function Write-Note { param([string]$Text) Write-Host "    $Text" -ForegroundColor Gray }

# Every call below may hang - that is the failure being recovered from - so each
# one runs in a job with a deadline. The command line is passed as a string and
# split on whitespace on the other side: none of the arguments used here contain
# a space, and building an array across a job boundary is where this would
# otherwise get subtle.
function Invoke-Docker {
    param([string]$Line, [int]$Seconds = 60)
    $job = Start-Job -ScriptBlock {
        param($l)
        $argv = @($l -split '\s+' | Where-Object { $_ })
        $output = & docker @argv 2>&1
        [pscustomobject]@{
            Text = (($output | ForEach-Object { "$_" }) -join "`n").Trim()
            Code = $LASTEXITCODE
        }
    } -ArgumentList $Line
    try {
        if (Wait-Job $job -Timeout $Seconds) { return (Receive-Job $job) }
        return [pscustomobject]@{ Text = "(docker did not answer within ${Seconds}s)"; Code = -1 }
    } finally {
        Remove-Job $job -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-Kubectl {
    param([string]$Line, [string]$Kubeconfig, [int]$Seconds = 40)
    $job = Start-Job -ScriptBlock {
        param($l, $kc)
        if ($kc) { $env:KUBECONFIG = $kc }
        $argv = @($l -split '\s+' | Where-Object { $_ })
        $output = & kubectl @argv 2>&1
        [pscustomobject]@{
            Text = (($output | ForEach-Object { "$_" }) -join "`n").Trim()
            Code = $LASTEXITCODE
        }
    } -ArgumentList $Line, $Kubeconfig
    try {
        if (Wait-Job $job -Timeout $Seconds) { return (Receive-Job $job) }
        return [pscustomobject]@{ Text = "(kubectl did not answer within ${Seconds}s)"; Code = -1 }
    } finally {
        Remove-Job $job -Force -ErrorAction SilentlyContinue
    }
}

# The kubeconfig is regenerated by terraform on every apply and carries a port
# that changes with it, so it is read from the state instead of assumed.
function Get-Kubeconfig {
    $tfDir = Join-Path (Split-Path -Parent $PSScriptRoot) "terraform\k3d"
    if (Get-Command terraform -ErrorAction SilentlyContinue) {
        Push-Location $tfDir
        try {
            $fromTf = & terraform output -raw kubeconfig_path 2>$null
        } finally {
            Pop-Location
        }
        if ($LASTEXITCODE -eq 0 -and $fromTf -and (Test-Path "$fromTf".Trim())) { return "$fromTf".Trim() }
    }
    $default = Join-Path $env:USERPROFILE ".kube\sock-shop-k3d.yaml"
    if (Test-Path $default) { return $default }
    return $null
}

function Get-VmStat {
    $job = Start-Job -ScriptBlock { wsl -d docker-desktop sh -c "head -1 /proc/loadavg; free -m | sed -n 2p" }
    try {
        if (-not (Wait-Job $job -Timeout 20)) { return $null }
        $text = ((Receive-Job $job 2>&1 | ForEach-Object { "$_" }) -join "`n")
    } finally {
        Remove-Job $job -Force -ErrorAction SilentlyContinue
    }
    $load = $null
    $freeMb = $null
    if ($text -match '(?m)^\s*(\d+\.\d+)') { $load = [double]$matches[1] }
    # `free -m` prints: total used free shared buff/cache available. The number
    # that answers "does another node fit" is `available`: the page cache is
    # reclaimable, while `free` is small on any healthy Linux and would cry wolf
    # on a stand that is doing nothing at all.
    if ($text -match '(?m)^Mem:\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)') {
        $freeMb = [int]$matches[6]
    } elseif ($text -match '(?m)^Mem:\s+\d+\s+\d+\s+(\d+)') {
        $freeMb = [int]$matches[1]
    }
    return [pscustomobject]@{ Load = $load; FreeMb = $freeMb }
}

function Wait-Engine {
    param([int]$Seconds = 120)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $answer = Invoke-Docker -Line "info --format {{.ServerVersion}}" -Seconds 12
        if ($answer.Code -eq 0 -and $answer.Text -match '^\d+\.') { return $answer.Text }
        Start-Sleep -Seconds 3
    }
    return $null
}

function Wait-Api {
    param([string]$Kubeconfig, [int]$Seconds = 180)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $answer = Invoke-Kubectl -Line "get --raw /readyz --request-timeout=5s" -Kubeconfig $Kubeconfig -Seconds 15
        if ($answer.Code -eq 0) { return $true }
        Start-Sleep -Seconds 3
    }
    return $false
}

# The load balancer publishes 127.0.0.1:57443, so the kubeconfig cannot be used
# before it is running - and it cannot run before the agents, because k3d's nginx
# resolves every node name at startup ("host not found in upstream
# k3d-sock-shop-agent-0:443" is what a stopped agent produces). The way out is to
# ask the server itself: it carries kubectl and its own credentials.
function Wait-ApiInCluster {
    param([int]$Seconds = 240)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $answer = Invoke-Docker -Line "exec $server kubectl get --raw /readyz --request-timeout=5s" -Seconds 20
        if ($answer.Code -eq 0) { return $true }
        Start-Sleep -Seconds 3
    }
    return $false
}

function Get-ContainerEndpoint {
    param([string]$Name)
    # `docker inspect -f '{{.NetworkSettings.Networks.k3d-sock-shop.IPAddress}}'`
    # is not valid: a Go template identifier cannot contain '-', so that format
    # fails with "bad character U+002D" and the endpoint looks unreadable. The
    # plain JSON has no such problem.
    $raw = Invoke-Docker -Line "inspect $Name" -Seconds 20
    if ($raw.Code -ne 0 -or -not $raw.Text) { return $null }
    try {
        $obj = @($raw.Text | ConvertFrom-Json)[0]
        $net = $obj.NetworkSettings.Networks.PSObject.Properties |
            Where-Object { $_.Name -eq "k3d-sock-shop" } | Select-Object -First 1
        if (-not $net) { return "" }
        return "$($net.Value.IPAddress)"
    } catch {
        return $null
    }
}

function Test-Running {
    param([string]$Name)
    $state = Invoke-Docker -Line "inspect -f {{.State.Status}} $Name" -Seconds 15
    return ($state.Code -eq 0 -and $state.Text -eq "running")
}

# A container that was stopped and then started after a Docker/WSL restart comes
# back with an empty network endpoint (EndpointID and IPAddress blank). k3s then
# exits with code 0 - "failed to start networking: ... failed to find interface
# with specified node ip" - so the container looks like it started and vanished,
# and the load balancer reports "host not found in upstream" (FIXES.md, sections
# 18 and 22). Dropping and rebuilding the endpoint fixes it.
#
# The address is deliberately NOT pinned: k3s adopts whatever address the
# interface has at startup (measured: an agent came up on the address that used
# to belong to its neighbour and was fine), while pinning the old one collides
# with whichever container took it while this one was stopped.
function Repair-Endpoint {
    param([string]$Name)
    $ip = Get-ContainerEndpoint -Name $Name
    if ($null -eq $ip) { return $false }
    if ($ip) { return $true }
    $null = Invoke-Docker -Line "network disconnect -f k3d-sock-shop $Name" -Seconds 30
    $connect = Invoke-Docker -Line "network connect k3d-sock-shop $Name" -Seconds 30
    if ($connect.Code -ne 0) {
        Write-Bad "${Name}: could not rebuild its network endpoint: $($connect.Text)"
        return $false
    }
    Write-Note "$Name had no network endpoint - reconnected to k3d-sock-shop"
    return $true
}

# `docker start` restores the endpoint recorded in the container config, and that
# record can name an address another node owns by now - "failed to set up
# container networking: Address already in use". Rebuilding the endpoint clears
# the stale record, so the start is retried once.
function Start-Container {
    param([string]$Name)
    $null = Repair-Endpoint $Name
    $result = Invoke-Docker -Line "start $Name" -Seconds 60
    if ($result.Code -ne 0 -and $result.Text -match 'Address already in use') {
        $null = Invoke-Docker -Line "network disconnect -f k3d-sock-shop $Name" -Seconds 30
        $null = Invoke-Docker -Line "network connect k3d-sock-shop $Name" -Seconds 30
        $result = Invoke-Docker -Line "start $Name" -Seconds 60
    }
    return $result
}

# A node container that lost its endpoint to a Docker/WSL restart does not always
# come back: k3s exits with code 0 before the interface it needs exists. Measured
# cause, and it is not the endpoint: the Node object in etcd still carries the
# address the container had in an earlier run. k3s starts the network-policy
# controller with that recorded address, does not find it on any interface
# ("failed to find interface with specified node ip") and shuts the agent down -
# with exit code 0, so the container looks like it started and vanished.
#
# Deleting the Node object makes the agent register from scratch, and then it
# adopts whatever address Docker gave it (measured on this stand: agent-1 came
# back on 172.19.0.4 while the stale record said 172.19.0.3, and stayed up).
# Without the stale record it would have been the sixth rebuild of the cluster,
# and the pods of that node are recreated by their controllers anyway, because a
# deleted node takes its pods with it.
function Reset-NodeRegistration {
    param([string]$Name)
    # --ignore-not-found, because on a healthy start there is nothing to delete -
    # this also has to be harmless on the very first boot of the cluster.
    $result = Invoke-Docker -Line "exec $server kubectl delete node $Name --ignore-not-found --request-timeout=15s" -Seconds 30
    if ($result.Code -ne 0) {
        Write-Note "$Name : could not drop its stale Node object: $($result.Text)"
        return $false
    }
    Write-Note "$Name had a stale Node object (its old address no longer exists) - dropped it so the agent can register again"
    return $true
}

# kubeconform-style strictness does not apply here, but the ordering does: the
# node object can only be dropped while the server answers, and the agent can only
# register while the server answers - both are already true at this point.
function Start-NodeWithRetry {
    param([string]$Name)
    $result = Start-Container $Name
    if ($result.Code -ne 0) { return $result }
    Start-Sleep -Seconds 10
    if (Test-Running $Name) { return $result }

    $log = Invoke-Docker -Line "logs --tail 20 $Name" -Seconds 20
    if ($log.Text -notmatch 'failed to find interface with specified node ip') { return $result }

    Write-Note "$Name exited on the stale node address - dropping the Node object and starting it again"
    if (-not (Reset-NodeRegistration $Name)) { return $result }
    $result = Start-Container $Name
    if ($result.Code -ne 0) { return $result }
    Start-Sleep -Seconds 10
    return $result
}

# A node container that still refuses to come back after the node object was
# dropped is the case where recreating the cluster is the dependable answer - the
# path section 17 already recorded as the one that works. About six minutes, and
# the bot's journal lives on a PVC that goes with the old cluster, so a rebuilt
# stand starts with an empty journal.
function Invoke-Rebuild {
    Write-Step "Rebuilding the cluster"
    Write-Note "A node container did not survive the stop, and k3s will not run without the"
    Write-Note "interface it expects. The stand is terraform, so it is recreated instead"
    Write-Note "(deploy.ps1 -Down, then -WithAIOps; FIXES.md, sections 17 and 22)."
    $deploy = Join-Path (Split-Path -Parent $PSScriptRoot) "deploy.ps1"
    & $deploy -Down
    if ($LASTEXITCODE -ne 0) { Write-Bad "deploy.ps1 -Down failed (exit $LASTEXITCODE)"; exit 3 }
    & $deploy -WithAIOps
    if ($LASTEXITCODE -ne 0) { Write-Bad "deploy.ps1 -WithAIOps failed (exit $LASTEXITCODE)"; exit 3 }
    Write-Host ""
    Write-Host "The stand was rebuilt and is running." -ForegroundColor Green
    exit 0
}

$kubeconfig = Get-Kubeconfig

# --- Status -----------------------------------------------------------------
function Invoke-Status {
    Write-Step "Docker engine"
    $engine = Invoke-Docker -Line "info --format {{.ServerVersion}}" -Seconds 15
    if ($engine.Code -ne 0) {
        Write-Bad "the engine did not answer: $($engine.Text)"
        Write-Note "Run deploy\diagnose-stand.ps1 first (FIXES.md, section 17)."
        exit 2
    }
    Write-Ok "engine $($engine.Text)"

    Write-Step "Containers"
    $listed = Invoke-Docker -Line "ps -a --filter name=k3d-sock-shop --format {{.Names}}|{{.Status}}" -Seconds 20
    if ($listed.Text) { $listed.Text -split "`n" | ForEach-Object { Write-Note $_ } }
    foreach ($name in @($balancer, $server) + $agents) {
        $state = Invoke-Docker -Line "inspect -f {{.State.Status}}/{{.HostConfig.RestartPolicy.Name}} $name" -Seconds 15
        if ($state.Code -eq 0 -and $state.Text) { Write-Note ("{0,-26} {1}" -f $name, $state.Text) }
        else { Write-Note ("{0,-26} missing" -f $name) }
    }

    Write-Step "Docker Desktop VM"
    $vm = Get-VmStat
    if ($vm -and $null -ne $vm.Load) {
        Write-Note ("load average: {0}" -f $vm.Load)
        if ($null -ne $vm.FreeMb) { Write-Note ("available: {0} MB" -f $vm.FreeMb) }
        if ($vm.Load -gt 50 -or ($null -ne $vm.FreeMb -and $vm.FreeMb -lt 200)) {
            Write-Bad "the VM is thrashing - .\deploy\stand.ps1 -Stop, then -Start"
        }
    } else {
        Write-Bad "could not read the VM"
    }

    Write-Step "Cluster"
    if (-not $kubeconfig) {
        Write-Bad "no kubeconfig - the cluster was never created (run deploy.ps1)"
        exit 4
    }
    $nodes = Invoke-Kubectl -Line "get nodes --request-timeout=10s" -Kubeconfig $kubeconfig -Seconds 25
    if ($nodes.Code -ne 0) { Write-Bad "kubectl cannot reach the API: $($nodes.Text)" }
    else { $nodes.Text -split "`n" | ForEach-Object { Write-Note $_ } }
    exit 0
}

# --- Stop -------------------------------------------------------------------
function Invoke-Stop {
    Write-Step "Docker engine"
    $version = Wait-Engine -Seconds 90
    if (-not $version) {
        Write-Bad "the engine does not answer, so nothing can be stopped cleanly"
        Write-Note "wsl --shutdown, start Docker Desktop, then: .\deploy.ps1 -Down; .\deploy.ps1"
        Write-Note "Full write-up: FIXES.md, section 17."
        exit 2
    }
    Write-Ok "engine $version"

    if ($Free) {
        Write-Step "Freeing what the stand accumulated"
        & (Join-Path $PSScriptRoot "cleanup-stand.ps1")
    }

    # Without this, the next Docker start brings all four containers up at once:
    # the state that produced load 355 and a dead-looking API (FIXES.md, 22).
    Write-Step "Restart policy -> no"
    $policy = Invoke-Docker -Line "update --restart=no $balancer $server $($agents -join ' ')" -Seconds 60
    if ($policy.Code -ne 0) { Write-Bad "docker update failed: $($policy.Text)"; exit 3 }
    Write-Ok "nothing will start by itself after a Docker restart"

    Write-Step "Stopping (agents first, so k3s writes its snapshot)"
    foreach ($name in @($agents + $server)) {
        $result = Invoke-Docker -Line "stop -t 60 $name" -Seconds 90
        if ($result.Code -eq 0) { Write-Note "$name stopped" }
        else { Write-Bad "${name}: $($result.Text)" }
    }
    $lb = Invoke-Docker -Line "stop -t 10 $balancer" -Seconds 30
    if ($lb.Code -eq 0) { Write-Note "$balancer stopped" } else { Write-Bad "$balancer : $($lb.Text)" }

    Write-Step "State"
    $listed = Invoke-Docker -Line "ps -a --filter name=k3d-sock-shop --format {{.Names}}|{{.Status}}" -Seconds 20
    if ($listed.Text) { $listed.Text -split "`n" | ForEach-Object { Write-Note $_ } }
    Write-Host ""
    Write-Host "The stand is stopped. Start it with: .\deploy\stand.ps1 -Start" -ForegroundColor Green
    exit 0
}

# --- Start ------------------------------------------------------------------
function Invoke-Start {
    Write-Step "Docker engine"
    $version = Wait-Engine -Seconds 180
    if (-not $version) {
        Write-Bad "the engine does not answer"
        Write-Note "wsl --shutdown, start Docker Desktop, then retry (FIXES.md, section 17)."
        exit 2
    }
    Write-Ok "engine $version"

    # The policy stays at `no` on purpose: it is what makes this script the only
    # thing that starts the stand. `unless-stopped` here would put the stampede
    # back on the next Docker restart.
    $null = Invoke-Docker -Line "update --restart=no $balancer $server $($agents -join ' ')" -Seconds 60

    Write-Step "Server"
    $result = Start-Container $server
    if ($result.Code -ne 0) { Write-Bad "${server}: $($result.Text)"; exit 3 }
    Write-Note "$server started"
    if (-not (Wait-ApiInCluster -Seconds 240)) {
        Write-Bad "the API inside the server did not answer within 240 s"
        Write-Note "closest next step: .\deploy\diagnose-stand.ps1"
        exit 3
    }
    Write-Ok "the API answers (asked inside the server, the load balancer is still down)"

    foreach ($agent in $agents) {
        # One node at a time is the whole point: the VM that could not answer
        # `docker ps` holds three nodes fine when they are not started together.
        $vm = Get-VmStat
        if ($vm -and ($vm.Load -gt 60 -or ($null -ne $vm.FreeMb -and $vm.FreeMb -lt 200))) {
            Write-Bad ("the VM has no headroom for another node: load {0}, available {1} MB" -f $vm.Load, $vm.FreeMb)
            Write-Note "The remaining agents were left stopped on purpose. Free the data first:"
            Write-Note "  .\deploy\cleanup-stand.ps1      then re-run .\deploy\stand.ps1 -Start"
            exit 4
        }
        $result = Start-NodeWithRetry $agent
        if ($result.Code -ne 0) { Write-Bad "${agent}: $($result.Text)"; exit 3 }
        # The node object can still say Ready for a moment after the agent died,
        # so ask the container itself before trusting the gate below.
        if (-not (Test-Running $agent)) {
            Write-Bad "$agent exited seconds after starting (exit 0 = the k3s node-address failure)"
            Write-Note "Its k3s log says 'failed to find interface with specified node ip', which means the"
            Write-Note "Node object still carries the address the container had before it was stopped."
            Write-Note "This script drops that object and starts the agent again; if it got here, that did"
            Write-Note "not help either."
            if (-not $NoRebuild) { Invoke-Rebuild }
            Write-Note "Recreate the cluster instead: .\deploy.ps1 -Down, then .\deploy.ps1 -WithAIOps"
            exit 5
        }
        $ready = Invoke-Docker -Line "exec $server kubectl wait --for=condition=Ready node/$agent --timeout=240s" -Seconds 260
        if ($ready.Code -ne 0) { Write-Bad "${agent} did not become Ready: $($ready.Text)"; exit 5 }
        Write-Ok "$agent Ready"
    }

    # Only now: k3d's nginx resolves every node name when it builds its config, so
    # a load balancer started before the agents exits with "host not found in
    # upstream" and nothing publishes 127.0.0.1:57443.
    Write-Step "Load balancer"
    $lb = Start-Container $balancer
    if ($lb.Code -ne 0) { Write-Bad "${balancer}: $($lb.Text)"; exit 3 }
    if (-not $kubeconfig) {
        Write-Bad "no kubeconfig - run .\deploy.ps1 once to create the cluster"
        exit 4
    }
    if (-not (Wait-Api -Kubeconfig $kubeconfig -Seconds 120)) {
        Write-Bad "127.0.0.1:57443 does not answer although the load balancer started"
        Write-Note "docker logs $balancer --tail 20 says why (usually 'host not found in upstream')"
        if (-not $NoRebuild) { Invoke-Rebuild }
        exit 3
    }
    Write-Ok "the API is reachable from the host"

    # A node that exits with code 0 shortly after it was Ready is the failure this
    # script exists to make visible: say it here instead of letting the workload
    # waits time out against a cluster that has already lost half of itself.
    $notRunning = @(@($balancer, $server) + $agents | Where-Object { -not (Test-Running $_) })
    if ($notRunning.Count -gt 0) {
        Write-Bad ("not running: " + ($notRunning -join ", "))
        Write-Note "A node that exits with code 0 reports 'failed to find interface with specified"
        Write-Note "node ip' in its k3s log."
        if (-not $NoRebuild) { Invoke-Rebuild }
        Write-Note "Recreate the cluster instead: .\deploy.ps1 -Down, then .\deploy.ps1 -WithAIOps"
        exit 5
    }
    Write-Ok "all four containers are running"

    Write-Step "Cluster"
    $nodes = Invoke-Kubectl -Line "get nodes --request-timeout=10s" -Kubeconfig $kubeconfig -Seconds 25
    if ($nodes.Text) { $nodes.Text -split "`n" | ForEach-Object { Write-Note $_ } }

    Write-Step "Workloads"
    $app = Invoke-Kubectl -Line "wait --for=condition=available deployment --all -n sock-shop --timeout=300s" -Kubeconfig $kubeconfig -Seconds 320
    if ($app.Code -ne 0) { Write-Bad "the application did not come back: $($app.Text)" }
    else { Write-Ok "the application is available" }
    $bot = Invoke-Kubectl -Line "-n monitoring rollout status deploy/triage-bot --timeout=180s" -Kubeconfig $kubeconfig -Seconds 200
    if ($bot.Code -ne 0) { Write-Bad "the triage bot did not come back: $($bot.Text)" }
    else { Write-Ok "the triage bot is Ready" }

    Write-Host ""
    Write-Host "The stand is up. It will NOT come back by itself after a Docker restart - that is" -ForegroundColor Green
    Write-Host "deliberate: use .\deploy\stand.ps1 -Start for that (FIXES.md, section 22)." -ForegroundColor Green
    exit 0
}

if ($Stop -and $Start) { throw "Choose one: -Stop, -Start or -Status." }
if ($Stop) { Invoke-Stop } elseif ($Start) { Invoke-Start } else { Invoke-Status }
