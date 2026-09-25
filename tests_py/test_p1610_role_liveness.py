import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from dev_orchestrator.incidents.liveness import resolve_role_liveness
from dev_orchestrator.core.ai_planner import AIPlannerCoordinator
from dev_orchestrator.core.ai_reviewer import AIReviewerCoordinator


class TestRoleLiveness(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir) / "runtime"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.project_id = "proj-test"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _setup_idle_ledgers(self):
        (self.runtime_root / "ai-planner.json").write_text(
            json.dumps({"plans": {}}), encoding="utf-8"
        )
        (self.runtime_root / "ai-reviewer.json").write_text(
            json.dumps({"reviews": {}}), encoding="utf-8"
        )

    def test_all_roles_dead(self):
        self._setup_idle_ledgers()
        fake_planner = MagicMock()
        fake_planner.has_live_role.return_value = False
        fake_reviewer = MagicMock()
        fake_reviewer.has_live_role.return_value = False

        res = resolve_role_liveness(
            runtime_root=self.runtime_root,
            project_id=self.project_id,
            snapshot={"worker": {"state": "idle", "pid": None}},
            planner=fake_planner,
            reviewer=fake_reviewer,
        )

        self.assertFalse(res["worker"]["alive"])
        self.assertFalse(res["planner"]["alive"])
        self.assertFalse(res["reviewer"]["alive"])
        self.assertTrue(res["all_dead"])
        self.assertFalse(res["any_unknown"])
        self.assertFalse(res["any_alive"])

    def test_worker_alive_blocks_all_dead(self):
        self._setup_idle_ledgers()
        fake_planner = MagicMock()
        fake_planner.has_live_role.return_value = False
        fake_reviewer = MagicMock()
        fake_reviewer.has_live_role.return_value = False

        # Live worker via lineage file with current process PID
        lineage_data = {
            "records": {
                "rec-1": {
                    "project_id": self.project_id,
                    "lifecycle_phase": "running",
                    "pid": os.getpid(),
                }
            }
        }
        (self.runtime_root / "execution-lineage.json").write_text(
            json.dumps(lineage_data), encoding="utf-8"
        )

        res = resolve_role_liveness(
            runtime_root=self.runtime_root,
            project_id=self.project_id,
            snapshot={},
            planner=fake_planner,
            reviewer=fake_reviewer,
        )

        self.assertTrue(res["worker"]["alive"])
        self.assertFalse(res["all_dead"])
        self.assertTrue(res["any_alive"])

    def test_planner_thread_alive_blocks_all_dead(self):
        self._setup_idle_ledgers()
        fake_planner = MagicMock()
        fake_planner.has_live_role.return_value = True
        fake_reviewer = MagicMock()
        fake_reviewer.has_live_role.return_value = False

        res = resolve_role_liveness(
            runtime_root=self.runtime_root,
            project_id=self.project_id,
            snapshot={},
            planner=fake_planner,
            reviewer=fake_reviewer,
        )

        self.assertTrue(res["planner"]["alive"])
        self.assertFalse(res["all_dead"])
        self.assertTrue(res["any_alive"])

    def test_reviewer_thread_alive_blocks_all_dead(self):
        self._setup_idle_ledgers()
        fake_planner = MagicMock()
        fake_planner.has_live_role.return_value = False
        fake_reviewer = MagicMock()
        fake_reviewer.has_live_role.return_value = True

        res = resolve_role_liveness(
            runtime_root=self.runtime_root,
            project_id=self.project_id,
            snapshot={},
            planner=fake_planner,
            reviewer=fake_reviewer,
        )

        self.assertTrue(res["reviewer"]["alive"])
        self.assertFalse(res["all_dead"])
        self.assertTrue(res["any_alive"])

    def test_unknown_role_fails_closed(self):
        # Ledgers missing, no coordinator provided
        res = resolve_role_liveness(
            runtime_root=self.runtime_root,
            project_id=self.project_id,
            snapshot={},
            planner=None,
            reviewer=None,
        )

        self.assertIsNone(res["planner"]["alive"])
        self.assertIsNone(res["reviewer"]["alive"])
        self.assertTrue(res["any_unknown"])
        self.assertFalse(res["all_dead"])

    def test_corrupt_ledger_yields_unknown(self):
        (self.runtime_root / "ai-planner.json").write_text("INVALID_JSON{", encoding="utf-8")
        fake_planner = MagicMock()
        fake_planner.has_live_role.return_value = False
        fake_reviewer = MagicMock()
        fake_reviewer.has_live_role.return_value = False

        res = resolve_role_liveness(
            runtime_root=self.runtime_root,
            project_id=self.project_id,
            snapshot={},
            planner=fake_planner,
            reviewer=fake_reviewer,
        )

        self.assertIsNone(res["planner"]["alive"])
        self.assertTrue(res["any_unknown"])
        self.assertFalse(res["all_dead"])

    def test_coordinator_methods(self):
        # Test real coordinator class has_live_role
        config_path = self.runtime_root / "projects.json"
        config_path.write_text(json.dumps({"projects": []}), encoding="utf-8")
        planner = AIPlannerCoordinator(self.runtime_root, config_path)
        self.assertFalse(planner.has_live_role("non-existent"))

        reviewer = AIReviewerCoordinator(self.runtime_root, config_path)
        self.assertFalse(reviewer.has_live_role("non-existent"))

    def test_idle_ledgers_with_none_coordinators_yields_unknown(self):
        # Even if ledger files exist and are idle, missing coordinators must yield unknown
        self._setup_idle_ledgers()
        res = resolve_role_liveness(
            runtime_root=self.runtime_root,
            project_id=self.project_id,
            snapshot={},
            planner=None,
            reviewer=None,
        )

        self.assertIsNone(res["planner"]["alive"])
        self.assertEqual(res["planner"]["state"], "unknown")
        self.assertIsNone(res["reviewer"]["alive"])
        self.assertEqual(res["reviewer"]["state"], "unknown")
        self.assertTrue(res["any_unknown"])
        self.assertFalse(res["all_dead"])


if __name__ == "__main__":
    unittest.main()
