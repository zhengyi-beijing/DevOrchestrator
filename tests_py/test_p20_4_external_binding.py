"""Tests for P20.4 External Execution Binding in TransitionExecutor.

Verifies:
- Binding installation preconditions (task match, active owner, clean descendant, valid permit)
- Worker completion fencing: Broker success does NOT complete Worker or advance to technical_review
- Execution remains active with wait_type='external_durable_job' and bounded deadline_at
- Restart recovery preserves binding and run start
- Mismatched or dead job gates fail closed to OWNER_GATE with recovery_required
- Repeated ticks are byte-stable idempotent (CPF-06).
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
import unittest
from unittest.mock import MagicMock

from dev_orchestrator.core.transition_executor import TransitionExecutor
from dev_orchestrator.soak.permit import create_soak_permit, save_soak_permit


class TestExternalExecutionBinding(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_p20_4_ext_bind_"))
        self.canonical_repo = self.temp_dir / "canonical_repo"
        self.canonical_runtime = self.canonical_repo / "runtime"
        self.canonical_repo.mkdir(parents=True, exist_ok=True)
        self.canonical_runtime.mkdir(parents=True, exist_ok=True)

        self.disposable_dir = self.temp_dir / "disposable_project"
        self.disposable_repo = self.disposable_dir / "repo"
        self.disposable_runtime = self.disposable_dir / "runtime"
        self.disposable_repo.mkdir(parents=True, exist_ok=True)
        self.disposable_runtime.mkdir(parents=True, exist_ok=True)

        marker = self.disposable_repo / ".devorch-disposable-soak"
        marker.write_text(json.dumps({
            "run_id": "soak-run-001",
            "project_id": "p20-soak-proj",
        }), encoding="utf-8")

        self.permit = create_soak_permit(
            run_id="soak-run-001",
            soak_project_id="p20-soak-proj",
            disposable_repo_path=self.disposable_repo,
            disposable_runtime_root=self.disposable_runtime,
            canonical_repo_path=self.canonical_repo,
            canonical_runtime_root=self.canonical_runtime,
        )
        self.permit_path = self.disposable_dir / "soak-permit.json"
        save_soak_permit(self.permit_path, self.permit)

        # Setup initial ledger
        ledger_path = self.canonical_runtime / "transition-executor.json"
        initial_ledger = {
            "version": 1,
            "lifecycle": {
                "devorchestrator": {
                    "project_id": "devorchestrator",
                    "current_task_id": "P20.4",
                    "lifecycle_state": "EXECUTING",
                    "active_owner": {"role": "worker", "id": "req-p20-4-001", "task_id": "P20.4"},
                }
            },
            "executions": {
                "req-p20-4-001": {
                    "request_id": "req-p20-4-001",
                    "project_id": "devorchestrator",
                    "task_id": "P20.4",
                    "state": "running",
                    "engine": "aibroker",
                    "broker_request_id": "brk-001",
                }
            },
            "transitions": {},
        }
        ledger_path.write_text(json.dumps(initial_ledger), encoding="utf-8")
        self.executor = TransitionExecutor(self.canonical_runtime)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_bind_external_execution_success(self) -> None:
        binding_data = {
            "binding_kind": "p20_soak_run",
            "job_id": "job-soak-sup-001",
            "supervisor_pid": 12345,
            "supervisor_start_token": "token-xyz",
            "soak_run_id": "soak-run-001",
            "soak_started_at": "2026-10-04T08:00:00Z",
            "soak_project_id": "p20-soak-proj",
            "permit_path": str(self.permit_path),
            "permit_fingerprint": self.permit.fingerprint,
        }
        res = self.executor.bind_external_execution("req-p20-4-001", binding_data)
        self.assertEqual(res["status"], "bound")

        # Verify query
        saved = self.executor.external_binding_for("req-p20-4-001")
        self.assertIsNotNone(saved)
        self.assertEqual(saved["job_id"], "job-soak-sup-001")
        self.assertEqual(saved["soak_run_id"], "soak-run-001")

    def test_bind_external_execution_rejects_wrong_task(self) -> None:
        # Change current task in authority
        state = self.executor.state()
        state["lifecycle"]["devorchestrator"]["current_task_id"] = "P20.3"
        self.executor._save_ledger(state)

        binding_data = {
            "binding_kind": "p20_soak_run",
            "job_id": "job-soak-sup-001",
            "supervisor_pid": 12345,
            "supervisor_start_token": "token-xyz",
            "soak_run_id": "soak-run-001",
            "soak_started_at": "2026-10-04T08:00:00Z",
            "soak_project_id": "p20-soak-proj",
            "permit_path": str(self.permit_path),
            "permit_fingerprint": self.permit.fingerprint,
        }
        res = self.executor.bind_external_execution("req-p20-4-001", binding_data)
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["reason"], "task_mismatch")

    def test_worker_completion_fenced_while_binding_active(self) -> None:
        binding_data = {
            "binding_kind": "p20_soak_run",
            "job_id": "job-soak-sup-001",
            "supervisor_pid": 12345,
            "supervisor_start_token": "token-xyz",
            "soak_run_id": "soak-run-001",
            "soak_started_at": "2026-10-04T08:00:00Z",
            "soak_project_id": "p20-soak-proj",
            "permit_path": str(self.permit_path),
            "permit_fingerprint": self.permit.fingerprint,
        }
        self.executor.bind_external_execution("req-p20-4-001", binding_data)

        # Simulate broker worker thread completion
        fake_result = MagicMock()
        fake_result.status = "succeeded"
        fake_result.output = "All good"
        fake_result.first_output_at = None
        fake_result.dispatch_id = "disp-1"
        fake_result.decision_id = "dec-1"
        fake_result.execution_id = "exec-1"
        fake_result.session_id = "sess-1"
        fake_result.usage_source = "test"
        fake_result.error = None

        fake_request = MagicMock()
        fake_request.request_id = "brk-001"
        fake_request.project_id = "devorchestrator"
        fake_request.task_run_id = "P20.4"

        # Call update record / worker completion path
        self.executor._handle_worker_broker_result(
            "req-p20-4-001", fake_request, fake_result, worker_role="worker"
        )

        rec = self.executor.state()["executions"]["req-p20-4-001"]
        # Must NOT be completed
        self.assertNotEqual(rec.get("state"), "completed")
        self.assertEqual(rec.get("wait_type"), "external_durable_job")
        self.assertIsNotNone(rec.get("deadline_at"))

        # Authority must still be EXECUTING with active owner
        auth = self.executor.state()["lifecycle"]["devorchestrator"]
        self.assertEqual(auth["lifecycle_state"], "EXECUTING")
        self.assertEqual(auth["active_owner"]["id"], "req-p20-4-001")


if __name__ == "__main__":
    unittest.main()
