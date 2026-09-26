# Control-Plane Invariant Method Contract

This document specifies the default control-plane development and validation contract established in P16.14, institutionalizing the invariant-driven engineering method proven during P16.13.

## 1. Core Principles

1. **Invariants First**: Lifecycle-affecting tasks must declare the invariants and transition boundaries they affect before implementation begins.
2. **Explicit Transition/Fault Model**: Lifecycle changes must map to registered durable fault scenarios (`CPF-` registry).
3. **Adversarial Verification**: Reviews check invariant preservation, idempotence, restart/replay behavior, and fail-closed boundaries under injected faults, not only happy-path execution.
4. **Autonomous Convergence with Fail-Closed Fences**: Zero-touch convergence on unambiguous evidence; fail-closed `OWNER_GATE` on ambiguous, contradictory, or non-converging authority.
5. **No Parallel Control Plane**: Reuses the authoritative lifecycle, transition journal, centralized invariant evaluator, and zero-touch recovery mechanisms.

---

## 2. Task-Level Classification Rule

A task is classified as **control-plane** only when:
- Its task specification explicitly includes a `## Control-Plane Impact` declaration.
- Its candidate plan or committed design touches protected control-plane runtime surfaces.
- Its technical review diff touches protected runtime surfaces.

**Protected Runtime Surfaces (`CONTROL_PLANE_RUNTIME_SURFACES`)**:
- `src/dev_orchestrator/core/lifecycle_authority.py`
- `src/dev_orchestrator/core/transition_executor.py`
- `src/dev_orchestrator/core/successor_consistency.py`
- `src/dev_orchestrator/core/control_plane_contract.py`
- `src/dev_orchestrator/core/control_plane_faults.py`
- `src/dev_orchestrator/core/watchdog.py`
- `src/dev_orchestrator/core/staged_roadmap.py`
- Core lifecycle symbols: `evaluate_lifecycle_invariants`, `open_declaration_gate`, `resolve_declaration_gate`, `_lifecycle_launch_guard`, `reconcile_successor_handoff`, `reconcile_lifecycle_authority`, `resolve_successor`, `_is_declaration_gate_replayable`.

**Exclusions**:
- Repository identity or project name NEVER classifies a task as control-plane.
- The mere existence of `lifecycle_authority.py` on disk does not classify unrelated work.
- Documentation, UI, provider adapters, and benchmarks are **ordinary** tasks unless they modify protected surfaces.
- An explicit ordinary classification conflicting with protected surface modifications is **invalid** (opt-outs are rejected).

---

## 3. Canonical Declaration Grammar

A control-plane declaration must consist of exactly one markdown section:

```markdown
## Control-Plane Impact
- Invariants: <comma-separated invariant codes>
- Transition boundaries: <comma-separated transition boundaries>
- Fault scenarios: <comma-separated scenario IDs>
- Convergence evidence: <human-readable description of convergence criteria>
```

### Required Invariant Codes
Must be a subset of the six authoritative lifecycle invariants:
1. `CURRENT_TASK_MATCHES_ACTIVE_EXECUTION`
2. `TERMINAL_TASK_HAS_NO_RUNNING_EXECUTION`
3. `PENDING_DESIGN_NOT_EXECUTING`
4. `SUCCESSOR_HANDOFF_LINEAGE_VALID`
5. `NEXT_TASK_WITHOUT_HANDOFF`
6. `SINGLE_ACTIVE_LIFECYCLE_OWNER`

### Transition Boundaries
- `plan_freeze`
- `worker_launch`
- `special_gate_reconciliation`
- `owner_gate_transition`
- `successor_handoff`
- `handoff_publication`
- `authority_reconciliation`
- `watchdog_recovery`

### Validation Rules
- Exactly one `## Control-Plane Impact` section. Multiple sections are `ambiguous`.
- Missing required fields, unknown identifiers, or duplicates fail closed as `invalid`.
- All declared invariants must have their required fault scenarios covered in `Fault scenarios:` and referenced in the candidate plan's validation plan.

---

## 4. Authoritative Gate Transition and Repair Lifecycle

### Refusal Representation
When a control-plane task lacks a valid declaration or scenario coverage at Worker launch:
- `owner_gate` is set to `{code: "CONTROL_PLANE_DECLARATION_REQUIRED", gate_id: "...", resume_state: "...", ...}`.
- `lifecycle_state` is atomically set to `"OWNER_GATE"`.
- Existing `active_owner`, generation, task identity, transition identity, and recovery epoch inputs are unchanged.
- Repeated attempts on the same HEAD are **byte-stable**; no write or timestamp refresh occurs.
- A blocked actuation record is recorded on the execution ledger.

