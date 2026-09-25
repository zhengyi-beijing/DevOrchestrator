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

from dev_orchestrator.core.diagnostics import classify_evidence
from dev_orchestrator.core.project_status import build_project_status, project_runtime_status
from dev_orchestrator.core.watchdog import WatchdogCoordinator, StallAssessment
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
                        "heartbeat_timeout_seconds": 60,
                        "progress_timeout_seconds": 120,
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

        # 4. Diagnostics evidence classification
        evidence = {
            "all_roles_dead": True,
            "stale_task_evidence": True,
            "legal_wait": False,
            "no_owner_gate": True,
            "heartbeat_alive": True,
            "orchestrator_alive_task_stalled": True,
        }
        from types import SimpleNamespace
        assessment = SimpleNamespace(
            lifecycle_state="REVIEW_FAILED",
            stall_classification="orchestrator_alive_task_stalled",
        )
        diag = classify_evidence(evidence, assessment)
        self.assertEqual(diag.code, "orchestrator_alive_task_stalled")

        # 5. Project runtime status builds truthful status
        status = build_project_status(
            snapshot,
            self.runtime_root,
            phase="running",
            daemon_state="running",
            pid=os.getpid(),
        )
        self.assertTrue(status.get("system_alive"))
        self.assertFalse(status.get("task_active"))
        self.assertFalse(status.get("task_progressing"))
        self.assertIn("incident_metrics", status)
        self.assertEqual(status["incident_metrics"]["target_control_only_interventions"], 0)

        # 6. Failure harvesting tick captures the stalled incident
        summary = {"projects": [snapshot]}
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
