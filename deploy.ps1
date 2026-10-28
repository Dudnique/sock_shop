# ---------------------------------------------------------------------------
# deploy.ps1 - one-command bring-up / teardown of the Sock Shop stand.
#
# Thin ORCHESTRATOR. It creates no infrastructure itself:
#   1. Terraform provisions the k3d cluster  (single source of truth for infra).
#   2. kubectl deploys the application        (manifests).
#      With -Helm the same step installs deploy\kubernetes\helm-chart instead; the
#      rest of the stand is unchanged, because the chart labels its pods exactly
#      like manifests\ does and the policies/autoscalers select on those labels.
#   3. kubectl deploys the logging stack      (manifests-logging: Loki + Promtail).
#   4. kubectl deploys the tracing stack      (manifests-jaeger).
#   5. kubectl deploys the monitoring stack   (manifests-monitoring).
#   6. kubectl deploys the alerting stack     (manifests-alerting) + secrets.
#   7. (optional, -WithLoadtest) the load generator (manifests-loadtest).
#   8. (optional, -WithAIOps) the AIOps triage bot (manifests-aiops): the image
#      is built here, imported into the k3d node and the manifests applied.
#   It prints the resulting URLs.
#
# GITOPS MODE (-GitOps): steps 2-7 are not run by this script. It creates the
# cluster, installs Argo CD with Helm and applies one root Application; Argo CD
# then deploys every directory from this repository (deploy\kubernetes\argocd),
# including Vault and External Secrets. The secrets are NOT created here any more:
# once Vault is up, deploy\vault-seed.ps1 writes them into it from the same local
# file, and External Secrets turns them into the Kubernetes Secrets. -GitRevision <branch> makes it read a
# branch instead of main, which is how a change is tried before it is merged.
#
# The cluster is deliberately NOT created with `k3d cluster create` here: that
# would duplicate terraform/k3d and let the two definitions drift apart.
#
# SECRETS: the alerting stack needs two secrets - a Telegram bot token and a
# Slack incoming-webhook URL. They are NEVER committed. Provide them either as
# environment variables (TELEGRAM_BOT_TOKEN, SLACK_WEBHOOK_URL) or in a local,
# git-ignored file: deploy\secrets.local.ps1
# The AIOps bot (only with -WithAIOps) takes an optional third credential, the
# DeepSeek API key (DEEPSEEK_API_KEY), from the environment or from that same
# file. It is optional on purpose: without it the bot still deploys on the
# rules-only path and every card says which tier answered.
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
#          .\deploy.ps1 -WithAIOps    # ...and deploy the AIOps triage bot
#          .\deploy.ps1 -Helm         # ...and install the application from the Helm chart
#          .\deploy.ps1 -GitOps      # Argo CD deploys everything from git (see below)
#          .\deploy.ps1 -Down         # tear it down
# ---------------------------------------------------------------------------
[CmdletBinding()]
param(
    [switch]$Down,
    [string]$TfDir,
    # Optional: also deploy the k6 load generator (manifests-loadtest).
    # Without traffic the dashboards stay empty and the alert rules have
    # nothing to fire on, so this is what makes the stand "alive" for a demo.
    [switch]$WithLoadtest,
    # Optional: also deploy the AIOps triage bot (manifests-aiops): build the
    # image, import it into the k3d node, apply the manifests and wait for it to
    # become Ready. Nothing happens without this switch, so the stand is
    # unchanged for anyone who only wants the application stack.
    # A DEEPSEEK_API_KEY in the environment or in deploy\secrets.local.ps1 is
    # used if present; without it the bot comes up on the rules-only path.
    [switch]$WithAIOps,
    # Optional: install the application from deploy\kubernetes\helm-chart instead
    # of `kubectl apply -f manifests\`. Everything else stays on the raw
    # manifests: the chart covers the app (8 services, 6 datastores, RabbitMQ,
    # ingress), so network policies, autoscalers, monitoring, alerting and the bot
    # are applied exactly as before - they select pods by the same `name: <svc>`
    # label the chart uses.
    # Trade-offs, both deliberate: manifests\ remains the source of truth (the
    # chart is a second copy of the same objects, so change both), and the chart
    # can only be installed into a cluster where those objects are not already
    # owned by an earlier `kubectl apply` (helm refuses to adopt them).
    [switch]$Helm,
    # Optional: let Argo CD deploy the stand from git instead of this script
    # applying the manifests (deploy\kubernetes\argocd). The cluster and the AIOps
    # image are still created here; the secrets go into Vault (vault-seed.ps1). The
    # application comes from the Helm chart, so -Helm is not needed (and refused).
    [switch]$GitOps,
    # The branch or tag Argo CD reads in -GitOps mode. `main` unless a change is
    # being tried on a live stand before it is merged.
    [string]$GitRevision = "main"
)

