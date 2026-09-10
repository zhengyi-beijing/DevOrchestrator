import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dev_orchestrator.ai import (
    AIBrokerClientConfig,
    AIBrokerExecutionPort,
    AIBrokerInvocationError,
    AIRoleRequest,
    ResourceContext,
)


class AIBrokerExecutionPortTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.gettempdir()) / "devorch-broker-test"
        self.config = AIBrokerClientConfig(
            python_executable=Path(sys.executable),
            broker_repo=self.root / "broker",
            config_path=self.root / "resources.yaml",
            database_path=self.root / "facts.sqlite3",
        )
        self.port = AIBrokerExecutionPort(self.config)

    def request(self, **overrides):
        values = dict(
            project_id="devorchestrator", role="worker", prompt="work",
            working_directory=Path("C:/work/repo"), request_id="req-1",
            task_run_id="task-1", stage_run_id="stage-1", role_run_id="role-1",
        )
        values.update(overrides)
        return AIRoleRequest(**values)

    @staticmethod
    def payload(**overrides):
        value = {
            "dispatch_id": "dispatch-1", "request_id": "req-1",
            "decision_id": "decision-1", "resource_id": "resource-1",
            "execution_id": "execution-1", "session_id": None,
            "status": "succeeded", "output": "OK", "error": None,
            "usage": None, "usage_source": "unknown",
            "resource_context": {
                "resource_id": "resource-1", "provider": "deepseek",
                "account": "default", "model": "deepseek-v4-flash",
            },
        }
        value.update(overrides)
        return value

    @patch("dev_orchestrator.ai.aibroker_subprocess.subprocess.run")
    def test_success_maps_broker_result_and_preserves_semantics(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(self.payload()), stderr=""
        )
        result = self.port.execute(self.request())
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.output, "OK")
        self.assertEqual(result.dispatch_id, "dispatch-1")
        self.assertEqual(result.execution_id, "execution-1")
        self.assertEqual(result.resource_context.provider, "deepseek")
        argv = run.call_args.args[0]
        self.assertIn("ai_resource_broker.cli", argv)
        self.assertEqual(argv.count("dispatch"), 1)
        self.assertIn("--role", argv)
        self.assertNotIn("--provider", argv)

    @patch("dev_orchestrator.ai.aibroker_subprocess.subprocess.run")
    def test_transport_timeout_covers_long_task_timeout(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(self.payload()), stderr=""
        )
        self.port.execute(self.request(timeout_seconds=14400))
        self.assertEqual(run.call_args.kwargs["timeout"], 14460.0)

    @patch("dev_orchestrator.ai.aibroker_subprocess.subprocess.run")
    def test_no_candidate_is_terminal_result_not_transport_retry(self, run):
        payload = self.payload(
            status="no_candidate", resource_id=None, execution_id=None,
            output=None, resource_context=None,
        )
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout=json.dumps(payload), stderr=""
        )
        result = self.port.execute(self.request())
        self.assertEqual(result.status, "no_candidate")
        run.assert_called_once()

    @patch("dev_orchestrator.ai.aibroker_subprocess.subprocess.run")
    def test_validation_failure_is_transport_error(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=2, stdout="", stderr="error: bad config"
        )
        with self.assertRaisesRegex(AIBrokerInvocationError, "bad config"):
            self.port.execute(self.request())

    @patch("dev_orchestrator.ai.aibroker_subprocess.subprocess.run")
    def test_invalid_json_fails_closed(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not-json", stderr=""
        )
        with self.assertRaisesRegex(AIBrokerInvocationError, "invalid JSON"):
            self.port.execute(self.request())

    @patch("dev_orchestrator.ai.aibroker_subprocess.subprocess.run")
    def test_request_id_mismatch_fails_closed(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps(self.payload(request_id="other")), stderr=""
        )
        with self.assertRaisesRegex(AIBrokerInvocationError, "correlation mismatch"):
            self.port.execute(self.request())

    @patch("dev_orchestrator.ai.aibroker_subprocess.subprocess.run")
    def test_independence_passes_prior_evidence_not_routing_policy(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(self.payload()), stderr=""
        )
        previous = ResourceContext("old-r", "codex", "a", "model-x")
        self.port.execute(self.request(
            role="reviewer", independence="resource",
            previous_resource_context=previous,
        ))
        argv = run.call_args.args[0]
        self.assertIn("--independence", argv)
        self.assertIn("--previous-resource-id", argv)
        self.assertIn("old-r", argv)
        self.assertNotIn("--excluded-resource-id", argv)

    def test_request_validation_is_fail_closed(self):
        with self.assertRaises(ValueError):
            self.request(quality="ultra")
        with self.assertRaises(ValueError):
            self.request(independence="resource")
        with self.assertRaises(ValueError):
            self.request(request_id=" ")


if __name__ == "__main__":
    unittest.main()
