import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from datetime import datetime, timezone
from dev_orchestrator.core.diagnostics import classify_evidence
from dev_orchestrator.core.project_status import build_project_status, project_runtime_status
from dev_orchestrator.core.watchdog import WatchdogCoordinator, StallAssessment, evaluate_stall
from dev_orchestrator.incidents.harvesting import harvest_tick
from dev_orchestrator.incidents.store import load_incident_store
from dev_orchestrator.incidents.candidate import materialize_candidate, generate_candidate


class TestFalseRunningRegression(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir) / "runtime"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.repo = Path(self.temp_dir) / "repo"
        self.repo.mkdir(parents=True, exist_ok=True)

        subprocess.run(["git", "init", str(self.repo)], check=True, capture_output=True)
        (self.repo / "README.md").write_text("# Test Repo\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Tester"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "tester@test.com"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "init"], check=True, capture_output=True)
        proc = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
        self.head = proc.stdout.strip()

        self.project_id = "p-stalled"
        self.config_path = self.runtime_root / "projects.json"
        self.config_data = {
            "projects": [
                {
                    "project_id": self.project_id,
                    "repo_path": str(self.repo),
                    "watchdog": {
                        "enabled": True,
                        "auto_recovery": True,
                        "no_progress_threshold_minutes": 1,
                        "diagnostic_timeout_seconds": 30,
                    }
                }
            ]
        }
        self.config_path.write_text(json.dumps(self.config_data), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_reproduce_2026_09_25_false_running_case(self):
        """Reproduce the 2026-09-25 false-running case:
        Daemon heartbeat remains healthy, lifecycle is REVIEW_FAILED, all roles are dead,
        task activity is stale.
        Watchdog must classify orchestrator_alive_task_stalled, project status must report
        system_alive=True while task_active=False and task_progressing=False, and harvest_tick
        captures an incident family.
        """
        # 1. Daemon and monitor heartbeats are healthy (SYSTEM_ALIVE)
        (self.runtime_root / "daemon.json").write_text(
            json.dumps({"pid": os.getpid(), "heartbeat_at": "2026-09-25T10:00:00Z"}), encoding="utf-8"
        )
        (self.runtime_root / "monitor.json").write_text(
            json.dumps({"pid": os.getpid(), "heartbeat_at": "2026-09-25T10:00:00Z"}), encoding="utf-8"
        )

        # 2. Project snapshot in REVIEW_FAILED with stale task evidence and dead worker
        import hashlib
        from dev_orchestrator.core.watchdog import canonical_path
        repo_fp = hashlib.sha256(canonical_path(str(self.repo)).encode("utf-8")).hexdigest()[:16]
        snapshot = {
            "project_id": self.project_id,
            "lifecycle_state": "REVIEW_FAILED",
            "state": "REVIEW_FAILED",
            "repo_path": str(self.repo),
            "worker": {"state": "idle", "pid": None, "process_alive": False},
            "activity": {
                "task_active": False,
                "task_progressing": False,
                "last_task_activity_at": "2026-09-25T08:00:00Z",
                "last_meaningful_progress_at": "2026-09-25T08:00:00Z",
                "watchdog_safe": {
                    "repo_scope": "canonical",
                    "repo_root_fingerprint": repo_fp,
                    "last_activity_at": "2026-09-25T08:00:00Z",
                },
            },
            "git": {"head": self.head, "branch": "main", "dirty": False},
            "watchdog": {},
        }

        # 3. All roles are provably dead
        (self.runtime_root / "ai-planner.json").write_text(json.dumps({"plans": {}}), encoding="utf-8")
        (self.runtime_root / "ai-reviewer.json").write_text(json.dumps({"reviews": {}}), encoding="utf-8")

        fake_planner = MagicMock()
        fake_planner.has_live_role.return_value = False
        fake_reviewer = MagicMock()
        fake_reviewer.has_live_role.return_value = False

        # 4. Exercise evaluate_stall directly (not mocked)
        policy = {
            "enabled": True,
            "auto_recovery": True,
            "heartbeat_timeout_seconds": 60,
            "progress_timeout_seconds": 120,
            "threshold_seconds": 120,
            "diagnostic_timeout_seconds": 30,
            "max_attempts_per_run": 3,
        }
        signals = (
            "2026-09-25T08:00:00Z",
            "prog-fp-1",
            {
                "activity_evidence": "available",
                "activity_evidence_reason": None,
                "git_head": self.head,
                "sources": {"agent_file": {"last_activity_at": "2026-09-25T08:00:00Z"}},
                "last_task_activity_at": "2026-09-25T08:00:00Z",
            },
        )
        eval_now = datetime.fromisoformat("2026-09-25T10:00:00+00:00")
        assessment = evaluate_stall(
            snapshot,
            policy,
            signals,
            now=eval_now,
            executor_state={"executions": {}},
            runtime_root=self.runtime_root,
            planner=fake_planner,
            reviewer=fake_reviewer,
        )
        self.assertTrue(assessment.breached)
        self.assertEqual(assessment.stall_classification, "orchestrator_alive_task_stalled")
        self.assertTrue(assessment.role_liveness["all_dead"])
        self.assertFalse(assessment.role_liveness["any_unknown"])

        # 5. Exercise WatchdogCoordinator.advance end-to-end
        wd = WatchdogCoordinator(
            self.runtime_root,
            planner=fake_planner,
            reviewer=fake_reviewer,
        )
        fake_exec = MagicMock()
        fake_exec.state.return_value = {"executions": {}}
        summary = {"projects": [snapshot]}
        res1 = wd.advance(self.config_path, summary, executor=fake_exec, now=eval_now)
        self.assertEqual(res1[0].get("status"), "attempt_started")
        if self.project_id in wd._threads:
            wd._threads[self.project_id].join(timeout=5.0)

        eval_now2 = datetime.fromisoformat("2026-09-25T10:00:01+00:00")
        res2 = wd.advance(self.config_path, summary, executor=fake_exec, now=eval_now2)
        prow = wd.project_state(self.project_id)
        self.assertIsNotNone(prow.get("stall"))
        attempts = list(prow.get("attempts", {}).values())
        self.assertTrue(any(a.get("diagnosis") == "orchestrator_alive_task_stalled" for a in attempts))
        self.assertTrue(any(a.get("recovery") is not None for a in attempts))

        # 6. Project runtime status builds truthful status and remaps RUNNING to STALLED
        running_snapshot = dict(snapshot)
        running_snapshot["lifecycle_state"] = "RUNNING"
        running_snapshot["state"] = "RUNNING"
        status = build_project_status(
            running_snapshot,
            self.runtime_root,
            phase="running",
            daemon_state="running",
            pid=os.getpid(),
        )
        self.assertEqual(status.get("status"), "STALLED")
        self.assertTrue(status.get("system_alive"))
        self.assertFalse(status.get("task_active"))
        self.assertFalse(status.get("task_progressing"))
        self.assertIn("incident_metrics", status)
        self.assertEqual(status["incident_metrics"]["target_control_only_interventions"], 0)

        # 7. Failure harvesting tick captures the stalled incident
        captured = harvest_tick(
            self.runtime_root,
            config=self.config_data,
            summary=summary,
            planner=fake_planner,
            reviewer=fake_reviewer,
        )
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["classification"], "ORCHESTRATOR_ALIVE_TASK_STALLED")

    def test_candidate_test_collection_isolation(self):
        """Verify candidate tests in tests_candidate/ are ignored by default pytest collection,
        and only collected when DEVORCH_CANDIDATE_TESTS=1.
        """
        # Materialize a candidate into a staging directory
        cand_id = "cand-isolation-test"
        generate_candidate(self.runtime_root, candidate_id=cand_id)

        # Owner repo setup
        owner_config = {
            "projects": [
                {
                    "project_id": "dev_orchestrator",
                    "repo_path": str(self.repo),
                    "regression_owner": True,
                }
            ]
        }
        (self.repo / "pyproject.toml").write_text('[project]\nname = "dev_orchestrator"\n', encoding="utf-8")
        src_dir = self.repo / "src" / "dev_orchestrator"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "__init__.py").write_text("", encoding="utf-8")
        (self.repo / "tests_py").mkdir(parents=True, exist_ok=True)
        (self.repo / "tests_py" / "__init__.py").write_text("", encoding="utf-8")
        (self.repo / "tests_py" / "test_standard.py").write_text(
            "def test_always_pass(): assert True\n", encoding="utf-8"
        )

        owner_config_path = self.runtime_root / "owner_projects.json"
        owner_config_path.write_text(json.dumps(owner_config), encoding="utf-8")

        mat_res = materialize_candidate(self.runtime_root, cand_id, config_path=owner_config_path)
        self.assertTrue(mat_res["materialized"])

        # 1. Normal pytest collection inside repo without DEVORCH_CANDIDATE_TESTS
        env_normal = dict(os.environ)
        env_normal.pop("DEVORCH_CANDIDATE_TESTS", None)
        proc_normal = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q"],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            env=env_normal,
        )
        # Should collect test_standard.py but ZERO from tests_candidate
        self.assertIn("test_standard.py", proc_normal.stdout)
        self.assertNotIn("tests_candidate", proc_normal.stdout)

        # 2. Pytest collection with DEVORCH_CANDIDATE_TESTS=1
        env_cand = dict(os.environ)
        env_cand["DEVORCH_CANDIDATE_TESTS"] = "1"
        proc_cand = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q"],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            env=env_cand,
        )
        self.assertIn("tests_candidate", proc_cand.stdout)


if __name__ == "__main__":
    unittest.main()
