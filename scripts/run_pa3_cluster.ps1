param(
    [string]$Config = "config/pa3_local.yaml"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot ".venv310\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "Python executable not found at $python"
}

Write-Host "Starting PA3 cluster with config $Config"
Write-Host "Logs: $(Join-Path $repoRoot 'logs\pa3')"

& (Join-Path $PSScriptRoot "stop_pa3_cluster.ps1") -Config $Config

& $python -u (Join-Path $PSScriptRoot "run_pa3_cluster.py") --config $Config

if ($LASTEXITCODE -ne 0) {
    throw "PA3 cluster launcher failed with exit code $LASTEXITCODE"
}
