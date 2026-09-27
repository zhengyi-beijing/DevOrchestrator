"""P17 Acceptance model and GoalSatisfied verification tests."""
from __future__ import annotations

import unittest

from dev_orchestrator.convergence.findings import Finding, FindingSeverity
from dev_orchestrator.convergence.problems import FailureClass
from dev_orchestrator.convergence.verification import (
    AcceptanceKind,
    VerificationRecord,
    is_goal_satisfied,
    validate_owner_override,
)
from dev_orchestrator.convergence.work_record import validate_work_record


class TestP17AcceptanceModel(unittest.TestCase):
    def setUp(self) -> None:
        self.base_work_record = validate_work_record({
            "schema_version": 1,
            "project_id": "devorchestrator",
            "goal_id": "P17",
            "goal_revision": 1,
            "goal_spec_digest": "sha256:spec-1",
            "predecessor_goal_id": "P16.14",
            "repository_identity": {"repo_path": "C:\\repo", "branch": "main"},
            "status": "OPEN",
            "acceptance": {"kind": "NONE"},
            "authority_revision": "rev-1",
        })

    def test_acceptance_none_cannot_satisfy_goal(self) -> None:
        satisfied, reason = is_goal_satisfied(self.base_work_record)
        self.assertFalse(satisfied)
        self.assertIn("Acceptance is NONE", reason)

    def test_active_lease_blocks_goal_satisfaction(self) -> None:
        leased_record = validate_work_record({
            **self.base_work_record.to_dict(),
            "active_lease": {
                "role": "worker",
                "attempt_id": "a-1",
                "execution_id": "e-1",
                "acquired_at": "2026-09-27T00:00:00Z",
            },
            "acceptance": {
                "kind": "VERIFIED",
                "reviewer_verdict_id": "v-1",
                "anchor_head": "h-1",
            },
        })
        satisfied, reason = is_goal_satisfied(leased_record)
        self.assertFalse(satisfied)
        self.assertIn("Active execution lease still exists", reason)

    def test_owner_override_cannot_waive_blocking_finding(self) -> None:
        bad_override = {
            "kind": "OWNER_OVERRIDE",
            "owner_id": "owner",
            "command_id": "cmd-1",
            "scope": "test_scope",
            "reason": "waive blocker",
            "waived_obligations": ["no_unresolved_blocking_finding"],
        }
        valid, msg = validate_owner_override(bad_override)
        self.assertFalse(valid)
        self.assertIn("cannot waive non-waivable safety obligation", msg)

    def test_owner_override_cannot_waive_emergency_brake(self) -> None:
        bad_override = {
            "kind": "OWNER_OVERRIDE",
            "owner_id": "owner",
            "command_id": "cmd-1",
            "scope": "test_scope",
            "reason": "waive brake",
            "waived_obligations": ["emergency_brake_enforcement"],
        }
        valid, msg = validate_owner_override(bad_override)
        self.assertFalse(valid)
        self.assertIn("cannot waive non-waivable safety obligation", msg)

    def test_owner_override_valid_waived_evidence_succeeds(self) -> None:
        valid_override = {
            "kind": "OWNER_OVERRIDE",
            "owner_id": "project_owner",
            "command_id": "p17-bootstrap-owner-override-20260927",
            "scope": "P16.14 -> P17 migration/bootstrap exception",
            "reason": "Explicit owner acceptance of bounded delta",
            "waived_obligations": ["final_accepting_reviewer_verdict_at_P16.14_closure_anchor"],
        }
        valid, msg = validate_owner_override(valid_override)
        self.assertTrue(valid, msg)

        record = validate_work_record({
            **self.base_work_record.to_dict(),
            "acceptance": valid_override,
        })
        satisfied, reason = is_goal_satisfied(record)
        self.assertTrue(satisfied, reason)

    def test_verified_acceptance_with_unresolved_blocking_finding_fails(self) -> None:
        blocking_finding = Finding(
            finding_id="f-1",
            severity=FindingSeverity.BLOCKING,
            failure_class=FailureClass.IMPLEMENTATION_DEFECT,
            target={"file": "test.py"},
            scope_claim="task",
            machine_code="ASSERTION_FAILED",
            human_explanation="Test failed",
        )
        vr = VerificationRecord(
            verification_id="ver-1",
            project_id="devorchestrator",
            goal_id="P17",
            branch="main",
            exact_head="h-1",
            clean_status_fingerprint="clean",
            acceptance_criterion_results={"crit-1": True},
            executed_checks=({"command": "pytest", "exit_status": 0},),
            reviewer_decision="ACCEPT",
            structured_findings=(blocking_finding,),
        )
        record = validate_work_record({
            **self.base_work_record.to_dict(),
            "acceptance": {"kind": "VERIFIED", "reviewer_verdict_id": "v-1", "anchor_head": "h-1"},
        })
        satisfied, reason = is_goal_satisfied(record, verification=vr)
        self.assertFalse(satisfied)
        self.assertIn("BLOCKING", reason)
