"""Append-only registry of control-plane fault scenarios (CPF-).

Every scenario records an origin task, affected lifecycle invariant codes,
transition boundary, expected convergence outcome, human-readable description,
and a resolvable test reference.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from dev_orchestrator.core.lifecycle_authority import INVARIANT_CODES


TRANSITION_BOUNDARIES = frozenset({
    "plan_freeze",
    "worker_launch",
    "special_gate_reconciliation",
    "owner_gate_transition",
    "successor_handoff",
    "handoff_publication",
    "authority_reconciliation",
    "watchdog_recovery",
})

ALLOWED_EXPECTATIONS = frozenset({
    "fail_closed_gate",
    "converge_single_owner",
    "byte_stable_idempotent",
    "wait_without_mutation",
    "replay_journal",
})


@dataclass(frozen=True)
class FaultScenario:
    scenario_id: str
    origin_task: str
    invariant_codes: tuple[str, ...]
    transition_boundary: str
    expectation: str
    description: str
    test_reference: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "origin_task": self.origin_task,
            "invariant_codes": list(self.invariant_codes),
            "transition_boundary": self.transition_boundary,
            "expectation": self.expectation,
            "description": self.description,
            "test_reference": self.test_reference,
        }


_FAULT_REGISTRY: tuple[FaultScenario, ...] = (
    FaultScenario(
        scenario_id="CPF-01",
        origin_task="P16.13",
        invariant_codes=("CURRENT_TASK_MATCHES_ACTIVE_EXECUTION", "SINGLE_ACTIVE_LIFECYCLE_OWNER"),
        transition_boundary="authority_reconciliation",
        expectation="fail_closed_gate",
        description="Stale predecessor Worker runs after successor publication; source remains authoritative and target fences",
        test_reference="tests_py/test_p1613_successor_consistency.py::SuccessorConsistencyFaultMatrixTests::test_A_and_H_stale_predecessor_worker_fences_published_successor",
    ),
    FaultScenario(
        scenario_id="CPF-02",
        origin_task="P16.13",
        invariant_codes=("NEXT_TASK_WITHOUT_HANDOFF",),
        transition_boundary="watchdog_recovery",
        expectation="converge_single_owner",
        description="Accepted NEXT_TASK review decision loses handoff; zero-touch recovery rebuilds exactly one handoff",
        test_reference="tests_py/test_p1613_successor_consistency.py::SuccessorConsistencyFaultMatrixTests::test_B_review_verdict_without_handoff_recovers_once",
    ),
    FaultScenario(
        scenario_id="CPF-03",
        origin_task="P16.13",
        invariant_codes=("SUCCESSOR_HANDOFF_LINEAGE_VALID",),
        transition_boundary="successor_handoff",
        expectation="replay_journal",
        description="Daemon restart mid-transition replays missing handoff from the transition journal",
        test_reference="tests_py/test_p1613_successor_consistency.py::SuccessorConsistencyFaultMatrixTests::test_C_restart_mid_transition_replays_missing_handoff",
    ),
    FaultScenario(
        scenario_id="CPF-04",
        origin_task="P16.13",
        invariant_codes=("SUCCESSOR_HANDOFF_LINEAGE_VALID",),
        transition_boundary="successor_handoff",
        expectation="fail_closed_gate",
        description="Contradictory or ambiguous successor claims fail closed without creating transitions",
        test_reference="tests_py/test_p1613_successor_consistency.py::SuccessorConsistencyFaultMatrixTests::test_E_roadmap_and_staged_claim_disagreement_fails_closed",
    ),
    FaultScenario(
        scenario_id="CPF-05",
        origin_task="P16.13",
        invariant_codes=("SUCCESSOR_HANDOFF_LINEAGE_VALID",),
        transition_boundary="successor_handoff",
        expectation="wait_without_mutation",
        description="Dirty worktree defers roadmap repair without repository mutation until clean",
        test_reference="tests_py/test_p1613_successor_consistency.py::SuccessorConsistencyFaultMatrixTests::test_D_dirty_reconcile_waits_then_advances_without_owner_continue",
    ),
    FaultScenario(
        scenario_id="CPF-06",
        origin_task="P16.13",
        invariant_codes=("SINGLE_ACTIVE_LIFECYCLE_OWNER",),
        transition_boundary="authority_reconciliation",
        expectation="byte_stable_idempotent",
        description="Repeated daemon ticks do not duplicate transitions or increment generation",
        test_reference="tests_py/test_p1613_successor_consistency.py::SuccessorConsistencyFaultMatrixTests::test_F_duplicate_review_handoff_is_idempotent",
    ),
    FaultScenario(
        scenario_id="CPF-07",
        origin_task="P16.13",
        invariant_codes=("SUCCESSOR_HANDOFF_LINEAGE_VALID", "NEXT_TASK_WITHOUT_HANDOFF"),
        transition_boundary="handoff_publication",
        expectation="converge_single_owner",
        description="Crash after intent before publication recovers to single authoritative owner",
        test_reference="tests_py/test_p1613_successor_consistency.py::SuccessorConsistencyFaultMatrixTests::test_I_and_J_repeated_recovery_after_intent_is_single_and_live",
    ),
    FaultScenario(
        scenario_id="CPF-08",
        origin_task="P16.14",
        invariant_codes=("PENDING_DESIGN_NOT_EXECUTING", "SINGLE_ACTIVE_LIFECYCLE_OWNER"),
        transition_boundary="worker_launch",
        expectation="fail_closed_gate",
        description="Control-plane task missing declaration fails closed to CONTROL_PLANE_DECLARATION_REQUIRED owner gate",
        test_reference="tests_py/test_p1614_invariant_workflow.py::ControlPlaneGateLifecycleTests::test_unannounced_control_plane_launch_refuses_to_owner_gate",
    ),
    FaultScenario(
        scenario_id="CPF-09",
        origin_task="P16.14",
        invariant_codes=("CURRENT_TASK_MATCHES_ACTIVE_EXECUTION", "SINGLE_ACTIVE_LIFECYCLE_OWNER"),
        transition_boundary="special_gate_reconciliation",
        expectation="converge_single_owner",
        description="Repaired HEAD with valid declaration resolves declaration gate and replays blocked execution request",
        test_reference="tests_py/test_p1614_invariant_workflow.py::ControlPlaneGateLifecycleTests::test_repaired_head_reconciles_and_replays_blocked_request",
    ),
    FaultScenario(
        scenario_id="CPF-10",
        origin_task="P16.14",
        invariant_codes=("SINGLE_ACTIVE_LIFECYCLE_OWNER", "TERMINAL_TASK_HAS_NO_RUNNING_EXECUTION"),
        transition_boundary="worker_launch",
        expectation="byte_stable_idempotent",
        description="Same-HEAD launch retries against an un-repaired declaration gate are byte-stable with no timestamp refresh",
        test_reference="tests_py/test_p1614_invariant_workflow.py::ControlPlaneGateLifecycleTests::test_same_head_repeated_launch_attempts_are_byte_stable",
    ),
)


def fault_scenarios() -> tuple[FaultScenario, ...]:
    return _FAULT_REGISTRY


def get_fault_scenario(scenario_id: str) -> FaultScenario | None:
    for scenario in _FAULT_REGISTRY:
        if scenario.scenario_id == scenario_id:
            return scenario
    return None


def scenarios_for_invariant(invariant_code: str) -> tuple[FaultScenario, ...]:
    return tuple(
        scenario for scenario in _FAULT_REGISTRY
        if invariant_code in scenario.invariant_codes
    )


def required_scenarios_for(invariant_codes: Iterable[str]) -> tuple[str, ...]:
    matched: set[str] = set()
    codes = set(invariant_codes)
    for scenario in _FAULT_REGISTRY:
        if any(code in codes for code in scenario.invariant_codes):
            matched.add(scenario.scenario_id)
    return tuple(sorted(matched))


def validate_fault_registry(repo_root: str | Path | None = None) -> list[str]:
    """Validate fault registry integrity, constraints, and test reference resolution."""
    errors: list[str] = []
    seen_ids: set[str] = set()
    covered_invariants: set[str] = set()

    root_path = Path(repo_root).resolve() if repo_root else Path.cwd().resolve()

    for scenario in _FAULT_REGISTRY:
        if not scenario.scenario_id.startswith("CPF-"):
            errors.append(f"{scenario.scenario_id}: ID does not start with CPF-")
        if scenario.scenario_id in seen_ids:
            errors.append(f"{scenario.scenario_id}: duplicate scenario ID")
        seen_ids.add(scenario.scenario_id)

        if not scenario.invariant_codes:
            errors.append(f"{scenario.scenario_id}: invariant_codes is empty")
        for code in scenario.invariant_codes:
            if code not in INVARIANT_CODES:
                errors.append(f"{scenario.scenario_id}: unknown invariant code '{code}'")
            covered_invariants.add(code)

        if scenario.transition_boundary not in TRANSITION_BOUNDARIES:
            errors.append(f"{scenario.scenario_id}: unknown transition boundary '{scenario.transition_boundary}'")

        if scenario.expectation not in ALLOWED_EXPECTATIONS:
            errors.append(f"{scenario.scenario_id}: unknown expectation '{scenario.expectation}'")

        if not scenario.description.strip():
            errors.append(f"{scenario.scenario_id}: description is empty")

        # Validate test reference
        ref_parts = scenario.test_reference.split("::")
        if len(ref_parts) != 3:
            errors.append(f"{scenario.scenario_id}: test reference '{scenario.test_reference}' must have format path::Class::method")
            continue
        rel_path, cls_name, method_name = ref_parts
        test_file = root_path / rel_path
        if not test_file.is_file():
            errors.append(f"{scenario.scenario_id}: test file does not exist: '{rel_path}'")
            continue

        try:
            tree = ast.parse(test_file.read_text(encoding="utf-8", errors="replace"), filename=str(test_file))
            found_method = False
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == cls_name:
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == method_name:
                            found_method = True
                            break
            if not found_method:
                errors.append(f"{scenario.scenario_id}: test reference '{scenario.test_reference}' not found in AST of '{rel_path}'")
        except Exception as exc:
            errors.append(f"{scenario.scenario_id}: failed to parse '{rel_path}': {exc}")

    missing_invariants = set(INVARIANT_CODES) - covered_invariants
    if missing_invariants:
        errors.append(f"Registry does not cover all lifecycle invariants: missing {sorted(missing_invariants)}")

    return errors
