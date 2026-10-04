# P20.3 Deterministic Recovery & Heterogeneous Escalation Contract

Status: **FROZEN / IMPLEMENTED / TECHNICAL REVIEW ACCEPT (2026-10-04)**

Independent ACCEPT at 15abd1a, 2026-10-04T07:35:13Z. All blocking findings below
are closed; earlier pending labels are historical. Evidence:
docs/evidence/P20_3_COMMAND_OUTCOME_REVIEW.json. Accepted NON_BLOCKING inputs
are carried into docs/P20_4_SOAK_IMPLEMENTATION_NOTES.md. No soak evidence yet.

Latest command-outcome delta: planning recovery dedup is paired with inbox and
history reconciliation. A pending command is an actuated retry, not convergence;
only accepted command evidence settles it. Rejected/unknown outcomes, enqueue
failure, expired unpublished reservations and exhausted budgets gate durably.
Fresh reservations keep their configured lease. Existing owner-decision gates
and evidence are preserved. Legacy prematurely reconciled control bindings are
rechecked against actual outcomes; no replacement effect is launched. Independent
8cbc30df Review confirmed the preceding corrections and returned REMEDIATE for
this bounded delta. 273 tests and 22 subtests pass; current evidence uses
P20_3_COMMAND_*. Acceptance is pending.

Latest local review delta: an already-consumed semantic budget exceeding a
lowered configuration terminates in a stable durable owner gate. Recovery
reservations bind the actual control-command ID before enqueue; restart can
reconcile publication before settlement without abandoning a live effect.
The durable Reviewer runner publishes the actual packet request, and Harness
status correlates Broker-selected reviewer resource with owned spawn identity.
Timeout cleanup uses only those verified bindings and records an owner gate
before any relaunch. Graphify cleanup recognizes the actual read-only command.
265 tests and 22 subtests pass; current evidence is P20_3_FINAL_*.
The independent ac2cf329 Review returned REMEDIATE, confirming the previous
seven fixes and requesting these three local corrections. Its evidence and the
earlier verification below remain historical. Acceptance and soak are pending.

Root bounded remediation supersedes round-2 implementation claims. Default
semantic remediation is cumulative and capped at two across resource recovery;
resource recovery preserves the candidate rather than restarting full planning.
Legacy exhausted gates can add a semantic round only with an explicit finite
increase of the configured semantic budget. Exhaustion invokes an actual
independent read-only adjudicator, requiring exact resolved blocker evidence,
request correlation and a validated additive patch; textual presence is never
an approval. Owner-gate identity is deterministic.

Availability merges authenticated Broker controls/health with configured
resource identity, role and quality. A heterogeneous candidate requires known
previous provider/model and a change of that pair; account rotation is not
heterogeneity. Watchdog resource selection reaches the Planner via a durable
command bound to the recovery reservation. Recovery disable and quarantine
inhibit effects, reservation TTL survives restart, and enqueue failure settles
the actual reserved attempt.

Cleanup requires exact request/resource, age, captured creation identity and
positively identified read-only Graphify argv. P14 heartbeat captures child
creation time at spawn and Reviewer Harness consumes the authenticated job
heartbeat. Unknown provider-native children remain outside cleanup permission.
241 tests plus 22 subtests pass; current evidence and source hashes are in
docs/evidence/P20_3_ROOT_BOUNDED_DELTA_GREEN.txt and MANIFEST.json. This is
deterministic verification, not real AI or six-hour qualification.

Authoritative sources:
- `docs/P20_OWNER_REQUIREMENTS.md` (Section 5)
- `agent/staged/P20.md`
- `agent/next.md`
- `docs/P20_BOOTSTRAP_REVIEW_TIMEOUT_FIX.md`
- `docs/development-workflow.md`

---

## 1. Overview & Frozen Direction

