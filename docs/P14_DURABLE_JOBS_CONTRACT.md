# P14 Remote Execution Resilience & Recoverable Jobs Contract

## 1. Overview and Purpose

P14 introduces a durable asynchronous job execution runtime beneath DevOrchestrator's execution transport boundary. It enables long-running local, SSH, and remote operations (such as build, test, and validation steps) to execute reliably and survive client, transport, and daemon interruptions without duplicate side effects.

### Core Principles
- **Execution Evidence Only**: DevOrchestrator coordinators retain sole lifecycle authority. The jobs runtime produces and stores durable execution evidence only.
- **Idempotency and Replay Safety**: Initial submissions derive a deterministic `job_id` from the caller's idempotency key. Retries derive a deterministic successor `job_id` from the predecessor `job_id` and a stable, caller-supplied `retry_request_id`.
- **One-Successor-Per-Predecessor Fencing**: Under store locking, a predecessor records its retry successor write-once. Replaying the same `retry_request_id` returns the identical successor without duplicate side effects or supervisor spawns. A differing `retry_request_id` once a successor is claimed fails with a conflict error.
- **Reconcile-Before-Retry**: Ambiguous timeouts and supervisor crashes are always reconciled from durable evidence first before any retry attempt can be claimed or spawned.
- **Fail-Closed Host-Local Allowlist Security**: Executing hosts (local or remote over SSH) resolve runtime roots, project repository roots, and command `argv` solely from host-local trusted configuration (`execution-jobs.json`). No wire-supplied command, script, path, or environment override is executed.
- **Strict Path Containment**: All working directories and job roots are resolved via realpath and must reside within host-locally configured repository roots and runtime roots. Symlinks or traversal (`..`) escaping containment fail closed with zero execution.

---

## 2. State Machine and Legal Transitions

The execution job state machine contains six explicit states:
- `queued`: The job record and spec are durably persisted in the store, awaiting detached supervisor spawn.
- `running`: The supervisor process is active, has claimed ownership with its PID and start token, and is streaming stdout/stderr into bounded NDJSON logs while emitting periodic heartbeats.
- `completed`: The process completed with return code 0 and wrote an immutable, atomic `result.json` record. (Terminal)
- `failed`: The process failed (non-zero exit, timeout, spawn error, or rejected validation) and wrote an immutable `result.json` record. (Terminal)
- `cancelled`: The job was explicitly cancelled by operator command or cancel token. (Terminal)
- `unknown_recovery`: A running supervisor process disappeared without writing `result.json` or terminal evidence, or remote transport is unreachable with ambiguous execution outcome. The outcome cannot be verified from durable evidence. (Non-terminal, non-retryable without intervention)

### Transition Table

| Current State | Permitted Next States | Guard / Condition |
| :--- | :--- | :--- |
| `queued` | `running`, `cancelled`, `failed`, `unknown_recovery` | `running` on supervisor spawn; `cancelled` on cancel before start; `failed` on spawn failure; `unknown_recovery` on transport unreachable / ambiguous dispatch |
| `running` | `completed`, `failed`, `cancelled`, `unknown_recovery` | Terminal states on process termination / timeout / cancellation; `unknown_recovery` on supervisor disappearance without result or transport unreachable |
| `unknown_recovery` | `running`, `completed`, `failed`, `cancelled` | Reconciled if remote process liveness, late terminal evidence, or durable result is subsequently discovered on reconnect |
| `completed` | *(none)* | Write-once terminal state |
| `failed` | *(none)* | Write-once terminal state |
| `cancelled` | *(none)* | Write-once terminal state |

Any transition not explicitly listed in `VALID_TRANSITIONS` raises `JobTransitionError`. Terminal states (`completed`, `failed`, `cancelled`) are immutable.

---

## 3. Identity Derivation and Fencing

### 3.1 Initial Submission Identity
Submission requires a `JobSpec` containing:
- `project_id`: Configured project identifier.
- `command_ref`: Allowlisted command reference name.
- `idempotency_key`: Caller-provided idempotency token.
- `transport`: `"local"` or `"ssh"`.
- `kind`: Phase/category (default `"validation"`).
- Optional correlation fields: `task_id`, `stage_run_id`, `role_run_id`, `source_request_id`, `broker_request_id`, `expected_working_directory`, `input_digest`.

