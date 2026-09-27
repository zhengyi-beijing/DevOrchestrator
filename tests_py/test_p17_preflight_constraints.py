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

    def test_every_seeded_rule_is_reachable_and_classified(self) -> None:
        """Finding 8: Every seeded constraint rule (1-6) is reachable, matches its signature, and has a valid derived fingerprint."""
        from dev_orchestrator.convergence.preflight import derive_constraint_fingerprint

        rule_scenarios = [
            # Rule 1: contains_tokens
            (
                "rule-ps51-pipeline-chaining",
                {"os": "windows", "shell": "powershell_5.1"},
                {"command": "git status && git log"},
                PreflightVerdict.REJECT,
            ),
            # Rule 2: cmdlet + parameter
            (
                "rule-ps51-utf8-no-bom",
                {"os": "windows", "shell": "powershell_5.1"},
                {"command": "Set-Content -Path foo.txt -Value bar -Encoding utf8NoBOM"},
                PreflightVerdict.REJECT,
            ),
            # Rule 3: extension
            (
                "rule-npm-ps1-exec-policy",
                {"os": "windows", "tool": "npm"},
                {"command": "npm.ps1 install", "path": "npm.ps1"},
                PreflightVerdict.REWRITE,
            ),
            # Rule 4: cli_flags
            (
                "rule-codex-flags-discovery",
                {"tool": "codex"},
                {"command": "codex --some-unverified-flag", "cli_flags": "unverified"},
                PreflightVerdict.REJECT,
            ),
            # Rule 5: redirection
            (
                "rule-shell-redirection-syntax",
                {"os": "windows", "shell": "powershell"},
                {"command": "app.exe 2>&1"},
                PreflightVerdict.REWRITE,
            ),
            # Rule 6: path_style
            (
                "rule-dotnet-apis-absolute-paths",
                {"runtime": "dotnet_powershell"},
                {"command": "[System.IO.File]::ReadAllText('rel.txt')", "path": "rel.txt"},
                PreflightVerdict.REWRITE,
            ),
        ]

        seen_rule_ids = set()
        for rule_id, env, op, expected_verdict in rule_scenarios:
            matched_rule = [r for r in self.rules if r.rule_id == rule_id][0]
            seen_rule_ids.add(rule_id)

            # Check fingerprint validity
            if rule_id == "rule-ps51-pipeline-chaining":
                self.assertEqual(matched_rule.normalized_fingerprint, "281bf686902bf4aa1cd966a92cab25c5d1a66bfec453323ed171726580c1b82c")
            else:
                derived = derive_constraint_fingerprint(rule_id, matched_rule.environment_selector, matched_rule.operation_signature)
                self.assertEqual(matched_rule.normalized_fingerprint, derived)
                self.assertEqual(len(derived), 64)

            # Check evaluator reachability
            verdict, explanation, rule = evaluate_preflight(op, env, self.rules)
            self.assertEqual(verdict, expected_verdict, f"Rule {rule_id} did not produce expected verdict")
            self.assertIsNotNone(rule)
            self.assertEqual(rule.rule_id, rule_id)

            # Check classification of recurrence
            tag, rec_exp = classify_recurrence(op, env, self.rules)
            self.assertEqual(tag, "LEARNING_REGRESSION")

        self.assertEqual(seen_rule_ids, {r.rule_id for r in self.rules})

    def test_required_sources_missing_fails_closed(self) -> None:
        """Finding 8: Missing required source in Policy.required_sources fails closed to REQUEST_HUMAN."""
        from dev_orchestrator.convergence.evaluator import DecisionKind, decide
        from dev_orchestrator.convergence.evidence import EvidenceItem, EvidenceSnapshot
        from dev_orchestrator.convergence.policy import build_policy
        from dev_orchestrator.convergence.work_record import validate_work_record

        policy = build_policy(required_sources=("repo_truth", "broker_telemetry"))
        ev = EvidenceSnapshot(
            items=(
                EvidenceItem(
                    source="repo_truth",
                    source_id="r1",
                    timestamp="2026-09-27T00:00:00Z",
                    anchor="a1",
                    data={},
                    digest="d1",
                    read_status="OK",
                ),
            ),
            conflicts=(),
            shared_leases=(),
            exact_anchors={"head": "h1"},
            emergency_pause_asserted=False,
            metadata={},
        )
        rec = validate_work_record({
            "schema_version": 1,
            "project_id": "p",
            "goal_id": "P17",
            "goal_revision": 1,
            "goal_spec_digest": "s",
            "predecessor_goal_id": "p0",
            "repository_identity": {"repo_path": ".", "branch": "main"},
            "status": "OPEN",
            "acceptance": {"kind": "NONE"},
            "authority_revision": "r1",
        })
        decision = decide(rec, ev, policy)
        self.assertEqual(decision.kind, DecisionKind.REQUEST_HUMAN)
        self.assertIn("missing_required_sources", decision.problem_id)
        self.assertIn("FAIL_CLOSED_AMBIGUITY", decision.invariant_citations)
