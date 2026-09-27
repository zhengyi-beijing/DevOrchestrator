"""P17 Learned environment constraints and preflight evaluation tests."""
from __future__ import annotations

import unittest

from dev_orchestrator.convergence.preflight import (
    CapabilityConstraint,
    PreflightVerdict,
    classify_recurrence,
    evaluate_preflight,
    get_seeded_rules,
)


class TestP17PreflightConstraints(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = get_seeded_rules()
        self.env_ps51 = {"os": "windows", "shell": "powershell_5.1"}

    def test_ps51_pipeline_chaining_rule_provenance_and_fingerprint(self) -> None:
        ps51_rule = [r for r in self.rules if r.rule_id == "rule-ps51-pipeline-chaining"][0]
        self.assertEqual(ps51_rule.normalized_fingerprint, "281bf686902bf4aa1cd966a92cab25c5d1a66bfec453323ed171726580c1b82c")
        self.assertEqual(ps51_rule.evidence.get("provenance"), "seed:p11b:rdc-powershell-5.1")
        self.assertEqual(ps51_rule.action, PreflightVerdict.REJECT)

    def test_preflight_rejects_incompatible_command(self) -> None:
        cmd = {"command": "git status && git log"}
        verdict, explanation, rule = evaluate_preflight(cmd, self.env_ps51, self.rules)
        self.assertEqual(verdict, PreflightVerdict.REJECT)
        self.assertIsNotNone(rule)
        self.assertEqual(rule.rule_id, "rule-ps51-pipeline-chaining")
        self.assertIn("&&", explanation)

    def test_preflight_allows_compatible_command(self) -> None:
        cmd = {"command": "git status; git log"}
        verdict, explanation, rule = evaluate_preflight(cmd, self.env_ps51, self.rules)
        self.assertEqual(verdict, PreflightVerdict.ALLOW)
        self.assertIsNone(rule)

    def test_recurrence_classified_as_learning_regression(self) -> None:
        executed_cmd = {"command": "git checkout main || exit 1"}
        tag, explanation = classify_recurrence(executed_cmd, self.env_ps51, self.rules)
        self.assertEqual(tag, "LEARNING_REGRESSION")
        self.assertIn("Control-plane defect", explanation)
        self.assertIn("seed:p11b:rdc-powershell-5.1", explanation)