Phase P20.3 converges recovery rules across DevOrchestrator into a pure, bounded, deterministic **Recovery Policy** (`recovery_policy.py`) paired with an in-transaction **Recovery Ledger** (`recovery_ledger.py`), while closing the three confirmed bootstrap recovery gaps identified in `docs/P20_BOOTSTRAP_REVIEW_TIMEOUT_FIX.md`.

The Recovery Policy holds **no authority** and introduces no new state machine. Sole execution authority remains with `TransitionExecutor`, `WatchdogCoordinator`, `PlanCoordinator`, `JobRecovery`, and `OwnerControlStore`. The policy acts strictly as a pure, total, side-effect-free recommender: identical inputs always yield byte-identical decisions. Existing lifecycle coordinators re-enforce owner gates, pause state, clean Git, HEAD identity, and role independence immediately before any effect.

---

## 2. Eight Authorized Recovery Actions & Fail-Closed Ladder

The Recovery Policy outputs exactly one of the eight owner-authorized actions:

```python
RECOVERY_ACTIONS = frozenset({
    "NO_ACTION_ALREADY_RECOVERED",
    "OWNER_GATE",
    "RECONCILE_RUNTIME",
    "RESTART_COMPONENT",
    "HETEROGENEOUS_ESCALATION",
    "SWITCH_RESOURCE",
    "RETRY_CHANGED_STRATEGY",
    "RETRY_SAME_STRATEGY",
})
```

The decision evaluation order in `decide_recovery(inputs, *, now=None)` follows an explicit fail-closed ladder:

1. **`NO_ACTION_ALREADY_RECOVERED`**:
   - Condition: Incident status is `resolved`, or family state is `resolved`/`quarantined`.
   - Effect: No-op; zero new records, zero store revisions.

2. **`OWNER_GATE`**:
   - Condition: Unresolved or contradictory authority, pending owner gate, paused project, stale HEAD, unexpected dirty worktree, conflicting lineage, unknown safety, or exhausted recovery budget (same-strategy >= 1 or total >= 3).
   - Effect: Inhibit all automatic actuation; emit durable actionable owner gate without timestamp refresh.

3. **`RECONCILE_RUNTIME`**:
   - Condition: Ambiguous role liveness (`alive is None`) or an un-reconciled existing effect identity.
   - Effect: Bind to the single existing effect identity via lease/job reconciliation; never launch a parallel Worker or job.

4. **`RESTART_COMPONENT`**:
   - Condition: Component (daemon, harness, background service) is proven dead and possesses a reconcilable single identity. Worker processes are never restarted via this action.

5. **`HETEROGENEOUS_ESCALATION`**:
   - Condition: Semantic, high-risk, or repeated failure signature where unchanged strategy retry is exhausted or disallowed, and an eligible heterogeneous resource is configured.
   - Effect: Escalate to distinct provider/model preserving reviewer independence. If no eligible candidate exists, fail closed to `OWNER_GATE`.

6. **`SWITCH_RESOURCE`**:
   - Condition: Transient resource failure (`provider_quota_exhausted`, `provider_unavailable`, `aibroker_unavailable`, `transport_unavailable`) and a non-excluded resource is available.
   - Effect: Switch to alternate resource within provider/pool boundaries.

7. **`RETRY_CHANGED_STRATEGY`**:
   - Condition: Verifiable repair plan, prompt/context revision, model, or provider change within budget.

8. **`RETRY_SAME_STRATEGY`**:
   - Condition: First transient failure within budget (same-strategy retry <= 1, total recoveries <= 3) with identical strategy key.

---

## 3. Strategy Key Normalization (`resolve_recovery_strategy_key`)

`resolve_recovery_strategy_key(incident, *, role, provider, model, context_revision, repair_plan_id)` computes a normalized strategy identity:

- **Included**: role, failure class, provider, model, context/prompt revision, repair-plan ID.
- **Excluded**: AGY accounts, resource rotation tokens, request IDs, timestamps, and absolute file paths.

