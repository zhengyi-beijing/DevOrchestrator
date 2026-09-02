[CmdletBinding()]
param(
    [string]$ListenAddress = '127.0.0.1',
    [ValidateRange(1,65535)][int]$Port = 8770
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $root 'runtime'
$pidPath = Join-Path $runtime 'web.pid'
$server = Join-Path $root 'src\web-server.ps1'
[System.IO.Directory]::CreateDirectory($runtime) | Out-Null
if (Test-Path -LiteralPath $pidPath) {
    $oldPid = 0
    $raw = (Get-Content -LiteralPath $pidPath -Raw).Trim()
    if ([int]::TryParse($raw, [ref]$oldPid) -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
        throw "Web dashboard already running with PID $oldPid."
    }
}
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = Join-Path $PSHOME 'powershell.exe'
$psi.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$server`" -ListenAddress `"$ListenAddress`" -Port $Port"
$psi.WorkingDirectory = $root
$psi.UseShellExecute = $true
$psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
$process = New-Object System.Diagnostics.Process
$process.StartInfo = $psi
if (-not $process.Start()) { throw 'Failed to start Web dashboard.' }
$startedPid = $process.Id
$process.Dispose()
$heartbeatPath = Join-Path $runtime 'web.json'
$deadline = (Get-Date).AddSeconds(5)
do {
    Start-Sleep -Milliseconds 100
    if (Test-Path -LiteralPath $heartbeatPath) {
        try {
            $heartbeat = Get-Content -LiteralPath $heartbeatPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ([int]$heartbeat.pid -eq $startedPid -and [string]$heartbeat.state -eq 'running') {
                [ordered]@{
                    state = 'running'
                    pid = $startedPid
                    listen_address = [string]$heartbeat.listen_address
                    port = [int]$heartbeat.port
                    url = if ($ListenAddress -eq '0.0.0.0') { "http://localhost:$Port/" } else { "http://$ListenAddress`:$Port/" }
                } | ConvertTo-Json
                exit 0
            }
        } catch { }
    }
} while ((Get-Date) -lt $deadline)
throw "Web dashboard process $startedPid started but heartbeat was not observed within 5 seconds."
