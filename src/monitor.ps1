[CmdletBinding()]
param(
    [ValidateRange(5, 3600)][int]$IntervalSeconds = 60,
    [switch]$Once
)

$ErrorActionPreference = 'Stop'
$script:ProjectRoot = Split-Path -Parent $PSScriptRoot
$script:RuntimeRoot = Join-Path $script:ProjectRoot 'runtime'
$script:ProjectsRuntime = Join-Path $script:RuntimeRoot 'projects'
$script:HistoryRoot = Join-Path $script:RuntimeRoot 'history'
$script:EventsPath = Join-Path $script:HistoryRoot 'events.jsonl'
$script:RunsPath = Join-Path $script:HistoryRoot 'runs.jsonl'
$script:ConfigPath = Join-Path $script:ProjectRoot 'config\projects.json'
$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
. (Join-Path $PSScriptRoot 'telemetry.ps1')
[System.IO.Directory]::CreateDirectory($script:ProjectsRuntime) | Out-Null
[System.IO.Directory]::CreateDirectory($script:HistoryRoot) | Out-Null

function Write-JsonAtomic {
    param([string]$Path, [object]$Value)
    $directory = Split-Path -Parent $Path
    [System.IO.Directory]::CreateDirectory($directory) | Out-Null
    $tmp = Join-Path $directory ('.tmp-' + [guid]::NewGuid().ToString('N'))
    [System.IO.File]::WriteAllText($tmp, ($Value | ConvertTo-Json -Depth 8), $script:Utf8NoBom)
    if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Force }
    [System.IO.File]::Move($tmp, $Path)
}

function Get-MatchingLine {
    param([string]$Path, [string]$Pattern)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    return @(Get-Content -LiteralPath $Path -Encoding UTF8 | Where-Object { $_ -match $Pattern } | Select-Object -First 1)[0]
}

function Get-GitInfo {
    param([string]$Root)
    $branch = (& git -C $Root rev-parse --abbrev-ref HEAD 2>$null | Select-Object -First 1)
    $head = (& git -C $Root rev-parse HEAD 2>$null | Select-Object -First 1)
    $changes = @(& git -C $Root status --porcelain 2>$null)
    return [ordered]@{
        branch = [string]$branch
        head = [string]$head
        dirty = ($changes.Count -gt 0)
        changed_entries = $changes.Count
    }
}

function Get-WorkerInfo {
    param([string]$Root, [string]$RelativeRuntime)
    $runRoot = Join-Path $Root $RelativeRuntime
    $statusPath = Join-Path $runRoot 'status.json'
    if (-not (Test-Path -LiteralPath $statusPath)) {
        return [ordered]@{ state = 'not_started'; kind = 'none'; process_alive = $false }
    }
    $status = Get-Content -LiteralPath $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $pidValue = 0
    $alive = [int]::TryParse([string]$status.pid, [ref]$pidValue) -and ($null -ne (Get-Process -Id $pidValue -ErrorAction SilentlyContinue))
    $kind = if ([string]$status.command -match 'headless next') { 'task' } else { 'utility' }
    $info = [ordered]@{ state = [string]$status.state; kind = $kind; process_alive = $alive }
    foreach ($name in @('pid','started_at','updated_at','exit_code','command','model')) {
        if ($status.PSObject.Properties.Name -contains $name) { $info[$name] = $status.$name }
    }
    return $info
}

function Get-GitChangedActivityUtc {
    param([string]$Root)
    $statusLines = @(& git -c core.quotepath=false -C $Root status --porcelain --untracked-files=all 2>$null)
    $times = @()
    foreach ($line in $statusLines) {
        if ([string]::IsNullOrWhiteSpace([string]$line) -or ([string]$line).Length -lt 4) { continue }
        $relative = ([string]$line).Substring(3).Trim()
        if ($relative -match ' -> ') { $relative = @($relative -split ' -> ')[-1] }
        $relative = $relative.Trim('"')
        $path = Join-Path $Root $relative
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $times += (Get-Item -LiteralPath $path).LastWriteTimeUtc
        }
    }
    if ($times.Count -eq 0) { return $null }
    return ($times | Sort-Object -Descending | Select-Object -First 1)
}

