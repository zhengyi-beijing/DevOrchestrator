import tempfile
import time
import unittest
from pathlib import Path

from dev_orchestrator.core.diagnostics import (
    DIAGNOSIS_CODES,
    classify_evidence,
    collect_evidence,
    evidence_hash,
)


class DummyAssessment:
    def __init__(self, lifecycle_state="EXECUTING", no_progress_seconds=1200.0, threshold_seconds=900.0):
        self.lifecycle_state = lifecycle_state
        self.no_progress_seconds = no_progress_seconds
        self.threshold_seconds = threshold_seconds


class WatchdogDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.repo_dir = self.root / "repo"
        self.runtime_dir = self.root / "runtime"
        self.repo_dir.mkdir(parents=True)
        self.runtime_dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_7_diagnostic_classifications(self):
        """Verify all 7 diagnosis codes can be classified deterministically."""
        self.assertEqual(len(DIAGNOSIS_CODES), 7)
        assessment = DummyAssessment()

        # 1. process_dead
        ev_dead = {
            "process_liveness": {"process_alive": False, "pid": 1234},
            "repository_truth": {"valid": True},
        }
        diag1 = classify_evidence(ev_dead, assessment)
        self.assertEqual(diag1.code, "process_dead")
        self.assertEqual(diag1.confidence, 1.0)
        self.assertFalse(diag1.owner_gate_required)

        # 2. provider_or_quota_blocked
        ev_quota = {
            "process_liveness": {"process_alive": True, "pid": 1234},
            "broker": {"status": "rate_limit_exceeded (429)"},
            "repository_truth": {"valid": True},
        }
        diag2 = classify_evidence(ev_quota, assessment)
        self.assertEqual(diag2.code, "provider_or_quota_blocked")
        self.assertTrue(diag2.owner_gate_required)

        # 3. state_desync
        ev_desync = {
            "process_liveness": {"process_alive": True, "pid": 1234},
            "ledgers": {"transition-executor.json": {"state": "completed"}},
            "repository_truth": {"valid": True},
        }
        diag3 = classify_evidence(ev_desync, assessment)
        self.assertEqual(diag3.code, "state_desync")
        self.assertTrue(diag3.owner_gate_required)

        # 4. external_wait
        ev_wait = {
            "process_liveness": {"process_alive": True, "pid": 1234},
            "broker": {"status": "waiting_bridge"},
            "repository_truth": {"valid": True},
        }
        diag4 = classify_evidence(ev_wait, assessment)
        self.assertEqual(diag4.code, "external_wait")
        self.assertTrue(diag4.owner_gate_required)

        # 5. healthy_slow
        ev_healthy = {
            "process_liveness": {"process_alive": True, "pid": 1234},
            "repository_truth": {"valid": True, "dirty": True, "uncommitted_files": ["f1.py"]},
        }
        healthy_assessment = DummyAssessment(no_progress_seconds=950.0, threshold_seconds=900.0)
        diag5 = classify_evidence(ev_healthy, healthy_assessment)
        self.assertEqual(diag5.code, "healthy_slow")
        self.assertFalse(diag5.owner_gate_required)

        # 6. agent_stalled
        ev_stalled = {
            "process_liveness": {"process_alive": True, "pid": 1234},
            "repository_truth": {"valid": True, "dirty": False},
        }
        stalled_assessment = DummyAssessment(no_progress_seconds=2000.0, threshold_seconds=900.0)
        diag6 = classify_evidence(ev_stalled, stalled_assessment)
        self.assertEqual(diag6.code, "agent_stalled")
        self.assertFalse(diag6.owner_gate_required)

        # 7. unknown (skipped deadline or ambiguous)
        ev_unknown = {
            "repository_truth": {"status": "skipped_deadline"},
        }
        diag7 = classify_evidence(ev_unknown, assessment)
        self.assertEqual(diag7.code, "unknown")
        self.assertTrue(diag7.owner_gate_required)

    def test_evidence_hash_stability(self):
        """evidence_hash produces identical deterministic hash regardless of key order or volatile timings."""
        e1 = {
            "z": 1,
            "a": [3, 2, 1],
            "sub": {"k": "v"},
            "collected_at": "2026-09-12T10:00:00Z",
        }
        e2 = {
            "collected_at": "2026-09-12T11:00:00Z",
            "sub": {"k": "v"},
            "a": [3, 2, 1],
            "z": 1,
        }
        h1 = evidence_hash(e1)
        h2 = evidence_hash(e2)
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 16)

    def test_deadline_budget_exhaustion(self):
        """When deadline is in the past, collect_evidence skips sub-collectors gracefully."""
        past_deadline = time.monotonic() - 10.0
        evidence = collect_evidence(
            project={"repo_path": str(self.repo_dir)},
            snapshot={"project_id": "p1"},
            runtime_root=self.runtime_dir,
            deadline=past_deadline,
        )
        self.assertEqual(evidence["repository_truth"]["status"], "skipped_deadline")
        self.assertEqual(evidence["process_liveness"]["status"], "skipped_deadline")

    def test_static_api_guards_against_destructive_calls(self):
        """Static analysis: watchdog.py and diagnostics.py must NOT import or call process-killing or git-write APIs."""
        code_dir = Path(__file__).resolve().parent.parent / "src" / "dev_orchestrator" / "core"
        watchdog_code = (code_dir / "watchdog.py").read_text(encoding="utf-8")
        diagnostics_code = (code_dir / "diagnostics.py").read_text(encoding="utf-8")

        forbidden_names = [
            "terminate_pid",
            "terminate_process_tree",
            "git commit",
            "git push",
            "git reset",
            "git checkout",
            "git revert",
            "git merge",
            "git rebase",
        ]

        for code, fname in [(watchdog_code, "watchdog.py"), (diagnostics_code, "diagnostics.py")]:
            for forbidden in forbidden_names:
                self.assertNotIn(
                    forbidden,
                    code,
                    msg=f"Forbidden API/command {forbidden!r} found in {fname}",
                )


if __name__ == "__main__":
    unittest.main()
