param(
    [string]$Config = "config/pa3_local.yaml"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot ".venv310\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "Python executable not found at $python"
}

$portJson = @'
import json
import sys

from src.pa3.config import load_pa3_config

cfg = load_pa3_config(sys.argv[1])

tcp_ports = [cfg.soap.port]
udp_ports = []

for replica in cfg.customer_replicas:
    tcp_ports.extend([replica.buyer_grpc_port, replica.seller_grpc_port])
    udp_ports.append(replica.udp_port)

for replica in cfg.product_replicas:
    tcp_ports.extend([replica.grpc_port, replica.raft_port])

for frontend in cfg.seller_frontend_replicas:
    tcp_ports.append(frontend.port)

for frontend in cfg.buyer_frontend_replicas:
    tcp_ports.append(frontend.port)

print(json.dumps({"tcp": sorted(set(tcp_ports)), "udp": sorted(set(udp_ports))}))
'@

$ports = $portJson | & $python - $Config | ConvertFrom-Json

$pids = New-Object System.Collections.Generic.HashSet[int]
$tcpTargetPorts = New-Object System.Collections.Generic.HashSet[int]
$udpTargetPorts = New-Object System.Collections.Generic.HashSet[int]

foreach ($port in $ports.tcp) {
    [void]$tcpTargetPorts.Add([int]$port)
}

foreach ($port in $ports.udp) {
    [void]$udpTargetPorts.Add([int]$port)
}

foreach ($line in (netstat -ano -p tcp)) {
    $parts = ($line -split '\s+') | Where-Object { $_ }
    if ($parts.Count -lt 5) {
        continue
    }
    if ($parts[0] -ne "TCP" -or $parts[3] -ne "LISTENING") {
        continue
    }
    if ($parts[1] -notmatch ':(\d+)$') {
        continue
    }
    $localPort = [int]$matches[1]
    if (-not $tcpTargetPorts.Contains($localPort)) {
        continue
    }
    $owningPid = [int]$parts[4]
    if ($owningPid -gt 0) {
        [void]$pids.Add($owningPid)
    }
}

foreach ($line in (netstat -ano -p udp)) {
    $parts = ($line -split '\s+') | Where-Object { $_ }
    if ($parts.Count -lt 4) {
        continue
    }
    if ($parts[0] -ne "UDP") {
        continue
    }
    if ($parts[1] -notmatch ':(\d+)$') {
        continue
    }
    $localPort = [int]$matches[1]
    if (-not $udpTargetPorts.Contains($localPort)) {
        continue
    }
    $owningPid = [int]$parts[-1]
    if ($owningPid -gt 0) {
        [void]$pids.Add($owningPid)
    }
}

if ($pids.Count -eq 0) {
    $processes = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and (
                $_.CommandLine -match 'src\.pa3' -or
                $_.CommandLine -match 'src\.financial\.soap_server'
            )
        }

    foreach ($process in $processes) {
        if ($process.ProcessId -gt 0) {
            [void]$pids.Add([int]$process.ProcessId)
        }
    }
}

if ($pids.Count -eq 0) {
    Write-Host "No PA3 listeners found for config $Config"
    return
}

foreach ($targetPid in ($pids | Sort-Object)) {
    try {
        Stop-Process -Id $targetPid -Force -ErrorAction Stop
        Write-Host ("Stopped PID {0}" -f $targetPid)
    }
    catch {
        Write-Warning ("Failed to stop PID {0}: {1}" -f $targetPid, $_.Exception.Message)
    }
}

Start-Sleep -Seconds 1
Write-Host "PA3 listeners stopped."