**Anti-Loop Invariant**: Account rotation alone produces the identical `strategy_key`. Rotating across AGY accounts consumes the same single-retry budget and cannot circumvent the anti-loop fence.

---

## 4. Clamped Budgets & Configuration

Configured in `src/dev_orchestrator/incidents/policy.py` and registered in `KNOWN_INCIDENT_KEYS`:

| Policy Key | Default | Description |
|---|---|---|
| `max_same_strategy_retries` | 1 | Maximum retries with identical strategy key per incident |
| `max_recoveries_per_incident` | 3 | Maximum total automatic recoveries per incident |
| `max_recovery_attempts_per_family` | 5 | Maximum lifetime attempts across all incidents in a family |
| `recovery_attempt_reservation_ttl_seconds` | 300.0 | TTL for reserved attempts before reconciliation |
| `heterogeneous_required_classes` | `{"reviewer_rejected", "stalled_turn", ...}` | Classes requiring heterogeneous escalation |
| `recovery_enabled` | True | Global gate for automatic recovery actuation |

---

## 5. Durable Recovery Ledger Lifecycle

Implemented in `src/dev_orchestrator/incidents/recovery_ledger.py` with `RECOVERY_ATTEMPT_SCHEMA_VERSION = 1`:

- **Status Set**: `reserved` -> `actuated` -> (`succeeded` | `failed` | `abandoned` | `reconciled` | `gated`).
- **Reservation Discipline**: `reserve_recovery_attempt()` writes a `reserved` record inside `IncidentStore.execute_txn` under the inter-process file lock before any effect is executed. Derives `attempt_id` deterministically from `(project_id, family_id, strategy_key, attempt_index)`. Duplicate reservations return the existing record idempotently.
- **Outcome Settlement**: `record_recovery_outcome()` transitions to terminal status with latency and effect identity. Repeated terminal recordings are no-ops with zero revision increments.
- **Replay Reconciliation**: `reconcile_recovery_attempts()` inspects pending `reserved`/`actuated` records upon daemon restart:
  - Binds to existing effect if verified present (`reconciled`).
  - Marks `abandoned` only upon proven absence.
  - Gates closed (`gated`) with an actionable `OWNER_GATE` on crash ambiguity. Never starts a duplicate Worker or job.

---

## 6. Closure of Three Confirmed Bootstrap Recovery Gaps

### Gap 1: Cumulative Semantic Budget Carry Across Failovers & Cycles
- `AIPlannerCoordinator` carries cumulative remediation counts (`remediation_round`, `cumulative_remediation_rounds`) and the full `rejection_chain` across reviewer resource failovers, recovery cycles, and daemon restarts.
- The recovery cycle update branch no longer resets `remediation_round` to 0.
- `max_plan_remediation_rounds` (default 2) and `hard_total_review_rejects` remain strict bounds.

### Gap 1b: Bounded Read-Only Delta Adjudication
- After the configured remediation rounds (2) are consumed, `AIPlannerCoordinator.adjudicate_plan_delta()` adjudicates only the unresolved blocking delta read-only per `docs/development-workflow.md`.
- Preserves accepted plan content and candidate plan. Cannot modify the repository. Unresolved delta yields a durable actionable `OWNER_GATE`.

### Gap 2: Inconclusive Transport Loss While Broker Runs
- In `AIPlannerCoordinator._run_plan_review`, when `port.status(request_id)` is inconclusive while the Broker still reports running, `_handle_inconclusive_review_transport_loss()` replaces the silent terminal `failed` plan with a durable actionable `OWNER_GATE`.
- Prevents silent stalls in `PENDING_DESIGN` tasks and eliminates `PENDING_DESIGN_NOT_EXECUTING` invariant violations.
- Negative tests verify valid `cancelled` and `no_candidate` statuses do not gate.

