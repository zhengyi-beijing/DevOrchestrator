# Active-Project Progress Watchdog and Automatic Read-Only Diagnostics Design

Status: **V5 ACCEPTED DESIGN** (Supersedes v1-v4; incorporates Sol v4 canonical path provenance and Sol v3 fingerprint purity / runtime-root freezes)

## 1. Executive Summary and Problem Statement

DevOrchestrator coordinates long-running AI-driven development workflows across multiple projects. In complex orchestration scenarios, an active project (e.g. in `PLANNING`, `REVIEWING_PLAN`, `APPLYING_PLAN`, `EXECUTING`, `REVIEWING`, or `REMEDIATING`) may stall due to agent disconnections, silent process death, quota depletion, or state desynchronization without transitioning to a failed or blocked state.

Previously, DevOrchestrator lacked an automated watchdog to detect such stalls. A naive watchdog writing operation mirror files back into the project repository would create a dangerous feedback loop: its own status updates would refresh repository file timestamps (`mtime`), resetting the stall detection timer and perpetually masking deadlocks. Furthermore, clock leakage into attempt identifiers caused duplicate diagnostic spawns across ticks.

This specification establishes an active-project progress watchdog and deterministic read-only diagnostic system with:
1. **Source-Level Activity Filtering**: Watchdog-owned files are excluded from raw path-identifiable entries *before* any aggregate timestamp, age, or fingerprint is derived, eliminating self-refresh feedback loops while preserving legacy telemetry semantics.
2. **Canonical Path Identity and Dual Provenance**: Strict resolution using `realpath`, `abspath`, and `normcase` with `os.path.commonpath` containment checks, recording canonical fingerprints for both repository root and runtime root.
3. **Clock-Free Fingerprint Purity**: A frozen allowlist of durable event values strictly forbidding timestamps, ages, wall-clock, or monotonic values from entering `progress_fingerprint` or `attempt_key`.
4. **Deterministic Single-Flight Diagnostic Attempts**: Exactly one diagnostic attempt per durable stall event, bounded by monotonic deadlines and generation-based fencing.
5. **Fail-Closed State Persistence**: Quarantining corrupt or future-version state files rather than silently erasing execution history.
6. **Crash-Atomic Safe Recovery**: A two-phase reserve/enqueue/reconcile protocol using deterministic `wd-<attempt_key>` command identifiers, restricted exclusively to the existing non-destructive `continue` control action behind explicit opt-in.

---

## 2. Qualifying Progress Signals and Exclusion Rules

### 2.1 Qualifying Progress Signals
The watchdog monitors project progress solely by inspecting durable state artifacts:
1. `activity.watchdog_safe.last_activity_at`: The maximum mtime among qualifying, non-watchdog project files.
2. `git.head`: The current Git commit SHA-1 of the repository.
3. `activity.watchdog_safe.changed_entries_considered`: The count of porcelain changed entries excluding watchdog-owned paths.
4. Role records: The latest execution record timestamp and id in `ai-planner.json`, `ai-reviewer.json`, and `transition-executor.json` where `metadata.source != "watchdog"`.
5. Progress Channel: The newest notification entry in `history/progress.json` where `milestone` is NOT in `WATCHDOG_MILESTONES` and `details.source != "watchdog"`.

### 2.2 Watchdog-Generated Excluded Signals
The following signals are authored by the watchdog and MUST be excluded from progress evaluation:
- `.devorch/status.json` (repo-owned operational status mirror)
- `.devorch/watchdog*.json` and `.devorch/watchdog.json.corrupt-*` (repo-owned watchdog state/quarantine files)
- `runtime/watchdog.json` and `runtime/watchdog.json.corrupt-*` (runtime-owned watchdog state and quarantine copies)
- `runtime/control/inbox/wd-*.json` and `runtime/control/history/wd-*.json` (watchdog-submitted control commands)
- Progress notifications with milestone in `{"STALL_DETECTED", "DIAGNOSTIC_STARTED", "DIAGNOSTIC_RESULT", "RECOVERY_STARTED"}`
- Any progress notification or role record carrying `source = "watchdog"` (including watchdog-emitted `OWNER_GATE`).

---

## 3. Canonical Path Identity and Dual Provenance (Sol v4 / Plan v5)

### 3.1 Canonical Path Normalization
Path comparison strings vary across platforms, case conventions, trailing slashes, relative representations, and symlinks/junctions. Canonical path identity is strictly defined as:
```python
def canonical_path(p: Path | str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(p))))
```

