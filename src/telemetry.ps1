Set-StrictMode -Version 2.0

function Convert-ToUtcDate {
    param([AllowNull()][object]$Value)
    if ($null -eq $Value -or [string]::IsNullOrWhiteSpace([string]$Value)) { return $null }
    $parsed = [datetimeoffset]::MinValue
    if ([datetimeoffset]::TryParse([string]$Value, [ref]$parsed)) { return $parsed.ToUniversalTime() }
    return $null
}

function Get-TaskId {
    param([AllowNull()][string]$Title)
    if ([string]::IsNullOrWhiteSpace($Title)) { return $null }
    $match = [regex]::Match($Title, '\bP\d+(?:\.\d+)*(?:[a-z])?\b', 'IgnoreCase')
    if ($match.Success) { return $match.Value }
    return $null
}

function Get-RunId {
    param([string]$ProjectId, [object]$Worker)
    $startedValue = if ($Worker.PSObject.Properties.Name -contains 'started_at') { $Worker.started_at } else { $null }
    $started = Convert-ToUtcDate $startedValue
    if ($null -eq $started) { return $null }
    $pidPart = if ($Worker.PSObject.Properties.Name -contains 'pid') { [string]$Worker.pid } else { 'na' }
    return ('{0}-{1}-{2}' -f $ProjectId, $started.ToString('yyyyMMddTHHmmssfffZ'), $pidPart)
}
function Read-JsonLines {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return @() }
    $items = @()
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        try { $items += ($line | ConvertFrom-Json) } catch { }
    }
    return @($items)
}

function Append-JsonLine {
    param([string]$Path, [object]$Value, [System.Text.Encoding]$Encoding)
    $directory = Split-Path -Parent $Path
    [System.IO.Directory]::CreateDirectory($directory) | Out-Null
    $json = $Value | ConvertTo-Json -Depth 8 -Compress
    [System.IO.File]::AppendAllText($Path, $json + [Environment]::NewLine, $Encoding)
}

function Test-RunRecorded {
    param([string]$Path, [string]$RunId)
    if ([string]::IsNullOrWhiteSpace($RunId)) { return $false }
    foreach ($item in @(Read-JsonLines -Path $Path)) {
        if ([string]$item.run_id -eq $RunId) { return $true }
    }
    return $false
}
function Get-Median {
    param([double[]]$Values)
    $sorted = @($Values | Sort-Object)
    if ($sorted.Count -eq 0) { return $null }
    $middle = [int][math]::Floor($sorted.Count / 2)
    if (($sorted.Count % 2) -eq 1) { return [double]$sorted[$middle] }
    return ([double]$sorted[$middle - 1] + [double]$sorted[$middle]) / 2.0
}

function Get-EtaPolicy {
    param([object]$Project, [string]$TaskId, [string]$RunsPath)
    $eta = $Project.eta
    $minimum = [double]$eta.default_worker_minutes.min
    $maximum = [double]$eta.default_worker_minutes.max
    $source = 'project_default'
    $confidence = 'low'
    foreach ($override in @($eta.task_overrides)) {
        if ([string]$override.task_id -eq $TaskId) {
            $minimum = [double]$override.min
            $maximum = [double]$override.max
            $source = 'task_override'
            $confidence = 'medium'
            break
        }
    }
    if ($source -eq 'project_default') {
        $durations = @()
        foreach ($run in @(Read-JsonLines -Path $RunsPath)) {
            if ([string]$run.project_id -eq [string]$Project.id -and [string]$run.result -eq 'completed') {
                $durations += ([double]$run.duration_seconds / 60.0)
            }
        }
        $needed = [int]$eta.historical_min_samples
        if ($durations.Count -ge $needed) {
            $median = Get-Median -Values $durations
            $minimum = [math]::Max(5, [math]::Round($median * 0.70, 1))
            $maximum = [math]::Max($minimum, [math]::Round($median * 1.35, 1))
            $source = 'project_history_median'
            $confidence = if ($durations.Count -ge 8) { 'high' } else { 'medium' }
        }
    }
    return [ordered]@{
        min_minutes = $minimum
        max_minutes = $maximum
        source = $source
        confidence = $confidence
    }
}