The `spec_hash` is computed as:
```
spec_hash = "sha256:" + sha256(canonical_json(spec))
```
Note: `input_digest` is strictly additive; when omitted or `None`, `canonical_json(spec)` produces byte-identical results to existing specs.

The deterministic `job_id` is derived as:
```
job_id = "job-" + sha256(project_id + ":" + idempotency_key)[:16]
```

### 3.2 Retry Identity and One-Successor Rule
Retrying a job requires:
- `job_id`: Predecessor job identifier.
- `retry_request_id`: Stable caller-provided identifier for the retry request.

The deterministic successor `job_id` is derived as:
```
successor_job_id = "job-" + sha256(predecessor_job_id + ":retry:" + retry_request_id)[:16]
```

Under predecessor store lock:
1. The predecessor job is checked. If it already records `retry.successor_job_id`:
   - If `retry.retry_request_id == retry_request_id`, the identical `successor_job_id` is returned.
   - If `retry.retry_request_id != retry_request_id`, `JobConflictError` is raised.
2. If no successor is recorded, the predecessor must be terminal (`completed`, `failed`, `cancelled`) OR marked `recovery.recovery_safe_retry == True`. Otherwise, retry is rejected.
3. The retry intent is recorded write-once in `predecessor.job.json` before spawning the successor supervisor.
4. If a daemon crash occurs between intent claim and supervisor spawn, startup recovery detects the recorded-but-unspawned intent and re-drives the exact same successor without creating duplicate successors.

---

## 4. Host-Local Trust Boundary and Path Containment

### 4.1 Configuration Source of Truth
Neither command `argv`, repository paths, nor runtime roots are accepted from wire requests. Each executing host loads its configuration strictly from host-local trusted configuration:
- Local daemon: `<runtime_root>/execution-jobs.json`
- Remote SSH helper: `DEVORCH_JOBS_CONFIG` environment variable, falling back to `~/.devorch/execution-jobs.json`.

An absent or disabled configuration file causes all job operations to fail closed with zero execution.

### 4.2 Path Containment and Realpath Validation
Before any command executes:
1. `repo_path` is resolved from the project configuration.
2. `cwd` specified in `command_ref` must be relative. Absolute overrides are rejected.
3. The candidate working directory `candidate = repo_path / cwd` is normalized and canonicalized via `realpath`.
4. Containment check `os.path.commonpath([realpath(repo_path), realpath(candidate)]) == realpath(repo_path)` is enforced. Any path traversal (`..`) or symlink escaping `repo_path` causes immediate fail-closed rejection with `failure_kind="cwd_escapes_repo_containment"`.
5. If the request supplied `expected_working_directory`, it is evaluated solely as an equality assertion against `realpath(candidate)`. Any mismatch fails closed without execution.
6. Execution always executes with `shell=False`.
7. Allowlisted command `argv` may include the `{job_dir}` placeholder, which is securely resolved to the supervisor's canonical job directory, preventing arbitrary wire-supplied arguments.

---

## 5. Supervisor Execution and Process Isolation

Execution is detached from daemon ticks via a dedicated supervisor process:
- Invoked via: `python -m dev_orchestrator.jobs.supervisor --job-dir <dir>`
- Single-instance enforcement: Verifies PID liveness and writes a unique `start_token` into `job.json`.
- Process tree management: The child process is tracked and terminated on timeout or cancellation using OS-specific process tree termination.
- Bounded logs: Child stdout and stderr are multiplexed into `<job_dir>/log.ndjson` with line-length and file-size caps, preserving head and tail lines with truncation markers.
- Heartbeats: Periodically emitted to `<job_dir>/heartbeat.json` with strictly increasing sequence numbers.
- Write-once result: Atomically writes `<job_dir>/result.json` upon child process exit.

---

## 6. Remote SSH Transport and Remote Helper Boundary

Remote execution operates over SSH using `dev_orchestrator.ai.remote_helper`:
- Fixed helper: The remote host executes `python -m dev_orchestrator.ai.remote_helper` over OpenSSH.
- Protocol: JSON lines over stdin/stdout with request-response correlation.
- Closed operations: `job_start`, `job_status`, `job_logs`, `job_cancel`, `job_artifact`.
- No arbitrary commands: The helper resolves commands solely from the remote host's trusted configuration.
- Host identity verification: SSH host keys are verified against configured known hosts.

