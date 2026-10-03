# P20.2 Automatic Failure Harvesting & Regression Promotion Contract

Status: **FROZEN / IMPLEMENTED (2026-10-04)**

Authoritative sources:
- `docs/P20_OWNER_REQUIREMENTS.md` (Section 4)
- `agent/staged/P20.md`
- `agent/next.md`
- `docs/P20_BOOTSTRAP_REVIEW_TIMEOUT_FIX.md`
- `docs/development-workflow.md`

---

## 1. Overview & Frozen Direction

Phase P20.2 extends the DevOrchestrator failure incident pipeline with automated harvesting across nine durable lifecycle failure sources and establishes a typed regression candidate verification lifecycle. All operations maintain an observation-only posture over lifecycle authority ledgers, enforce strict write fences preventing any automated modifications to production test sources (`tests_py`), and bind verified observations to the Control-Plane Fault scenarios CPF-01 through CPF-10.

---

## 2. Nine Durable-Source Harvest Detectors

Harvesting reads primary durable ledgers beneath `runtime/` to detect failures, mapping findings into the 19 owner-authorized `FAILURE_CLASSES` without altering semantic signature inputs (preserving byte-stability and legacy family identities).

| Detector Key | Primary Ledger | Emitted Failure Class | Occurrence Key Formula |
|---|---|---|---|
| `planner_failure` | `runtime/ai-planner.json` | `planner_timeout`, `malformed_ai_output` | `plan_fail:{pid}:{plan_id}:{attempt_idx}` |
| `worker_loss` | `runtime/execution-lineage.json` | `worker_process_lost`, `missing_execution_record` | `worker_loss:{pid}:{lineage_key}` |
| `reviewer_failure` | `runtime/ai-reviewer.json` | `reviewer_timeout`, `reviewer_rejected` | `rev_fail:{pid}:{review_id}:{decision_id}` |
| `watchdog_recovery` | `runtime/watchdog.json` | `watchdog_stall`, mapped `FAILURE_CLASSES` | `wd_fail:{pid}:{attempt_key}` |
| `transition_executor_failure` | `runtime/transition-executor.json` | `lost_handoff`, `lifecycle_projection_mismatch`, `duplicate_request` | `trans_fail:{pid}:{handoff_idempotency_key}` |
| `daemon_restart_failure` | `runtime/daemon-boots.jsonl` + `transition-executor.json` | `daemon_restart_during_handoff`, `daemon_restart_during_execution` | `boot_restart:{pid}:{boot_id}:{execution_or_trans_id}` |
| `aibroker_routing_failure` | `runtime/execution-accounting/events.jsonl` | `provider_quota_exhausted`, `provider_unavailable`, `aibroker_unavailable` | `broker_routing:{pid}:{request_id}` |
| `execution_transport_failure` | `runtime/logs/transport-operations.ndjson` | `transport_unavailable` | `transport_op:{pid}:{operation_id}` |
| `durable_job_failure` | `runtime/jobs/index.json` | `transport_unavailable`, `worker_process_lost`, `duplicate_request` | `job_fail:{pid}:{job_id}:{state}` |

### Isolation & Resilience
- Each detector runs inside per-detector exception guarding (`try/except`) so that a malformed or absent ledger in one detector never halts `harvest_tick` or the daemon tick.
- Records without a resolvable `project_id` are safely skipped to preserve multi-project tenant isolation.
- Ambiguous or contradictory records classify as `unknown_requires_owner` with explicit evidence, never inferring success.

---

## 3. Occurrence Identity & Signature Stability

### Occurrence Identity Rules (CPF-06, CPF-09)
Every occurrence key is derived exclusively from durable source-event identities (e.g. `request_id`, `boot_id`, `handoff_idempotency_key`, `attempt_id`). Occurrence keys **never** use wall-clock timestamps or daemon tick counters.
- **CPF-06 (Repeated ticks byte-stable)**: Repeated passes over unchanged ledgers generate identical occurrence keys, which dedup in-memory and in the store transaction, resulting in zero new occurrences and zero new revisions.
- **CPF-09 (Repaired HEAD replay)**: When a blocked request is replayed after gate resolution, the existing occurrence key ensures no duplicate incident or candidate is created.

