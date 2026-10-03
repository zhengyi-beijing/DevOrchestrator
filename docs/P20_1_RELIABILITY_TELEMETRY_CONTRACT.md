# P20.1 Reliability Telemetry & Failure Taxonomy Contract

Status: **FROZEN / IMPLEMENTED (2026-10-04)**

Authoritative sources:
- `docs/P20_OWNER_REQUIREMENTS.md` (Sections 3, 11-16)
- `agent/staged/P20.md`
- `agent/next.md`

---

## 1. Incident Schema & Contract

The canonical incident contract is defined in `src/dev_orchestrator/incidents/contract.py` with `INCIDENT_SCHEMA_VERSION = 1`.
Every failure incident must satisfy the `FailureIncident` contract:

| Field | Type | Description |
|---|---|---|
| `schema_version` | `int` | Version identifier (currently 1). |
| `incident_id` | `str` | Unique incident identifier (e.g., `inc-<uuid>`). |
| `project_id` | `str` | Project identifier (e.g., `devorchestrator`). |
| `task_id` | `str \| None` | Bounded roadmap task identifier (e.g., `P20.1`). |
| `execution_id` | `str \| None` | Active execution request identifier if applicable. |
| `plan_id` | `str \| None` | Associated plan identifier if applicable. |
| `review_id` | `str \| None` | Associated review decision identifier if applicable. |
| `lifecycle_generation` | `int \| None` | Authoritative lifecycle generation. |
| `lifecycle_transition_id` | `str \| None` | Lifecycle transition identifier. |
| `recovery_epoch_id` | `str \| None` | Recovery epoch identifier. |
| `failure_signature` | `str` | Normalized, volatile-free 32-hex SHA-256 hash. |
| `failure_family` | `str` | Durable family identity derived from `failure_signature`. |
| `failure_class` | `str` | One of the 19 owner-authorized failure classes. |
| `stage` | `str \| None` | Lifecycle stage (`planning`, `worker`, `review`, etc.). |
| `role` | `str \| None` | Active role (`planner`, `worker`, `reviewer`, `system`). |
| `provider` | `str \| None` | AI provider (excluded from signature). |
| `model` | `str \| None` | Model name (excluded from signature). |
| `resource` | `str \| None` | Routed resource identifier (excluded from signature). |
| `account` | `str \| None` | Routed account identifier (excluded from signature). |
| `first_seen_at` | `str` | ISO 8601 UTC timestamp of first occurrence. |
| `last_seen_at` | `str` | ISO 8601 UTC timestamp of most recent occurrence. |
| `occurrence_count` | `int` | Number of distinct occurrences of this family. |
| `evidence` | `dict \| None` | Bounded, sanitized diagnostic evidence. |
| `root_cause_hint` | `str \| None` | Diagnostic hint or error message snippet. |
| `previous_attempts` | `list \| None` | Prior attempt records. |
| `recovery_strategy` | `str \| None` | Chosen recovery strategy. |
| `recovery_attempt` | `int \| None` | Monotonic recovery attempt counter. |
| `recovery_result` | `str \| None` | Outcome of recovery (`accepted`, `rejected`, `failed`, `pending`). |
| `recovery_latency` | `float \| None` | Latency in seconds for recovery. |
| `terminal_disposition` | `str \| None` | Eventual terminal disposition. |
| `owner_gate_id` | `str \| None` | Gated owner gate identity if applicable. |
| `regression_candidate_id` | `str \| None` | Associated regression candidate identity. |
| `related_incident_ids` | `list[str] \| None` | Cross-referenced incident IDs. |

### Nullable Evidence Invariant
Nullable evidence stays `None` and is **never** inferred as a successful result.

---

## 2. 19-Class Normalized Failure Taxonomy

The 19 owner-authorized failure classes:

1. `malformed_ai_output`: Unparseable or schema-invalid model generation.
2. `planner_timeout`: Planner execution exceeded deadline.
3. `worker_process_lost`: Worker OS process disappeared unexpectedly.
4. `reviewer_rejected`: Technical or Plan Reviewer returned CHANGES_REQUIRED.
5. `reviewer_timeout`: Reviewer execution exceeded deadline.
6. `provider_quota_exhausted`: Upstream API quota/credit exhausted (HTTP 429).
7. `provider_unavailable`: Upstream AI provider outage (HTTP 500/502/503/504).
8. `aibroker_unavailable`: Loopback AIBroker listener unreachable.
9. `daemon_restart_during_execution`: Daemon restarted while Worker was executing.
10. `daemon_restart_during_handoff`: Daemon restarted across successor handoff boundary.
11. `stale_head`: Git HEAD advanced away from reviewed/verified anchor.
12. `unexpected_dirty_worktree`: Working tree has unexpected modifications.
13. `missing_execution_record`: Execution state missing from transition ledger.
14. `lost_handoff`: Accepted NEXT_TASK review verdict missing durable handoff.
15. `duplicate_request`: Duplicate execution request received.
16. `lifecycle_projection_mismatch`: UI/monitor projection conflicts with authority.
17. `retry_without_progress`: Same failure repeated without changing strategy.
18. `transport_unavailable`: Loopback control or execution transport unreachable.
19. `unknown_requires_owner`: Unclassifiable defect requiring human owner gate.