---

## 7. Accounting and Progress Observability

Execution jobs integrate into DevOrchestrator's accounting and observability systems:
- Accounting intervals: Emits `managed_validation` accounting events keyed by `job_id`.
- Watchdog integration: The progress watchdog inspects job heartbeats and state transitions without polling clock-dependent files.
- Secret redaction: Secrets matching configured patterns are automatically redacted from logs and job records.

---

## 8. Documented Recovery Bounds

1. **Max Runtime Bound**:
   - Every job has an explicit `max_runtime_seconds` (default 300.0s).
   - The supervisor forcibly terminates child processes exceeding this duration.

2. **Heartbeat Freshness Bound**:
   - Heartbeats are emitted at `heartbeat_interval_seconds` (default 5.0s).
   - Reconcile detects stalled jobs if no heartbeat is received within `max(15.0, interval * 3)`.

3. **Ambiguous Crash Bound**:
   - If a supervisor process terminates without writing `result.json`, the job transitions to `unknown_recovery`.
   - Automatic retry is refused until reconciled.

4. **Process Never Started Bound**:
   - If a supervisor PID fails to start or dies before emitting a start token, the job transitions to `failed` with `failure_kind="process_never_started"`.

5. **Retry Intent Crash Bound**:
   - If the daemon crashes after recording retry intent but before spawning the supervisor, startup recovery detects the pending intent and spawns the successor.

6. **Transport Interruption Bound**:
   - When an SSH connection drops or times out during dispatch or status check, the remote host retains its local durable store and detached supervisor execution.
   - The local coordinator reconciles the job to `unknown_recovery` with `failure_kind="transport_unreachable"` and `recovery_safe_retry=False`, strictly refusing retry attempts to prevent duplicate remote execution.
   - On reconnect, status is reconciled against the remote host-local durable store, resuming `running` observation or promoting to terminal completion; no automatic fallback to other transports or providers is attempted.

7. **Orchestration Tick Budget Bound**:
   - Periodic reconciliation during the daemon tick (`JobRecoveryCoordinator.advance()`) is bounded by `per_tick_budget` (default 10 jobs) to prevent daemon tick latency degradation.
   - Startup sweep (`recover()`) processes all jobs once upon daemon launch.

---

## 9. Bounded Retention and Pruning Policy

Job records and logs are managed under a strict retention policy declared in `execution-jobs.json`:

1. **Retention Parameters**:
   - `retention.max_jobs`: Maximum number of terminal jobs retained in the index (default 100).
   - `retention.max_age_days`: Maximum age in days for terminal jobs before pruning (default 7 days).

2. **Terminal-Only Pruning**:
   - Only jobs in terminal states (`completed`, `failed`, `cancelled`) are eligible for pruning.
   - Active and ambiguous jobs (`queued`, `running`, `unknown_recovery`) are strictly protected and NEVER pruned regardless of age or count.

3. **Lineage Protection**:
   - A predecessor job whose successor has not yet been spawned or settled is preserved to maintain retry audit lineage.

4. **Atomic Store Pruning**:
   - Pruning is executed under `InterProcessFileLock(jobs.lock)`.
   - Pruned job directories `<runtime>/jobs/<job_id>` are removed and their entries are pruned from `index.json`.
   - Retention is applied automatically during daemon startup recovery (`recover()`) and periodic tick sweeps (`advance()`).

---

## 10. Immutable Input and Bounded Output Artifacts

Jobs support content-addressed immutable inputs and size-bounded output artifacts:

1. **Content-Addressed Input Manifest**:
   - Callers may supply an input payload (e.g. review request and prepared manifest) upon job submission.
   - Stored in `<job_dir>/input.json` with cryptographic SHA-256 digest recorded in `JobSpec.input_digest` and `JobRecord.input_digest`.
   - When present, `input_digest` participates in `spec_hash` and identity validation. When absent, `spec_hash` remains strictly byte-identical to predecessor job records.
   - Input payloads are preserved byte-identically across retry successors.

