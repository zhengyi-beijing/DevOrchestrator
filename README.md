# Dev Orchestrator

Independent local orchestration/observability service for AI-assisted development projects.

## P0 scope

P0 is intentionally read-only. It monitors registered projects and writes normalized runtime snapshots without calling AI, starting Workers, advancing phases, or touching hardware.

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
- `runtime/projects/<id>.json`: normalized per-project snapshot.

Possible project states currently include `READY_TO_RUN`, `WORKER_RUNNING`, `WAITING_REVIEW`, `WORKER_FAILED`, `WORKER_LOST`, `BLOCKED`, `WAITING_PHASE_GATE`, and `IDLE`.

## Safety boundary

P0 performs observation only. It does not execute `next`, run tests/builds, invoke DeepSeek/Codex, alter Git state, or issue hardware commands.

## Planned next layers

1. Runtime telemetry and ETA history.
2. Read-only Web dashboard over normalized snapshots.
3. Explicit `PHASE_AUTO` state machine with hard phase/hardware/architecture gates.
