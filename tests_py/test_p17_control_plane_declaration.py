"""P17 Control-Plane Impact declaration and prelaunch gate tests.

Validates the canonical declaration in agent/next.md and agent/staged/P17.md,
exact four-field grammar, authoritative invariant and boundary naming,
CPF-01 through CPF-10 coverage, and the prelaunch gate M0 regression.
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from dev_orchestrator.core.control_plane_contract import (
    ControlPlaneDeclaration,
    evaluate_launch_declaration,
    load_control_plane_declaration,
    parse_control_plane_declaration,
)
from dev_orchestrator.core.control_plane_faults import (
    TRANSITION_BOUNDARIES,
    fault_scenarios,
    required_scenarios_for,
    validate_fault_registry,
)
from dev_orchestrator.core.lifecycle_authority import INVARIANT_CODES


class TestP17ControlPlaneDeclaration(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parent.parent

    def test_fault_registry_integrity(self) -> None:
        """Validate CPF-01 through CPF-10 registry passes all checks."""
        errors = validate_fault_registry()
        self.assertEqual(errors, [], f"Fault registry validation errors: {errors}")

    def test_p17_declaration_grammar_in_task_files(self) -> None:
        """Validate both agent/next.md and agent/staged/P17.md have valid identical declarations."""
        next_md = self.repo_root / "agent" / "next.md"
        staged_md = self.repo_root / "agent" / "staged" / "P17.md"

        self.assertTrue(next_md.exists(), "agent/next.md missing")
        self.assertTrue(staged_md.exists(), "agent/staged/P17.md missing")

        decl_next = parse_control_plane_declaration(next_md.read_text(encoding="utf-8"))
        decl_staged = parse_control_plane_declaration(staged_md.read_text(encoding="utf-8"))

        self.assertEqual(decl_next.kind, "declared")
        self.assertEqual(decl_staged.kind, "declared")

        # Must declare all 6 lifecycle invariants
        self.assertEqual(set(decl_next.invariants), set(INVARIANT_CODES))
        self.assertEqual(set(decl_staged.invariants), set(INVARIANT_CODES))

        # Must declare all 8 transition boundaries
        self.assertEqual(set(decl_next.transition_boundaries), set(TRANSITION_BOUNDARIES))
        self.assertEqual(set(decl_staged.transition_boundaries), set(TRANSITION_BOUNDARIES))

        # Must cover CPF-01 through CPF-10
        expected_cpf = {f"CPF-{i:02d}" for i in range(1, 11)}
        self.assertEqual(set(decl_next.fault_scenarios), expected_cpf)
        self.assertEqual(set(decl_staged.fault_scenarios), expected_cpf)

        self.assertTrue(bool(decl_next.convergence_evidence.strip()))
        self.assertTrue(bool(decl_staged.convergence_evidence.strip()))

    def test_evaluate_launch_declaration_at_current_head(self) -> None:
        """Validate launch declaration at current repository HEAD."""
        scope, declaration, gate_eval = evaluate_launch_declaration(
            project_id="devorchestrator",
            repo_path=str(self.repo_root),
            task_id="P17",
            head="HEAD",
        )
        self.assertEqual(scope.kind, "control_plane")
        self.assertEqual(declaration.kind, "declared")
        self.assertTrue(gate_eval.allowed)
        self.assertIsNone(gate_eval.code)

    def test_prelaunch_gate_m0_artifact_and_resume_evidence(self) -> None:
        """Validate P17_PRELAUNCH_GATE.json and authoritative resume history."""
        gate_file = self.repo_root / "agent" / "evidence" / "P17_PRELAUNCH_GATE.json"
        self.assertTrue(gate_file.exists(), "P17_PRELAUNCH_GATE.json missing")

        with open(gate_file, "r", encoding="utf-8") as f:
            gate_data = json.load(f)

        self.assertEqual(gate_data.get("schema_version"), 1)
        self.assertEqual(gate_data.get("task_id"), "P17")
        self.assertEqual(gate_data.get("pre_resume_status"), "PASS")
        self.assertEqual(gate_data["g0_1"]["status"], "PASS")
        self.assertEqual(gate_data["g0_2"]["status"], "PASS")
        self.assertEqual(gate_data["g0_3"]["status"], "PASS")
        self.assertEqual(gate_data["g0_4"]["status"], "PASS")

        # Verify authoritative resume history file exists and settled accepted
        resume_history = self.repo_root / "agent" / "evidence" / "p17-g0-final-resume-20260927.json"
        self.assertTrue(resume_history.is_file(), f"Tracked resume evidence {resume_history} must exist")
        with open(resume_history, "r", encoding="utf-8") as f:
            res_data = json.load(f)
            self.assertEqual(res_data.get("action"), "resume")
            self.assertEqual(res_data.get("state"), "accepted")
            self.assertEqual(res_data.get("effect"), "resume_future_launches")
            self.assertEqual(res_data["expected"]["task_id"], "P17")
