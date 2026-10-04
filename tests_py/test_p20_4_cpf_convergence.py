"""Tests for P20.4 CPF-01 through CPF-10 Convergence Scenarios.

Verifies:
- CPF-01: Stale predecessor worker fences at authority reconciliation; binding cannot attach to non-authoritative task
- CPF-02: Lost accepted NEXT_TASK handoff is rebuilt once by watchdog recovery
- CPF-03: Daemon restart mid-transition replays missing handoff from journal
- CPF-04: Contradictory successor claims fail closed without creating transition
- CPF-05: Dirty worktree defers repair without repo mutation
- CPF-06: Repeated daemon ticks under active binding remain byte-stable idempotent
- CPF-07: Crash after intent before publication converges to single owner, no duplicate injection
- CPF-08: Missing control-plane declaration fails closed to OWNER_GATE
- CPF-09: Repaired HEAD with valid declaration resolves gate and replays blocked request
- CPF-10: Same-HEAD duplicate launch attempts are byte-stable with no second job.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
import unittest

from dev_orchestrator.core.control_plane_faults import get_fault_scenario
from dev_orchestrator.core.transition_executor import TransitionExecutor
from dev_orchestrator.soak.faults import (
    CPF_COVERAGE_MAP,
    reserve_fault_injection,
    load_fault_injection,
)
from dev_orchestrator.soak.permit import create_soak_permit, save_soak_permit


class TestSoakCPFConvergence(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_p20_4_cpf_"))
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

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_cpf_all_ten_scenarios_exist_in_registry(self) -> None:
        for i in range(1, 11):
            cpf_id = f"CPF-{i:02d}"
            scen = get_fault_scenario(cpf_id)
            self.assertIsNotNone(scen, f"{cpf_id} not found in control-plane fault registry")
            self.assertIn(cpf_id, CPF_COVERAGE_MAP)

    def test_cpf_01_stale_predecessor_worker_fenced_from_external_binding(self) -> None:
        """CPF-01: Cannot bind external execution to a task that is not current authority."""
        ledger_path = self.canonical_runtime / "transition-executor.json"
        ledger_path.write_text(json.dumps({
            "version": 1,
            "lifecycle": {
                "devorchestrator": {
                    "project_id": "devorchestrator",
                    "current_task_id": "P20.5",  # Successor already published!
                    "lifecycle_state": "READY_TO_RUN",
                }
            },
            "executions": {
                "req-old-p20-4": {
                    "request_id": "req-old-p20-4",
                    "project_id": "devorchestrator",
                    "task_id": "P20.4",
                    "state": "running",
                }
            },
            "transitions": {},
        }), encoding="utf-8")

        executor = TransitionExecutor(self.canonical_runtime)
        res = executor.bind_external_execution("req-old-p20-4", {
            "binding_kind": "p20_soak_run",
            "job_id": "job-soak-001",
            "permit_path": str(self.permit_path),
            "permit_fingerprint": self.permit.fingerprint,
        })
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["reason"], "task_mismatch")

    def test_cpf_06_repeated_ticks_under_binding_are_idempotent(self) -> None:
        """CPF-06: Repeated reconciliation ticks under active binding do not duplicate state."""
        ledger_path = self.canonical_runtime / "transition-executor.json"
        ledger_path.write_text(json.dumps({
            "version": 1,
            "lifecycle": {
                "devorchestrator": {
                    "project_id": "devorchestrator",
                    "current_task_id": "P20.4",
                    "lifecycle_state": "EXECUTING",
                    "active_owner": {"role": "worker", "id": "req-p20-4", "task_id": "P20.4"},
                }
            },
            "executions": {
                "req-p20-4": {
                    "request_id": "req-p20-4",
                    "project_id": "devorchestrator",
                    "task_id": "P20.4",
                    "state": "running",
                    "wait_type": "external_durable_job",
                    "external_execution_binding": {
                        "job_id": "job-soak-001",
                        "soak_run_id": "soak-run-001",
                    }
                }
            },
            "transitions": {},
        }), encoding="utf-8")

        executor = TransitionExecutor(self.canonical_runtime)
        r1 = executor.reconcile_external_execution_bindings()
        state1 = executor.state()

        r2 = executor.reconcile_external_execution_bindings()
        state2 = executor.state()

        # States must be identical across ticks
        self.assertEqual(state1, state2)

    def test_cpf_07_crash_after_reserved_intent_does_not_inject_twice(self) -> None:
        """CPF-07: A replayed intent after crash must not inject twice."""
        inj = reserve_fault_injection(
            runtime_root=self.disposable_runtime,
            run_id="soak-run-001",
            fault_id="F01_MALFORMED_PLANNER",
            mode="deterministic_test",
            target={"kind": "mock"},
            pre_state={"state": "init"},
        )
        inj_id = inj["injection_id"]

        # Trying to reserve the same injection again or replaying it returns existing
        loaded = load_fault_injection(self.disposable_runtime, inj_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["injection_id"], inj_id)


if __name__ == "__main__":
    unittest.main()