$ErrorActionPreference = "Stop"

# $PSScriptRoot is not reliably set inside a param() default block, so resolve
# the default here, in the script body (where it is always available).
if (-not $TfDir) { $TfDir = Join-Path $PSScriptRoot "terraform\k3d" }

foreach ($tool in @("terraform", "docker", "kubectl") + $(if ($Helm -or $GitOps) { @("helm") } else { @() })) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "Tool '$tool' not found in PATH. Install it and retry."
    }
}

if ($GitOps -and $Helm) {
    throw "-GitOps already deploys the application from the Helm chart; drop -Helm."
}
if ($GitOps -and $WithLoadtest -and -not $Down) {
    Write-Host "    -WithLoadtest in GitOps mode turns the loadtest Application on; it is not applied by this script." -ForegroundColor Yellow
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
    param(
        [Parameter(Mandatory)][string[]] $Arguments,
        # The probes below are all kubectl, but `k3d image import` needs the same
        # stderr protection: the helper used to hard-code `kubectl`, so the AIOps
        # step ran `kubectl image import` and failed with exit 1 on every run.
        [string] $Command = "kubectl",
        # kubectl probes are only interested in stdout, so stderr lines are
        # dropped. k3d reports the actual reason on stderr, so the AIOps caller
        # keeps it - otherwise the failure is an empty string.
        [switch] $KeepStderr
    )
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $raw = & $Command @Arguments 2>&1
        $exitCode = $LASTEXITCODE
        $stdout = $raw | Where-Object {
            $KeepStderr -or $_ -isnot [System.Management.Automation.ErrorRecord]
        }
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

    # --- 0. Docker engine preflight -----------------------------------------
    # The stand's most confusing failure is not a terraform error: it is a Docker
    # Desktop VM that came back from an unclean shutdown with all four k3d
    # containers starting at once. In that state `docker` and `kubectl` both hang
    # (or answer 500 on the named pipe) and terraform fails for a reason that
    # names none of it - which is exactly how a half-hour went missing once.
    # Ask the engine once, with a deadline, then name the recovery command.
    # Full analysis: FIXES.md, section 17.
    $engineJob = Start-Job -ScriptBlock { docker info --format "{{.ServerVersion}}|{{.MemTotal}}" }
    try {
        $engineAnswered = [bool](Wait-Job $engineJob -Timeout 15)
        $engineOutput = if ($engineAnswered) {
            ((Receive-Job $engineJob 2>&1 | ForEach-Object { "$_" }) -join "`n").Trim()
        } else { "" }
    } finally {
        Remove-Job $engineJob -Force -ErrorAction SilentlyContinue
    }
    if (-not $engineAnswered) {
        throw ("The Docker engine did not answer within 15 s, so nothing was deployed: the " +
               "containers may still be listed, but the VM behind them is not serving. Recover with " +
               "'wsl --shutdown', start Docker Desktop, then '.\\deploy\\stand.ps1 -Start' if the " +
               "cluster is intact (it starts one node at a time) or '.\\deploy.ps1 -Down' + " +
               "'.\\deploy.ps1' if it is not. 'deploy\\diagnose-stand.ps1' says which of the three " +
               "failures it is (FIXES.md, section 17).")
    }
    if ($engineOutput -match '(\d+\.\d+[^\|]*)\|(\d+)') {
        $engineVersion = $matches[1].Trim()
        $vmMemoryGb = [math]::Round([int64]$matches[2] / 1GB, 1)
        Write-Host "    Docker engine $engineVersion, VM memory $vmMemoryGb GB"
        # Not fatal: the stand does fit, it just grows. A fresh cluster sits at
        # ~4.6 GB of this VM; after three days the failed one had reached 7.4 GB,
        # which is where a cold start stops fitting.
        if ($vmMemoryGb -lt 8) {
            Write-Host ("    NOTE: a fresh stand uses ~4.6 GB of this VM, but the number grows day by day " +
                        "(it reached 7.4 GB before failing once). The growth is discarded data, not state: " +
                        "run 'deploy\cleanup-stand.ps1' to drop it without a rebuild. Rebuild with " +
                        "'deploy.ps1 -Down' + '-WithAIOps' only if the stand does not come back. " +
                        "'memory=10GB' in %UserProfile%\.wslconfig is an optional extra (FIXES.md, section 17).") -ForegroundColor Yellow
        }
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

    # k3d writes its OWN random API port into the kubeconfig even when the k3d
    # config pins one: measured on this stand, the load balancer published
    # 6443 -> 57443 while the generated kubeconfig said 50950, and every kubectl
    # call after a green `terraform apply` died with "connection refused" - the
    # cluster was healthy, the file was wrong. Point it at the real port, and
    # keep the merged ~/.kube/config in step as well.
    $apiPort  = terraform output -raw api_port
    $kubePath = $env:KUBECONFIG
    $kubeText = Get-Content $kubePath -Raw
    # 127.0.0.1, not 0.0.0.0: a kubeconfig whose server is `https://0.0.0.0:57443`
    # makes kubectl on Windows hang in a TLS handshake, while the very same API
    # answers `curl -k https://127.0.0.1:57443/healthz` with 401.
    $fixed    = $kubeText -replace 'https://(0\.0\.0\.0|127\.0\.0\.1|localhost):\d+', "https://127.0.0.1:$apiPort"
    if ($fixed -ne $kubeText) {
        Set-Content -Path $kubePath -Value $fixed -NoNewline
        Write-Host "    kubeconfig API address -> https://127.0.0.1:$apiPort" -ForegroundColor Yellow
    }
}
finally {
    Pop-Location
}

Write-Host "    KUBECONFIG=$env:KUBECONFIG"

Write-Host "    Waiting for nodes to be ready..." -ForegroundColor Cyan
kubectl wait --for=condition=Ready node --all --timeout=180s | Out-Null

# --- 1b. Never let the four k3d containers start together ---------------------
# k3d creates them with `unless-stopped`, so a Docker Desktop restart starts the
# load balancer, the server and both agents in the same second while k3s replays
# its snapshot. Measured on this stand: load average 355 on a 7.5 GB VM, 68 MB
# free, `docker ps` answering 500 and every `kubectl` call dying at the TLS
# handshake - the cluster looked dead while it was only starving (FIXES.md,
# section 22). With the policy at `no`, starting the stand becomes the explicit
# `deploy\stand.ps1 -Start`, which starts one node at a time and waits.
$k3dContainers = @(docker ps --filter "name=k3d-sock-shop" --format "{{.Names}}" 2>$null |
    Where-Object { $_ -match '^k3d-sock-shop-' })
if ($k3dContainers.Count -gt 0) {
    docker update --restart=no $k3dContainers | Out-Null
    # The parentheses matter: `-f` binds to the *last* operand of `+`, so without
    # them the format string was `deploy\stand.ps1 -Start` (no placeholders) and
    # the line printed a literal `{0}` instead of the container count. Seen in the
    # first -Helm run on a fresh cluster (FIXES.md, section 24).
    Write-Host (("    restart policy -> no on {0} k3d container(s); start the stand with " +
                 "deploy\stand.ps1 -Start") -f $k3dContainers.Count) -ForegroundColor Gray
}

# --- 2. Application ---------------------------------------------------------
# Two interchangeable ways to create the same application objects. -Helm switches
# only this step, so network policies, autoscalers, logging, tracing, monitoring,
# alerting and the bot below are applied from the raw manifests either way.
if ($GitOps) {
    Write-Host "==> 2/8 Application: left to Argo CD (GitOps mode)" -ForegroundColor Cyan
} elseif ($Helm) {
    Write-Host "==> 2/8 Application (sock-shop, Helm chart)" -ForegroundColor Cyan
    $chart = Join-Path $PSScriptRoot "deploy\kubernetes\helm-chart"
    # --wait because the rest of the script assumes a working application, and the
    # timeout is generous because the chart keeps the upstream probe timings
    # (readiness of carts and shipping starts at 360 s). --rollback-on-failure rolls
    # the release back instead of leaving half an application behind on failure -
    # the same thing `--atomic` does, minus the deprecation warning Helm 4 prints
    # for the old name.
    #
    # 15m, not 10m: a first install takes about six minutes (FIXES.md, section 24),
    # but an upgrade of a stand that is already serving traffic takes about fifteen,
    # because every replica the HPAs add mid-roll-out waits its own 360 s of
    # initialDelaySeconds before it counts as available (section 25).
    #
    # --server-side=true --force-conflicts because the HPAs own `spec.replicas`.
    # Helm 4 applies server-side by default, so the second run of `-Helm` on a stand
    # that has been under load failed with `conflict with "k3s" with subresource
    # "scale"` on the deployments the autoscalers had resized - and the rollback
    # failed with the same conflict, leaving the release neither upgraded nor
    # rolled back (FIXES.md, section 25). Forcing the conflict resolves it in the
    # deploy's favour; the HPA re-scales from the next sample on.
    helm upgrade --install sock-shop $chart --namespace sock-shop --create-namespace `
        --server-side=true --force-conflicts `
        --rollback-on-failure --wait --timeout 15m
    if ($LASTEXITCODE -ne 0) { throw "helm upgrade --install sock-shop failed (exit $LASTEXITCODE)" }
} else {
    Write-Host "==> 2/8 Application (sock-shop)" -ForegroundColor Cyan
    kubectl apply -f (Join-Path $PSScriptRoot "deploy\kubernetes\manifests")
    # 15m and not 300s for the same reason the Helm path passes --timeout 15m: the
    # manifests now carry the chart's readinessProbe timings, and carts and shipping
    # start their readiness checks at 360 s. 300 s was measured against a stand whose
    # pods had no probes at all - a wait that only ever asked "is the container
    # running", while the Service was already sending traffic to a JVM that was not
    # listening yet (FIXES.md, section 26).
    kubectl wait --for=condition=available --timeout=900s deployment --all -n sock-shop
    if ($LASTEXITCODE -ne 0) { throw "deployments in sock-shop did not become available (exit $LASTEXITCODE)" }
}

# --- 3. Network policies ----------------------------------------------------
# Before the monitoring stack on purpose: the policies open the scrape path for
# Prometheus by namespace, and a policy applied after the first scrape would
# leave the monitoring stack with dead targets until the next rollout.
# `kubectl apply -f <dir>` does not recurse, so nothing under legacy/ is ever
# applied by accident. Verification is a separate, explicit step:
#   .\deploy\kubernetes\manifests-policy\verify-netpol.ps1
Write-Host "==> 3/8 Network policies (manifests-policy)" -ForegroundColor Cyan
if (-not $GitOps) {
    kubectl apply -f (Join-Path $PSScriptRoot "deploy\kubernetes\manifests-policy")
}

# --- 4. Autoscaling (HPA v2) ------------------------------------------------
# Two prerequisites, and both are worth stating because the stand used to have
# neither in effect: `requests.cpu` on the target pods (the HPA scales on a
# PERCENTAGE of the request) and metrics-server (the usage comes from the
# metrics.k8s.io API). k3s ships metrics-server, so this is one line here - and
# the wait below turns a missing metrics-server into a clear failure instead of
# leaving eight dead autoscalers behind, which is how this repository looked
# before: 8 manifests, 0 HPAs in the cluster.
Write-Host "==> 4/8 Autoscaling (HPA v2)" -ForegroundColor Cyan
if (-not $GitOps) {
kubectl apply -f (Join-Path $PSScriptRoot "deploy\kubernetes\autoscaling")

$hpaDeadline = (Get-Date).AddMinutes(2)
$hpaMetricsReady = $false
while ((Get-Date) -lt $hpaDeadline) {
    # No `{"\n"}` inside the jsonpath: PowerShell 5.1 drops those embedded double
    # quotes when it hands the argument to kubectl, which then receives a bare
    # `\n` and dies with "unrecognized character in action: U+005C '\'" - the
    # script exited right here on every run and steps 5-8 never happened.
    # The metrics are only pattern-matched below, so a concatenated list is fine.
    $hpaMetrics = kubectl -n sock-shop get hpa `
        -o jsonpath='{range .items[*]}{.status.currentMetrics}{end}' 2>$null
    if ($hpaMetrics -match 'averageUtilization') { $hpaMetricsReady = $true; break }
    Start-Sleep -Seconds 5
}
if (-not $hpaMetricsReady) {
    throw "No HPA reported CPU metrics within 2 minutes. HPA needs metrics-server (k3s ships it) and requests.cpu on the target pods; without both, the objects exist but never scale."
}
Write-Host "    HPA metrics are flowing (metrics-server reachable)." -ForegroundColor Green
}

# --- 5. Logging (Loki + Promtail) -------------------------------------------
# Applied after the application on purpose: Promtail tails the log files of the
# pods that are already running, so Loki starts collecting immediately.
Write-Host "==> 5/8 Logging (Loki + Promtail)" -ForegroundColor Cyan
if (-not $GitOps) { kubectl apply -f (Join-Path $PSScriptRoot "deploy\kubernetes\manifests-logging") }

# --- 6. Tracing (Jaeger) ----------------------------------------------------
Write-Host "==> 6/8 Tracing (Jaeger)" -ForegroundColor Cyan
if (-not $GitOps) { kubectl apply -f (Join-Path $PSScriptRoot "deploy\kubernetes\manifests-jaeger") }

# --- 7. Monitoring ----------------------------------------------------------
Write-Host "==> 7/8 Monitoring" -ForegroundColor Cyan

# The dashboard/datasource import is a one-shot Job and a Job spec is immutable,
# so a plain re-apply of an existing Job fails. Delete the finished Job first:
# the import then always runs against the current configmap.
if (-not $GitOps) {
    kubectl -n monitoring delete job grafana-import-dashboards --ignore-not-found | Out-Null
}

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

if ($GitOps) {
    # The Secret is built by External Secrets from Vault; vault-seed.ps1 fills
    # Vault (below) and returns the password for the summary.
    $grafanaUser = if ($env:GRAFANA_ADMIN_USER) { $env:GRAFANA_ADMIN_USER } else { "admin" }
    $grafanaPassword = ""
    $grafanaPasswordGenerated = $false
} else {
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
}

if (-not $GitOps) { kubectl apply -f (Join-Path $PSScriptRoot "deploy\kubernetes\manifests-monitoring") }

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
if ($GitOps) {
    # No cluster Secret to fall back on and nothing to create here: the values go
    # into Vault, so they must be available now.
    if (-not $env:TELEGRAM_BOT_TOKEN -or -not $env:SLACK_WEBHOOK_URL) {
        throw ("-GitOps needs TELEGRAM_BOT_TOKEN and SLACK_WEBHOOK_URL (they are written into Vault): " +
               "set them in deploy\secrets.local.ps1 (git-ignored; see README, section Secrets).")
    }
} else {
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
           "Create deploy\secrets.local.ps1 (it is git-ignored; see README, section Secrets) " +
           "and set $env:TELEGRAM_BOT_TOKEN and $env:SLACK_WEBHOOK_URL in it - there is no need to type a token on a command line.")
}

}

# note: examples/ is a sub-directory, and `kubectl apply -f <dir>` is NOT
# recursive, so the *.example.yaml files inside it are intentionally ignored.
if (-not $GitOps) { kubectl apply -f (Join-Path $PSScriptRoot "deploy\kubernetes\manifests-alerting") }

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
if (-not $GitOps) {
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
}

# --- 7. Load generator (optional) -------------------------------------------
# Deliberately optional: two k6 pods hammering front-end are noise in a
# training cluster, but they are exactly what makes the dashboards, the alert
# rules and the traces non-empty (see manifests-loadtest/loadtest-dep.yaml).
if ($WithLoadtest -and -not $GitOps) {
    Write-Host "==> load generator (manifests-loadtest)" -ForegroundColor Cyan
    kubectl apply -f (Join-Path $PSScriptRoot "deploy\kubernetes\manifests-loadtest")
    kubectl -n loadtest rollout status deploy/load-test --timeout=120s | Out-Null
}

# --- AIOps triage bot (optional) --------------------------------------------
# Deliberately optional: the alerting stack works without it, and the bot is the
# component of the stand that is still being built (M1 = advisor only, it acts on
# nothing). With the switch it is built, pushed into the node and applied.
if ($WithAIOps) {
    Write-Host "==> AIOps triage bot (manifests-aiops)" -ForegroundColor Cyan

    # The image is published nowhere: it is built here and imported straight into
    # the k3d node's containerd, which is why the Deployment says IfNotPresent.
    # Terraform owns the cluster on this stand, but importing an image is a k3d
    # CLI operation with no Terraform equivalent - so the CLI is required for
    # this step only, and the check lives here rather than at the top of the
    # script where it would break `-Down` and plain deployments.
    if (-not (Get-Command k3d -ErrorAction SilentlyContinue)) {
        throw ("-WithAIOps needs the k3d CLI to get the locally built image into the node " +
               "(k3d image import). Install k3d and retry, or deploy the app stack without the switch.")
    }

    $botDir = Join-Path $PSScriptRoot "aiops\triage-bot"
    # Same tag as spec.template.spec.containers[0].image in 02-triage-bot-dep.yaml.
    $botImage = "triage-bot:0.1.0-m1"
    Write-Host "    Building $botImage from aiops\triage-bot\Dockerfile..." -ForegroundColor Cyan
    # `docker build` writes its progress to stderr, and with
    # $ErrorActionPreference = "Stop" one stderr byte from a native command is a
    # terminating error: the image was built and the script died before the
    # import, leaving a deploy that looked finished with no bot in the cluster.
    # The same trap as the probes at the top of this script, so the same helper.
    $build = Invoke-QuietProbe -Command docker -KeepStderr `
        -Arguments @("build", "-t", $botImage, $botDir)
    if ($build.ExitCode -ne 0) {
        throw "docker build failed for $botImage (exit $($build.ExitCode)): $($build.Output)"
    }

    # `k3d image import` writes its progress to stderr, and with
    # $ErrorActionPreference = "Stop" a single stderr byte from a native command
    # is a terminating error - the same trap the probes at the top of this script
    # exist for, so they are used here too.
    $import = Invoke-QuietProbe -Command k3d -KeepStderr `
        -Arguments @("image", "import", $botImage, "-c", "sock-shop")
    if ($import.ExitCode -ne 0) {
        throw "k3d image import failed (exit $($import.ExitCode)): $($import.Output)"
    }
    Write-Host "    Image imported into the k3d node 'sock-shop'." -ForegroundColor Green

    # The model key is OPTIONAL and follows the same contract as
    # alertmanager-secrets above: taken from the environment or the git-ignored
    # secrets file, otherwise read back out of the Secret already in the cluster,
    # and never typed as a literal on a command line. Missing everywhere is not an
    # error: the bot comes up on the rules-only path and says so in /healthz and
    # on every card, which is a better outcome than a failed deploy.
    if (-not $GitOps) {
    $llmSecretExists = (Invoke-QuietProbe -Arguments @(
        "-n", "monitoring", "get", "secret", "aiops-llm", "-o", "name")).ExitCode -eq 0
    if (-not $env:DEEPSEEK_API_KEY -and $llmSecretExists) {
        $env:DEEPSEEK_API_KEY = Get-ClusterSecretValue -Namespace monitoring `
            -SecretName aiops-llm -Key deepseek_api_key
    }
    if ($env:DEEPSEEK_API_KEY) {
        kubectl -n monitoring create secret generic aiops-llm `
            --from-literal=deepseek_api_key=$env:DEEPSEEK_API_KEY `
            --dry-run=client -o yaml | kubectl apply -f -
        Write-Host "    aiops-llm Secret applied (model tier is available)."
    } elseif ($llmSecretExists) {
        Write-Host "    aiops-llm exists but its value is not readable - leaving it untouched"
    } else {
        Write-Host "    No DEEPSEEK_API_KEY in the environment or in $secretsFile - deploying the rules-only path." -ForegroundColor Yellow
        Write-Host "    The bot still triages; /healthz will report the model tier as unavailable." -ForegroundColor Yellow
    }
    }

    # note: examples/ is a sub-directory, and `kubectl apply -f <dir>` is NOT
    # recursive, so examples/triage-bot-secret.example.yaml is never applied.
    if (-not $GitOps) {
        kubectl apply -f (Join-Path $PSScriptRoot "deploy\kubernetes\manifests-aiops")
        kubectl -n monitoring rollout status deploy/triage-bot --timeout=180s | Out-Null
        Write-Host "    triage-bot is Ready (listening on triage-bot.monitoring.svc.cluster.local:8080)." -ForegroundColor Green
    } else {
        Write-Host "    Image and Secret are in place; Argo CD applies manifests-aiops below." -ForegroundColor Green
    }
}

