# Transition Executor / Worker Actuation V1

Status: OWNER AUTHORIZED / CONTRACT FROZEN 2026-09-05

## Goal

Close one bounded unattended loop without weakening the existing Decision Guard:

```text
managed Worker completes
  -> WORKER_DONE
  -> Web Sol reviewer
  -> Decision Guard
  -> APPLY + NEXT_TASK
  -> Transition Executor
  -> one new Worker
```

V1 does not automate `next_stage`, `remediate`, `retry`, owner-gated actions, or hardware actions.

## Execution authority

A Web Sol disposition is necessary but never sufficient execution authority.
A project is executable only when its local project config contains all of:

- `execution.enabled == true`;
- `execution.owner_authorized == true`;
- `execution.allowed_next_actions == ["next_task"]`;
- `execution.preferred_backends` is an ordered non-empty subset of `agy` / `dsh`;
- backend-specific options live under `execution.backends.agy` and `execution.backends.dsh`.

The executor accepts only a newly consumed `APPLY + NEXT_TASK` response whose role is `reviewer` and event is `worker_done`.

## Fresh-truth and task gates

Immediately before launch the executor re-reads repository truth and requires:

- repository truth is valid and clean;
- branch and HEAD still equal the reviewed request identity;
- the project monitor state is `READY_TO_RUN`;
- no external task Worker is currently alive;
- the current `agent/next.md` resolves to a task id;
- for automatic `NEXT_TASK`, that current task id differs from the task just reviewed.

The launched Worker request uses role `worker`, working directory `repo_path`, and requires `code` + `repository` capabilities. Routing remains provider-neutral through `AgentRouter`. V1 supports `agy` and `dsh`; `preferred_backends` defines deterministic project-local priority and normal router fallback. AGY may configure an explicit executable path so daemon behavior never depends on an ambient PATH refresh.

## Idempotency and replay

Before starting a model, the executor persists an entry keyed by the source request id in `runtime/transition-executor.json`.

- the same request id can launch at most once;
- at most one managed Worker may be active per project;
- duplicate daemon ticks or duplicate response consumption do not launch again;
- a daemon restart that finds `launching` or `running` state marks it `recovery_required` and never replays it automatically.

This fail-closed restart rule is required because the current `AgentBackend` contract tracks active runs only within one process.

## Project isolation and completion truth

Authoritative managed-run truth stays under the DevOrchestrator runtime (`agent-runs/` plus the transition ledger). For operator visibility only, the daemon writes a derived project-local mirror at `.devorch/status.json`. That mirror is never consumed as execution authority, contains no prompt/nonce/claim token, is written atomically, and `.devorch/` is added only to the repository-local `.git/info/exclude` so tracked project files and Git cleanliness are unchanged.

When a managed run completes successfully, the daemon overlays that DevOrchestrator-owned terminal run onto the in-memory project summary solely for the existing `WORKER_DONE` dispatcher. The synthetic Worker projection carries the task id captured at launch, so a Worker that updates `agent/next.md` cannot relabel the just-completed run as the next task.

A successfully dispatched review request is recorded in the transition ledger. If the browser binding is unavailable, the completed run remains pending and is retried through the existing dispatcher idempotency rules.

## Stop conditions

No Worker is launched when any of these is true:

- execution config is absent, disabled, malformed, or not owner-authorized;
- disposition is not `APPLY`;
- next action is not `NEXT_TASK`;
- event/role is outside `WORKER_DONE` reviewer flow;
- repository truth is unavailable, dirty, stale, or changed after review;
- current task cannot be identified or has not advanced;
- another managed/external Worker is active;
- routing has no eligible backend.

A managed run that exits nonzero is recorded `failed` and stops the automatic chain in V1. There is no implicit retry.

## One-time owner bootstrap

An unattended chain needs an initial Worker before any `WORKER_DONE` decision exists. V1 therefore permits one explicitly configured bootstrap token:

```json
"bootstrap": { "request_id": "owner-unique-id", "task_id": "P1" }
```

The bootstrap uses the same execution policy, fresh-truth checks, one-active-run rule, router, and ledger. The exact bootstrap request id is persisted before launch and can never auto-run twice. The configured `task_id` must equal the current `agent/next.md` task id.

## Acceptance

1. Unit tests prove config authority and `APPLY + NEXT_TASK` gates.
2. Duplicate decisions/ticks start one Worker only.
3. Restart of an active ledger entry becomes `recovery_required` without replay.
4. Completion overlay preserves the task id captured at launch.
5. Failed Worker stops without implicit retry.
6. Full `tests_py`, `node --check`, and `git diff --check` remain green.
7. Real AGY and DSH probes remain non-prompt; no acceptance test sends a real AI task.
8. Real LineScanViewer smoke starts P1 only through an explicit bootstrap token; subsequent successful reviews may use automatic `NEXT_TASK`.