2. **Bounded Output Artifacts**:
   - Jobs may write structured output artifacts into `<job_dir>/artifacts/<artifact_name>`.
   - Artifact names are restricted to safe POSIX basenames (`[A-Za-z0-9_.-]+`); directory traversal (`..`), `/`, and `\\` are strictly prohibited.
   - Artifacts enforce maximum byte limits (default 10MB per artifact).
   - Output artifacts are indexed in `JobRecord.artifacts` with their file name, SHA-256 digest, and size in bytes.

3. **Digest-Verified Local and Remote Retrieval**:
   - `JobService.get_artifact(job_id, name)` retrieves output artifacts.
   - For local jobs, the file is read from `<job_dir>/artifacts/<name>` and verified against the recorded SHA-256 digest.
   - For SSH jobs, the artifact is retrieved via `remote_helper` (`operation="job_artifact"`), and its content SHA-256 is verified before being returned to the caller.


---

## 5. Supervisor and Bounded Logs

### 5.1 Detached Supervisor Process
The supervisor runs as a detached process via `spawn_detached` (`DETACHED_PROCESS` + `CREATE_NEW_PROCESS_GROUP` on Windows; new session on POSIX):
- Rechecks `result.json` and PID liveness upon startup; refuses to rerun if another supervisor is active or terminal result exists.
- Records PID and unique `start_token` into `job.json`.
- Streams stdout and stderr through `BoundedNDJSONLog`.
- Emits strictly increasing integer sequence numbers in `heartbeat.json`.
- Enforces `max_runtime_seconds` by terminating the child process tree via `terminate_process_tree` if exceeded.
- Writes immutable `result.json` write-once with exit code and terminal stats upon exit.

### 5.2 Bounded NDJSON Log Policy
Logs are stored at `<runtime>/jobs/<job_id>/log.ndjson`:
- Per-line capping: Lines exceeding `max_line_bytes` (default 4 KB) are truncated with recorded byte truncation.
- Head + Tail retention: When file size exceeds `max_job_bytes` (default 2 MB), the file is compacted to preserve the first `head_lines` (default 1000) and last `tail_lines` (default 1000) separated by a synthetic truncation marker record.
- Torn-tail tolerance: Readers ignore unparseable or incomplete lines at EOF caused by mid-write reading.
- Secret redaction: All read paths (`job-logs` CLI, Control API) apply `dev_orchestrator.control.logs.redact_secrets` before returning log lines.

---

## 6. Recovery and Reconcile Lifecycle

### 6.1 Reconcile Semantics
`reconcile(job_id)` evaluates durable evidence on disk:
- If `result.json` exists: reads exit code and terminal outcome, promoting the job to `completed` or `failed`.
- If supervisor process is alive (`is_pid_alive(pid)` and matching `start_token`): retains `running` state.
- If supervisor process is dead and no `result.json` exists:
  - If process never started (`started_at` is null and `pid` is null/0): marks `failed`, `failure_kind="never_started"`, and `recovery.recovery_safe_retry = True`.
  - If process started mid-run and died: marks `unknown_recovery`, `recovery.recovery_safe_retry = False`. Retries are refused.

### 6.2 Daemon Integration
- Daemon startup: `JobRecoveryCoordinator.recover()` sweeps all jobs, repairs corruptions into quarantine, completes recorded-but-unspawned retry intents, and reconciles active jobs.
- Daemon tick: `JobRecoveryCoordinator.advance()` runs a bounded sweep (default budget 10 jobs) to advance active jobs and retry intents without blocking orchestration ticks.
- Accounting integration: For jobs reaching a terminal state (`completed`, `failed`), `managed_validation` accounting intervals are emitted exactly once, guarded by `accounting_recorded_at`.

---

## 7. HTTP and CLI Surfaces

### Control API (Loopback + Auth)
- `GET /api/v1/control/jobs`: Lists jobs with optional `?project_id=`.
- `GET /api/v1/control/jobs/{job_id}`: Returns full job record.
- `GET /api/v1/control/jobs/{job_id}/logs`: Returns paginated, secret-redacted logs with `cursor` and `limit`.

