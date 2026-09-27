"""P17 Closed-schema findings and OUTPUT_INVALID error handling tests."""
from __future__ import annotations

import unittest

from dev_orchestrator.convergence.evaluator import DecisionKind, decide
from dev_orchestrator.convergence.evidence import EvidenceSnapshot
from dev_orchestrator.convergence.findings import (
    Finding,
    FindingSeverity,
    OutputInvalidError,
    parse_structured_findings,
)
from dev_orchestrator.convergence.policy import build_policy
from dev_orchestrator.convergence.problems import FailureClass
from dev_orchestrator.convergence.work_record import validate_work_record


class TestP17FindingsAndOutputInvalid(unittest.TestCase):
    def test_parse_valid_structured_findings(self) -> None:
        raw = [
            {
                "finding_id": "f-1",
                "severity": "BLOCKING",
                "failure_class": "IMPLEMENTATION_DEFECT",
                "target": {"file": "src/foo.py", "symbol": "bar"},
                "scope_claim": "task",
                "machine_code": "TYPE_ERROR",
                "human_explanation": "Type mismatch in bar()",
            }
        ]
        findings = parse_structured_findings(raw)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, FindingSeverity.BLOCKING)
        self.assertEqual(findings[0].failure_class, FailureClass.IMPLEMENTATION_DEFECT)

    def test_malformed_findings_raise_output_invalid(self) -> None:
        # None raises OutputInvalidError
        with self.assertRaises(OutputInvalidError):
            parse_structured_findings(None)

        # Missing target['file'] raises OutputInvalidError
        with self.assertRaises(OutputInvalidError):
            parse_structured_findings([{"finding_id": "f-1", "severity": "BLOCKING", "failure_class": "IMPLEMENTATION_DEFECT", "target": {}}])

        # Invalid severity raises OutputInvalidError
        with self.assertRaises(OutputInvalidError):
            parse_structured_findings([
                {
                    "finding_id": "f-1",
                    "severity": "CRITICAL",
                    "failure_class": "IMPLEMENTATION_DEFECT",
                    "target": {"file": "foo.py"},
                    "machine_code": "ERR",
                }
            ])

    def test_output_invalid_triggers_bounded_schema_repair(self) -> None:
        """OUTPUT_INVALID produces RETRY_SAME_STRATEGY with validator feedback."""
        record = validate_work_record({
            "schema_version": 1,
            "project_id": "devorchestrator",
            "goal_id": "P17",
            "goal_revision": 1,
            "goal_spec_digest": "sha256:spec-1",
            "predecessor_goal_id": "P16.14",
            "repository_identity": {"repo_path": "C:\\repo", "branch": "main"},
            "status": "OPEN",
            "current_problem": {
                "problem_id": "prob-out-invalid",
                "failure_class": "OUTPUT_INVALID",
                "normalized_fingerprint": "fp-out-inv",
            },
            "attempts": [],
            "acceptance": {"kind": "NONE"},
            "authority_revision": "rev-1",
        })
        policy = build_policy(max_output_invalid_repairs=2)
        decision = decide(record, EvidenceSnapshot(), policy)
        self.assertEqual(decision.kind, DecisionKind.RETRY_SAME_STRATEGY)
        self.assertTrue(decision.parameters.get("validator_feedback"))
