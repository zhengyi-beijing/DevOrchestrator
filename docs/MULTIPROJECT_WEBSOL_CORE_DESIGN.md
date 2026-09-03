# Multi-project daemon + Web Sol control contract

Status: DESIGN FROZEN / TEST-FIRST
Baseline: `64950cb` (`AgentBackend + Router foundation` ACCEPTED)

## Target topology

```text
1 computer
  -> 1 DevOrchestrator daemon
       -> project A: repo + worker/task state + conversation binding A
       -> project B: repo + worker/task state + conversation binding B
       -> ...
```

The daemon is infrastructure. Project names are configuration data; Core must not branch on `labdemo`, `xray-hw-platform`, or any other product name.

## Audit of the current accepted implementation

- SATISFIED: `run_monitor_once()` already iterates `config.projects`; runtime snapshots/history are keyed by project id.
- SATISFIED: Agent routing requests already carry `project_id`; provider selection is project-neutral.
- PARTIAL: project config has id/root/worker runtime, but no conversation binding and no orchestration-readiness gate.
- PARTIAL: Core has no LabDemo conditional, but monitor directly assumes the `agent/*.md + worker status.json` project contract instead of selecting a ProjectAdapter.
- CONFLICT: monitor and Web are separate OS processes; the target architecture requires one DevOrchestrator daemon process per computer.
- NOT IMPLEMENTED: Web Sol event policy, request/response protocol, structured next action, repository-truth revalidation, stale/nonce/dirty/owner gates, Bridge transport boundary.
## Project configuration contract

Canonical project fields are:

```text
project_id
repo_path
conversation_binding { transport, adapter, binding_id }
worker_runtime
adapter                # default: agent_files
eta                     # existing policy
```

For P2 compatibility, legacy `id`/`root` remain accepted and normalize to `project_id`/`repo_path`. A missing conversation binding is monitorable but **not orchestration-ready**; Core must fail closed before emitting a Web Sol request or consuming a decision for that project. Duplicate project ids are invalid.

`conversation_binding` identifies where a project-specific reasoning event is transported. It is routing metadata, not a place to store credentials.

## ProjectAdapter boundary

Core selects a ProjectAdapter by project config. The initial adapter is `agent_files`, which understands the existing `agent/CURRENT.md`, `agent/next.md`, `agent/result.md`, and configured Worker runtime. LabDemo and xray-hw-platform may both use this adapter initially; a project may later supply a different adapter without changing Core.

Adapter responsibilities: project snapshot, Worker/task projection, project-specific task/stage hints. Core responsibilities: daemon lifecycle, project registry, repository truth, event/decision policy, storage, Web UI, Agent routing.
## Web Sol event policy

ChatGPT Web is not a log terminal. `PROGRESS`, `HEARTBEAT`, ordinary state sampling, and routine Worker stdout stay in daemon runtime/UI.

Only reasoning events are eligible for Web Sol transport in this slice:

```text
REVIEW_REQUIRED
WORKER_DONE
TEST_FAILED
OWNER_GATE
RECOVERY_REQUIRED
```

Bridge is transport only. The Userscript is the ChatGPT Web adapter only. Neither may choose the next action, start a Worker, advance a task, or reinterpret repository state.

## Web Sol protocol

Every request and response identity carries at least:

```text
project_id, request_id, task_id/stage_id,
branch, head, role, event, nonce
```

At least one of `task_id` or `stage_id` must be present. Web Sol roles are `PLANNER`, `REVIEWER`, `SUPERVISOR`. Decisions are `NEXT`, `REMEDIATE`, `RETRY`, `OWNER_GATE`, `STOP`.
`next_action` is mandatory and separate from decision rationale:

```text
CONTINUE_CURRENT_STAGE
NEXT_TASK
NEXT_STAGE
STOP
```

Allowed combinations:
- `NEXT` -> CONTINUE_CURRENT_STAGE | NEXT_TASK | NEXT_STAGE
- `REMEDIATE` -> CONTINUE_CURRENT_STAGE
- `RETRY` -> CONTINUE_CURRENT_STAGE
- `OWNER_GATE` -> STOP
- `STOP` -> STOP

This separation prevents an ambiguous `NEXT` from silently crossing a stage boundary.

## Repository-truth / decision guard

Web Sol never controls a Worker directly. After a response arrives, DevOrchestrator obtains fresh repository truth and validates the response before any future workflow may act.

Fail-closed rules:
- project/request/nonce mismatch -> IGNORE;
- task/stage/role/event identity mismatch -> IGNORE;
- response/request/current HEAD or branch mismatch -> STALE, no execution;
- missing/invalid `next_action` -> STOP;
- repository truth unavailable -> STOP;
- current dirty workspace without an explicitly established known-dirty contract -> REVIEW_REQUIRED;
- OWNER_GATE -> STOP and wait for human authorization.
## Single-daemon lifecycle

The preferred runtime entrypoint is one process:

```text
start-daemon
  -> daemon PID/heartbeat
  -> monitor loop for all configured projects
  -> read-only Web server thread in the same PID
```

`daemon.json`, `monitor.json`, and `web.json` must report the same live PID in daemon mode. Existing separate monitor/Web commands remain compatibility paths but are no longer the target deployment architecture.

## Minimal implementation slice

1. Add config normalization + conversation binding/orchestration-ready projection.
2. Add ProjectAdapter registry with the existing agent-file contract as the default adapter.
3. Add Web Sol event/protocol models and pure fail-closed decision validation; no transport implementation and no Worker execution.
4. Add fresh repository-truth reader used by the decision guard.
5. Add unified daemon lifecycle while preserving existing P2 CLI compatibility.
6. Freeze two-project tests proving project isolation and a single daemon PID.

Explicit non-goals: PHASE_AUTO, Bridge implementation, Userscript changes, actual Web Sol sends, automatic task/stage transition, project-specific special cases, real hardware actions, and resuming the stashed Codex Reviewer WIP.
