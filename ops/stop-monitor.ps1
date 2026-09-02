[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $root 'runtime'
$pidPath = Join-Path $runtime 'monitor.pid'
$heartbeatPath = Join-Path $runtime 'monitor.json'
$pidValue = 0
if (Test-Path -LiteralPath $pidPath) {
    $raw = (Get-Content -LiteralPath $pidPath -Raw).Trim()
    [void][int]::TryParse($raw, [ref]$pidValue)
}
if ($pidValue -gt 0) {
    $process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
    if ($null -ne $process) { Stop-Process -Id $pidValue -Force }
}
$stopped = [ordered]@{
    state = 'stopped'
    pid = $pidValue
    stopped_at = (Get-Date).ToUniversalTime().ToString('o')
}
$utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($heartbeatPath, ($stopped | ConvertTo-Json), $utf8)
if (Test-Path -LiteralPath $pidPath) { Remove-Item -LiteralPath $pidPath -Force }
$stopped | ConvertTo-Json
