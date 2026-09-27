# P17 Phased Migration Gates & Residual Risk Assessment

Status: **FROZEN / ACCEPTED (PHASE W)**  
Task: P17 Single-Authority Goal Convergence Baseline  

Specifies the entry, execution, and exit criteria for Migration Gates M0 through M9.
**P17 strictly implements M0 through M4 and stops before M5.**
M5 through M9 are future, explicit owner-gated migrations that MUST NOT be auto-started.

---

## 1. Gates Executed and Completed in P17 (M0 – M4)

### Gate M0: Freeze Evidence & Pre-Launch Authorization
- **Status**: `PASSED / COMPLETED`
- **Criteria**:
  1. Authoritative owner pause asserted throughout pre-launch verification.
  2. No live Worker, Planner, or Reviewer role running for P17.
  3. Four-field Control-Plane Impact declaration committed to `agent/next.md` and `agent/staged/P17.md`.
  4. Pre-launch gate evidence recorded in `agent/evidence/P17_PRELAUNCH_GATE.json` with `pre_resume_status: PASS`.
  5. Authenticated resume command `p17-g0-final-resume-20260927` settled accepted against exact clean M0 HEAD `fbb30d1...`.
- **Evidence**: `agent/evidence/P17_PRELAUNCH_GATE.json`, `runtime/control/history/p17-g0-final-resume-20260927.json`.

### Gate M1: Pure Single-Authority Convergence Model
- **Status**: `PASSED / COMPLETED`
- **Criteria**:
  1. `WorkRecord` v0 schema defined and validated with strict field validation, canonical sorted JSON, and deterministic SHA-256 digesting.
  2. Pure, deterministic, side-effect-free `decide()` implemented with 12 frozen `DecisionKind` values.
  3. In-memory CAS comparison helper validated.
  4. Sentinels prove zero filesystem, process, network, clock, or RNG access during decision evaluation.
- **Evidence**: `src/dev_orchestrator/convergence/work_record.py`, `evaluator.py`, `tests_py/test_p17_work_record.py`, `tests_py/test_p17_evaluator_purity.py`.

### Gate M2: Historical Replay Validation
- **Status**: `PASSED / COMPLETED`
- **Criteria**:
  1. All 26 canonical historical incident classes represented as schema-valid JSON fixtures in `tests_py/data/p17_corpus/`.
  2. Replay harness executes 26/26 cases with 100% expected decision and invariant agreement.
  3. Exactly zero duplicate executions produced across the entire corpus.
  4. Crash injection between durable write boundaries yields bit-for-bit identical decision trace hashes.
- **Evidence**: `tests_py/test_p17_replay_corpus.py`, `tests_py/test_p17_replay_determinism.py`.

### Gate M3: Read-Only Shadow Live Evaluation
- **Status**: `PASSED / COMPLETED`
- **Criteria**:
  1. `ReadOnlyEvidenceRoot` wraps legacy directories and fails closed on all write or delete attempts.
  2. `validate_shadow_sink` strictly enforces output confinement to `runtime/p17-shadow/` and rejects live/evidence root sinks.
  3. `ShadowEvaluator` projects live legacy state (`.devorch/status.json`, `owner-control.json`) into proposed v0 decisions with source evidence digests.
  4. Invocation verified via CLI: `python -m dev_orchestrator.convergence shadow --evidence-root . --stdout --json`.
- **Evidence**: `src/dev_orchestrator/convergence/shadow.py`, `tests_py/test_p17_shadow_fence.py`.

### Gate M4: Architecture Acceptance
- **Status**: `PASSED / COMPLETED`
- **Criteria**:
  1. All 21 convergence invariants hold and map to production lifecycle invariants and CPF scenarios.
  2. Human-clock interventions eliminated for deterministic transitions (`NO_HUMAN_CLOCK`).
  3. Learned environment constraints (PowerShell 5.1, encoding, etc.) preflighted and recurrence classified as `LEARNING_REGRESSION`.
  4. Target stable/dev root fail-closed resolution and state-root ownership lock validated in isolated fixtures.
  5. Import boundaries enforced: zero production modules import convergence.
- **Evidence**: `tests_py/test_p17_invariants.py`, `test_p17_preflight_constraints.py`, `test_p17_architecture_boundaries.py`, `test_p17_contract_amendments.py`.

---

## 2. **P17 STOPS HERE FOR PRODUCTION AUTHORITY**

P17 introduces **no second production writer** and does **not switch runtime authority**.
Production controllers remain the sole writers.

---

## 3. Future Gated Migrations (Deferred; Explicit Owner Approval Required)

| Gate | Scope & Objective | Prerequisites | Rollback Strategy |
| :---: | :--- | :--- | :--- |
| **M5** | External Canonical State & Config Root Migration Rehearsal | M4 complete. Absolute state root configured in shadow simulation. | Retain in-repo `runtime/` as fallback. |
| **M6** | Single-Project Canary with Legacy Rollback Authority | M5 complete. One test project managed by target single writer with legacy shadow observer. | Fall back to legacy transition executor instantly on invariant failure. |
| **M7** | Dual-Run Comparison with New Authority Writer Disabled by Default | M6 canary successful. Both engines evaluate; compare decisions; legacy writes. | Toggle disable flag. |
| **M8** | Explicit Owner-Approved Authority Switch | M7 zero divergence sustained over N production runs. Explicit owner command. | Retain legacy state sync for 30 days. |
| **M9** | Retirement of Redundant Legacy Budget & Decision Paths | M8 stable in production. Delete `execution_intent` and old prose parsing. | Git tag rollback anchor. |

---

## 4. Residual Risks and Mitigations

1. **Risk: Legacy Prose Parsing Remains Active in Production**  
   - *Mitigation*: P17 classifies prose parsing as `POLICY` (`LCA-04`) and proves the closed-schema model in v0. Production code remains untouched until M9.
2. **Risk: Accidentally Importing Prototype into Production Daemon**  
   - *Mitigation*: Monitored and enforced by AST static analysis in `tests_py/test_p17_architecture_boundaries.py`.
3. **Risk: PowerShell 5.1 Host Operator Incompatibilities**  
   - *Mitigation*: Preserved verified failure memory provenance `seed:p11b:rdc-powershell-5.1` and fingerprint `281bf686902bf4aa1cd966a92cab25c5d1a66bfec453323ed171726580c1b82c`. Replaced all `&&`/`||` with PowerShell-safe sequencing.