Containment check is strictly path-aware and NEVER uses prefix, `startswith`, or suffix string comparison:
```python
def path_contains(root: Path | str, candidate: Path | str) -> bool:
    try:
        c_root = canonical_path(root)
        c_cand = canonical_path(candidate)
        return os.path.commonpath([c_root, c_cand]) == c_root
    except ValueError:
        # Raised on Windows for cross-drive comparisons or UNC/drive mismatch
        return False
```

### 3.2 Exclusion Predicate
The exclusion authority is frozen as a pure, three-argument function with no global or ambient runtime root:
```python
is_watchdog_owned_path(repo_root: Path | str, candidate: Path | str, *, runtime_root: Path | str | None = None) -> bool
```
- **Repo-owned paths**:
  - Exact: `.devorch/status.json`
  - Globs: `.devorch/watchdog*.json`, `.devorch/watchdog.json.corrupt-*`
- **Runtime-owned paths**: (Evaluated only when `runtime_root` is provided AND `path_contains(repo_root, runtime_root)` is True, as in self-hosted DevOrchestrator-dev):
  - Exact: `watchdog.json`
  - Globs: `watchdog.json.corrupt-*`, `control/inbox/wd-*.json`, `control/history/wd-*.json`

When `runtime_root` is `None` or resolves outside `repo_root`, runtime-owned rules are inert, preventing over-exclusion in external project layouts.

### 3.3 Dual Provenance Contract
The `activity.watchdog_safe` projection recorded on project snapshots contains cryptographic provenance fingerprints:
- `repo_root_fingerprint`: `sha256(canonical_path(repo_root))[:16]`
- `repo_scope`: `'canonical'`
- `runtime_root_fingerprint`: `sha256(canonical_path(runtime_root))[:16]` when `runtime_root` is known, else `None`
- `runtime_scope`: `'runtime-aware'` (if `runtime_root` is inside `repo_root`), `'runtime-external'` (if `runtime_root` is outside `repo_root`), or `'unknown'` (if `runtime_root` is `None`).

### 3.4 Provenance Fail-Closed Policy
The WatchdogCoordinator recomputes canonical fingerprints and verifies provenance before using any snapshot's activity block:
- If `repo_scope != 'canonical'` or `repo_root_fingerprint` is missing: fail closed with `repo-root-unknown`.
- If `repo_root_fingerprint != sha256(canonical_path(repo_root))[:16]`: fail closed with `repo-root-mismatch`.
- If coordinator's `runtime_root` is inside `repo_root` (`path_contains(repo_root, runtime_root)`):
  - If `runtime_scope == 'unknown'`: fail closed with `runtime-root-unaware`.
  - If `runtime_root_fingerprint != sha256(canonical_path(runtime_root))[:16]`: fail closed with `runtime-root-mismatch`.

Any provenance failure forces `activity_evidence = 'unavailable'`, causing the coordinator to suppress attempts and recoveries (monitor-only, no stall declared).

---

## 4. Fingerprint Purity and Attempt Identity (Sol v3 Fix A)

### 4.1 Frozen Allowlist (`FINGERPRINT_FIELDS`)
`progress_fingerprint` captures durable event state. It is assembled exclusively from:
- `git.head` (string SHA or empty)
- `activity.watchdog_safe.last_activity_at` (string ISO timestamp or None)
- `activity.watchdog_safe.newest_kind` (string or None)
- `activity.watchdog_safe.newest_path` (string repo-relative path or None)
- `activity.watchdog_safe.sources` per-kind (`worker_runtime`, `agent_file`, `git_changed`):
  - `last_activity_at` (string ISO or None)
  - `path` (string repo-relative path or None)
- `activity.watchdog_safe.changed_entries_considered` (integer count)
- `role_records` (newest non-watchdog role record id and timestamp per role file)
- `progress_entry` (newest qualifying progress notification id, timestamp, and milestone)

### 4.2 Forbidden Fields (`FINGERPRINT_FORBIDDEN`)
The fingerprint builder strictly verifies that none of the following fields are present in the payload:
- `age_seconds`, `watchdog_safe_activity_age_seconds`, `last_activity_age_seconds`
- `no_progress_seconds`, `now`, `last_checked_at`
- Any key matching `elapsed*`, `monotonic*`, `uptime*`
- `excluded_paths`, `excluded_count` (watchdog-mutable observability fields)
- `repo_root_fingerprint`, `runtime_root_fingerprint`, `repo_scope`, `runtime_scope` (evidence validation only)

`build_progress_fingerprint(payload)` raises `ValueError` fail-closed if any key outside `FINGERPRINT_FIELDS` or inside `FINGERPRINT_FORBIDDEN` is passed.