---

## 3. Failure Signature & Stability (CPF-10)

`failure_signature` is implemented in `src/dev_orchestrator/incidents/fingerprint.py`.

- **Allowlisted inputs**: Only normalized semantic facts (`failure_class`, `stage`, `role`, `lifecycle_class`, `contract_class`, `invariant_identifier`, `diagnosis_code`, `blocker_code`, `git_anchor_relation`, `continuation_relation`, `error_type`, `target_phase`).
- **Excluded inputs**: Random IDs (`uuid`, `incident_id`, `execution_id`), timestamps (`first_seen_at`, `last_seen_at`, `timestamp`), absolute paths, full prompts, environment dumps, `provider`, `model`, `resource`, `account`.
- **Strategy changes**: Changing model, account, or provider constitutes a strategy variation (input to P20.3), not a new failure family.
- **Byte-stability**: Same-HEAD launch retries against an un-repaired gate produce byte-identical failure signatures across processes and restarts with no timestamp refresh.

---

## 4. Store Durability, In-Transaction Dedup & Persistent Corruption Fencing

`IncidentStore` is hardened in `src/dev_orchestrator/incidents/store.py`:

- **In-Transaction Dedup**: Dedup decisions are made inside `store.execute_txn` while holding `InterProcessFileLock(incidents.lock)`. Concurrent captures from multiple processes of the same `(failure_signature, occurrence_key)` yield exactly one occurrence and one packet.
- **Persistent Corruption Fence**:
  - When `index.json` is corrupt or has an unsupported future schema, `_quarantine_index` isolates it as `index.corrupt.<stamp>-<hash>` and writes `runtime/incident-packets/corruption-fence.json`.
  - Every subsequent `IncidentStore` instance consults `corruption-fence.json`. Absence of `index.json` after quarantine is **not** treated as a healthy blank index; the store remains degraded and fenced.
  - While fenced, any mutating operation raises `IncidentStoreFencedError`.
  - **No Programmatic Clearing API**: P20.1 deliberately provides no code path to clear the fence automatically.
  - **Manual Owner Resolution Procedure**:
    1. Operator inspects quarantined artifacts in `runtime/incident-packets/index.corrupt.*`.
    2. Operator verifies whether data can be recovered or reconstructed.
    3. Operator manually removes or renames `corruption-fence.json`.
    4. Operator re-launches or restarts the store/daemon.
- **Bounded Capacity & Typed Backpressure**:
  - Configurable limits: `max_families` (default 1000), `max_occurrences_per_family` (default 5000), `max_total_occurrences` (default 50000), `max_journal_intents` (default 100).
  - Exceeding capacity raises typed `IncidentCapacityExceededError` instead of dropping data silently.
  - `compact_incident_store`: Compacts evidence references, preserves totals (`recurrence_count`, `dropped_evidence_count`), preserves family identities and all `occurrences` dedup keys.

---

## 5. Ingress Secret Sanitization

`src/dev_orchestrator/incidents/sanitize.py` enforces sanitization before ANY bytes reach packets, index, journal, or candidates:
- `Authorization` and `Bearer <token>` headers are redacted.
- API keys (`sk-...`, `api_key=...`, `key=...`), passwords, secrets, private keys, and PEM blocks are replaced with `[REDACTED]`.
- Embedded assignments like `KEY=value` in error messages are sanitized.
- Full prompts are bounded to at most 1024 characters with `...[TRUNCATED]`.
- Environment dumps redact sensitive variable values.

---

## 6. Run-Binding Rule & Authority

Reliability runs are registered in `runtime/reliability/runs.jsonl`:
- **Explicit lifecycle**: Opened via `open_reliability_run` and closed via `close_reliability_run`. Never implicitly opened by daemon ticks.
- **Membership Rule**: A source event belongs to run $R$ if and only if:
  1. `project_id == R.project_id`.
  2. The recorded timestamp is in $[R.opened\_at, R.closed\_at \text{ or now})$.
  3. Any `run_id` or `boot_id` carried by the ledger matches $R$.
- **Read-Only Telemetry Posture**: Telemetry functions read ledgers and never write to transition, owner, execution, or lifecycle files.

---

## 7. 16 Run-Scoped Telemetry Metrics Source Map

Every metric has exactly one primary ledger; cross-ledger corroboration never adds to the count:

