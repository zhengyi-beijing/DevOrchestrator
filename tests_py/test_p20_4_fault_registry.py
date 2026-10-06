"""Tests for P20.4 Fault Registry, Injection Protocol and Sanitization.

Verifies:
- 15/15 owner fault definitions mapped one-to-one
- Explicit distinction between deterministic_test and live_injection modes
- CPF scenario mapping (CPF-01 through CPF-10)
- Injection protocol: intent reservation flushed before effect
- Disposition recording with incident, family, recovery strategy/outcome
- Secret sanitization on fault and incident evidence payloads.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
import unittest

from dev_orchestrator.soak.faults import (
    SOAK_FAULTS,
    CPF_COVERAGE_MAP,
    SoakFault,
    get_soak_fault,
    plan_fault_schedule,
    reserve_fault_injection,
    record_fault_disposition,
    load_fault_injection,
)
from dev_orchestrator.soak.permit import create_soak_permit


class TestSoakFaultRegistry(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_p20_4_faults_"))
        self.canonical_repo = self.temp_dir / "canonical_repo"
        self.canonical_runtime = self.canonical_repo / "runtime"
        self.canonical_repo.mkdir(parents=True, exist_ok=True)
        self.canonical_runtime.mkdir(parents=True, exist_ok=True)

        self.disposable_dir = self.temp_dir / "disposable_project"
        self.disposable_repo = self.disposable_dir / "repo"
        self.disposable_runtime = self.disposable_dir / "runtime"
        self.disposable_repo.mkdir(parents=True, exist_ok=True)
        self.disposable_runtime.mkdir(parents=True, exist_ok=True)

        marker_file = self.disposable_repo / ".devorch-disposable-soak"
        marker_file.write_text(json.dumps({
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

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_fifteen_faults_registered_with_correct_disposition(self) -> None:
        self.assertEqual(len(SOAK_FAULTS), 15)
        expected_fault_ids = {
            "F01_MALFORMED_PLANNER",
            "F02_PLANNER_TIMEOUT",
            "F03_WORKER_KILLED",
            "F04_REVIEWER_REJECT",
            "F05_QUOTA_EXHAUSTED",
            "F06_PROVIDER_UNAVAILABLE",
            "F07_BROKER_UNAVAILABLE",
            "F08_DAEMON_RESTART_ACTIVE",
            "F09_DAEMON_RESTART_HANDOFF",
            "F10_STALE_HEAD",
            "F11_UNEXPECTED_DIRT",
            "F12_MISSING_PROJECTION",
            "F13_LOST_HANDOFF",
            "F14_DUPLICATE_RETRY",
            "F15_REPEATED_SIGNATURE",
        }
        actual_fault_ids = {f.fault_id for f in SOAK_FAULTS}
        self.assertEqual(actual_fault_ids, expected_fault_ids)

        for fault in SOAK_FAULTS:
            self.assertIn(fault.expected_disposition, {"automatic_recovery", "owner_gate", "fail_closed"})
            self.assertIsInstance(fault.unsafe, bool)
            self.assertTrue(set(fault.allowed_modes).issubset({"deterministic_test", "live_injection"}))

    def test_cpf_coverage_mapping_complete(self) -> None:
        # CPF-01 through CPF-10 must all be mapped
        expected_cpfs = {f"CPF-{i:02d}" for i in range(1, 11)}
        actual_cpfs = set(CPF_COVERAGE_MAP.keys())
        self.assertEqual(actual_cpfs, expected_cpfs)

    def test_injection_reservation_and_disposition(self) -> None:
        injection = reserve_fault_injection(
            runtime_root=self.disposable_runtime,
            run_id="soak-run-001",
            fault_id="F01_MALFORMED_PLANNER",
            mode="deterministic_test",
            target={"kind": "planner_output", "path": str(self.disposable_repo / "agent" / "next.md")},
            pre_state={"status": "normal"},
        )
        self.assertIsNotNone(injection)
        injection_id = injection["injection_id"]
        self.assertEqual(injection["state"], "reserved")
        self.assertEqual(injection["mode"], "deterministic_test")

        # Record disposition
        disp = record_fault_disposition(
            runtime_root=self.disposable_runtime,
            injection_id=injection_id,
            incident_id="inc-f01-001",
            failure_family="family-malformed-planner",
            recovery_strategy="RETRY_CHANGED_STRATEGY",
            recovery_result="success",
            recovery_latency=1.5,
            owner_gate_id=None,
            disposition="automatic_recovery",
        )
        self.assertEqual(disp["state"], "completed")
        self.assertEqual(disp["disposition"], "automatic_recovery")

        loaded = load_fault_injection(self.disposable_runtime, injection_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["disposition"], "automatic_recovery")
        self.assertEqual(loaded["incident_id"], "inc-f01-001")

    def test_secret_sanitization_in_fault_payloads(self) -> None:
        # Reserve with sensitive payload
        injection = reserve_fault_injection(
            runtime_root=self.disposable_runtime,
            run_id="soak-run-001",
            fault_id="F01_MALFORMED_PLANNER",
            mode="deterministic_test",
            target={"kind": "planner_output", "prompt": "Authorization: Bearer sk-ant-secret-12345"},
            pre_state={"token": "ghp_1234567890abcdef"},
        )
        inj_id = injection["injection_id"]
        loaded = load_fault_injection(self.disposable_runtime, inj_id)
        raw_json = json.dumps(loaded)
        self.assertNotIn("sk-ant-secret-12345", raw_json)
        self.assertNotIn("ghp_1234567890abcdef", raw_json)
        self.assertIn("[REDACTED", raw_json)


if __name__ == "__main__":
    unittest.main()
