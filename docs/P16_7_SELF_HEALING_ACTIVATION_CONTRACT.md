# P16.7 Self-Healing Project Activation & Readiness Contract

## 1. Overview & Objective

This contract specifies the architecture, protocols, and boundaries for self-healing project activation and readiness in DevOrchestrator, based on the concrete 2026-09-23 `xray-hw-platform` incident regression.

The primary objective is to allow DevOrchestrator to diagnose and automatically recover from project-activation and readiness failures (such as unregistered repositories with orphan projections, unstructured legacy Markdown tokens, and transient broker/inspection failures) without repeated manual owner interventions ("继续", "目前状态", "修复这个问题").

---

## 2. Machine-Readable Readiness Contract

### 2.1 Authoritative Schema (`agent/execution-state.json`)

The single authoritative readiness file is located at `<repo>/agent/execution-state.json`. Free-form Markdown Status regex matching is explicitly non-authoritative.

- **Schema Version**: Exactly `1`.
- **Closed Vocabulary**:
  ```json
  "execution_state": "pending_design" | "ready_to_run" | "executing" | "completed" | "blocked"
  ```
- **Task Identity Binding**: `task_id` must match the current task header in `agent/next.md`. Any mismatch between `task_id` and the current task heading immediately fails closed before lifecycle projection and before any worker launch:
  ```
  code: READINESS_TASK_ID_MISMATCH
  state: invalid
  stale: true
  ```
- **Atomic Successor Activation**:
  When activating a staged successor (e.g. `P1` -> `P2`), `AIPlannerCoordinator._activate_staged_successor` atomically writes `agent/next.md` and `agent/execution-state.json` (`execution_state: "pending_design"`, `task_id: "P2"`) in a single Git commit boundary (`git add` and `git commit` pathspecs cover both files). Rollback on failure cleanly restores both files. A completed predecessor's `ready_to_run` state can never activate an unplanned successor.

### 2.2 Bounded Legacy Component Grammar

When `agent/execution-state.json` is absent, legacy `Status:` lines in `agent/next.md` are parsed through a deterministic, closed component mapping:

| Legacy Component Pattern | Canonical Semantic Role |
| :--- | :--- |
| `READY`, `READY_TO_RUN`, `DESIGN READY`, `EXECUTABLE` | `design_ready` |
| `PENDING DESIGN` | `pending_design` |
| `BLOCKED` | `blocked` |
| `COMPLETE`, `ACCEPTED`, `DONE` | `completed` |
| `OWNER_GOAL_DEFINED`, `OWNER_APPROVED` | `owner_goal_defined` (attribute) |
| `NOT_STARTED` | `not_started` |
| `IN_PROGRESS`, `RUNNING` | `executing` |

- **Migration Candidates**:
  - `design_ready` + `not_started` (or `design_ready` alone) => candidate `ready_to_run`
  - `pending_design` => candidate `pending_design`
- **Fail-Closed Diagnostics**:
  - Composite tokens (e.g. `READY / OWNER_GOAL_DEFINED / NOT_STARTED`) project `IDLE` with code `READINESS_TOKEN_UNSTRUCTURED` and migration candidate `ready_to_run`.
  - Conflicting lifecycle pairs (e.g. `blocked` and `design_ready`) or unresolvable components fail closed with `READINESS_TOKEN_UNRESOLVABLE`.

### 2.3 Audited Readiness Migration

`migrate_legacy_readiness(...)` runs exclusively within the daemon-authoritative supervisor tick and requires all 7 predicates before writing:
1. The caller supplies the project entry from the effective daemon registry; the migration function validates its project identity and repository path rather than independently re-reading a guessed config location.
2. Structured file is absent or stale for a superseded task.
3. Exactly one non-conflicting candidate (`ready_to_run` or `pending_design`).
4. Task contract validates (single task heading, single Status line, matching telemetry).
5. Clean Git anchor (`read_repository_truth` valid, zero unexpected dirty entries).
6. No active owner gate, owner pause, or active worker execution.
7. Migration budget unconsumed for `(project_id, task_id, recovery_epoch_id)`.

On satisfaction:
- Writes `agent/execution-state.json` with candidate state and current `task_id`.
- Commits only that path: `chore(readiness): adopt structured execution state for <task_id>`.
- Appends audit entry to `runtime/readiness-migrations.jsonl`.

---

## 3. Bootstrap Activation & Orphan State

### 3.1 Bootstrap Authority & Ledger

DevOrchestrator never scans the arbitrary filesystem for repositories. The sole bootstrap origin is an explicit owner-supplied activation request:
- CLI: `devorch project-activate --repo <path> [--project-id <id>] [--profile <name>] [--config <path>]`
- Control API: `POST /api/v1/control/projects/activate`

Stored in `runtime/activation-requests.json` (`schema_version: 1`).

### 3.2 Orphan State Detection

