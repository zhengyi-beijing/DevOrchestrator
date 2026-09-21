import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

BENCHMARK_SRC = Path(__file__).resolve().parents[1] / "benchmark" / "src"
if str(BENCHMARK_SRC) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_SRC))

from dev_orchestrator.ai.contracts import (
    AIRoleRequest,
    AIRoleResult,
    ResourceContext,
)
from dev_orchestrator.ai.execution_port import AIExecutionPort

from aibench.broker_client import BrokerBenchmarkClient, MockExecutionPort
from aibench.contracts import ResourceSnapshot


class TestP16BrokerClient(unittest.TestCase):
    def setUp(self):
        self.port = MockExecutionPort()
        self.snapshot = ResourceSnapshot(
            snapshot_id="snap_test",
            source="test",
            registry_digest="digest_test",
            timestamp="2026-01-01T00:00:00Z",
            resources=(
                {"resource_id": "res_1", "provider": "p1", "account": "a1", "model": "m1", "enabled": True},
                {"resource_id": "res_2", "provider": "p2", "account": "a2", "model": "m2", "enabled": True},
                {"resource_id": "res_3", "provider": "p3", "account": "a3", "model": "m3", "enabled": True},
            ),
        )

    def test_snapshot_from_yaml_and_sanitization(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(
                "resources:\n"
                "- resource_id: agy/test/res-1\n"
                "  provider: agy\n"
                "  account: test\n"
                "  model: gemini-3.8\n"
                "  api_key: secret_api_key_must_be_stripped\n"
                "  executable: C:\\secret\\path.exe\n"
                "  enabled: true\n"
            )
            yaml_path = Path(f.name)

        try:
            client = BrokerBenchmarkClient(self.port, config_path=yaml_path)
            snap = client.snapshot_resources(source_override="resources_file")
            self.assertEqual(len(snap.resources), 1)
            res = snap.resources[0]
            self.assertEqual(res["resource_id"], "agy/test/res-1")
            self.assertNotIn("api_key", res)
            self.assertNotIn("secret_api_key_must_be_stripped", str(res))
        finally:
            yaml_path.unlink(missing_ok=True)

    def test_execute_exact_pins_target_by_excluding_others(self):
        client = BrokerBenchmarkClient(self.port)
        req = AIRoleRequest(
            project_id="bench",
            role="planner",
            prompt="test prompt",
            working_directory=Path("."),
        )

        attempt = client.execute_exact("res_1", req, self.snapshot)
        self.assertEqual(attempt.status, "succeeded")
        self.assertEqual(attempt.resource_id, "res_1")

        # Verify excluded_resource_ids was populated with all OTHER resources in snapshot
        dispatched_req = self.port.recorded_requests[-1]
        self.assertEqual(set(dispatched_req.excluded_resource_ids), {"res_2", "res_3"})

    def test_execute_exact_fails_on_returned_resource_mismatch(self):
        client = BrokerBenchmarkClient(self.port)
        req = AIRoleRequest(
            project_id="bench",
            role="planner",
            prompt="test prompt",
            working_directory=Path("."),
        )
        # Port returns res_2 when res_1 was requested
        self.port.next_result = AIRoleResult(
            request_id=req.request_id,
            role_run_id="",
            status="succeeded",
            resource_context=ResourceContext(resource_id="res_2"),
        )

        with self.assertRaises(RuntimeError) as ctx:
            client.execute_exact("res_1", req, self.snapshot)
        self.assertIn("exact resource mismatch", str(ctx.exception))

    def test_execute_reliability_chain_retries_resource_failure(self):
        client = BrokerBenchmarkClient(self.port)
        self.port.fail_then_succeed_target = "res_1"

        req = AIRoleRequest(
            project_id="bench",
            role="planner",
            prompt="test prompt",
            working_directory=Path("."),
        )

        attempts = client.execute_reliability_chain(req, max_attempts=3)
        self.assertEqual(len(attempts), 2)
        # First attempt failed with quota_exhausted on res_1
        self.assertEqual(attempts[0].status, "failed")
        self.assertEqual(attempts[0].failure_classification, "quota_exhausted")
        # Second attempt succeeded on res_2
        self.assertEqual(attempts[1].status, "succeeded")
        self.assertEqual(attempts[1].resource_id, "res_2")


if __name__ == "__main__":
    unittest.main()
