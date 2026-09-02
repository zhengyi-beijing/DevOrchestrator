[CmdletBinding()]
param([ValidateRange(5,3600)][int]$IntervalSeconds = 60)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $root 'runtime'
$pidPath = Join-Path $runtime 'monitor.pid'
$monitor = Join-Path $root 'src\monitor.ps1'
[System.IO.Directory]::CreateDirectory($runtime) | Out-Null
if (Test-Path -LiteralPath $pidPath) {
    $oldPid = 0
    $raw = (Get-Content -LiteralPath $pidPath -Raw).Trim()
    if ([int]::TryParse($raw, [ref]$oldPid) -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
        throw "Monitor already running with PID $oldPid."
    }
}
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = Join-Path $PSHOME 'powershell.exe'
$psi.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$monitor`" -IntervalSeconds $IntervalSeconds"
$psi.WorkingDirectory = $root
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$process = New-Object System.Diagnostics.Process
$process.StartInfo = $psi
if (-not $process.Start()) { throw 'Failed to start monitor.' }
$startedPid = $process.Id
$process.Dispose()
$deadline = (Get-Date).AddSeconds(5)
do {
    Start-Sleep -Milliseconds 100
    $heartbeatPath = Join-Path $runtime 'monitor.json'
    if (Test-Path -LiteralPath $heartbeatPath) {
        try {
            $heartbeat = Get-Content -LiteralPath $heartbeatPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ([int]$heartbeat.pid -eq $startedPid -and $heartbeat.state -in @('running','degraded')) {
                $heartbeat | ConvertTo-Json -Depth 5
                exit 0
            }
        } catch { }
    }
} while ((Get-Date) -lt $deadline)
throw "Monitor process $startedPid started but heartbeat was not observed within 5 seconds."