`detect_orphan_state(project_id, repo_path, config, runtime_root)` identifies when a repository has a local status mirror (e.g. `<repo>/.devorch/status.json`) or runtime mirror while the project is absent from `config/projects.json`.
- Surfaces `ORPHANED_PROJECT_STATE` with stale phase, stale task ID, and stale state explicitly flagged as non-authoritative historical projection.
- Ensures `.devorch/` is registered in `.git/info/exclude`.

### 3.3 Atomic Registration Reconcile

`reconcile_project_registration(request, config_path, runtime_root)` validates:
- `repo_path` exists and is a Git worktree.
- `project_id` and `repo_path` are unique against loaded config.
- Resolves worker/runtime configuration template from config's `activation_profiles`.
- Atomically updates `config/projects.json` via read-modify-write without modifying existing entries.

Owner-facing configuration must provide at least one usable activation profile when automatic registration is expected. The tracked `config/projects.example.json` contains a `default` AIBroker profile and the bounded `self_healing` defaults. Production deployments should define the equivalent profile in their effective `projects.json`; otherwise bootstrap reconciliation fails closed with `REGISTRATION_TEMPLATE_MISSING` rather than guessing execution policy.

The activation request's `requested_action` (`continue` or `start`) is copied into the durable execution intent and is preserved across registration/readiness recovery, so the supervisor retries the original owner-authorized action rather than silently substituting `continue`.

---

## 4. Structured Blocker Diagnostics

Reachable via:
- CLI: `devorch project-explain-block (--project-id <id> | --repo <path>)`
- API: `GET /api/v1/control/projects/{id}/blockers`
- Mobile: `result["blockers"]`
- Control commands: blocked outcomes include `blockers` list.

Blocker dataclass format:
```python
@dataclass(frozen=True)
class Blocker:
    code: str
    predicate: str
    expected: Any
    observed: Any
    evidence_source: str
    remediation: str
    failure_class: str
    owner_gate_required: bool
```

### Shared Failure Classification Vocabulary
1. `transient_infrastructure`: RDC/SSH/broker timeouts, status call timed out.
2. `recoverable_orchestration`: Orphan registration, readiness migration, task-id drift, ready_to_run_unlaunched.
3. `lifecycle`: Valid task state that is not currently executable (for example `pending_design` before planning completes); this is neither a terminal failure nor an owner gate.
4. `owner_gate`: Genuine owner-only architectural, policy, or safety decision.
5. `recovery_exhausted`: Terminal state when self-healing budgets are exhausted. Never fabricates an owner gate.
6. `terminal`: Non-recoverable configuration or structural failures.

---

## 5. Durable Execution Intent & Self-Recovery Supervisor

### 5.1 Ledger (`runtime/execution-intent.json`)

Tracks active execution intent per project:
- Target state: `EXECUTING`.
- Recovery fingerprint history: hashes of `(project, task_id, lifecycle_state, blocker_code, git_anchor, worker_state)`.
- Budgets:
  - Max recovery actions: default 20.
  - Max identical failure repeats: default 3.
  - Max launch elapsed time: default 30 minutes.
- Configurable per-project via `self_healing` block in `config/projects.json`.

### 5.2 Activation Supervisor Execution Loop

`ActivationSupervisor.advance(...)` runs each daemon tick strictly after `WatchdogCoordinator.advance(...)`:
1. Consumes and clears watchdog handoff records.
2. Evaluates canonical blockers using `explain_block`.
3. If no blockers: drives forward transition via `submit_control_command`.
4. If blockers present:
   - Evaluates livelock cycle detection ($A \to B \to A \to B$) and repeat limits.
   - If exhausted: marks intent `exhausted`, logs `RECOVERY_EXHAUSTED` milestone, fails closed without owner gate.
   - If genuine owner gate: preserves gate without recovery loop interference.
   - If recoverable: applies bounded remediation (`reconcile_registration`, `migrate_readiness`, or transient backoff), increments action counter, and re-evaluates next tick.

### 5.3 Watchdog Handoff Coordination

In `WatchdogCoordinator.advance(...)`:
- When an active execution intent exists for the project with matching recovery epoch, `max_attempts_per_run` exhaustion records `recovery_handoff` with milestone `RECOVERY_HANDOFF` and status `"max_attempts_handed_off"` instead of persisting an `OWNER_GATE`.
- `_emit_owner_gate_once` suppresses gate emission for transient and recoverable failures under an active intent.
- Genuine owner gates and non-intent watchdog operations remain unchanged.

### 5.4 Idempotent Control Operations

Duplicate `continue` or `start` commands submitted while a worker is already launching or running resolve deterministically as an idempotent no-op:
```json
{
  "state": "accepted",
  "effect": "NOOP_ALREADY_EXECUTING",
  "execution_id": "...",
  "task_id": "..."
}
```
Eliminates race conditions between supervisor re-launches and manual owner commands.