### CLI Commands
- `dev_orchestrator jobs-list [--project-id <id>]`
- `dev_orchestrator job-status <job_id>`
- `dev_orchestrator job-logs <job_id> [--cursor <c>] [--limit <l>]`
- `dev_orchestrator job-submit --project-id <p> --command-ref <c> --idempotency-key <k> [--transport local|ssh]`
- `dev_orchestrator job-cancel <job_id> [--reason <r>]`
- `dev_orchestrator job-retry <job_id> --retry-request-id <req_id>`
- `dev_orchestrator job-reconcile <job_id>`

---

## 8. Documented Recovery Bounds

The recovery runtime operates within explicit, deterministic bounds to ensure that failures and interruptions cannot cause unboundedly delayed or duplicate work:

1. **Max Runtime and Child Process Bound**:
   - Each command declares `max_runtime_seconds` (default 300.0s).
   - If the child process tree does not exit before `max_runtime_seconds`, the supervisor terminates the entire process tree (`terminate_process_tree`) and writes `result.json` with `failure_kind="timeout"`.

2. **Heartbeat Freshness and Stalled Detection Bound**:
   - The supervisor emits heartbeat records at `heartbeat_interval_seconds` (default 5.0s) with strictly increasing `heartbeat_sequence`.
   - If the supervisor process is alive but `heartbeat_sequence` fails to advance for `max(15.0, heartbeat_interval_seconds * 3)` seconds, `reconcile` detects the job as stalled / PID-reused and transitions it to `unknown_recovery` with `failure_kind="heartbeat_stalled"`.

3. **Ambiguous Crash and Supervisor Disappearance Bound**:
   - If a supervisor process disappears mid-run without writing `result.json`, `reconcile` transitions the job to `unknown_recovery` with `failure_kind="supervisor_died_without_result"`.
   - `recovery_safe_retry` is set to `False`; automatic retry is strictly prohibited and requires manual intervention.

4. **Process Never Started Bound**:
   - If a job is queued or the supervisor fails before child process launch (`started_at` is null and PID is null/0), `reconcile` transitions the job to `failed` with `failure_kind="never_started"` and `recovery_safe_retry=True`, permitting deterministic retry.

5. **Retry Intent Crash Bound**:
   - If the daemon crashes between recording retry intent (`claim_retry`) and spawning the successor supervisor, daemon startup recovery (`JobRecoveryCoordinator.recover()`) detects the recorded-but-unspawned successor intent and re-drives the exact same successor `job_id` without creating duplicate successor records.

6. **Transport Interruption Bound**:
   - When an SSH connection drops or times out during dispatch or status check, the remote host retains its local durable store and detached supervisor execution.
   - The local coordinator reconciles the job to `unknown_recovery` with `failure_kind="transport_unreachable"` and `recovery_safe_retry=False`, strictly refusing retry attempts to prevent duplicate remote execution.
   - On reconnect, status is reconciled against the remote host-local durable store, resuming `running` observation or promoting to terminal completion; no automatic fallback to other transports or providers is attempted.

7. **Orchestration Tick Budget Bound**:
   - Periodic reconciliation during the daemon tick (`JobRecoveryCoordinator.advance()`) is bounded by `per_tick_budget` (default 10 jobs) to prevent daemon tick latency degradation.
   - Startup sweep (`recover()`) processes all jobs once upon daemon launch.

---

## 9. Bounded Retention and Pruning Policy

Job records and logs are managed under a strict retention policy declared in `execution-jobs.json`:

1. **Retention Parameters**:
   - `retention.max_jobs`: Maximum number of terminal jobs retained in the index (default 100).
   - `retention.max_age_days`: Maximum age in days for terminal jobs before pruning (default 7 days).

2. **Terminal-Only Pruning**:
   - Only jobs in terminal states (`completed`, `failed`, `cancelled`) are eligible for pruning.
   - Active and ambiguous jobs (`queued`, `running`, `unknown_recovery`) are strictly protected and NEVER pruned regardless of age or count.

3. **Lineage Protection**:
   - A predecessor job whose successor has not yet been spawned or settled is preserved to maintain retry audit lineage.

4. **Atomic Store Pruning**:
   - Pruning is executed under `InterProcessFileLock(jobs.lock)`.
   - Pruned job directories `<runtime>/jobs/<job_id>` are removed and their entries are pruned from `index.json`.
   - Retention is applied automatically during daemon startup recovery (`recover()`) and periodic tick sweeps (`advance()`).
