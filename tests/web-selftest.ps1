[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$server = Join-Path $root 'src\web-server.ps1'
$webRoot = Join-Path $root 'web'
$fixture = Join-Path $root 'runtime\selftest-web'
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "ASSERT: $Message" }
}
function Get-FreePort {
    $probe = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
    $probe.Start(); $port = ([System.Net.IPEndPoint]$probe.LocalEndpoint).Port; $probe.Stop(); return $port
}
function Write-Json([string]$Path, [object]$Value) {
    [System.IO.Directory]::CreateDirectory((Split-Path -Parent $Path)) | Out-Null
    [System.IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 10), $utf8)
}
function Raw-Status([int]$Port, [string]$RequestLine) {
    $client = New-Object System.Net.Sockets.TcpClient
    $client.Connect('127.0.0.1', $Port)
    $stream = $client.GetStream()
    $bytes = [System.Text.Encoding]::ASCII.GetBytes("$RequestLine`r`nHost: localhost`r`nConnection: close`r`n`r`n")
    $stream.Write($bytes,0,$bytes.Length); $stream.Flush()
    $reader = New-Object System.IO.StreamReader($stream)
    $line = $reader.ReadLine(); $client.Close()
    return [int]([regex]::Match($line, '^HTTP/1\.1 (\d{3})').Groups[1].Value)
}
if (Test-Path -LiteralPath $fixture) { Remove-Item -LiteralPath $fixture -Recurse -Force }
[System.IO.Directory]::CreateDirectory((Join-Path $fixture 'projects')) | Out-Null
[System.IO.Directory]::CreateDirectory((Join-Path $fixture 'history')) | Out-Null
$oldTick = [datetimeoffset]::UtcNow.AddMinutes(-10).ToString('o')
Write-Json (Join-Path $fixture 'monitor.json') ([ordered]@{
    state='running'; pid=999999; interval_seconds=60; started_at=$oldTick; last_tick_at=$oldTick; last_error=$null
})
$project = [ordered]@{
    id='labdemo'; name='LabDemo'; state='WAITING_PHASE_GATE'; observed_at=[datetimeoffset]::UtcNow.ToString('o')
    git=[ordered]@{branch='service-control';head='abcdef123456';dirty=$false;changed_entries=0}
    worker=[ordered]@{state='completed';kind='task';pid=1234;process_alive=$false}
    telemetry=[ordered]@{task_id='P4.3.2';elapsed_seconds=1412;health='OK';eta=[ordered]@{remaining_min_seconds=0;remaining_max_seconds=148;source='history';confidence='medium'}}
    last_activity_at=[datetimeoffset]::UtcNow.AddMinutes(-1).ToString('o'); next_title='P4.3.2 ACCEPTED'
}
Write-Json (Join-Path $fixture 'summary.json') ([ordered]@{observed_at=[datetimeoffset]::UtcNow.ToString('o');project_count=1;projects=@($project)})
Write-Json (Join-Path $fixture 'projects\labdemo.json') $project
[System.IO.File]::WriteAllText((Join-Path $fixture 'history\events.jsonl'), '{"project_id":"labdemo","from_state":"WORKER_RUNNING","to_state":"WAITING_PHASE_GATE"}' + [Environment]::NewLine, $utf8)
[System.IO.File]::WriteAllText((Join-Path $fixture 'history\runs.jsonl'), '{"project_id":"labdemo","task_id":"P4.3.2","result":"completed","duration_seconds":1412}' + [Environment]::NewLine, $utf8)

$parseErrors = @()
foreach ($file in @($server, (Join-Path $root 'ops\start-web.ps1'), (Join-Path $root 'ops\status-web.ps1'), (Join-Path $root 'ops\stop-web.ps1'))) {
    $tokens=$null; $errors=$null
    [System.Management.Automation.Language.Parser]::ParseFile($file,[ref]$tokens,[ref]$errors) | Out-Null
    $parseErrors += @($errors)
}
Assert-True ($parseErrors.Count -eq 0) 'PowerShell parser errors exist'
$port = Get-FreePort
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = Join-Path $PSHOME 'powershell.exe'
$psi.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$server`" -ListenAddress 127.0.0.1 -Port $port -RuntimeRoot `"$fixture`" -WebRoot `"$webRoot`""
$psi.WorkingDirectory = $root; $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
$process = New-Object System.Diagnostics.Process; $process.StartInfo = $psi
Assert-True $process.Start() 'fixture Web server failed to start'
try {
    $deadline = (Get-Date).AddSeconds(5)
    do {
        Start-Sleep -Milliseconds 50
        $webHeartbeat = Join-Path $fixture 'web.json'
    } while (-not (Test-Path -LiteralPath $webHeartbeat) -and (Get-Date) -lt $deadline)
    Assert-True (Test-Path -LiteralPath $webHeartbeat) 'fixture Web heartbeat missing'

    $base = "http://127.0.0.1:$port"
    $homeResponse = Invoke-WebRequest -UseBasicParsing "$base/"
    Assert-True ($homeResponse.StatusCode -eq 200 -and $homeResponse.Content.Contains('Dev Orchestrator')) 'GET / failed'
    $summary = Invoke-RestMethod "$base/api/summary"
    Assert-True ($summary.project_count -eq 1 -and $summary.projects[0].state -eq 'WAITING_PHASE_GATE') 'summary API shape failed'
    $monitor = Invoke-RestMethod "$base/api/monitor"
    Assert-True ($monitor.stale -eq $true -and $monitor.process_alive -eq $false) 'stale monitor derivation failed'
    $one = Invoke-RestMethod "$base/api/projects/labdemo"
    Assert-True ($one.id -eq 'labdemo') 'project endpoint failed'
    $events = Invoke-RestMethod "$base/api/events?limit=1"
    Assert-True ($events.items.Count -eq 1) 'bounded events endpoint failed'
    Assert-True ((Raw-Status $port 'HEAD /app.js HTTP/1.1') -eq 200) 'HEAD static failed'
    Assert-True ((Raw-Status $port 'GET /missing HTTP/1.1') -eq 404) '404 route failed'
    Assert-True ((Raw-Status $port 'POST /api/summary HTTP/1.1') -eq 405) 'method rejection failed'
    Assert-True ((Raw-Status $port 'GET /%2e%2e/README.md HTTP/1.1') -eq 400) 'encoded traversal rejection failed'
} finally {
    if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force }
    $process.Dispose()
}

$lifePort = Get-FreePort
$startJson = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'ops\start-web.ps1') -ListenAddress 127.0.0.1 -Port $lifePort | ConvertFrom-Json
Assert-True ($startJson.state -eq 'running') 'start-web lifecycle failed'
$status = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'ops\status-web.ps1') | ConvertFrom-Json
Assert-True ($status.process_alive -eq $true -and $status.port -eq $lifePort) 'status-web lifecycle failed'
Assert-True ((Raw-Status $lifePort 'GET /api/summary HTTP/1.1') -eq 200) 'lifecycle server request failed'
$stop = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'ops\stop-web.ps1') | ConvertFrom-Json
Assert-True ($stop.state -eq 'stopped') 'stop-web lifecycle failed'
$statusAfter = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'ops\status-web.ps1') | ConvertFrom-Json
Assert-True ($statusAfter.process_alive -eq $false) 'Web process still alive after stop'

Write-Host 'web-selftest: PASS'