### 4.3 Identity Equations
- `run_key`: First non-empty of `telemetry.run_id`, active execution id in `transition-executor.json`, active `role_run_id` in planner/reviewer, or deterministic fallback `'norun:' + sha256(f"{project_id}|{task_id}|{lifecycle_state}")[:12]`.
- `run_scope_key`: `sha256(f"{project_id}|{task_id}|{lifecycle_state}|{run_key}")[:16]` (budget scope; stable across progress).
- `attempt_key`: `sha256(f"{run_scope_key}|{progress_fingerprint}")[:16]` (deduplication scope; changes only on genuine progress).
- `command_id`: `f"wd-{attempt_key}"` (deterministic control command identifier).

---

## 5. Monitored Lifecycle States and Policy Overrides

### 5.1 Monitored States
The watchdog monitors only active lifecycle states where progress is expected:
```python
ACTIVE_LIFECYCLE_STATES = frozenset({
    "PLANNING",
    "REVIEWING_PLAN",
    "APPLYING_PLAN",
    "EXECUTING",
    "REVIEWING",
    "REMEDIATING",
})
```
All other states (`IDLE`, `READY_TO_RUN`, `WAITING_REVIEW`, `WAITING_PHASE_GATE`, `BLOCKED`, `WORKER_LOST`, `WORKER_FAILED`, `MONITOR_ERROR`, `UNAVAILABLE`) are ignored.

### 5.2 Lifecycle Override Normalization and Family Fallback
Configuration allows overriding the stall threshold per state:
```python
LIFECYCLE_OVERRIDE_FAMILY = {
    "PLANNING": "PLANNING",
    "REVIEWING_PLAN": "PLANNING",
    "APPLYING_PLAN": "PLANNING",
    "EXECUTING": "EXECUTING",
    "REVIEWING": "REVIEWING",
    "REMEDIATING": "REMEDIATING",
}
```
Resolution order:
1. Exact normalized state in `lifecycle_overrides`.
2. Family root state in `lifecycle_overrides`.
3. Project default `no_progress_threshold_minutes`.
4. Global default (15 minutes).

Duplicate keys that normalize to the same state (e.g. `planning` and `PLANNING`) raise a validation error during config loading.

---

## 6. Durable State Handling and Fail-Closed Matrix

Watchdog coordinator state is stored in `runtime/watchdog.json`.

### 6.1 State Load Matrix
| Condition | Action | Coordinator Mode | Owner Notification |
|---|---|---|---|
| File absent | Initialize empty default state | Normal | None |
| File unreadable / OS error | Copy to `watchdog.json.corrupt-<stamp>`, do NOT overwrite original | `degraded=true` (monitor-only, no attempts, no recovery) | Single `OWNER_GATE` keyed by file hash |
| Invalid JSON / non-object | Quarantine to `watchdog.json.corrupt-<stamp>` | `degraded=true` | Single `OWNER_GATE` keyed by file hash |
| Schema version > 1 | Quarantine to `watchdog.json.corrupt-<stamp>` | `degraded=true` | Single `OWNER_GATE` keyed by file hash |
| Structural corruption in `projects` map | Quarantine to `watchdog.json.corrupt-<stamp>` | `degraded=true` | Single `OWNER_GATE` keyed by file hash |
| Single project row corrupted | Move row to `quarantined_projects[project_id]` | Normal for valid projects; affected project suppressed | Single `OWNER_GATE` for affected project |

Quarantine copies and state files are included in watchdog-owned path definitions and never register as project activity.

---

## 7. Bounded Read-Only Diagnostics and Deadline Fencing

### 7.1 Read-Only Evidence Collection
Diagnostics collect system state across 6 dimensions under an explicit monotonic deadline:
1. **Repository Truth**: Branch, HEAD, dirty status, uncommitted changes via `read_repository_truth`.
2. **Process Liveness**: Verification of recorded worker PID via `is_pid_alive`.
3. **AIBroker Dispatch/Resource State**: Read-only status check via `port.status(request_id)`.
4. **Role Ledgers**: State from `ai-planner.json`, `ai-reviewer.json`, `transition-executor.json`.
5. **Agent Files and Output Tails**: Existence, mtime, and secret-redacted tail lines (bounded to 500 characters).
6. **Watchdog Activity Rollup**: Pre-collected `activity.watchdog_safe` from snapshot.

