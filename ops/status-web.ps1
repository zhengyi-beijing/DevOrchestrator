[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $root 'runtime'
$heartbeatPath = Join-Path $runtime 'web.json'
if (-not (Test-Path -LiteralPath $heartbeatPath)) {
    [ordered]@{ state='not_started'; process_alive=$false } | ConvertTo-Json
    exit 0
}
$heartbeat = Get-Content -LiteralPath $heartbeatPath -Raw -Encoding UTF8 | ConvertFrom-Json
$pidValue = 0
$alive = [int]::TryParse([string]$heartbeat.pid, [ref]$pidValue) -and
         ($null -ne (Get-Process -Id $pidValue -ErrorAction SilentlyContinue))
$address = if ($heartbeat.listen_address) { [string]$heartbeat.listen_address } else { '127.0.0.1' }
$port = if ($heartbeat.port) { [int]$heartbeat.port } else { 8770 }
[ordered]@{
    state = if ($alive) { [string]$heartbeat.state } else { 'stopped' }
    pid = $pidValue
    process_alive = $alive
    listen_address = $address
    port = $port
    started_at = $heartbeat.started_at
    last_request_at = $heartbeat.last_request_at
    url = if ($address -eq '0.0.0.0') { "http://localhost:$port/" } else { "http://$address`:$port/" }
} | ConvertTo-Json -Depth 5