function Get-LastActivityUtc {
    param([string]$Root, [string]$RelativeRuntime)
    $runRoot = Join-Path $Root $RelativeRuntime
    $paths = @(
        (Join-Path $runRoot 'status.json'),
        (Join-Path $runRoot 'stdout.log'),
        (Join-Path $runRoot 'stderr.log'),
        (Join-Path $Root 'agent\CURRENT.md'),
        (Join-Path $Root 'agent\next.md'),
        (Join-Path $Root 'agent\result.md')
    )
    $times = @()
    foreach ($path in $paths) {
        if (Test-Path -LiteralPath $path) { $times += (Get-Item -LiteralPath $path).LastWriteTimeUtc }
    }
    $gitActivity = Get-GitChangedActivityUtc -Root $Root
    if ($null -ne $gitActivity) { $times += $gitActivity }
    if ($times.Count -eq 0) { return $null }
    return (($times | Sort-Object -Descending | Select-Object -First 1).ToString('o'))
}

function Resolve-MonitorState {
    param([object]$Worker, [string]$NextStatus, [AllowNull()][string]$NextUpdatedAt)
    if ($Worker.kind -eq 'task' -and $Worker.state -in @('starting','running') -and -not $Worker.process_alive) { return 'WORKER_LOST' }
    if ($Worker.kind -eq 'task' -and $Worker.state -in @('starting','running')) { return 'WORKER_RUNNING' }
    if ($Worker.kind -eq 'task' -and $Worker.state -eq 'failed') { return 'WORKER_FAILED' }
    if ($NextStatus -match 'BLOCKED') { return 'BLOCKED' }
    if ($NextStatus -match 'ACCEPTED|awaiting') { return 'WAITING_PHASE_GATE' }
    if ($Worker.kind -eq 'task' -and $Worker.state -eq 'completed') {
        if ($NextStatus -match 'DESIGN READY|EXECUTABLE') {
            $workerUpdated = Convert-ToUtcDate $(if ($Worker.PSObject.Properties.Name -contains 'updated_at') { $Worker.updated_at } else { $null })
            $nextUpdated = Convert-ToUtcDate $NextUpdatedAt
            if ($null -ne $workerUpdated -and $null -ne $nextUpdated -and $nextUpdated -gt $workerUpdated) {
                return 'READY_TO_RUN'
            }
        }
        return 'WAITING_REVIEW'
    }
    if ($NextStatus -match 'DESIGN READY|EXECUTABLE') { return 'READY_TO_RUN' }
    return 'IDLE'
}

function Get-ProjectSnapshot {
    param([object]$Project)
    $root = [System.IO.Path]::GetFullPath([string]$Project.root)
    $observed = (Get-Date).ToUniversalTime().ToString('o')
    if (-not (Test-Path -LiteralPath $root -PathType Container)) {
        return [ordered]@{ id=$Project.id; name=$Project.name; root=$root; observed_at=$observed; state='UNAVAILABLE' }
    }
    $nextPath = Join-Path $root 'agent\next.md'
    $currentPath = Join-Path $root 'agent\CURRENT.md'
    $nextTitle = Get-MatchingLine -Path $nextPath -Pattern '^# '
    $nextStatus = Get-MatchingLine -Path $nextPath -Pattern '^Status:'
    $nextUpdatedAt = if (Test-Path -LiteralPath $nextPath) { (Get-Item -LiteralPath $nextPath).LastWriteTimeUtc.ToString('o') } else { $null }
    $phaseHint = Get-MatchingLine -Path $currentPath -Pattern '^- P[0-9].*(DESIGN READY|NOT STARTED|BLOCKED|RUNNING)'
    $worker = [pscustomobject](Get-WorkerInfo -Root $root -RelativeRuntime ([string]$Project.worker_runtime))
    $state = Resolve-MonitorState -Worker $worker -NextStatus ([string]$nextStatus) -NextUpdatedAt $nextUpdatedAt
    $titleText = if ($nextTitle) { $nextTitle.Substring(2).Trim() } else { $null }
    $statusText = if ($nextStatus) { $nextStatus.Substring(7).Trim() } else { $null }
    $lastActivity = Get-LastActivityUtc -Root $root -RelativeRuntime ([string]$Project.worker_runtime)
    $taskId = Get-TaskId -Title $titleText
    $telemetry = Get-WorkerTelemetry -Project $Project -Worker $worker -TaskId $taskId -LastActivityAt $lastActivity -RunsPath $script:RunsPath
    return [ordered]@{
        id = [string]$Project.id
        name = [string]$Project.name
        root = $root
        observed_at = $observed
        state = $state
        git = Get-GitInfo -Root $root
        worker = $worker
        telemetry = $telemetry
        last_activity_at = $lastActivity
        next_title = $titleText
        next_status = $statusText
        next_updated_at = $nextUpdatedAt
        phase_hint = if ($phaseHint) { $phaseHint.Trim() } else { $null }
    }
}

