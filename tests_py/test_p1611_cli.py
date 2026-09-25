"""Unit tests for P16.11 CLI subcommands: pool-status, agy-benchmark, and agy-route."""
import json
import unittest
from io import StringIO
from unittest.mock import patch

from dev_orchestrator.cli import main


class TestP1611CLI(unittest.TestCase):
    def test_cli_pool_status(self):
        """pool-status outputs valid JSON telemetry for the 3 AGY accounts."""
        with patch("sys.stdout", new=StringIO()) as out:
            rc = main(["pool-status"])
            self.assertEqual(rc, 0)
            data = json.loads(out.getvalue())
            self.assertEqual(data["total_accounts"], 3)
            self.assertEqual(data["available_accounts"], 3)
            self.assertIn("agy-1", data["accounts"])
            self.assertIn("agy-2", data["accounts"])
            self.assertIn("agy-3", data["accounts"])

    def test_cli_agy_benchmark_clean(self):
        """agy-benchmark outputs valid JSON benchmark results under normal conditions."""
        with patch("sys.stdout", new=StringIO()) as out:
            rc = main(["agy-benchmark"])
            self.assertEqual(rc, 0)
            data = json.loads(out.getvalue())
            self.assertIn("agy_first_metrics", data)
            self.assertIn("baseline_metrics", data)
            self.assertIn("comparison", data)
            self.assertEqual(data["agy_first_metrics"]["total_tasks"], 5)
            self.assertEqual(data["agy_first_metrics"]["agy_coverage"], 1.0)
            self.assertFalse(data["same_failure_escalation_demonstrated"])

    def test_cli_agy_benchmark_simulate_failure(self):
        """agy-benchmark --simulate-failure demonstrates same-failure escalation."""
        with patch("sys.stdout", new=StringIO()) as out:
            rc = main(["agy-benchmark", "--simulate-failure", "replay_worker_cache"])
            self.assertEqual(rc, 0)
            data = json.loads(out.getvalue())
            self.assertTrue(data["same_failure_escalation_demonstrated"])
            self.assertEqual(data["agy_first_metrics"]["escalation_rate"], 0.2)
            self.assertEqual(data["agy_first_metrics"]["repeated_failure_rate"], 0.0)

    def test_cli_agy_route_worker(self):
        """agy-route --role worker selects AGY pool."""
        with patch("sys.stdout", new=StringIO()) as out:
            rc = main(["agy-route", "--role", "worker"])
            self.assertEqual(rc, 0)
            data = json.loads(out.getvalue())
            self.assertEqual(data["routing_tier"], "agy_pool")
            self.assertEqual(data["provider"], "agy")
            self.assertFalse(data["is_escalated"])

    def test_cli_agy_route_reviewer_provider_independence(self):
        """agy-route for reviewer with provider independence escalates outside AGY."""
        with patch("sys.stdout", new=StringIO()) as out:
            rc = main([
                "agy-route",
                "--role", "reviewer",
                "--independence", "provider",
                "--worker-provider", "agy",
                "--worker-account", "agy-1",
            ])
            self.assertEqual(rc, 0)
            data = json.loads(out.getvalue())
            self.assertEqual(data["routing_tier"], "independent_reviewer")
            self.assertNotEqual(data["provider"], "agy")
            self.assertTrue(data["is_escalated"])
            self.assertTrue(data["independence_enforced"])

    def test_cli_agy_route_same_failure_escalation(self):
        """agy-route with failure-signature prevents blind AGY rotation and triggers heterogeneous escalation."""
        with patch("sys.stdout", new=StringIO()) as out:
            rc = main([
                "agy-route",
                "--role", "worker",
                "--failure-signature", "deadbeef1234",
            ])
            self.assertEqual(rc, 0)
            data = json.loads(out.getvalue())
            self.assertEqual(data["routing_tier"], "heterogeneous_escalation")
            self.assertNotEqual(data["provider"], "agy")
            self.assertTrue(data["is_escalated"])
            self.assertTrue(data["same_failure_prevented"])


if __name__ == "__main__":
    unittest.main()
