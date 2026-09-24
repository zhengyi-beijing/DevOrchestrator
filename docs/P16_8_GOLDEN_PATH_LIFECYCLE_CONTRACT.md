# P16.8 DevO Golden-Path Lifecycle Hardening Contract

## 1. Overview & Golden-Path Guarantee

This contract establishes the deterministic lifecycle continuity rules and operational contracts for DevOrchestrator's Golden-Path Lifecycle under P16.8.

### 1.1 The Golden-Path Invariant
A fresh, owner-authorized task must advance reliably from `PENDING_DESIGN` to `DONE` under a single owner `continue` command without requiring intermediate manual command repair, repeated Continue invocations, or manual status-line patching.

Intermediate state transitions (`PENDING_DESIGN` -> `READY_TO_RUN` -> `EXECUTING` -> `WAITING_REVIEW` -> `DONE`) are autonomously managed by the `ActivationSupervisor`, `AIPlannerCoordinator`, `TransitionExecutor`, and post-execution review pipeline.

---

## 2. Authoritative Task State Precedence (3-Rule Contract)

Task readiness authority is resolved exclusively through `resolve_task_state(...)` in `src/dev_orchestrator/core/readiness.py`. Free-form Markdown Status regex matching is explicitly non-authoritative when structured readiness is available.

The precedence rules are strictly evaluated in order:

### Rule 1: Valid Structured Readiness Authority
When `<repo>/agent/execution-state.json` exists, conforms to `schema_version: 1`, has a valid vocabulary state (`pending_design`, `ready_to_run`, `executing`, `completed`, `blocked`), and its `task_id` strictly matches the active task ID:
- It **always** supplies the authoritative task state.
- `authority` is set to `"structured"`.
- `valid` is set to `True`.
- Any textual disagreement with Markdown `agent/next.md` is diagnostic-only (`consistency: "conflict"`) and **never** blocks execution or launch.

### Rule 2: Absent Structured Readiness Fallback
When `<repo>/agent/execution-state.json` does not exist:
- The system safely falls back to parsed Markdown from `agent/next.md`.
- `authority` is set to `"markdown"`.
- Legacy component tokens are parsed via `parse_legacy_status_components(...)`.

### Rule 3: Invalid / Stale Structured Readiness Fails Closed
When `<repo>/agent/execution-state.json` exists but is invalid, corrupted, schema-incompatible, or has a mismatched/stale `task_id`:
- The system **fails closed**.
- `authority` is set to `"none"`.
- `state` is set to `"invalid"`, `valid: False`, `stale: True`.
- Markdown fallback is **prohibited**. The system will never bypass a corrupted or mismatched structured state by looking at Markdown.

---

## 3. Canonical Task Status Grammar & Parser

All raw substring and regular expression checks against `Status:` lines are eliminated from production codebase. All modules must parse status via `parse_task_status(...)` in `src/dev_orchestrator/core/task_status.py`.

### 3.1 Closed Semantic Predicates
The `ParsedTaskStatus` dataclass exposes closed semantic queries:
- `is_pending_design()`: Matches `PENDING DESIGN`, `PENDING_DESIGN`, `PLANNING`
- `is_ready_to_run()`: Matches `READY TO RUN`, `READY_TO_RUN`, `DESIGN READY`, `EXECUTABLE`, `APPROVED`
- `is_completed()`: Matches `COMPLETED`, `ACCEPTED`, `DONE`, `FINISHED`
- `is_blocked()`: Matches `BLOCKED`, `WAITING_OWNER`
- `is_executing()`: Matches `EXECUTING`, `IN_PROGRESS`, `RUNNING`

### 3.2 Canonical Editing & Formatting
Status line extraction and formatting are centralized:
- `find_status_lines(text)`: Returns all occurrences of `Status:` lines.
- `require_single_status_line(text)`: Enforces exactly one Status line in `agent/next.md`, failing closed if 0 or >1 lines exist.
- `render_status_line(status)`: Generates canonical format: `- **Status**: <canonical_status>`.

---

## 4. ExecutionContext Continuity & Lifecycle State Machine

ExecutionContext is maintained at `runtime/execution-context.json` and protected against concurrency issues by `InterProcessFileLock` at `runtime/execution-context.lock`.

### 4.1 Schema Contract (`schema_version: 1`)
```json
{
  "schema_version": 1,
  "project_id": "proj-xyz",
  "task_id": "P1",
  "git_anchor": "a1b2c3d4e5f6...",
  "active_role": "planner" | "reviewer" | "worker" | null,
  "role_history": ["planner"],
  "stage": "planning" | "plan_review" | "execution" | "technical_review" | "completed" | "idle",
  "next_action": "plan" | "plan_review" | "execute" | "technical_review" | "remediate" | "none",
  "disposition": "advance" | "hold" | "remediate" | "transient_infrastructure" | "owner_gate" | "owner_stop" | "terminal_success" | "terminal_failure",
  "idle_ticks": 0,
  "created_at": "2026-09-24T12:00:00Z",
  "updated_at": "2026-09-24T12:00:00Z",
  "recovery_snapshot": {}
}
```

