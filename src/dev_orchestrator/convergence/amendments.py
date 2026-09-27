"""P17 Contract amendments, legacy classifications, and test AST validation.

Classifies conflicting legacy assertions into SAFETY vs POLICY and defines
retirement dispositions for duplicated decision/budget mechanisms.
"""
from __future__ import annotations

import ast
from pathlib import Path
import re
from typing import Any, Mapping, Sequence


LEGACY_CONTRACT_AMENDMENTS: tuple[dict[str, Any], ...] = (
    {
        "assertion_id": "LCA-01",
        "contract": "TRANSITION_EXECUTOR_CONTRACT",
        "description": "Owner continue required to advance after review remediation budget exhaustion",
        "classification": "POLICY",
        "reason": "Target convergence automatically emits WRITE_HANDOFF or escalating failover instead of manual continue stall",
        "covering_tests": [
            "tests_py/test_p1614_invariant_workflow.py::ControlPlaneGateLifecycleTests::test_repaired_head_reconciles_and_replays_blocked_request",
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
    {
        "assertion_id": "LCA-06",
        "contract": "src/dev_orchestrator/core/control_plane_contract.py",
        "description": "Control-plane launch requires byte-identical four-field declaration at exact committed Git HEAD",
        "classification": "SAFETY",
        "reason": "Permanent control-plane invariant (CPF-08, CPF-09, CPF-10)",
        "covering_tests": [
            "tests_py/test_p17_control_plane_declaration.py::TestP17ControlPlaneDeclaration::test_p17_declaration_grammar_in_task_files",
        ],
    },
    {
        "assertion_id": "LCA-07",
        "contract": "src/dev_orchestrator/control/coordinator.py",
        "description": "Human decision CAS requires matching project HEAD, lifecycle revision, and projected gate ID",
        "classification": "POLICY",
        "reason": "Fragile legacy CAS replaced by question_id and question_revision CAS in target model",
        "covering_tests": [
            "tests_py/test_p17_human_and_emergency.py::TestP17HumanAndEmergency::test_human_request_creation_and_cas_answer",
        ],
    },
)

RETIREMENT_DISPOSITIONS: dict[str, dict[str, Any]] = {
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


def load_legacy_contract_amendments(doc_path: Path | str | None = None) -> tuple[dict[str, Any], ...]:
    """Load contract amendments from markdown doc or fallback to canonical tuple."""
    if doc_path is None:
        return LEGACY_CONTRACT_AMENDMENTS
    p = Path(doc_path)
    if not p.is_file():
        return LEGACY_CONTRACT_AMENDMENTS

    content = p.read_text(encoding="utf-8")
    entries: list[dict[str, Any]] = []
    # Parse table rows matching | **LCA-xx** | ...
    for line in content.splitlines():
        line = line.strip()
        if not line.startswith("|") or "LCA-" not in line:
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) < 6:
            continue
        m_id = re.search(r"LCA-\d+", cells[0])
        if not m_id:
            continue
        aid = m_id.group(0)
        contract = cells[1].strip("` ")
        desc = cells[2].strip()
        classification = cells[3].strip("` ")
        reason = cells[4].strip()
        raw_tests = cells[5]
        # Split tests by <br> or newline and clean backticks
        test_refs = [
            t.strip("` ")
            for t in re.split(r"<br\s*/?>", raw_tests)
            if t.strip("` ")
        ]
        entries.append({
            "assertion_id": aid,
            "contract": contract,
            "description": desc,
            "classification": classification,
            "reason": reason,
            "covering_tests": test_refs,
        })

    return tuple(entries) if entries else LEGACY_CONTRACT_AMENDMENTS


def load_retirement_dispositions(doc_path: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Load retirement dispositions."""
    return dict(RETIREMENT_DISPOSITIONS)


def validate_amendment_test_references(
    root_dir: Path | str,
    amendments: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Validate that every covering_tests entry resolves to an existing file and test via AST."""
    root = Path(root_dir)
    errors: list[str] = []

    for entry in amendments:
        aid = entry.get("assertion_id", "UNKNOWN")
        tests = entry.get("covering_tests", [])
        if not tests:
            errors.append(f"{aid}: no covering tests defined")
            continue

        for test_ref in tests:
            parts = test_ref.split("::")
            rel_file = parts[0]
            target_path = root / rel_file
            if not target_path.is_file():
                errors.append(f"{aid}: test file not found: {rel_file}")
                continue

            try:
                tree = ast.parse(target_path.read_text(encoding="utf-8", errors="replace"), filename=str(target_path))
            except Exception as exc:
                errors.append(f"{aid}: syntax error in {rel_file}: {exc}")
                continue

            if len(parts) == 1:
                # File itself exists and parsed
                continue
            elif len(parts) == 2:
                # Function or method in file
                func_name = parts[1]
                found = any(
                    isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func_name
                    for n in tree.body
                )
                if not found:
                    # Check inside classes as well
                    for n in tree.body:
                        if isinstance(n, ast.ClassDef):
                            if any(isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name == func_name for m in n.body):
                                found = True
                                break
                if not found:
                    errors.append(f"{aid}: function/method {func_name!r} not found in {rel_file}")
            elif len(parts) == 3:
                # Class::Method
                cls_name = parts[1]
                method_name = parts[2]
                found_cls = False
                found_method = False
                for n in tree.body:
                    if isinstance(n, ast.ClassDef) and n.name == cls_name:
                        found_cls = True
                        found_method = any(
                            isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name == method_name
                            for m in n.body
                        )
                        break
                if not found_cls:
                    errors.append(f"{aid}: class {cls_name!r} not found in {rel_file}")
                elif not found_method:
                    errors.append(f"{aid}: method {method_name!r} not found in {cls_name} ({rel_file})")

    return errors
