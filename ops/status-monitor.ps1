[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $root 'runtime'
$heartbeatPath = Join-Path $runtime 'monitor.json'
if (-not (Test-Path -LiteralPath $heartbeatPath)) {
    [ordered]@{ state='not_started'; process_alive=$false } | ConvertTo-Json
    exit 0
}
$heartbeat = Get-Content -LiteralPath $heartbeatPath -Raw -Encoding UTF8 | ConvertFrom-Json
$pidValue = 0
$alive = [int]::TryParse([string]$heartbeat.pid, [ref]$pidValue) -and ($null -ne (Get-Process -Id $pidValue -ErrorAction SilentlyContinue))
$lastTick = [DateTimeOffset]::Parse([string]$heartbeat.last_tick_at)
$ageSeconds = [math]::Round(((Get-Date).ToUniversalTime() - $lastTick.UtcDateTime).TotalSeconds, 1)
[ordered]@{
    state = [string]$heartbeat.state
    pid = $pidValue
    process_alive = $alive
    interval_seconds = [int]$heartbeat.interval_seconds
    started_at = [string]$heartbeat.started_at
    last_tick_at = [string]$heartbeat.last_tick_at
    heartbeat_age_seconds = $ageSeconds
    stale = (-not $alive) -or ($ageSeconds -gt ([int]$heartbeat.interval_seconds * 2.5))
    last_error = $heartbeat.last_error
} | ConvertTo-Json -Depth 5
