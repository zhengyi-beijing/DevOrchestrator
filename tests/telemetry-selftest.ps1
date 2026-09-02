$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path -Parent $PSScriptRoot) 'src\telemetry.ps1')
$root = Join-Path $env:TEMP ('devorchestrator-telemetry-' + [guid]::NewGuid().ToString('N'))
[System.IO.Directory]::CreateDirectory($root) | Out-Null
$runsPath = Join-Path $root 'runs.jsonl'
$encoding = New-Object System.Text.UTF8Encoding($false)
$script:passed = 0
$script:failed = 0
function Assert-Case([bool]$Condition, [string]$Name) {
    if ($Condition) { $script:passed++; Write-Output "[PASS] $Name" }
    else { $script:failed++; Write-Output "[FAIL] $Name" }
}
$project = [pscustomobject]@{
    id = 'labdemo'
    eta = [pscustomobject]@{
        default_worker_minutes = [pscustomobject]@{ min = 30; max = 90 }
        historical_min_samples = 3
        stall_warning_minutes = 15
        hard_timeout_minutes = 180
        task_overrides = @([pscustomobject]@{ task_id = 'P4.2.3b'; min = 45; max = 90 })
    }
}
Assert-Case ((Get-TaskId 'P4.2.3b DESIGN READY') -eq 'P4.2.3b') 'task id parsing'
$utility = [pscustomobject]@{ kind='utility'; state='completed'; pid=1; started_at='2026-09-02T00:00:00Z'; updated_at='2026-09-02T00:00:01Z' }
$ready = Get-WorkerTelemetry -Project $project -Worker $utility -TaskId 'P4.2.3b' -LastActivityAt ([datetimeoffset]::UtcNow.ToString('o')) -RunsPath $runsPath
Assert-Case ($null -eq $ready.run_id) 'utility has no run id'
Assert-Case ($null -eq $ready.elapsed_seconds) 'utility has no task elapsed time'
Assert-Case (($ready.eta.total_min_seconds -eq 2700) -and ($ready.eta.total_max_seconds -eq 5400)) 'task override ETA is 45-90 minutes'

$started = [datetimeoffset]::UtcNow.AddMinutes(-10)
$running = [pscustomobject]@{
    kind='task'; state='running'; pid=12345
    started_at=$started.ToString('o'); updated_at=$started.ToString('o')
    command='dsh --profile headless next'
}
$live = Get-WorkerTelemetry -Project $project -Worker $running -TaskId 'P4.2.3b' -LastActivityAt ([datetimeoffset]::UtcNow.ToString('o')) -RunsPath $runsPath
Assert-Case (-not [string]::IsNullOrWhiteSpace([string]$live.run_id)) 'task has stable run id'
Assert-Case (($live.elapsed_seconds -ge 590) -and ($live.elapsed_seconds -le 620)) 'running elapsed time is measured'
Assert-Case ($live.health -eq 'OK') 'recent activity is healthy'

$stale = Get-WorkerTelemetry -Project $project -Worker $running -TaskId 'P4.2.3b' -LastActivityAt ([datetimeoffset]::UtcNow.AddMinutes(-20).ToString('o')) -RunsPath $runsPath
Assert-Case ($stale.health -eq 'STALLED_WARNING') 'stale activity is flagged'
$current = [pscustomobject]@{ id='labdemo'; state='READY_TO_RUN'; telemetry=[pscustomobject]@{ task_id='P4.2.3b'; run_id=$null } }
$previous = [pscustomobject]@{ id='labdemo'; state='READY_TO_RUN'; telemetry=[pscustomobject]@{ task_id='P4.2.3b'; run_id='old-utility' } }
Assert-Case ($null -eq (New-StateEvent -Previous $previous -Current $current)) 'idle run-id cleanup does not create event noise'

Append-JsonLine -Path $runsPath -Value ([ordered]@{ project_id='labdemo'; result='completed'; duration_seconds=2400 }) -Encoding $encoding
Append-JsonLine -Path $runsPath -Value ([ordered]@{ project_id='labdemo'; result='completed'; duration_seconds=3600 }) -Encoding $encoding
Append-JsonLine -Path $runsPath -Value ([ordered]@{ project_id='labdemo'; result='completed'; duration_seconds=3000 }) -Encoding $encoding
$historyProject = [pscustomobject]@{
    id='other'
    eta=[pscustomobject]@{
        default_worker_minutes=[pscustomobject]@{min=20;max=100}; historical_min_samples=3
        stall_warning_minutes=15; hard_timeout_minutes=180; task_overrides=@()
    }
}
foreach($seconds in @(2400,3600,3000)) { Append-JsonLine -Path $runsPath -Value ([ordered]@{ project_id='other'; result='completed'; duration_seconds=$seconds }) -Encoding $encoding }
$policy = Get-EtaPolicy -Project $historyProject -TaskId 'P9.1' -RunsPath $runsPath
Assert-Case ($policy.source -eq 'project_history_median') 'history median becomes ETA source after threshold'

Write-Output ("SUMMARY passed={0} failed={1}" -f $script:passed,$script:failed)
Remove-Item -LiteralPath $root -Recurse -Force
if ($script:failed -ne 0) { exit 1 }
