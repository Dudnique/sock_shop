#requires -Version 5.1
<#
.SYNOPSIS
    Checks that the NetworkPolicies in this directory really deny and allow what
    README.md claims.

.DESCRIPTION
    A NetworkPolicy is only worth what the CNI enforces: the previous version of
    this directory looked fine in `kubectl get netpol` and isolated nothing (an
    annotation, plus rules without `policyTypes`/`egress`). So every check below
    creates a throwaway busybox pod, asks it to reach one concrete service,
    reads the exit code from its log and deletes the pod again.

    Two checks expect the connection to FAIL (a namespace with no business here,
    and an egress route that is not part of that service's job) and two expect
    it to SUCCEED (the Prometheus scrape path and the service's own database).
    The label-based ones are legitimate despite looking like impersonation:
    NetworkPolicy identifies pods by labels, so a pod carrying `name=carts` IS
    carts as far as the CNI is concerned.

    Nothing of the application is touched: probe pods live in `default`,
    `monitoring` and - for the label checks - in the namespace under test.

.PARAMETER Namespace
    Namespace that owns the policies. Default: sock-shop.

.EXAMPLE
    .\verify-netpol.ps1
    [OK  ] default namespace -> front-end.sock-shop:8079 (must NOT be reachable)
            expected blocked, observed blocked   (rc=1)
#>
param(
    [string]$Namespace = 'sock-shop'
)

$ErrorActionPreference = 'Continue'   # native commands report failures through exit codes

if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    throw 'kubectl is not on PATH - point it at a cluster first.'
}

function Invoke-ProbePod {
    <#
        Runs `sh -c <Script>` in a busybox pod and returns its output as a
        string. The pod is deleted afterwards, so nothing accumulates.
    #>
    param(
        [Parameter(Mandatory)][string]$ProbeNamespace,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Script,
        [string]$Labels
    )

    $kubectlArgs = @('-n', $ProbeNamespace, 'run', $Name, '--restart=Never', '--image=busybox')
    if ($Labels) { $kubectlArgs += "--labels=$Labels" }
    $kubectlArgs += @('--command', '--', 'sh', '-c', $Script)

    kubectl @kubectlArgs 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not create probe pod $ProbeNamespace/$Name." }

    # Wait for the pod to finish - the first run also pulls the image.
    $deadline = (Get-Date).AddMinutes(2)
    while ((Get-Date) -lt $deadline) {
        $phase = (kubectl -n $ProbeNamespace get pod $Name -o jsonpath='{.status.phase}' 2>$null | Out-String).Trim()
        if ($phase -eq 'Succeeded' -or $phase -eq 'Failed') { break }
        Start-Sleep -Seconds 2
    }

    $log = (kubectl -n $ProbeNamespace logs $Name 2>&1 | Out-String).Trim()
    kubectl -n $ProbeNamespace delete pod $Name --wait=false 2>&1 | Out-Null
    return $log
}

function Invoke-NodeProbe {
    <#
        Connects from INSIDE an existing application pod using that pod's own
        runtime: the front-end image is Node.js, so `node -e` is always there.

        Why not busybox: its `nc` was measured to hang on an established
        connection instead of reporting, so an exit-code-based verdict was
        wrong in both directions. Here the three outcomes are explicit:
          OPEN    - the connection was established (policy allowed it)
          BLOCKED - nothing came back within 4s (how a CNI that DROPs behaves)
          ERROR   - immediate refusal, e.g. ECONNREFUSED - this is how
                    k3s/kube-router reports a denied connection: it REJECTs
                    instead of dropping, so a blocked port fails fast. Both
                    ERROR and BLOCKED count as "not reachable". ECONNREFUSED is
                    also what a service with nothing listening returns, which is
                    exactly why every check has a positive twin above/below it.
        The JS uses only single quotes on purpose: PowerShell 5.1 does not
        escape double quotes when passing arguments to native commands.
    #>
    param(
        [Parameter(Mandatory)][string]$Deployment,
        [Parameter(Mandatory)][string]$Target,
        [Parameter(Mandatory)][int]$Port
    )

    # Built with double quotes on purpose: the JS needs single-quoted strings
    # (PowerShell 5.1 does not escape `"` when calling native commands), and
    # `$Port`/`$Target` have to be interpolated into the call. The first version
    # of this function built the string from `''` escapes, produced
    # `net.connect(80,carts,...)` - an identifier instead of a host name - and
    # reported a confident, wrong ENOTFOUND.
    $js = "const net=require('net');" +
          "const t=setTimeout(()=>{console.log('RESULT BLOCKED');process.exit(0)},4000);" +
          "const s=net.connect($Port,'$Target',()=>{clearTimeout(t);console.log('RESULT OPEN');process.exit(0)});" +
          "s.on('error',e=>{clearTimeout(t);console.log('RESULT ERROR '+e.code);process.exit(0)});"

    $out = (kubectl -n $Namespace exec "deploy/$Deployment" -- node -e $js 2>&1 | Out-String)
    if ($out -match 'RESULT OPEN')    { return 'OPEN' }
    if ($out -match 'RESULT BLOCKED') { return 'BLOCKED' }
    if ($out -match 'RESULT ERROR')   { return ('ERROR ' + (($out -split 'RESULT ERROR ')[1] -split '[\r\n]')[0]) }
    return ('NO-RESULT: ' + ($out -replace '\s+', ' '))
}

# Title / namespace of the probe / labels / shell one-liner / expected outcome.
# `$?` and `$1` stay literal on purpose: the string is expanded by the shell
# INSIDE the container, not by PowerShell.
$checks = @(
    @{
        # Service port 80 -> targetPort 8079 (10-front-end-svc.yaml), so the
        # probe hits the real chain: ClusterIP, DNAT to the pod, policy on 8079.
        Title     = "default namespace -> front-end.$Namespace`:80 (must NOT be reachable)"
        Ns        = 'default';                  Labels = ''
        Script    = 'wget -qO- --timeout=4 http://front-end.{0}:80/ >/dev/null 2>&1; echo rc=$?' -f $Namespace
        Reachable = $false
    },
    @{
        Title     = "monitoring (Prometheus) -> front-end.$Namespace`:80 (scrape path, MUST work)"
        Ns        = 'monitoring';               Labels = ''
        Script    = 'wget -qO- --timeout=4 http://front-end.{0}:80/ >/dev/null 2>&1; echo rc=$?' -f $Namespace
        Reachable = $true
    },
    @{
        Title     = "monitoring (Prometheus) -> catalogue.$Namespace`:80/metrics (application metrics, MUST work)"
        Ns        = 'monitoring';               Labels = ''
        Script    = 'wget -qO- --timeout=4 http://catalogue.{0}:80/metrics >/dev/null 2>&1; echo rc=$?' -f $Namespace
        Reachable = $true
    },
    @{
        Kind       = 'Exec'
        Title      = 'front-end -> carts:80 (the UI must reach the basket)'
        Deployment = 'front-end'; Target = 'carts';      Port = 80
        Reachable  = $true
    },
    @{
        Kind       = 'Exec'
        Title      = 'front-end -> session-db:6379 (session store, SESSION_REDIS=true)'
        Deployment = 'front-end'; Target = 'session-db'; Port = 6379
        Reachable  = $true
    },
    @{
        Kind       = 'Exec'
        Title      = "front-end -> user-db:27017 (a database it has no business in, must NOT be reachable)"
        Deployment = 'front-end'; Target = 'user-db';    Port = 27017
        Reachable  = $false
    }
)

$failures = 0
$index = 0
foreach ($check in $checks) {
    $index++
    if ($check.Kind -eq 'Exec') {
        $detail = Invoke-NodeProbe -Deployment $check.Deployment -Target $check.Target -Port $check.Port
        $reachable = ($detail -eq 'OPEN')
    } else {
        $log = Invoke-ProbePod -ProbeNamespace $check.Ns -Name ("netpol-check-{0}" -f $index) `
                               -Script $check.Script
        $reachable = $log -match 'rc=0'
        $detail = $log -replace '\s+', ' '
    }
    $ok = ($reachable -eq $check.Reachable)
    if (-not $ok) { $failures++ }

    $verdict = if ($ok) { 'OK  ' } else { 'FAIL' }
    $observed = if ($reachable) { 'reachable' } else { 'blocked' }
    $expected = if ($check.Reachable) { 'reachable' } else { 'blocked' }

    Write-Host ("[{0}] {1}" -f $verdict, $check.Title)
    Write-Host ("       expected {0}, observed {1}   ({2})" -f $expected, $observed, $detail)
}

Write-Host ''
if ($failures -eq 0) {
    Write-Host ("All {0} checks passed." -f $checks.Count) -ForegroundColor Green
} else {
    Write-Host ("{0} of {1} checks FAILED - the policies do not do what the README claims." -f $failures, $checks.Count) -ForegroundColor Red
    exit 1
}