# --- GitOps: Argo CD and the app-of-apps ------------------------------------
# Everything above that git cannot hold (the cluster, the three Secrets, the bot
# image) exists by now. What is left is to hand the rest to Argo CD: install it,
# give it one root Application, and wait for it to make the stand match git.
if ($GitOps) {
    Write-Host "==> GitOps: Argo CD and the app-of-apps (revision: $GitRevision)" -ForegroundColor Cyan
    $argoDir = Join-Path $PSScriptRoot "deploy\kubernetes\argocd"

    # Pinned, like every other version on this stand: argo-cd chart 10.11.6 is
    # Argo CD v3.5.6. --wait returns when the four components are available.
    helm repo add argo https://argoproj.github.io/argo-helm --force-update | Out-Null
    helm upgrade --install argocd argo/argo-cd --version 10.11.6 `
        --namespace argocd --create-namespace `
        -f (Join-Path $argoDir "values.yaml") --wait --timeout 10m
    if ($LASTEXITCODE -ne 0) { throw "helm upgrade --install argocd failed (exit $LASTEXITCODE)" }

    # The root Application is a file in git; only the revision and the two
    # optional components are filled in here. The patterns are narrow on purpose:
    # each matches the one line it is meant to change and fails loudly (below)
    # if the file ever stops containing it.
    $root = Get-Content (Join-Path $argoDir "root-app.yaml") -Raw
    $root = $root -replace 'targetRevision: main', "targetRevision: $GitRevision"
    $root = $root -replace '(name: revision\s+value: )main', "`$1$GitRevision"
    if ($WithAIOps) { $root = $root -replace '(name: aiops\.enabled\s+value: )"false"', '$1"true"' }
    if ($WithLoadtest) { $root = $root -replace '(name: loadtest\.enabled\s+value: )"false"', '$1"true"' }
    if ($root -notmatch "targetRevision: $([regex]::Escape($GitRevision))") {
        throw "root-app.yaml does not contain the revision after substitution - check the patterns in deploy.ps1"
    }
    $root | kubectl apply -f -
    if ($LASTEXITCODE -ne 0) { throw "applying the root Application failed (exit $LASTEXITCODE)" }

    # Vault is the first Application (wave -3) and External Secrets the second;
    # everything else waits behind them. As soon as the Vault pod is Ready it is
    # empty: fill it now, and the waves can proceed. Order matters: seeding any
    # later would leave the ExternalSecrets failing and the waves stuck.
    Write-Host "    Waiting for the Vault pod, then writing the secrets into it (deploy\vault-seed.ps1)..." -ForegroundColor Cyan
    $vaultDeadline = (Get-Date).AddMinutes(10)
    $vaultUp = $false
    while ((Get-Date) -lt $vaultDeadline) {
        $vp = Invoke-QuietProbe -Arguments @("-n", "vault", "get", "pod", "vault-0",
            "-o", "jsonpath={.status.containerStatuses[0].ready}")
        if ($vp.ExitCode -eq 0 -and "$($vp.Output)".Trim() -eq "true") { $vaultUp = $true; break }
        Start-Sleep -Seconds 5
    }
    if (-not $vaultUp) {
        throw "Vault did not become Ready within 10 minutes. See: kubectl -n argocd get application vault"
    }
    $grafanaPassword = & (Join-Path $PSScriptRoot "deploy\vault-seed.ps1") | Select-Object -Last 1

    # The root plus one Application per entry of apps/values.yaml (10 always: Vault,
    # External Secrets and the ExternalSecrets among them; the bot and the load
    # generator when asked for).
    $expectedApps = 1 + 10 + $(if ($WithAIOps) { 1 } else { 0 }) + $(if ($WithLoadtest) { 1 } else { 0 })
    Write-Host "    Waiting for $expectedApps Applications to be Synced and Healthy (about 10 minutes from an empty machine, measured; the JVM services start their readiness checks at 360-420 s)..." -ForegroundColor Cyan
    $gitopsDeadline = (Get-Date).AddMinutes(40)
    $lastReport = ""
    $allGreen = $false
    while ((Get-Date) -lt $gitopsDeadline) {
        $raw = kubectl -n argocd get applications -o json 2>$null
        if ($LASTEXITCODE -eq 0 -and $raw) {
            $items = @(($raw | ConvertFrom-Json).items)
            $report = ($items | Sort-Object { $_.metadata.name } | ForEach-Object {
                "{0}={1}/{2}" -f $_.metadata.name, $_.status.sync.status, $_.status.health.status
            }) -join "  "
            if ($report -ne $lastReport) { Write-Host "    $report"; $lastReport = $report }
            $green = @($items | Where-Object { $_.status.sync.status -eq "Synced" -and $_.status.health.status -eq "Healthy" })
            if ($items.Count -ge $expectedApps -and $green.Count -eq $items.Count) { $allGreen = $true; break }
        }
        Start-Sleep -Seconds 20
    }
    if (-not $allGreen) {
        throw "Argo CD Applications are not all Synced and Healthy after 40 minutes. See: kubectl -n argocd get applications"
    }
    Write-Host "    All Applications are Synced and Healthy." -ForegroundColor Green
}