function Invoke-Tick {
    $config = Get-Content -LiteralPath $script:ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $snapshots = @()
    foreach ($project in @($config.projects)) {
        $snapshotPath = Join-Path $script:ProjectsRuntime ($project.id + '.json')
        $previous = $null
        if (Test-Path -LiteralPath $snapshotPath) {
            try { $previous = Get-Content -LiteralPath $snapshotPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { }
        }
        try {
            $snapshot = [pscustomobject](Get-ProjectSnapshot -Project $project)
        }
        catch {
            $snapshot = [pscustomobject][ordered]@{
                id = [string]$project.id; name = [string]$project.name; root = [string]$project.root
                observed_at = (Get-Date).ToUniversalTime().ToString('o'); state = 'MONITOR_ERROR'
                error = $_.Exception.Message
            }
        }
        $event = New-StateEvent -Previous $previous -Current $snapshot
        if ($null -ne $event) { Append-JsonLine -Path $script:EventsPath -Value $event -Encoding $script:Utf8NoBom }
        if ($snapshot.PSObject.Properties.Name -contains 'telemetry') {
            $run = New-RunRecord -Project $project -Snapshot $snapshot
            if ($null -ne $run -and -not (Test-RunRecorded -Path $script:RunsPath -RunId ([string]$run.run_id))) {
                Append-JsonLine -Path $script:RunsPath -Value $run -Encoding $script:Utf8NoBom
            }
        }
        $snapshots += $snapshot
        Write-JsonAtomic -Path $snapshotPath -Value $snapshot
    }
    $summary = [ordered]@{
        observed_at = (Get-Date).ToUniversalTime().ToString('o')
        project_count = $snapshots.Count
        projects = $snapshots
    }
    Write-JsonAtomic -Path (Join-Path $script:RuntimeRoot 'summary.json') -Value $summary
    return $summary
}

if (-not (Test-Path -LiteralPath $script:ConfigPath)) { throw "Missing config: $script:ConfigPath" }
if ($Once) {
    Invoke-Tick | ConvertTo-Json -Depth 8
    exit 0
}

$startedAt = (Get-Date).ToUniversalTime().ToString('o')
$pidPath = Join-Path $script:RuntimeRoot 'monitor.pid'
[System.IO.File]::WriteAllText($pidPath, [string]$PID, $script:Utf8NoBom)
while ($true) {
    $lastError = $null
    try { $summary = Invoke-Tick }
    catch { $lastError = $_.Exception.Message }
    $heartbeat = [ordered]@{
        state = if ($lastError) { 'degraded' } else { 'running' }
        pid = $PID
        interval_seconds = $IntervalSeconds
        started_at = $startedAt
        last_tick_at = (Get-Date).ToUniversalTime().ToString('o')
        last_error = $lastError
    }
    Write-JsonAtomic -Path (Join-Path $script:RuntimeRoot 'monitor.json') -Value $heartbeat
    Start-Sleep -Seconds $IntervalSeconds
}
