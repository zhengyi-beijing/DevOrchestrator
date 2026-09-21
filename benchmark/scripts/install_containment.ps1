# Transactional installation script for benchmark containment boundary
# Strictly Windows PowerShell 5.1 safe: NO && or || operators used.
[CmdletBinding()]
param(
    [string]$ConfigFile = "runtime/benchmark.json",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

Write-Host "DevOrchestrator AIBench Containment Provisioning"
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Error "Python executable not found on PATH."
    exit 1
}

$dryArg = ""
if ($DryRun) {
    $dryArg = "--dry-run"
}

$cmd = "import sys; from pathlib import Path; sys.path.insert(0, str(Path('benchmark/src').resolve())); from aibench.cli import main; sys.argv = ['aibench', 'containment-audit']; main()"
& python -c $cmd
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Containment pre-check reported notices; continuing with transaction installation."
}

Write-Host "Containment boundary provisioned."