# --- Summary ----------------------------------------------------------------
Write-Host ""
Write-Host "Done. URLs:" -ForegroundColor Green
Write-Host ("  Sock Shop   {0}" -f $appUrl)
if ($WithLoadtest) { Write-Host "  Load test   2 k6 pods hammering front-end (namespace loadtest)" }
if ($WithAIOps) {
    Write-Host  "  Triage bot  ClusterIP only (deliberate): kubectl -n monitoring port-forward svc/triage-bot 8081:8080"
    Write-Host  "              http://localhost:8081/healthz | /metrics | /incidents?limit=3"
    Write-Host  "              a synthetic alert without waiting for a rule to fire: .\aiops\triage-bot\run-local.ps1"
}
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
    $pwSource = if ($GitOps) { "from Vault, secret/sock-shop/grafana" } else { "from the existing grafana-admin Secret" }
    Write-Host ("    Grafana admin password ({0}): {1}" -f $pwSource, $grafanaPassword) -ForegroundColor Yellow
}
if ($GitOps) {
    $argoPassword = Get-ClusterSecretValue -Namespace argocd -SecretName argocd-initial-admin-secret -Key password
    Write-Host ("  Argo CD     http://argocd.localhost:{0}   (login: admin)" -f $ingressPort)
    Write-Host  "              needs '127.0.0.1 argocd.localhost' in your hosts file; without it:"
    Write-Host  "              kubectl port-forward -n argocd svc/argocd-server 8090:80  ->  http://localhost:8090"
    Write-Host ("    Argo CD admin password (argocd-initial-admin-secret): {0}" -f $argoPassword) -ForegroundColor Yellow
    Write-Host  "  Vault       kubectl port-forward -n vault svc/vault 8200:8200  ->  http://localhost:8200"
    Write-Host  "              dev mode: login with the token 'root'; the secrets are under secret/sock-shop"
    Write-Host  "              (dev mode forgets everything on a restart: run deploy\vault-seed.ps1 again)"
}