# Dev Orchestrator

Independent local observability/orchestration service for AI-assisted development projects.

## Current scope: P2

P0 established the read-only 60-second project monitor. P1 added runtime telemetry, Worker history, activity health, and ETA. P2 adds a dependency-free read-only Web dashboard over those normalized runtime files.

The project still does **not** invoke AI, start Workers, advance phases, mutate observed Git repositories, run hardware actions, or expose a generic shell/prompt endpoint.

## Architecture

```text
Observed projects (read-only)
        |
        v
src/monitor.ps1  +  src/telemetry.ps1
        |
        v
runtime/*.json + runtime/history/*.jsonl
        |
        v
src/web-server.ps1
        |
        v
Browser dashboard
```

Monitor and Web server are independent processes. A dead/stale monitor is shown explicitly by the dashboard rather than hidden.
## Monitor commands

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\src\monitor.ps1 -Once
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\start-monitor.ps1 -IntervalSeconds 60
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\status-monitor.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\stop-monitor.ps1
```

## Web dashboard

Default loopback start:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\start-web.ps1
```

Open:

```text
http://127.0.0.1:8770/
```

Status / stop:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\status-web.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\stop-web.ps1
```
Explicit LAN bind example (only when LAN exposure is intended):

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\start-web.ps1 -ListenAddress 10.138.7.167 -Port 8770
```

The P2 server uses `.NET TcpListener`, so a specific local IP bind does not require an HttpListener URLACL. Windows firewall/network policy can still control reachability.

### Read-only HTTP surface

Allowed methods: `GET`, `HEAD` only.

- `/` dashboard
- `/app.js`, `/style.css`
- `/api/monitor`
- `/api/summary`
- `/api/projects/<id>`
- `/api/events?limit=N` (bounded to 100)
- `/api/runs?limit=N` (bounded to 100)

All other routes return 404; write methods return 405; encoded/raw path traversal is rejected. There are no Worker, shell, prompt, Git mutation, or hardware-control endpoints.

## Runtime state

- `runtime/monitor.json`: monitor heartbeat.
- `runtime/summary.json`: normalized registered-project overview.
- `runtime/projects/<id>.json`: project state including telemetry/ETA.
- `runtime/history/events.jsonl`: state/run transitions.
- `runtime/history/runs.jsonl`: immutable terminal Worker samples.
- `runtime/web.json`: Web server heartbeat and bind information.
## Tests

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tests\telemetry-selftest.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tests\web-selftest.ps1
```

`web-selftest.ps1` is hardware-free and uses fixture runtime state plus loopback HTTP. It verifies static/API success, stale-monitor derivation, bounded history, HEAD, 404, 405, encoded traversal rejection, and start/status/stop lifecycle.

## Safety boundary

P2 is observation only. Software acceptance does not imply hardware acceptance. Dashboard state is a projection of normalized project state and does not infer safety or hardware truth beyond the data provided by each registered adapter.

## Planned next layer

P3 is an explicit `PHASE_AUTO` state machine with hard phase/hardware/architecture gates. P3 is not implemented or authorized by P2.