### Gap 3: Bounded Role-Tool Child & Reviewer Harness Cleanup
- `cleanup_stale_role_tool_children()` performs bounded cleanup of timed-out read-only reviewer harness sessions and their tool children (e.g. graphify queries), correlated to the exact source request and resource identity.
- Only terminates verified read-only children; never touches daemon, worker, or user processes.
- Worker liveness remains strictly conservative: ambiguous Worker liveness (`alive is None`) never authorizes retry or cleanup.

---

## 7. Reliability Telemetry Integration

`compute_reliability_telemetry` sources recovery metrics primarily from the durable recovery ledger:
- `automatic_recovery_count`: Count of completed recovery attempts from ledger (`identity_key="attempt_id"`).
- `automatic_recovery_success_count`: Count of succeeded attempts.
- `recovery_latency_seconds`: Latencies recorded on settled recovery records.
- Computed coverage replaces placeholder reasons with honest computed values. Unknown coverage remains `None`, never numeric zero.

---

## 8. Control-Plane Impact

### Invariants Preserved
- `CURRENT_TASK_MATCHES_ACTIVE_EXECUTION`: Recovery never dispatches for non-current tasks.
- `TERMINAL_TASK_HAS_NO_RUNNING_EXECUTION`: Resolved incidents yield `NO_ACTION_ALREADY_RECOVERED`.
- `PENDING_DESIGN_NOT_EXECUTING`: Inconclusive transport loss gates rather than leaving unhandled failed plans or orphan workers.
- `SUCCESSOR_HANDOFF_LINEAGE_VALID`: Replay reconciliation rebinds existing handoffs without creating duplicate lineage.
- `NEXT_TASK_WITHOUT_HANDOFF`: Watchdog recovery re-creates exactly one missing handoff under a single durable attempt.
- `SINGLE_ACTIVE_LIFECYCLE_OWNER`: Stale predecessors are fenced; crash-after-intent converges to exactly one owner.

### Transition Boundaries
- `authority_reconciliation`: Ambiguous or contradictory authority fails closed to `OWNER_GATE`.
- `watchdog_recovery`: Automatic planning recovery routes through recovery ledger with bounded retries.
- `owner_gate_transition`: Gates are stable across restarts without timestamp refresh and inhibit all background actuation.
- `successor_handoff`: Handoff recovery deduplicates attempts and rebinds existing handoffs.
- `handoff_publication`: Publication intent replay binds exactly one terminal attempt record.
- `worker_launch`: Un-declared control-plane tasks fail closed to `CONTROL_PLANE_DECLARATION_REQUIRED`.

### Control-Plane Fault Scenarios (CPF-01 through CPF-10)
- **CPF-01**: Stale predecessor Worker fences successor; ambiguous Worker liveness (`alive is None`) returns `RECONCILE_RUNTIME` or `OWNER_GATE`, never retry.
- **CPF-02**: Accepted review decision without handoff rebuilds handoff exactly once with a single durable attempt.
- **CPF-03**: Daemon restart mid-transition replays missing handoff from journal; `reconcile_recovery_attempts` binds the reserved attempt without duplicate handoff.
- **CPF-04**: Contradictory successor claims fail closed to `OWNER_GATE` with zero transition created.
- **CPF-05**: Unexpected dirty worktree defers repair and waits without mutation; advances only once clean.
- **CPF-06**: Repeated daemon ticks over unchanged state create zero duplicate transitions, zero ledger records, and zero store revisions.
- **CPF-07**: Crash after transition intent converges to a single live handoff and single terminal attempt record upon repeated reconciliation.
- **CPF-08**: Missing control-plane declaration fails closed to `CONTROL_PLANE_DECLARATION_REQUIRED`; recovery policy returns `OWNER_GATE`.
- **CPF-09**: Repaired HEAD resolves declaration gate and replays blocked request exactly once.
- **CPF-10**: Same-HEAD retries against declaration gate remain byte-stable with no timestamp refresh in gate or attempt records.

### Convergence Evidence
Durable bounded attempts reconcile exactly one existing effect, unknown safety gates once, independent reviewers remain eligible, and all CPF faults converge without manual continuation.