### 4.2 Git Anchor Revalidation & Stale Recovery
The supervisor continuously verifies `git_anchor` against the repository's current `HEAD`.
- If `HEAD` advances (e.g. from `HEAD_A` to `HEAD_B`) or `task_id` diverged:
  1. `context_is_stale(...)` evaluates to `True`.
  2. The supervisor emits the `EXECUTION_CONTEXT_STALE` milestone.
  3. The context is automatically re-anchored to the clean repository `HEAD_B`.
  4. Any obsolete background planner or worker execution for `HEAD_A` is discarded, and a replacement planner is dispatched on `HEAD_B`.

### 4.3 Idle Tick & Continuation Fault
- When an active intent is present with a valid next action and the project is idle (no role currently executing):
  - **Tick 1**: Holds for observation (`idle_ticks` incremented to `1`), emitting `CONTINUATION_HOLD`.
  - **Tick 2**: If still idle (`idle_ticks >= 1`), emits `CONTINUATION_FAULT`, resets `idle_ticks: 0`, and idempotently redispatches with a deterministic command ID (`cmd-rec-<project>-<task>-<epoch>-<action>-<count>`).

### 4.4 Transient Infrastructure Backoff
- When a transient blocker is encountered (e.g., broker timeout, network hiccup):
  - Sets backoff timestamp (`backoff_until`) without consuming recovery action budget.
  - While waiting for backoff, supervisor reports `transient_backoff_waiting`.
  - When backoff expires and the error clears, execution resumes immediately without burning recovery action count.

---

## 5. Additive Activity Telemetry Contract

Exposed through `core/activity_telemetry.py`, `core/project_status.py`, and `control/surface.py`, projecting a 9-field machine-readable activity block:

| Field | Type | Description |
| :--- | :--- | :--- |
| `stage` | String | Current phase (`planning`, `plan_review`, `execution`, `technical_review`, `completed`, `idle`) |
| `active_role` | String / null | Currently running role (`planner`, `reviewer`, `worker`, or `null`) |
| `worker_state` | String / null | State of worker execution (`running`, `completed`, `failed`, or `null`) |
| `code_execution` | Boolean | `true` if active code or test execution is occurring |
| `orchestrator_activity` | String | Human/agent readable summary of current orchestrator activity |
| `next_action` | String | Next autonomous transition (`plan`, `plan_review`, `execute`, `technical_review`, `remediate`, `none`) |
| `intent_state` | String | State of owner execution intent (`active`, `satisfied`, `exhausted`, `stopped`, `idle`) |
| `disposition` | String | Current supervisor disposition |
| `task_state_source` | String | Source of authoritative task state (`structured`, `markdown`, `fallback`, `unknown`) |

---

## 6. Actionable Artifact Error Payloads

When AI artifacts (e.g., plan definitions) violate schema limits (such as the 24-item list bound in `ai_planner.py`), `StructuredOutputError` and `PlannerProtocolError` provide structured, machine-actionable feedback:

```json
{
  "field": "steps",
  "expected": "<= 24 items",
  "actual": 25,
  "correction": "Consolidate or prune the plan to at most 24 steps",
  "actionable_message": "Artifact validation failed for 'steps': expected <= 24 items, got 25. Consolidate or prune the plan to at most 24 steps"
}
```

This prevents unguided trial-and-error loops by providing unambiguous repair instructions directly to the planner.

---

## 7. Terminal Action Closure

When a task reaches terminal completion (`completed` / `DONE`):
- `resolve_next_action(...)` resolves to `("none", "terminal_success")`.
- Active execution intent is marked `satisfied` with reason `"current task is terminal"`.
- Mutating control actions (`continue`, `retry`, `rereview`, `reconcile`) are disabled and return terminal closure errors.
- Successor activation requires an explicit owner or automated staged transition.

---

## 8. Registered Progress Milestones

The following lifecycle milestones are registered in `core/progress.py`:
- `CONTINUATION_DISPATCHED`: An autonomous forward continuation was dispatched.
- `CONTINUATION_FAULT`: An idle continuation fault was detected and triggered redispatch.
- `CONTINUATION_HOLD`: Continuation held for an active role or transient backoff.
- `EXECUTION_CONTEXT_STALE`: An ExecutionContext git anchor or task mismatch was detected and re-anchored.