function Get-WorkerTelemetry {
    param([object]$Project, [object]$Worker, [string]$TaskId, [string]$LastActivityAt, [string]$RunsPath)
    $now = [datetimeoffset]::UtcNow
    $policy = Get-EtaPolicy -Project $Project -TaskId $TaskId -RunsPath $RunsPath
    $isTask = ([string]$Worker.kind -eq 'task')
    $startedValue = if ($isTask -and $Worker.PSObject.Properties.Name -contains 'started_at') { $Worker.started_at } else { $null }
    $updatedValue = if ($isTask -and $Worker.PSObject.Properties.Name -contains 'updated_at') { $Worker.updated_at } else { $null }
    $started = Convert-ToUtcDate $startedValue
    $updated = Convert-ToUtcDate $updatedValue
    $activity = Convert-ToUtcDate $LastActivityAt
    $elapsed = $null
    if ($null -ne $started) {
        $endPoint = if ([string]$Worker.state -in @('completed','failed') -and $null -ne $updated) { $updated } else { $now }
        $elapsed = [math]::Max(0, [math]::Round(($endPoint - $started).TotalSeconds, 1))
    }
    $activityAge = if ($null -ne $activity) { [math]::Max(0, [math]::Round(($now - $activity).TotalSeconds, 1)) } else { $null }
    $minTotal = [double]$policy.min_minutes * 60.0
    $maxTotal = [double]$policy.max_minutes * 60.0
    $remainingMin = if ($null -eq $elapsed) { $minTotal } else { [math]::Max(0, $minTotal - $elapsed) }
    $remainingMax = if ($null -eq $elapsed) { $maxTotal } else { [math]::Max(0, $maxTotal - $elapsed) }
    $health = 'OK'
    if ([string]$Worker.kind -eq 'task' -and [string]$Worker.state -in @('starting','running')) {
        if ($null -ne $elapsed -and $elapsed -ge ([double]$Project.eta.hard_timeout_minutes * 60.0)) { $health = 'TIMEOUT' }
        elseif ($null -ne $activityAge -and $activityAge -ge ([double]$Project.eta.stall_warning_minutes * 60.0)) { $health = 'STALLED_WARNING' }
    }
    return [ordered]@{
        run_id = if ($isTask) { Get-RunId -ProjectId ([string]$Project.id) -Worker $Worker } else { $null }
        task_id = $TaskId
        elapsed_seconds = $elapsed
        last_activity_age_seconds = $activityAge
        health = $health
        eta = [ordered]@{
            total_min_seconds = [math]::Round($minTotal, 1)
            total_max_seconds = [math]::Round($maxTotal, 1)
            remaining_min_seconds = [math]::Round($remainingMin, 1)
            remaining_max_seconds = [math]::Round($remainingMax, 1)
            source = [string]$policy.source
            confidence = [string]$policy.confidence
            overrun = ($null -ne $elapsed -and $elapsed -gt $maxTotal)
        }
    }
}

function New-RunRecord {
    param([object]$Project, [object]$Snapshot)
    $worker = $Snapshot.worker
    $telemetry = $Snapshot.telemetry
    if ([string]$worker.kind -ne 'task' -or [string]$worker.state -notin @('completed','failed')) { return $null }
    if ([string]::IsNullOrWhiteSpace([string]$telemetry.run_id)) { return $null }
    return [ordered]@{
        run_id = [string]$telemetry.run_id
        project_id = [string]$Project.id
        task_id = [string]$telemetry.task_id
        task_title = [string]$Snapshot.next_title
        engine = 'dsh'
        command = [string]$worker.command
        started_at = [string]$worker.started_at
        completed_at = [string]$worker.updated_at
        duration_seconds = $telemetry.elapsed_seconds
        result = if ([string]$worker.state -eq 'completed') { 'completed' } else { 'failed' }
        exit_code = if ($worker.PSObject.Properties.Name -contains 'exit_code') { $worker.exit_code } else { $null }
        git_branch_observed = [string]$Snapshot.git.branch
        git_head_observed = [string]$Snapshot.git.head
        git_dirty_observed = [bool]$Snapshot.git.dirty
        recorded_at = [datetimeoffset]::UtcNow.ToString('o')
        estimate = $telemetry.eta
    }
}

function New-StateEvent {
    param([object]$Previous, [object]$Current)
    $oldState = if ($null -eq $Previous) { $null } else { [string]$Previous.state }
    $newState = [string]$Current.state
    $oldRun = if ($null -eq $Previous -or -not ($Previous.PSObject.Properties.Name -contains 'telemetry')) { $null } else { [string]$Previous.telemetry.run_id }
    $newRun = if (-not ($Current.PSObject.Properties.Name -contains 'telemetry')) { $null } else { [string]$Current.telemetry.run_id }
    if ($oldState -eq $newState -and ([string]::IsNullOrWhiteSpace($newRun) -or $oldRun -eq $newRun)) { return $null }
    $taskId = $null
    if ($Current.PSObject.Properties.Name -contains 'telemetry' -and $null -ne $Current.telemetry) {
        $taskId = [string]$Current.telemetry.task_id
    }
    return [ordered]@{
        observed_at = [datetimeoffset]::UtcNow.ToString('o')
        project_id = [string]$Current.id
        task_id = $taskId
        run_id = $newRun
        from_state = $oldState
        to_state = $newState
    }
}