### Replay Eligibility
A launch request blocked by `CONTROL_PLANE_DECLARATION_REQUIRED` is eligible for replay only when:
1. The execution row is explicitly blocked with `blocked_gate_code == "CONTROL_PLANE_DECLARATION_REQUIRED"`.
2. The current authenticated repository HEAD differs from the blocked record's HEAD.
3. The matching declaration gate is currently authoritative on the project.
All other blocked rows and same-HEAD attempts remain non-replayable.

### Launch Ordering & Reconciliation
1. **Repository & Task Authentication**: Verify clean worktree, branch, head, and task identity.
2. **Replay Check**: Check transient remediation block or declaration-gate replay eligibility.
3. **Special Declaration-Gate Reconciliation**:
   - If `CONTROL_PLANE_DECLARATION_REQUIRED` is active: evaluate committed `agent/next.md` at the new HEAD.
   - If valid: resolve gate via `resolve_declaration_gate(authority)`. The gate evidence is archived into bounded `resolved_owner_gates` (max 50, deduplicated by `gate_id`), `owner_gate` is cleared, `lifecycle_state` is restored to `resume_state`, and authority schema lazily upgrades to `schema_version = 2`.
   - If invalid: preserve `OWNER_GATE` and record blocked actuation.
4. **Generic Owner-Gate & PENDING_DESIGN Fences**: All other owner gates remain fenced and cannot be auto-cleared.
5. **Current Declaration Evaluation**: For tasks without an active gate, evaluate committed `agent/next.md`. Refuse to `CONTROL_PLANE_DECLARATION_REQUIRED` if invalid.
6. **Barriers & Worker Launch**: Launch exactly one Worker, preserving prior blocked record in `replayed_from_block`.

---

## 5. Fault Scenario Registry (`CPF-`)

| ID | Origin | Invariants | Boundary | Expectation | Description |
|---|---|---|---|---|---|
| `CPF-01` | P16.13 | CURRENT_TASK_MATCHES_ACTIVE_EXECUTION, SINGLE_ACTIVE_LIFECYCLE_OWNER | authority_reconciliation | fail_closed_gate | Stale predecessor Worker fences published successor |
| `CPF-02` | P16.13 | NEXT_TASK_WITHOUT_HANDOFF | watchdog_recovery | converge_single_owner | Lost handoff recovers once without manual continue |
| `CPF-03` | P16.13 | SUCCESSOR_HANDOFF_LINEAGE_VALID | successor_handoff | replay_journal | Daemon restart mid-transition replays missing handoff from journal |
| `CPF-04` | P16.13 | SUCCESSOR_HANDOFF_LINEAGE_VALID | successor_handoff | fail_closed_gate | Contradictory/ambiguous successor claims fail closed |
| `CPF-05` | P16.13 | SUCCESSOR_HANDOFF_LINEAGE_VALID | successor_handoff | wait_without_mutation | Dirty worktree defers roadmap repair without repository mutation |
| `CPF-06` | P16.13 | SINGLE_ACTIVE_LIFECYCLE_OWNER | authority_reconciliation | byte_stable_idempotent | Repeated daemon ticks do not duplicate transitions or increment generation |
| `CPF-07` | P16.13 | SUCCESSOR_HANDOFF_LINEAGE_VALID, NEXT_TASK_WITHOUT_HANDOFF | handoff_publication | converge_single_owner | Crash after intent before publication recovers to single owner |
| `CPF-08` | P16.14 | PENDING_DESIGN_NOT_EXECUTING, SINGLE_ACTIVE_LIFECYCLE_OWNER | worker_launch | fail_closed_gate | Missing declaration refuses to CONTROL_PLANE_DECLARATION_REQUIRED gate |
| `CPF-09` | P16.14 | CURRENT_TASK_MATCHES_ACTIVE_EXECUTION, SINGLE_ACTIVE_LIFECYCLE_OWNER | special_gate_reconciliation | converge_single_owner | Repaired HEAD resolves gate and replays blocked request |
| `CPF-10` | P16.14 | SINGLE_ACTIVE_LIFECYCLE_OWNER, TERMINAL_TASK_HAS_NO_RUNNING_EXECUTION | worker_launch | byte_stable_idempotent | Same-HEAD launch retries against declaration gate are byte-stable |

Every registered scenario has an executable test reference verified via AST analysis in `validate_fault_registry()`.

---

## 6. Adversarial Review & Scope-Gap Reporting

- **Prompt Injection**: Roles (`planner`, `plan_reviewer`, `worker`, `remediator`, `technical_reviewer`) receive the bounded canonical contract block containing declared invariants, boundaries, required scenario IDs, and the 4-point verification checklist.
- **Review Scope-Gap Check**: At Technical Review, git diff is checked against `CONTROL_PLANE_RUNTIME_SURFACES`. If protected surfaces were modified without a valid declaration, a blocking finding with `rule_id="CONTROL_PLANE_SCOPE_GAP"` is generated, routing the task to remediation.
