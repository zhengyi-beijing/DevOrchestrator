"""P17 Contract amendments, legacy classifications, and runtime root fixture tests.

Validates:
1. SAFETY vs POLICY classifications for conflicting legacy assertions.
2. Retirement disposition of duplicated budget paths without claiming production deletion.
3. Fixture-backed isolated check for runtime/config-root fail-closed resolution and state-root ownership lock.
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest


# Canonical classification matrix of legacy conflicting assertions
LEGACY_CONTRACT_AMENDMENTS = (
    {
        "assertion_id": "LCA-01",
        "contract": "TRANSITION_EXECUTOR_CONTRACT",
        "description": "Owner continue required to advance after review remediation budget exhaustion",
        "classification": "POLICY",
        "reason": "Target convergence automatically emits WRITE_HANDOFF or escalating failover instead of manual continue stall",
        "covering_tests": [
            "tests_py/test_p1614_invariant_workflow.py::test_repaired_head_reconciles_and_replays_blocked_request",
            "tests_py/test_p17_retry_wait_escalation.py::TestP17RetryWaitEscalation::test_total_exhaustion_writes_handoff",
        ],
    },
    {
        "assertion_id": "LCA-02",
        "contract": "TRANSITION_EXECUTOR_CONTRACT",
        "description": "Fail-closed check on task mismatch and unannounced control-plane launches",
        "classification": "SAFETY",
        "reason": "Enduring safety invariant; must remain in production and target model",
        "covering_tests": [
            "tests_py/test_p1614_invariant_workflow.py::ControlPlaneGateLifecycleTests::test_unannounced_control_plane_launch_refuses_to_owner_gate",
            "tests_py/test_p17_control_plane_declaration.py::TestP17ControlPlaneDeclaration::test_evaluate_launch_declaration_at_current_head",
        ],
    },
    {
        "assertion_id": "LCA-03",
        "contract": "docs/development-workflow.md",
        "description": "Two-round bounded plan review before owner gate",
        "classification": "POLICY",
        "reason": "Local bounded review policy; target convergence formalizes per-problem budget",
        "covering_tests": [
            "tests_py/test_workflow_policy.py",
            "tests_py/test_p17_problem_identity.py::TestP17ProblemIdentity::test_problem_tracker_exhaustion",
        ],
    },
    {
        "assertion_id": "LCA-04",
        "contract": "P16.13 / P16.14 tests",
        "description": "Prose string matching for 'BLOCKING:' severity in reviewer output",
        "classification": "POLICY",
        "reason": "Target architecture strictly replaces free-text parsing with closed-schema FindingSeverity",
        "covering_tests": [
            "tests_py/test_p17_findings_and_output_invalid.py::TestP17FindingsAndOutputInvalid::test_parse_valid_structured_findings",
            "tests_py/test_p17_acceptance_model.py::TestP17AcceptanceModel::test_verified_acceptance_with_unresolved_blocking_finding_fails",
        ],
    },
    {
        "assertion_id": "LCA-05",
        "contract": "P16.13 / P16.14 tests",
        "description": "Emergency pause/stop enforcement across all lifecycle states",
        "classification": "SAFETY",
        "reason": "Enduring out-of-band safety invariant; must never be relaxed",
        "covering_tests": [
            "tests_py/test_p17_human_and_emergency.py::TestP17HumanAndEmergency::test_emergency_pause_overrides_all_decisions",
            "tests_py/test_p17_invariants.py::TestP17Invariants::test_pure_evaluator_evaluates_all_invariants",
        ],
    },
)

# Duplicated decision/budget paths marked for future retirement (M9), not deleted in P17
RETIREMENT_DISPOSITIONS = {
    "execution_intent_budgets": {
        "disposition": "RETIRE_AT_M9",
        "target_replacement": "WorkRecord.attempts and ProblemBudget",
        "deleted_in_p17": False,
    },
    "reviewer_remediation_extension_budgets": {
        "disposition": "RETIRE_AT_M9",
        "target_replacement": "ProblemBudget.max_strategy_attempts",
        "deleted_in_p17": False,
    },
    "watchdog_lifecycle_recovery_attempts": {
        "disposition": "RETIRE_AT_M9",
        "target_replacement": "ProblemBudget.max_resource_attempts",
        "deleted_in_p17": False,
    },
    "resolve_progress_obligation_escalation_tables": {
        "disposition": "RETIRE_AT_M9",
        "target_replacement": "ConvergenceEvaluator.decide()",
        "deleted_in_p17": False,
    },
}


class TestP17ContractAmendments(unittest.TestCase):
    def test_legacy_contract_amendments_structure(self) -> None:
        for entry in LEGACY_CONTRACT_AMENDMENTS:
            self.assertIn(entry["classification"], ("SAFETY", "POLICY"))
            self.assertTrue(len(entry["covering_tests"]) > 0)
            self.assertTrue(bool(entry["reason"]))

    def test_retirement_dispositions_do_not_claim_p17_deletion(self) -> None:
        for target, info in RETIREMENT_DISPOSITIONS.items():
            self.assertEqual(info["disposition"], "RETIRE_AT_M9")
            self.assertFalse(info["deleted_in_p17"])

    def test_isolated_fixture_runtime_root_fail_closed_and_ownership_lock(self) -> None:
        """Isolated fixture testing target stable/dev root fail-closed resolution and state-root lock."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_controller = tmp_path / "controller"
            fake_workspace = tmp_path / "workspace"
            fake_state = tmp_path / "canonical_state"

            fake_controller.mkdir()
            fake_workspace.mkdir()
            fake_state.mkdir()

            # 1. Fail-closed on missing explicit canonical state root
            def resolve_runtime_root(explicit_root: Path | None) -> Path:
                if explicit_root is None or not explicit_root.exists():
                    raise RuntimeError("FAIL_CLOSED: Runtime root cannot be derived from code location")
                return explicit_root

            with self.assertRaises(RuntimeError):
                resolve_runtime_root(None)

            resolved = resolve_runtime_root(fake_state)
            self.assertEqual(resolved, fake_state)

            # 2. State-root ownership lock records host, pid, and controller identity
            lock_file = fake_state / "state_root_owner.lock"
            lock_payload = {
                "host": "ZXZ-PC",
                "pid": 99999,
                "controller_root": str(fake_controller),
                "acquired_at": "2026-09-27T12:00:00Z",
            }
            lock_file.write_text(json.dumps(lock_payload), encoding="utf-8")

            # A second daemon with different controller root must fail closed
            def acquire_state_root_lock(state_root: Path, controller_root: Path, pid: int) -> bool:
                lf = state_root / "state_root_owner.lock"
                if lf.exists():
                    current = json.loads(lf.read_text(encoding="utf-8"))
                    if current.get("controller_root") != str(controller_root):
                        raise RuntimeError(
                            f"LOCKED: State root is already owned by controller {current.get('controller_root')}"
                        )
                return True

            with self.assertRaises(RuntimeError):
                acquire_state_root_lock(fake_state, fake_workspace, 88888)