### 7.2 Deadline-Based Hard Timeout and Late-Result Fencing
Because Python threads cannot be forcibly killed safely:
- Every collector is passed `remaining_budget = deadline - monotonic()`.
- If `remaining_budget <= 0`, collector is skipped with `status = "skipped_deadline"`.
- Coordinator ticks execute `_reap_overdue_attempts()`: any attempt whose `deadline_at` expired is rewritten to `state = "timed_out"`, `diagnosis = "unknown"`, cooldown is armed, and the fence generation is incremented.
- Completed diagnostic threads verify their `fence_token = f"{attempt_key}:{generation}"` under lock. If the token no longer matches or the attempt was reaped, the late result is discarded into `late_discarded_diagnosis` with no milestone emission and no recovery trigger.

### 7.3 Diagnosis Vocabulary
Rule-based deterministic classification yields one of seven codes:
1. `healthy_slow`: Telemetry confirms activity within historical bounds or ETA overrun grace.
2. `agent_stalled`: Process alive, but no file or progress updates within threshold.
3. `process_dead`: Recorded PID is no longer alive, but lifecycle remains in active state.
4. `provider_or_quota_blocked`: Broker reports rate-limit, 429, or quota exhaustion.
5. `state_desync`: Discrepancy between executor state and on-disk agent files.
6. `external_wait`: Worker waiting on external user or bridge input.
7. `unknown`: Fallback code when evidence is incomplete, skipped due to deadline, or ambiguous.

Classification uses NO model inference and consumes zero AIBroker quota.

---

## 8. Two-Phase Safe Recovery Protocol

### 8.1 Safety Boundary and Permissions
Automatic recovery is strictly opt-in (`auto_recovery: true`).
- **Permitted Safe Actions**: Stateless `continue` via `submit_control_command(..., 'continue', command_id=f"wd-{attempt_key}")`.
- **Allowed Diagnoses for Auto-Recovery**:
  - `agent_stalled`
  - `process_dead` (only when `is_pid_alive` confirmed False)
- **Forbidden Actions**: Process killing, Git branch/commit mutations, credential modifications, hardware operations, or scope expansion. All other diagnoses (`state_desync`, `provider_or_quota_blocked`, `external_wait`, `unknown`) require `OWNER_GATE`.

### 8.2 Reserve-Enqueue-Reconcile Protocol
1. **RESERVE**: Durably write recovery record to `attempts[attempt_key].recovery` with `state = "reserved"`, `command_id = f"wd-{attempt_key}"`, and claim the single recovery slot for `run_scope_key`.
2. **ENQUEUE**: Submit control command to `control/inbox/wd-<attempt_key>.json`. Update recovery state to `"requested"`. Emit `RECOVERY_STARTED` milestone.
3. **RECONCILE**: On subsequent ticks, coordinator inspects `control/inbox/wd-<attempt_key>.json` and `control/history/wd-<attempt_key>.json`.
   - Inbox file present -> command still pending.
   - History file present -> command completed (or blocked). Update state to `"completed"` / `"blocked"`.

### 8.3 Crash Point Resolution
| Crash Point | Observed On-Disk State | Reconcile Action |
|---|---|---|
| Crash after RESERVE before ENQUEUE | Record is `reserved`, neither inbox nor history file exists | Promote to `state = "unresolved"`, emit `OWNER_GATE`, do NOT re-enqueue |
| Crash after ENQUEUE before status write | Inbox or history file exists with id `wd-<attempt_key>` | Repair record to `requested` or terminal state matching history |
| Crash during command execution | History file recorded with outcome | Promote to terminal `completed` or `blocked` |

---

## 9. Progress Channel Integration

Watchdog emits notifications tagged with `details.source = "watchdog"` and stable `occurrence_key` strings:
- `STALL_DETECTED`: Normal level. Triggered when `no_progress_seconds >= threshold_seconds`.
- `DIAGNOSTIC_STARTED`: Verbose level. Triggered when diagnostic thread launches.
- `DIAGNOSTIC_RESULT`: Normal level. Triggered upon diagnostic completion.
- `RECOVERY_STARTED`: Normal level. Triggered upon successful recovery enqueue.
- `OWNER_GATE`: Quiet level. Triggered on unrecoverable stalls, failed recoveries, or degraded state.

---

## 10. Operational Surfacing

1. **Mirror File (`<repo>/.devorch/status.json`)**: Contains a `watchdog` section reflecting real-time watchdog status, diagnosis, evidence hash, and attempts.
2. **Web API (`GET /api/watchdog`)**: Read-only JSON endpoint returning `{projects: {...}, degraded: bool}`.
3. **CLI (`dev-orchestrator watchdog-status`)**: Read-only subcommand inspecting runtime watchdog state with optional `--project-id`.
