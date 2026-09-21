# Transactional uninstall script for benchmark containment boundary
# Strictly Windows PowerShell 5.1 safe: NO && or || operators used.
[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

Write-Host "DevOrchestrator AIBench Containment Teardown"
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Error "Python executable not found on PATH."
    exit 1
}

$cmd = "import sys; from pathlib import Path; sys.path.insert(0, str(Path('benchmark/src').resolve())); from aibench.provisioning import ContainmentProvisioner; p = ContainmentProvisioner(); print(p.uninstall())"
& python -c $cmd
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to uninstall containment boundary."
    exit $LASTEXITCODE
}

Write-Host "Containment boundary uninstalled."
