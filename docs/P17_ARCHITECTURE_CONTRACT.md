# P17 Single-Authority Goal Convergence Architecture Contract

Status: **FROZEN / ACCEPTED (PHASE W)**  
Task: P17 Single-Authority Goal Convergence Baseline  
Predecessor: P16.14  
Scope: Pure Prototype, Replay Harness, and Shadow Validation (M0-M4 only)

---

## 1. Architectural Principles and Freeze Decision

### 1.1 No Fourth Lifecycle Authority
P17 strictly introduces **zero** independent runtime or lifecycle authorities.
Existing production components (`TransitionExecutor`, `LifecycleAuthority`, `AIReviewerCoordinator`, `AIPlannerCoordinator`, `ControlCommandCoordinator`, and `WatchdogCoordinator`) remain the **sole production writers** of runtime state.

The convergence architecture prototyped in P17 (`src/dev_orchestrator/convergence/`) is:
- **Pure and side-effect free**: `decide(work_record, evidence, policy, now) -> Decision` reads no files, mutates no variables, spawns no processes, and makes no network or clock calls.
- **Non-authoritative**: it never writes production lifecycle state, ledgers, or command inboxes.
- **Import-isolated**: it is imported by **no production runtime module** (enforced by AST tests in `tests_py/test_p17_architecture_boundaries.py`).
- **Fenced**: shadow evaluation writes strictly to `stdout` or the isolated workspace namespace `runtime/p17-shadow/`.

### 1.2 Target Control Loop
The target autonomous convergence loop replaces multi-party ad-hoc transitions with a single total loop:

$$\text{Goal} \longrightarrow \text{Observe Evidence} \longrightarrow \text{Decide} \longrightarrow \text{Act Once} \longrightarrow \text{Verify} \longrightarrow \begin{cases} \text{Satisfy Goal} \\ \text{Publish Successor} \\ \text{Failover / Retry} \\ \text{Wait Until} \\ \text{Request Human} \\ \text{Write Handoff} \end{cases}$$

All detailed planner, reviewer, provider, watchdog, and process states are treated strictly as **evidence and telemetry**, rather than competing lifecycle authorities.

---

## 2. Canonical Work Record Schema (v0)

The target single-writer Work Record models the complete durable goal state:

| Field Group | Type | Semantic Role |
| :--- | :--- | :--- |
| `schema_version` | `int` | Schema version (currently `1`). |
| `project_id` | `str` | Stable project identifier. |
| `goal_id` | `str` | Stable goal identifier (e.g. `P17`). |
| `goal_revision` | `int` | Monotonic goal specification revision. |
| `goal_spec_digest` | `str` | SHA-256 digest of goal specification content. |
| `predecessor_goal_id`| `str \| None` | Identifier of preceding goal. |
| `repository_identity`| `dict` | `repo_path` and `branch`. |
| `status` | `str` | `OPEN`, `NEEDS_HUMAN`, or `DONE`. |
| `active_lease` | `dict \| None`| `role`, `attempt_id`, `execution_id`, `effect_id`, `host`, `pid`, `epoch`, `heartbeat_at`, `acquired_at`. |
| `current_problem` | `dict \| None`| `problem_id`, `failure_class`, `criterion_or_invariant_id`, `normalized_fingerprint`, `first_seen_at`. |
| `attempts` | `list[dict]` | Historical attempts with strategy, tier, anchors, and typed outcomes. |
| `acceptance` | `dict` | Exactly one of `NONE`, `VERIFIED`, or `OWNER_OVERRIDE`. |
| `wait` | `dict \| None`| Bounded wait: `not_before`, `reset_source`, `reason_code`, `wakeup_obligation_id`. |
| `verification` | `dict \| None`| Verification record with executed checks, criteria results, and structured findings. |
| `successor` | `dict \| None`| `successor_goal_id`, `successor_spec_digest`, `publication_state`, `handoff_idempotency_key`. |
| `human_request` | `dict \| None`| Structured request with stable `question_id`, `question_revision`, typed options, and answer. |
| `handoff` | `dict \| None`| Complete resumable structured HANDOFF checkpoint. |
| `authority_revision` | `str` | Monotonic CAS token (`rev-1`, `rev-2`, ...). |
| `created_at` / `updated_at` | `str` | ISO 8601 UTC timestamps. |

---

## 3. Pure `decide()` Contract

The core decision evaluator signature:

```python
def decide(
    work_record: WorkRecord,
    evidence: EvidenceSnapshot,
    policy: Policy,
    now: str | datetime | None = None,
) -> Decision: ...
```

