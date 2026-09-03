# DevOrchestrator Python v1 — P2 Compatibility Slice

Status: **DESIGN READY / TEST-FIRST / IMPLEMENTATION AUTHORIZED**

## Goal

Replace the current PowerShell P0-P2 implementation with a portable Python >=3.11 implementation while preserving the accepted read-only behavior and runtime/API contracts. Windows is the first acceptance host; the implementation must avoid Windows-only business logic so the same package can run on macOS, Linux, and Raspberry Pi.

This slice is deliberately **P2-equivalent only**. It does not start AI agents, advance phases, mutate observed repositories, run builds, or perform hardware actions.

## Compatibility invariants

- Existing `config/projects.json` remains a valid configuration source.
- Existing `runtime/monitor.json`, `summary.json`, `projects/<id>.json`, `history/events.jsonl`, `history/runs.jsonl`, and `web.json` remain the public runtime files.
- Monitor state names remain compatible: `UNAVAILABLE`, `MONITOR_ERROR`, `WORKER_LOST`, `WORKER_RUNNING`, `WORKER_FAILED`, `BLOCKED`, `WAITING_PHASE_GATE`, `WAITING_REVIEW`, `READY_TO_RUN`, `IDLE`.
- The HTTP surface remains read-only and preserves the P2 route/method allowlist.
- Existing PowerShell implementation remains in-tree as a compatibility/reference path during this slice.

## Python architecture

```text
config/projects.json
      |
      v
dev_orchestrator.monitor
  |        |        |
  |        |        +-- platform.process (OS-specific PID liveness only)
  |        +----------- telemetry (pure functions)
  +-------------------- storage (atomic JSON / JSONL)
      |
      v
runtime/*
      |
      v
dev_orchestrator.web (stdlib HTTP server)
```

Package layout:

```text
src/dev_orchestrator/
  __init__.py  __main__.py  cli.py  config.py
  monitor/{project.py,telemetry.py}
  platform/process.py
  storage/json_store.py
  web/server.py
```

Business logic must use `pathlib`, `subprocess`, `datetime`, and typed Python data only. OS branching is confined to `platform/process.py` and detached-process lifecycle helpers. No shell parsing for Git output beyond invoking the Git CLI with argument arrays.

## CLI contract

All acceptance commands use the project interpreter explicitly, never bare `python` from PATH.

```text
<venv>/python -m dev_orchestrator monitor --once [--config PATH] [--runtime-root PATH]
<venv>/python -m dev_orchestrator monitor --interval 60
<venv>/python -m dev_orchestrator web --listen 127.0.0.1 --port 8770
<venv>/python -m dev_orchestrator start-monitor|status-monitor|stop-monitor
<venv>/python -m dev_orchestrator start-web|status-web|stop-web
```

`monitor --once` prints the normalized summary JSON and exits 0. Long-running monitor/web commands write PID/heartbeat files. Start/status/stop operate only on DevOrchestrator-owned PID files and must not kill unrelated processes.

## HTTP contract

Allowed methods are `GET` and `HEAD`. Static allowlist is `/`, `/app.js`, `/style.css`. API allowlist is `/api/monitor`, `/api/summary`, `/api/projects/<id>`, `/api/events?limit=N`, `/api/runs?limit=N`. History limits clamp to 1..100. Unknown routes return 404, write methods 405 with `Allow: GET, HEAD`, malformed/traversal paths 400. `Cache-Control: no-store` and `X-Content-Type-Options: nosniff` remain present.

The Web process reads runtime projections only; it never shells into observed projects.

## Monitor and telemetry semantics

- Git projection: branch, HEAD, dirty, changed entry count.
- Worker projection reads only `<project root>/<worker_runtime>/status.json`; missing means `not_started`.
- A `headless next` command is a task run; other commands are utility runs and must not pollute run history.
- Task ID parsing preserves `P4.2.3b`-style identifiers.
- ETA precedence is task override, then project historical completed-run median after threshold, then project default.
- Running task health is `TIMEOUT`, `STALLED_WARNING`, or `OK` using project policy.
- State transitions/history must not emit noise merely because a utility/stale run id disappears.
- Writes are atomic; terminal task run records are idempotent by stable run id.

## Test-first acceptance

Designer-owned tests live under `tests_py/` and must not be weakened, skipped, or rewritten to match the implementation. Acceptance requires:

1. Python telemetry/state parity tests GREEN.
2. Monitor fixture integration GREEN and no mutation of the observed fixture repository.
3. Web read-only contract GREEN, including HEAD/404/405/traversal/history bounds.
4. Existing PowerShell `telemetry-selftest.ps1` and `web-selftest.ps1` remain GREEN.
5. One real `LabDemo` `monitor --once` run succeeds read-only and produces a valid `runtime/projects/labdemo.json` projection.
6. `git diff --check` clean apart from accepted line-ending warnings.

## Explicit non-goals

No `PHASE_AUTO`, Worker start/review loop, AgentBackend/Router execution, token/quota telemetry, remote Node protocol, DB migration, generic command endpoint, observed-repo mutation, LabDemo P4.3.4, or real hardware action. The next slice will introduce the unified AgentBackend/Router only after this compatibility foundation is accepted.
