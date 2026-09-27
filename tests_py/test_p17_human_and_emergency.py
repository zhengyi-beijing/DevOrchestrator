"""P17 Human decision boundary and Emergency Brake tests."""
from __future__ import annotations

import unittest

from dev_orchestrator.convergence.evaluator import DecisionKind, decide
from dev_orchestrator.convergence.evidence import EvidenceSnapshot
from dev_orchestrator.convergence.human_request import (
    HumanRequestConflictError,
    HumanRequestOption,
    answer_human_request,
    create_human_request,
)
from dev_orchestrator.convergence.policy import build_policy
from dev_orchestrator.convergence.work_record import validate_work_record


class TestP17HumanAndEmergency(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = build_policy()
        self.base_record = {
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
        }

    def test_emergency_pause_overrides_all_decisions(self) -> None:
        rec = validate_work_record(self.base_record)
        evidence = EvidenceSnapshot(emergency_pause_asserted=True)
        decision = decide(rec, evidence, self.policy)
        self.assertEqual(decision.kind, DecisionKind.WAIT_UNTIL)
        self.assertTrue(decision.parameters.get("emergency_pause"))
        self.assertIn("EMERGENCY_BRAKE", decision.invariant_citations)

    def test_human_request_creation_and_cas_answer(self) -> None:
        req = create_human_request(
            goal_id="P17",
            request_type="product_choice",
            concrete_question="Confirm single-authority design?",
            options=[
                HumanRequestOption("opt-yes", "Yes, proceed with single authority", "proceed"),
                HumanRequestOption("opt-no", "No, keep dual authority", "abort"),
            ],
            semantic_key="single_auth_design",
        )
        self.assertFalse(req.is_answered)

        # Answering with matching question_id + question_revision succeeds
        answered = answer_human_request(
            req,
            question_id=req.question_id,
            question_revision=req.question_revision,
            option_id="opt-yes",
            owner_id="project_owner",
            command_id="cmd-answer-1",
            answered_at="2026-09-27T00:00:00Z",
        )
        self.assertTrue(answered.is_answered)
        self.assertEqual(answered.answer["option_id"], "opt-yes")

        # Second answer fails (discharge exactly once)
        with self.assertRaises(HumanRequestConflictError):
            answer_human_request(
                answered,
                question_id=req.question_id,
                question_revision=req.question_revision,
                option_id="opt-no",
                owner_id="project_owner",
                command_id="cmd-answer-2",
                answered_at="2026-09-27T00:01:00Z",
            )

    def test_stale_question_revision_rejects_answer(self) -> None:
        req = create_human_request(
            goal_id="P17",
            request_type="auth",
            concrete_question="Grant hardware access?",
            options=[HumanRequestOption("opt-allow", "Allow", "grant")],
            question_revision=2,
        )
        with self.assertRaises(HumanRequestConflictError):
            answer_human_request(
                req,
                question_id=req.question_id,
                question_revision=1,  # Stale revision
                option_id="opt-allow",
                owner_id="owner",
                command_id="cmd-1",
                answered_at="2026-09-27T00:00:00Z",
            )
