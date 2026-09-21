import sys
import unittest
from pathlib import Path

BENCHMARK_SRC = Path(__file__).resolve().parents[1] / "benchmark" / "src"
if str(BENCHMARK_SRC) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_SRC))

from aibench.contracts import (
    DECISION_NO_PROMOTE,
    DECISION_PROMOTE,
    REASON_BENEFIT_GATE_FAILED,
    REASON_CAPABILITY_UNSUPPORTED,
    REASON_COST_GATE_FAILED,
    REASON_EVIDENCE_GATE_FAILED,
    REASON_PRIVACY_GATE_FAILED,
    REASON_QUALITY_GATE_FAILED,
    REASON_STALENESS_GATE_FAILED,
    RunSummary,
    TrialPlan,
    ZvecProbeResult,
)
from aibench.decision import evaluate_promotion_decision
from aibench.runner import build_trial_plan


class TestP16DecisionAndGates(unittest.TestCase):
    def setUp(self):
        self.resources = ["res_1", "res_2", "res_3"]
        self.plan = build_trial_plan(self.resources, repeats=1, seed=42)

    def _make_summary(
        self,
        score_a: float = 0.8,
        score_b: float = 0.85,
        wall_delta: float = -2.5,
        tool_delta: float = -1.0,
        token_delta: float | None = -50.0,
        false_findings: int = 0,
        stale_error_rate: float = 0.05,
        index_time: float = 5.0,
        index_size_bytes: int = 1000000,
        resources_count: int = 3,
        roles_count: int = 4,
    ) -> RunSummary:
        res_breakdown = {f"res_{i}": {"completed_trials": 8, "total_trials": 8} for i in range(1, resources_count + 1)}
        roles_list = ["planner", "reviewer", "worker", "debugger"][:roles_count]
        role_breakdown = {r: {"completed_trials": 6, "total_trials": 6} for r in roles_list}

        return RunSummary(
            run_id="run_test",
            plan_id=self.plan.plan_id,
            created_at="2026-01-01T00:00:00Z",
            total_trials=24,
            completed_trials=24,
            track_a_summary={
                "total_trials": 12,
                "completed_trials": 12,
                "correctness_mean": score_a,
                "wall_time_seconds_mean": 10.0,
                "false_findings_total": 0,
                "required_findings_total": 20,
            },
            track_b_summary={
                "total_trials": 12,
                "completed_trials": 12,
                "correctness_mean": score_b,
                "wall_time_seconds_mean": 7.5,
                "false_findings_total": false_findings,
                "required_findings_total": 20,
            },
            comparison={
                "completed_pairs": 12,
                "mean_correctness_delta": round(score_b - score_a, 4),
                "mean_wall_time_delta_seconds": wall_delta,
                "mean_tool_calls_delta": tool_delta,
                "mean_tokens_delta": token_delta,
                "wall_time_stats": {"statistically_supported_benefit": wall_delta < 0},
                "tool_calls_stats": {"statistically_supported_benefit": tool_delta < 0},
                "tokens_stats": {"statistically_supported_benefit": (token_delta < 0) if token_delta is not None else False},
            },
            resource_breakdown=res_breakdown,
            role_breakdown=role_breakdown,
            quota_consumed={},
            staleness_metrics={
                "stale_error_rate": stale_error_rate,
                "index_time_seconds": index_time,
                "index_size_bytes": index_size_bytes,
            },
        )

    def test_unsupported_zvec_yields_no_promote_and_capability_unsupported(self):
        summary = self._make_summary()
        probe = ZvecProbeResult(supported=False, error_message="not found")

        decision = evaluate_promotion_decision(summary, probe, self.plan)
        self.assertEqual(decision.decision, DECISION_NO_PROMOTE)
        self.assertIn(REASON_CAPABILITY_UNSUPPORTED, decision.reasons)
        self.assertIn("capability_gate", decision.failed_gates)

        # Check downstream gate states
        self.assertEqual(
            decision.gate_results["benefit_gate"]["status"],
            "not_applicable_due_to_capability_failure",
        )
        self.assertEqual(
            decision.gate_results["privacy_gate"]["status"],
            "not_applicable_due_to_capability_failure",
        )
        self.assertEqual(
            decision.gate_results["staleness_gate"]["status"],
            "not_applicable_due_to_capability_failure",
        )

    def test_all_gates_passing_yields_promote(self):
        summary = self._make_summary()
        probe = ZvecProbeResult(
            supported=True,
            executable_path="C:\\zvec.exe",
            version="1.0.0",
            executable_hash="hash123",
            local_only_verified=True,
        )

        decision = evaluate_promotion_decision(summary, probe, self.plan)
        self.assertEqual(decision.decision, DECISION_PROMOTE)
        self.assertEqual(len(decision.failed_gates), 0)
        self.assertEqual(len(decision.reasons), 0)

    def test_evidence_gate_fails_with_insufficient_resources(self):
        summary = self._make_summary(resources_count=2)  # only 2 resources
        probe = ZvecProbeResult(supported=True, version="1.0", executable_hash="h", local_only_verified=True)

        decision = evaluate_promotion_decision(summary, probe, self.plan)
        self.assertEqual(decision.decision, DECISION_NO_PROMOTE)
        self.assertIn(REASON_EVIDENCE_GATE_FAILED, decision.reasons)
        self.assertIn("evidence_gate", decision.failed_gates)

    def test_quality_gate_fails_on_correctness_regression(self):
        # Track B score 0.6 is significantly worse than Track A score 0.8
        summary = self._make_summary(score_a=0.8, score_b=0.6)
        probe = ZvecProbeResult(supported=True, version="1.0", executable_hash="h", local_only_verified=True)

        decision = evaluate_promotion_decision(summary, probe, self.plan)
        self.assertEqual(decision.decision, DECISION_NO_PROMOTE)
        self.assertIn(REASON_QUALITY_GATE_FAILED, decision.reasons)
        self.assertIn("quality_gate", decision.failed_gates)

    def test_benefit_gate_fails_when_no_advantages_measured(self):
        # Track B is slower (+2.0s), uses more tools (+1.5), uses more tokens (+100)
        summary = self._make_summary(wall_delta=+2.0, tool_delta=+1.5, token_delta=+100.0)
        probe = ZvecProbeResult(supported=True, version="1.0", executable_hash="h", local_only_verified=True)

        decision = evaluate_promotion_decision(summary, probe, self.plan)
        self.assertEqual(decision.decision, DECISION_NO_PROMOTE)
        self.assertIn(REASON_BENEFIT_GATE_FAILED, decision.reasons)
        self.assertIn("benefit_gate", decision.failed_gates)

    def test_privacy_gate_fails_when_firewall_rule_not_verified(self):
        summary = self._make_summary()
        probe = ZvecProbeResult(supported=True, version="1.0", executable_hash="h", local_only_verified=False)

        decision = evaluate_promotion_decision(summary, probe, self.plan)
        self.assertEqual(decision.decision, DECISION_NO_PROMOTE)
        self.assertIn(REASON_PRIVACY_GATE_FAILED, decision.reasons)
        self.assertIn("privacy_gate", decision.failed_gates)

    def test_staleness_gate_fails_when_stale_rate_too_high(self):
        summary = self._make_summary(stale_error_rate=0.50)  # 50% error rate > 15% threshold
        probe = ZvecProbeResult(supported=True, version="1.0", executable_hash="h", local_only_verified=True)

        decision = evaluate_promotion_decision(summary, probe, self.plan)
        self.assertEqual(decision.decision, DECISION_NO_PROMOTE)
        self.assertIn(REASON_STALENESS_GATE_FAILED, decision.reasons)
        self.assertIn("staleness_gate", decision.failed_gates)

    def test_quality_gate_drop_within_threshold_passes(self):
        # Drop of 0.04 is within max_correctness_drop (-0.05) -> quality gate passes
        summary_ok = self._make_summary(score_a=0.80, score_b=0.76)
        probe = ZvecProbeResult(supported=True, version="1.0", executable_hash="h", local_only_verified=True)
        decision_ok = evaluate_promotion_decision(summary_ok, probe, self.plan)
        self.assertTrue(decision_ok.gate_results["quality_gate"]["passed"])

        # Drop of 0.06 exceeds max_correctness_drop (-0.05) -> quality gate fails
        summary_reg = self._make_summary(score_a=0.80, score_b=0.74)
        decision_reg = evaluate_promotion_decision(summary_reg, probe, self.plan)
        self.assertFalse(decision_reg.gate_results["quality_gate"]["passed"])

    def test_benefit_gate_requires_statistical_support(self):
        summary = self._make_summary(wall_delta=-2.0, tool_delta=-1.0, token_delta=-50.0)
        # Manually overwrite stats so that statistically_supported_benefit is False
        summary.comparison["wall_time_stats"]["statistically_supported_benefit"] = False
        summary.comparison["tool_calls_stats"]["statistically_supported_benefit"] = False
        summary.comparison["tokens_stats"]["statistically_supported_benefit"] = False
        probe = ZvecProbeResult(supported=True, version="1.0", executable_hash="h", local_only_verified=True)

        decision = evaluate_promotion_decision(summary, probe, self.plan)
        self.assertEqual(decision.decision, DECISION_NO_PROMOTE)
        self.assertIn("benefit_gate", decision.failed_gates)

    def test_capability_failure_branch_evaluates_evidence_and_fallback_gates(self):
        # Unsupported probe
        probe = ZvecProbeResult(supported=False, error_message="not installed")

        # Case 1: Complete 3 resources, 4 roles, 24/24 trials -> evidence and fallback gates pass
        summary_ok = self._make_summary(resources_count=3, roles_count=4)
        decision_ok = evaluate_promotion_decision(summary_ok, probe, self.plan)
        self.assertTrue(decision_ok.gate_results["evidence_gate"]["passed"])
        self.assertTrue(decision_ok.gate_results["fallback_gate"]["passed"])
        self.assertEqual(decision_ok.gate_results["quality_gate"]["status"], "not_applicable_due_to_capability_failure")

        # Case 2: Insufficient resources (2 < 3) -> evidence gate fails
        summary_insuf = self._make_summary(resources_count=2, roles_count=4)
        decision_insuf = evaluate_promotion_decision(summary_insuf, probe, self.plan)
        self.assertFalse(decision_insuf.gate_results["evidence_gate"]["passed"])
        self.assertIn("evidence_gate", decision_insuf.failed_gates)


if __name__ == "__main__":
    unittest.main()
