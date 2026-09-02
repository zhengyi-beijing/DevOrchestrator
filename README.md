# Dev Orchestrator

Independent local orchestration/observability service for AI-assisted development projects.

## Current scope: P1

P0 established the read-only 60-second project monitor. P1 adds runtime telemetry and ETA history while preserving the same observation-only safety boundary.

P1 records state transitions, real Worker durations, elapsed time, activity age, stall/timeout health, and an ETA range. It still does not call AI, start Workers, advance phases, or touch hardware.

Current polling interval: 60 seconds.

## Current project adapter

The first registered project is LabDemo. The monitor reads:

- Git branch, HEAD, and dirty entry count.
- `agent/CURRENT.md` and `agent/next.md` summary fields.
- `tmp/worker-dsh/status.json` and Worker process liveness.
- Last activity timestamps from Worker/agent files.

Snapshots are written only under `runtime/` in this repository.
## Commands

Single read-only sample:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\src\monitor.ps1 -Once
```

Start the detached monitor:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\start-monitor.ps1 -IntervalSeconds 60
```

Check or stop it:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\status-monitor.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\stop-monitor.ps1
```
## Runtime state

- `runtime/monitor.json`: watchdog heartbeat.
- `runtime/summary.json`: all registered projects.
- `runtime/projects/<id>.json`: normalized per-project snapshot including telemetry/ETA.
- `runtime/history/events.jsonl`: state/run transition history.
- `runtime/history/runs.jsonl`: one immutable record per completed/failed Worker run.

ETA uses an explicit task override when configured. Otherwise it starts from the project default; after the configured minimum number of completed runs, the fallback can use the project historical median. Health is `OK`, `STALLED_WARNING`, or `TIMEOUT`; P1 only reports these states and never terminates a Worker.

Possible project states currently include `READY_TO_RUN`, `WORKER_RUNNING`, `WAITING_REVIEW`, `WORKER_FAILED`, `WORKER_LOST`, `BLOCKED`, `WAITING_PHASE_GATE`, and `IDLE`.

## Safety boundary

P1 remains observation only. It does not execute `next`, run tests/builds, invoke DeepSeek/Codex, alter Git state, terminate Workers, or issue hardware commands.

Telemetry self-test:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tests\telemetry-selftest.ps1
```

## Planned next layers

1. Read-only Web dashboard over normalized snapshots.
2. Explicit `PHASE_AUTO` state machine with hard phase/hardware/architecture gates.

## Next queued task: P2 Web Dashboard

`NEXT.md` is the authoritative next-task handoff. P2 adds a separate read-only Web process over the existing normalized runtime snapshots. It does not enable Worker control or phase automation.

Planned default local URL: `http://127.0.0.1:8770/`. The implementation must also support an explicit listen address for LAN access; firewall/OS exposure changes remain separate from the read-only dashboard implementation.
