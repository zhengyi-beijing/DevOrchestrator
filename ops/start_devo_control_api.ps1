[CmdletBinding()]
param(
    [ValidateRange(1,65535)][int]$Port = 8770,
    [ValidateRange(5,3600)][int]$IntervalSeconds = 60
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $root 'runtime'
$launcher = Join-Path $PSScriptRoot 'devorch.cmd'

if (-not (Test-Path -LiteralPath $launcher)) { throw "Missing launcher: $launcher" }

$statusText = & $launcher status-daemon --runtime-root $runtime
if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect DevOrchestrator daemon state.' }
$status = $statusText | ConvertFrom-Json
if ($status.process_alive) {
    if ([string]$status.listen_address -ne '127.0.0.1') {
        throw 'Existing daemon is not loopback-bound; external control fails closed.'
    }
    $status | ConvertTo-Json -Depth 8
    exit 0
}

# start-daemon uses the repository hidden/no-window subprocess helper.
$resultText = & $launcher start-daemon --listen 127.0.0.1 --port $Port --interval $IntervalSeconds --runtime-root $runtime
if ($LASTEXITCODE -ne 0) { throw 'DevOrchestrator daemon failed to start.' }
$resultText
