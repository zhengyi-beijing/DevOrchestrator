"""Unit tests for P16.11 representative replay benchmark, independent review gate, and baseline comparison."""
import json
import unittest
from pathlib import Path

from dev_orchestrator.pool.models import PolicyDisposition
from dev_orchestrator.pool.replay_benchmark import (
    AGYBenchmarkReplayRunner,
    SimulatedPort,
    evaluate_independent_review,
    get_canonical_replay_tasks,
)


class TestAGYBenchmarkReplay(unittest.TestCase):
    def setUp(self):
        self.work_dir = Path(".").resolve()
        self.runner = AGYBenchmarkReplayRunner()

    def test_canonical_replay_tasks(self):
        """Canonical replay set contains exactly 5 tasks covering all major DevO role classes."""
        tasks = get_canonical_replay_tasks()
        self.assertEqual(len(tasks), 5)
        roles = [t.role for t in tasks]
        self.assertIn("planner", roles)
        self.assertIn("worker", roles)
        self.assertIn("debugger", roles)
        self.assertIn("evidence_packaging", roles)
        self.assertIn("reviewer", roles)

    def test_independent_review_evaluation(self):
        """evaluate_independent_review verifies required findings and provider independence."""
        task = get_canonical_replay_tasks()[1]  # worker task
        valid_payload = json.dumps({
            "task_id": task.task_id,
            "required_findings": ["get_or_set", "default_fn"],
        })
        accepted, msg = evaluate_independent_review(
            valid_payload,
            task,
            reviewer_provider="claude",
            worker_provider="agy",
        )
        self.assertTrue(accepted)
        self.assertIn("ACCEPTED", msg)

        # Missing finding fails review
        invalid_payload = json.dumps({
            "task_id": task.task_id,
            "required_findings": ["get_or_set"],  # missing default_fn
        })
        rejected, r_msg = evaluate_independent_review(
            invalid_payload,
            task,
            reviewer_provider="claude",
            worker_provider="agy",
        )
        self.assertFalse(rejected)
        self.assertIn("Missing required finding", r_msg)

        # Provider independence violation fails review
        task_strict = get_canonical_replay_tasks()[4]  # reviewer task
        # Modify independence to provider for testing
        task_prov = type(task_strict)(
            task_id=task_strict.task_id,
            role=task_strict.role,
            title=task_strict.title,
            prompt=task_strict.prompt,
            ground_truth=task_strict.ground_truth,
            independence="provider",
        )
        prov_rejected, prov_msg = evaluate_independent_review(
            valid_payload,
            task_prov,
            reviewer_provider="agy",
            worker_provider="agy",
            strict_independence=True,
        )
        self.assertFalse(prov_rejected)
        self.assertIn("Reviewer independence violation", prov_msg)

    def test_clean_benchmark_replay_run(self):
        """Under clean conditions, AGY-first pool achieves 100% coverage with zero cost on AGY tasks."""
        sim_port = SimulatedPort()
        result = self.runner.run_replay(sim_port, self.work_dir)

        agy_m = result.agy_first_metrics
        base_m = result.baseline_metrics

        self.assertEqual(agy_m["total_tasks"], 5)
        self.assertEqual(agy_m["accepted_tasks"], 5)
        self.assertEqual(agy_m["agy_coverage"], 1.0)
        self.assertEqual(agy_m["first_pass_acceptance_rate"], 1.0)
        self.assertEqual(agy_m["escalation_rate"], 0.0)
        self.assertEqual(agy_m["repeated_failure_rate"], 0.0)
        self.assertFalse(result.same_failure_escalation_demonstrated)

        # Comparison metrics
        self.assertGreaterEqual(result.comparison["cost_savings_ratio"], 0.8)
        self.assertEqual(result.routing_recommendations["worker"], PolicyDisposition.SUPPORTED.value)
        self.assertEqual(result.routing_recommendations["reviewer_with_provider_independence"], PolicyDisposition.UNSUPPORTED.value)

    def test_simulated_failure_demonstrates_heterogeneous_escalation(self):
        """Simulated failure on agy-1 triggers same-failure prevention and heterogeneous escalation."""
        sim_port = SimulatedPort(failure_on_agy1_task="replay_worker_cache")
        result = self.runner.run_replay(sim_port, self.work_dir)

        agy_m = result.agy_first_metrics
        self.assertEqual(agy_m["total_tasks"], 5)
        self.assertEqual(agy_m["accepted_tasks"], 5)  # Recovered via escalation!
        self.assertEqual(agy_m["agy_coverage"], 1.0)
        self.assertEqual(agy_m["first_pass_acceptance_rate"], 0.8)  # 4/5 first-pass
        self.assertEqual(agy_m["escalation_rate"], 0.2)  # 1/5 escalated
        self.assertEqual(agy_m["repeated_failure_rate"], 0.0)  # No repeated failure!
        self.assertTrue(result.same_failure_escalation_demonstrated)

        # Cost savings still substantial
        self.assertGreaterEqual(result.comparison["cost_savings_ratio"], 0.6)


if __name__ == "__main__":
    unittest.main()
