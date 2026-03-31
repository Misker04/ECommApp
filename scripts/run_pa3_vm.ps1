param(
    [string]$Config = "config/pa3_4vm_example.yaml",
    [Parameter(Mandatory = $true)]
    [int]$VmId,
    [ValidateSet("all", "backends", "soap", "customers", "products", "frontends")]
    [string]$Phase = "all"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonCandidates = @(
    (Join-Path $repoRoot ".venv310\Scripts\python.exe"),
    (Join-Path $repoRoot ".venv\Scripts\python.exe")
)

$python = $null
foreach ($candidate in $pythonCandidates) {
    if (Test-Path $candidate) {
        $python = $candidate
        break
    }
}

if (-not $python) {
    $python = "python"
}

& $python (Join-Path $repoRoot "scripts\run_pa3_vm.py") --config $Config --vm-id $VmId --phase $Phase