| # | Metric Name | Primary Ledger | Identity Key | Dedup Rule | Coverage |
|---|---|---|---|---|---|
| 1 | `manual_continue_count` | `runtime/control/audit.jsonl` | `command_id` | Distinct accepted `continue` commands from owner sources (sources starting with `automatic_` are excluded). | `full` / `unknown` |
| 2 | `owner_gate_count` | `runtime/execution-accounting/events.jsonl` | `gate_id` | Distinct `owner_gate_opened` events; reconciled with control surface by `gate_id`. | `full` / `unknown` |
| 3 | `duplicate_execution_count` | `runtime/transition-executor.json` | `(project_id, source_request_id, task_id)` | More than one active execution identity per logical request. | `full` |
| 4 | `duplicate_handoff_count` | `runtime/transition-executor.json` | `idempotency_key` | More than one consumption of a single handoff idempotency key. | `full` |
| 5 | `duplicate_successor_activation_count` | `runtime/transition-executor.json` | `transition_id` | Published transitions per `(source_task_id, target_task_id, generation)`. | `full` |
| 6 | `duplicate_job_count` | `runtime/jobs/index.json` | `(idempotency_key, job_id)` | Multiple jobs sharing identical idempotency key. | `full` / `unknown` |
| 7 | `failure_incident_count` | `runtime/incident-packets/index.json` | `(failure_signature, occurrence_key)` | Total distinct occurrences across families in window. | `full` |
| 8 | `repeated_failure_family_count` | `runtime/incident-packets/index.json` | `failure_signature` | Families with $> 1$ distinct occurrence key in window. | `full` |
| 9 | `recovery_latency_seconds` | `runtime/incident-packets/index.json` | `(incident_id, recovery_attempt)` | Series of measured recovery latencies in seconds. | `partial` |
| 10 | `recovery_latency_sample_count` | `runtime/incident-packets/index.json` | `(incident_id, recovery_attempt)` | Sample count of measured recovery latencies. | `full` |
| 11 | `automatic_recovery_count` | `runtime/execution-accounting/events.jsonl` | `attempt_id` | Accounting attempt outcomes with recovery attempts. | `partial` |
| 12 | `automatic_recovery_success_count` | `runtime/execution-accounting/events.jsonl` | `attempt_id` | Recovery attempts reaching accepted outcome; waits excluded. | `partial` |
| 13 | `task_completion_count` | `runtime/transition-executor.json` | `(project_id, task_id, generation)` | Settled executions with outcome `task_complete`. | `full` |
| 14 | `review_acceptance_count` | `runtime/execution-accounting/events.jsonl` | `attempt_id` | Reviewer outcomes with role in `{plan_reviewer, reviewer}` and outcome `accepted`. | `full` / `unknown` |
| 15 | `daemon_restart_count` | `runtime/daemon-boots.jsonl` | `boot_id` | Boots strictly after the initial boot within run window. | `full` / `unknown` |
| 16 | `lifecycle_invariant_escape_count` | `runtime/transition-executor.json` | `(invariant_code, transition_id)` | Durable evidence that an invalid state reached actuation or authority. | `full` |

### Invariant Escape Definition
An invariant escape occurs only when an invalid or contradictory state actually reaches actuation, changes authority, or leaks into an unauthorized transition before detection.
- A launch fenced before side-effects is **not** an escape.
- An ambiguity that fails closed and refuses transition is **not** an escape.
- Missing evidence yields `unknown` coverage rather than numeric zero.

---

## 8. CPF Observation Matrix (CPF-01 through CPF-10)

| Scenario | Title | Reliability Metric Observed | Primary Identity | Expected Value / Behavior |
|---|---|---|---|---|
| **CPF-01** | Stale predecessor Worker runs after successor publication | `duplicate_execution_count` | `(project_id, source_request_id, task_id)` | `0`; target fences; incidents package writes no lifecycle state. |
| **CPF-02** | Lost NEXT_TASK handoff rebuilt | `duplicate_handoff_count` | `idempotency_key` | `0`; rebuilt handoff counted once by `idempotency_key`; no escape. |
| **CPF-03** | Daemon restart mid-transition replays handoff | `daemon_restart_count` | `boot_id` | `1` per new boot; replay preserves counters and identities. |
| **CPF-04** | Contradictory or ambiguous successor claims fail closed | `lifecycle_invariant_escape_count` | `(invariant_code, transition_id)` | `0`; ambiguity gates without transition; no silent advance. |
| **CPF-05** | Dirty worktree defers roadmap repair without repo mutation | `automatic_recovery_success_count` | `attempt_id` | `0`; wait is not progress; no uncommitted repo mutations. |
| **CPF-06** | Repeated daemon ticks do not duplicate transitions | `duplicate_successor_activation_count` | `transition_id` | `0`; occurrence_key is durable source event, not tick time. |
| **CPF-07** | Crash after intent before publication | `duplicate_execution_count` | `execution_id` | `0`; committed intent applied or confirmed once; single owner. |
| **CPF-08** | Control-plane task missing declaration fails closed | `owner_gate_count` | `gate_id` | `1`; gate reconciled between accounting and control; never auto-cleared. |
| **CPF-09** | Repaired HEAD resolves declaration gate & replays request | `manual_continue_count` | `command_id` | `0`; automatic replay excluded from manual continue count. |
| **CPF-10** | Same-HEAD launch retries are byte-stable | `failure_incident_count` | `failure_signature` | `1`; byte-identical signature with no timestamp refresh. |
