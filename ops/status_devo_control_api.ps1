[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $root 'runtime'
$launcher = Join-Path $PSScriptRoot 'devorch.cmd'

$statusText = & $launcher status-daemon --runtime-root $runtime
if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect DevOrchestrator daemon state.' }
$status = $statusText | ConvertFrom-Json
$tokenPath = Join-Path $runtime 'control\api-token'
[ordered]@{
    state = [string]$status.state
    pid = $status.pid
    process_alive = [bool]$status.process_alive
    listen_address = [string]$status.listen_address
    port = $status.port
    url = [string]$status.url
    last_tick_at = $status.last_tick_at
    last_error = $status.last_error
    control_enabled = ([bool]$status.process_alive -and [string]$status.listen_address -eq '127.0.0.1')
    token_present = (Test-Path -LiteralPath $tokenPath)
    token_path = $tokenPath
} | ConvertTo-Json -Depth 8
