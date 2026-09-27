# P17 Legacy Contract Amendments & Classification Matrix

Status: **FROZEN / ACCEPTED (PHASE W)**  
Task: P17 Single-Authority Goal Convergence Baseline  

Classifies every conflicting assertion between legacy orchestration contracts/tests
and the target single-authority convergence architecture as either:
- **`SAFETY`**: Enduring safety invariant that MUST remain enforced across all versions; or
- **`POLICY`**: Legacy orchestration policy that may be amended or retired at a future migration gate (M5–M9), but is preserved untouched in production during P17.

---

## 1. Legacy Contract Amendments Matrix

| ID | Origin Document / Test Suite | Legacy Assertion / Rule | Classification | Target Convergence Disposition | Named Covering Test(s) |
| :---: | :--- | :--- | :---: | :--- | :--- |
| **LCA-01** | `TRANSITION_EXECUTOR_CONTRACT.md` | Manual `continue` command required to advance after review remediation budget exhaustion. | `POLICY` | Replaced by pure `is_goal_satisfied` and deterministic `WRITE_HANDOFF` / escalation; manual continue is never required as a progress clock (`NO_HUMAN_CLOCK`). Preserved in legacy production code until M9. | `tests_py/test_p1614_invariant_workflow.py::ControlPlaneGateLifecycleTests::test_repaired_head_reconciles_and_replays_blocked_request`<br>`tests_py/test_p17_retry_wait_escalation.py::TestP17RetryWaitEscalation::test_total_exhaustion_writes_handoff` |
| **LCA-02** | `TRANSITION_EXECUTOR_CONTRACT.md` | Fail-closed launch fencing on task mismatch, unannounced control-plane launches, and unconsumed predecessor ownership. | `SAFETY` | Permanent safety invariant (`SINGLE_ACTIVE_LEASE`, `CURRENT_TASK_MATCHES_ACTIVE_EXECUTION`). Must never be relaxed or bypassed. | `tests_py/test_p1614_invariant_workflow.py::ControlPlaneGateLifecycleTests::test_unannounced_control_plane_launch_refuses_to_owner_gate`<br>`tests_py/test_p17_control_plane_declaration.py::TestP17ControlPlaneDeclaration::test_evaluate_launch_declaration_at_current_head` |
| **LCA-03** | `docs/development-workflow.md` | Bounded plan review: at most two remediation rounds before opening `OWNER_GATE`. | `POLICY` | Local orchestration policy. Target model formalizes per-problem budgets in `ProblemBudget` (`max_strategy_attempts`). Preserved in production until M9. | `tests_py/test_workflow_policy.py`<br>`tests_py/test_p17_problem_identity.py::TestP17ProblemIdentity::test_problem_tracker_exhaustion` |
| **LCA-04** | `tests_py/test_ai_reviewer.py`<br>`tests_py/test_p1614_invariant_workflow.py` | Finding severity determined by parsing free-text prose for `"BLOCKING:"` substring. | `POLICY` | Replaced by closed-schema `FindingSeverity` enum (`BLOCKING`, `NON_BLOCKING`, `INFO`) and `OutputInvalidError`. Legacy prose parsing remains untouched in `ai_reviewer.py` during P17. | `tests_py/test_p17_findings_and_output_invalid.py::TestP17FindingsAndOutputInvalid::test_parse_valid_structured_findings`<br>`tests_py/test_p17_acceptance_model.py::TestP17AcceptanceModel::test_verified_acceptance_with_unresolved_blocking_finding_fails` |
| **LCA-05** | `src/dev_orchestrator/control/store.py`<br>`tests_py/test_p12_control_foundation.py` | Emergency pause/stop interlock must be checked before every side effect and cannot be bypassed by CAS drift. | `SAFETY` | Permanent out-of-band safety invariant (`EMERGENCY_BRAKE`). Overrides all autonomous actions. Re-verified before every actuator effect. | `tests_py/test_p17_human_and_emergency.py::TestP17HumanAndEmergency::test_emergency_pause_overrides_all_decisions`<br>`tests_py/test_p17_invariants.py::TestP17Invariants::test_pure_evaluator_evaluates_all_invariants` |
| **LCA-06** | `src/dev_orchestrator/core/control_plane_contract.py` | Control-plane launch requires byte-identical four-field declaration at exact committed Git HEAD. | `SAFETY` | Permanent control-plane invariant (`CPF-08`, `CPF-09`, `CPF-10`). Preserved and enforced. | `tests_py/test_p17_control_plane_declaration.py::TestP17ControlPlaneDeclaration::test_p17_declaration_grammar_in_task_files` |
| **LCA-07** | `src/dev_orchestrator/control/coordinator.py` | Human decision CAS requires matching project HEAD, lifecycle revision, and projected gate ID. | `POLICY` | Fragile legacy CAS that frequently stalled on harmless background commits. Target model CASes strictly on `(question_id, question_revision)`. Production preserves legacy CAS until M8. | `tests_py/test_p17_human_and_emergency.py::TestP17HumanAndEmergency::test_human_request_creation_and_cas_answer` |

---

## 2. Inventory of Duplicated Budget Paths (Retirement at M9)

The following duplicated decision/budget mechanisms exist in legacy production code.
**P17 does NOT delete or modify them in production.** They are scheduled for retirement at future migration gate M9:

1. **`execution_intent` budgets**:
   - Location: `src/dev_orchestrator/core/transition_executor.py`
   - Role: Tracks attempt budgets per execution intent.
   - Future target replacement: `WorkRecord.attempts` and `ProblemBudget.max_total_attempts`.
   - P17 disposition: `RETIRE_AT_M9` (active in production, untouched).

2. **`reviewer_remediation_extension` budgets**:
   - Location: `src/dev_orchestrator/core/ai_reviewer.py`
   - Role: Tracks remediation rounds for reviewer findings.
   - Future target replacement: `ProblemBudget.max_strategy_attempts`.
   - P17 disposition: `RETIRE_AT_M9` (active in production, untouched).

3. **`watchdog_lifecycle_recovery_attempts`**:
   - Location: `src/dev_orchestrator/core/watchdog.py`
   - Role: Tracks recovery loop attempts per watchdog attempt key.
   - Future target replacement: `ProblemBudget.max_resource_attempts`.
   - P17 disposition: `RETIRE_AT_M9` (active in production, untouched).

4. **`resolve_progress_obligation_escalation_tables`**:
   - Location: `src/dev_orchestrator/incidents/obligations.py`
   - Role: Maps textual lifecycle states to obligation escalation codes.
   - Future target replacement: `ConvergenceEvaluator.decide()` and `PROGRESS_TOTALITY`.
   - P17 disposition: `RETIRE_AT_M9` (active in production, untouched).