### 3.1 Decision Kinds (12 Frozen Values)
1. `NOOP_ACTIVE`: Work is currently active and demonstrably live, or goal is terminal with no pending action.
2. `WAIT_UNTIL`: Bounded wait until `not_before` timestamp without consuming retry budgets (e.g. quota reset, emergency pause, dirty worktree).
3. `EXECUTE`: Dispatch initial or unblocked task execution.
4. `VERIFY`: Dispatch independent typed verification after execution completion.
5. `RETRY_SAME_STRATEGY`: Bounded schema or validator repair at the same capability tier (e.g. `OUTPUT_INVALID`).
6. `RETRY_NEW_STRATEGY`: Try a different bounded implementation or test strategy against the same problem.
7. `FAILOVER_RESOURCE`: Fail over to an alternate provider/account at the same capability tier without consuming reasoning budget.
8. `ESCALATE_CAPABILITY`: Escalate requested capability tier after prior strategies/tiers failed to resolve the problem.
9. `REQUEST_HUMAN`: Request human authorization or decision when no safe automated path exists.
10. `WRITE_HANDOFF`: Write a structured, complete resumable HANDOFF upon retry budget exhaustion.
11. `SATISFY_GOAL`: Transition goal status to `DONE` based on verified evidence or valid owner override.
12. `PUBLISH_SUCCESSOR`: Derive deterministic successor handoff and publish next goal intent.

---

## 4. Problem Identity and Failure Classification

### 4.1 Volatile Field Rejection
Problem identity (`problem_id` and `normalized_fingerprint`) remains stable across non-semantic variations. The fingerprint derivation strictly excludes volatile values:
- Repository HEAD changes created while fixing the problem
- Daemon restart / execution epoch identifiers
- Process IDs (PID)
- Timestamps and ages
- Retry and request IDs
- Provider accounts (unless root cause is provider-specific)

### 4.2 Eleven Frozen Failure Classes
1. `OUTPUT_INVALID`: Malformed role output, truncated JSON, or invalid schema.
2. `RESOURCE_TRANSIENT`: Network timeout, transient socket drop, or rate limit.
3. `PROVIDER_UNAVAILABLE`: Model outage, provider 5xx, or authentication token expired.
4. `AUTH_OR_PERMISSION`: Missing OS permissions, credentials, or API keys only a human can supply.
5. `ENVIRONMENT_CONSTRAINT`: OS, tool, or shell incompatibility (e.g. PowerShell 5.1 chaining).
6. `IMPLEMENTATION_DEFECT`: Code syntax, type mismatch, logic error, or unit test failure.
7. `REASONING_OR_STRATEGY_DEFECT`: Planner or worker strategic deadlock or invalid approach.
8. `VERIFICATION_FAILURE`: Independent reviewer rejection or integration check failure.
9. `INTEGRITY_OR_IDENTITY_AMBIGUITY`: Conflicting state files, split-brain authority, or corrupted records.
10. `SAFETY_OR_IRREVERSIBLE_AUTHORIZATION`: High-risk operations requiring explicit human consent.
11. `CONTROL_PLANE_DEFECT`: Recurrence of a learned failure or internal orchestrator defect.

---

## 5. Non-Waivable Safety Obligations

An owner may waive evidence obligations only through a structured `OWNER_OVERRIDE`. However, policy strictly forbids waiving the following core safety invariants:
1. `no_unresolved_blocking_finding`: Cannot advance with an open `BLOCKING` finding.
2. `no_active_execution_lease`: Cannot satisfy or advance a goal while an execution worker is running.
3. `no_unresolved_integrity_ambiguity`: Conflicting authority claims must fail closed.
4. `emergency_brake_enforcement`: Authenticated pause/stop can never be waived or bypassed.
5. `explicit_safety_or_irreversible_action_authorization`: High-risk actions require explicit human authorization.

---

## 6. Legacy vs. v0 Convergence Comparison

| Metric | Legacy Production Behavior (P12-P16.14) | Prototype Convergence v0 (P17) |
| :--- | :--- | :--- |
| **Number of Authorities** | 3 (status.json, transition-executor.json, watchdog.json) | 1 (Canonical Work Record model) |
| **Decision Function** | Distributed across 4 daemon coordinators with stateful ticks | Pure function `decide()` without I/O or state |
| **Severity Parsing** | Free-text regex on strings like `BLOCKING:` | Closed-schema enum `FindingSeverity` (`BLOCKING`, `NON_BLOCKING`, `INFO`) |
| **Budget Model** | Multiple decoupled counters (`execution_intent`, `remediation_rounds`, `watchdog_recovery`) | Single per-problem budget in `ProblemBudget` |
| **Quota Reset** | Spin-retry loop consuming retry attempts | Budget-free `WAIT_UNTIL` with durable wakeup obligation |
| **Human Decision CAS** | Gated on HEAD, lifecycle revision, and projected gate ID | CAS on `(question_id, question_revision)` only |
| **Successor Handoff** | Markdown status projection plus scattered jsonl lines | Derived deterministically from verified acceptance digest |
| **Duplicate Actuation** | Possible during crash between disparate file writes | Zero duplicate execution; crash injection produces equal trace hashes |
| **PowerShell 5.1 Regressions** | Incompatibilities encountered repeatedly in production | Learned constraint preflight rejects/rewrites before execution |
