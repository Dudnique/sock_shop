# ---------------------------------------------------------------------------
# deploy.ps1 - one-command deploy of Sock Shop into a local k3d cluster.
#
# Requirements: docker, k3d, kubectl on PATH.
# Usage:        .\deploy.ps1            (or with params, see below)
#
# Example:      .\deploy.ps1 -ClusterName sock-shop -HttpPort 8079
# ---------------------------------------------------------------------------
[CmdletBinding()]
param(
    [string]$ClusterName = "sock-shop",
    [string]$Namespace   = "sock-shop",
    [int]   $HttpPort    = 8079
)

$ErrorActionPreference = "Stop"

foreach ($tool in @("docker", "k3d", "kubectl")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "Не найден '$tool' в PATH. Установи его и повтори."
    }
}

$manifestDir = (Join-Path $PSScriptRoot "deploy\kubernetes\manifests") -replace '\\', '/'

Write-Host "==> Кластер k3d '$ClusterName'" -ForegroundColor Cyan
if (k3d cluster list | Select-String -SimpleMatch $ClusterName) {
    Write-Host "    Кластер уже существует, использую его."
} else {
    k3d cluster create $ClusterName -p "${HttpPort}:80@loadbalancer"
}

Write-Host "==> Ждём готовности ноды..." -ForegroundColor Cyan
kubectl wait --for=condition=Ready node --all --timeout=180s | Out-Null

Write-Host "==> Применяю манифесты из $manifestDir" -ForegroundColor Cyan
kubectl apply -f $manifestDir

Write-Host "==> Ждём раскатки подов (до 5 минут)..." -ForegroundColor Cyan
kubectl wait --for=condition=available --timeout=300s deployment --all -n $Namespace

Write-Host ""
Write-Host "Готово. Открывай в браузере:  http://localhost:$HttpPort" -ForegroundColor Green