### Signature Stability (CPF-10)
`taxonomy_class` and `taxonomy_source` are recorded as non-signature fields on packets and families. They are not in `ALLOWED_FINGERPRINT_FIELDS`. Consequently:
- Bootstrap family identities (such as `fam-689a96fe6ea9` with signature `689a96fe6ea97a6a428e20efbf82ccc0`) remain byte-identical.
- Existing P16.10 test suites remain green without signature drift.

---

## 4. RegressionCandidate Contract Schema

Defined in `src/dev_orchestrator/incidents/regression_candidate.py` with `REGRESSION_CANDIDATE_SCHEMA_VERSION = 1`:

```python
@dataclass(frozen=True)
class RegressionCandidate:
    schema_version: int
    candidate_id: str
    project_id: str
    source_family_id: str
    source_failure_signature: str
    source_incident_ids: list[str]
    minimal_reproduction: dict[str, Any]
    expected_invariant: str
    actual_outcome: str
    regression_class: str
    replayability: str
    verification_status: str
    rejection_reason: str | None = None
    verification_evidence: dict[str, Any] | None = None
    verified_at: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
```

### Allowed Status & Replayability Values
- `VERIFICATION_STATUSES = frozenset({'proposed', 'reproducible', 'verified', 'rejected', 'promoted'})`
- `REPLAYABILITY_VALUES = frozenset({'deterministic_replayable', 'non_replayable', 'unknown'})`

---

## 5. Allowlisted ReplayRunner & Tracked Corpus

### ReplayRunner (`REPLAY_OPS`)
Replay operations execute real deterministic production code without running arbitrary model-generated Python, evaluating code strings, or importing dynamically generated modules.
- `fingerprint_stability`: verifies deterministic calculation of `failure_signature`.
- `incident_capture_dedup`: verifies idempotent capture and dedup against an isolated temporary store.
- `store_journal_reconcile`: verifies clean recovery and replay of pending transaction intents.
- `convergence_replay_case`: executes `ReplayHarness.run_case` over pure historical convergence fixtures.
- `convergence_invariants`: validates convergence invariant evaluation against execution state.

### Fixture Security Fences
- Payloads containing code injection tokens (`import `, `os.system`, `exec(`, `eval(`, `__code__`) or non-primitive object graphs immediately raise `ReplayFixtureInvalidError`.
- Unallowlisted operations raise `ReplayOpNotAllowedError`.
- Payloads exceeding `max_replay_fixture_bytes` (clamped up to 1MB, default 64KB) fail closed.

### Tracked Replay Corpus
Corpus test cases reside in `tests_py/data/p20_2_replay_corpus/*.json` with schema:
```json
{
  "case_id": "...",
  "replay_op": "...",
  "source_failure_signature": "...",
  "expected_invariant": "...",
  "expected_defect_outcome": "...",
  "defect_anchor": { ... },
  "corrected_anchor": { ... },
  "regression_class": "...",
  "cpf_scenarios": [ ... ],
  "notes": "..."
}
```

---

## 6. Verification Lifecycle & Rejection Reasons

### Verification Transitions
1. `verify_regression_candidate()` executes the allowlisted op across `defect_anchor` and `corrected_anchor` at least twice.
2. Status transitions:
   - `verified`: Replay is deterministic, defect anchor reproduces `actual_outcome`, and `defect_outcome != corrected_outcome` (non-vacuous discrimination).
   - `reproducible`: Defect anchor reproduces, but full invariant discrimination is pending.
   - `rejected`:
     - `vacuous_oracle_non_discriminating`: Defect and corrected outcomes are identical.
     - `non_deterministic_replay`: Repeated runs of the same anchor yield conflicting outcomes.
     - `defect_anchor_outcome_mismatch`: Defect anchor did not reproduce recorded actual outcome.
     - `vacuous_synthetic_oracle_ineligible`: Legacy forced-failure synthetic oracle.

### Legacy Synthetic Candidate Ineligibility (Step 12)
Candidate `cand-7dd84495b777` (and any candidate based solely on `DEVORCH_FIXTURE_MODE` forced failures) is marked `rejected` with rejection reason `vacuous_synthetic_oracle_ineligible`. Its historical records are preserved, but it is permanently ineligible for promotion.

---

## 7. Runtime-Only Promotion & Path Fence (CPF-05)

### Promotion Write Fence
`promote_verified_regression()` enforces strict path validation:
- Only verified candidates can be promoted; rejected candidates raise `ValueError`.
- Promotion writes exclusively beneath `runtime/incident-packets/`.
- Any destination pointing to `tests_py` or any repository path outside `runtime/incident-packets` is immediately refused with `ValueError`.
- The live repository worktree and test suites are **never** mutated by automated promotion.

---

## 8. Policy Limits, Compaction, and Corruption Fencing (CPF-08)

### Clamped Policy Limits
- `typed_replay_candidates`: `bool` (default `True`)
- `max_regression_candidates`: `int` clamped to `[1, 100000]` (default `1000`)
- `max_replay_corpus_cases`: `int` clamped to `[1, 50000]` (default `500`)
- `max_replay_fixture_bytes`: `int` clamped to `[1024, 1048576]` (default `65536`)
- `max_replay_repeats`: `int` clamped to `[2, 10]` (default `2`)

### Store Compaction
`compact_incident_store()` preserves totals, family identities, verified/rejected status, regression candidate linkages, and occurrence dedup keys, ensuring compaction never allows re-actuation.

### Fail-Closed Corruption Fencing
If an active `corruption-fence.json` is present or a candidate record contains malformed JSON:
- `verify_regression_candidate` and `promote_verified_regression` fail closed by raising `IncidentStoreFencedError` or a validation exception.
- Fences are never automatically cleared by harvesting or promotion code paths.

---

## 9. Control-Plane Fault (CPF) Observation Matrix

| Scenario | Title | Boundary | Observed Source | Computed Observation |
|---|---|---|---|---|
| **CPF-01** | Stale predecessor worker | `authority_reconciliation` | `execution-lineage.json` | Emits `worker_process_lost` / `missing_execution_record`; fences target without opening transition. |
| **CPF-02** | Lost handoff | `watchdog_recovery` | `transition-executor.json` | Emits `lost_handoff`; single-owner recovery converges with no duplicate handoff or family. |
| **CPF-03** | Restart mid-transition | `successor_handoff` | `daemon-boots.jsonl` | Emits `daemon_restart_during_handoff`; transition journal confirms once; occurrence count stays 1. |
| **CPF-04** | Contradictory successor claims | `successor_handoff` | `transition-executor.json` | Emits `unknown_requires_owner` with explicit reason; verification refuses to reach verified. |
| **CPF-05** | Dirty worktree | `successor_handoff` | Promotion write fence | Promotion refuses `tests_py` destination; worktree remains unmutated. |
| **CPF-06** | Repeated ticks byte-stable | `authority_reconciliation` | Store dedup engine | Repeated `harvest_tick` runs add 0 new occurrences or revisions to unchanged ledgers. |
| **CPF-07** | Crash after intent before publication | `handoff_publication` | Store journal | `reconcile_journal()` applies or confirms transaction; no orphan side files. |
| **CPF-08** | Missing declaration / corruption | `worker_launch` | Corruption fence & validators | Active corruption fence or malformed record refuses verification; owner gate is preserved as evidence. |
| **CPF-09** | Repaired-HEAD gate resolution | `special_gate_reconciliation` | Capture dedup | Blocked request replayed after gate resolution yields 0 new occurrences or duplicate candidates. |
| **CPF-10** | Same-HEAD retry byte-stability | `worker_launch` | `failure_signature` | Repeated same-failure runs maintain identical 32-hex signatures and family IDs across restarts. |
