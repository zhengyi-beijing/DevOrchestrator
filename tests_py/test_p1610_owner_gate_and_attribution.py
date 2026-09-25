import json
import shutil
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.incidents.attribution import classify_owner_action
from dev_orchestrator.incidents.owner_gate import resolve_owner_gate_authority
from dev_orchestrator.control.owner_store import OwnerControlStore
from dev_orchestrator.control.surface import latest_owner_gate, project_control_view, project_identity
from dev_orchestrator.core.control_commands import (
    ControlCommandCoordinator,
    latest_control_result,
    submit_control_command,
)


class TestOwnerGateAndAttribution(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir) / "runtime"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.project_id = "test-proj"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_classify_explicit_stop(self):
        self.assertEqual(classify_owner_action("stop", {}, {}, {}), "EXPLICIT_STOP")
        self.assertEqual(classify_owner_action("pause", {}, {}, {}), "EXPLICIT_STOP")

    def test_classify_control_only(self):
        before = {
            "lifecycle_state": "REVIEW_FAILED",
            "git": {"head": "abc1234", "branch": "main"},
            "gate_id": "gate-1",
        }
        cmd_record = {
            "action": "continue",
            "parameters": {},
            "expected": {"head": "abc1234", "branch": "main"},
            "target": {"gate_id": "gate-1"},
        }
        after = {
            "lifecycle_state": "EXECUTING",
            "worker_running": True,
        }
        classification = classify_owner_action("continue", cmd_record, before, after)
        self.assertEqual(classification, "CONTROL_ONLY")

    def test_classify_new_information(self):
        before = {
            "lifecycle_state": "REVIEW_FAILED",
            "git": {"head": "abc1234", "branch": "main"},
        }
        # With new parameters
        cmd_record_param = {
            "action": "continue",
            "parameters": {"extra_prompt": "remedy logic"},
        }
        self.assertEqual(classify_owner_action("continue", cmd_record_param, before, {}), "NEW_INFORMATION")

        # With changed HEAD
        cmd_record_head = {
            "action": "retry",
            "expected": {"head": "def5678"},
        }
        self.assertEqual(classify_owner_action("retry", cmd_record_head, before, {}), "NEW_INFORMATION")

    def test_resolve_owner_gate_authority_no_gate(self):
        snapshot = {"project_id": self.project_id, "state": "EXECUTING"}
        res = resolve_owner_gate_authority(self.runtime_root, snapshot)
        self.assertTrue(res["resolved"])
        self.assertFalse(res["pending"])
        self.assertIsNone(res["gate_id"])
        self.assertFalse(res["paused"])

    def test_resolve_owner_gate_authority_paused(self):
        store = OwnerControlStore(self.runtime_root)
        store.set_paused(self.project_id, True, command_id="cmd-pause-0", action="pause")

        snapshot = {"project_id": self.project_id, "state": "EXECUTING"}
        res = resolve_owner_gate_authority(self.runtime_root, snapshot)
        self.assertTrue(res["resolved"])
        self.assertTrue(res["pending"])
        self.assertTrue(res["paused"])

    def test_resolve_owner_gate_authority_with_gate(self):
        gate_data = {
            "projects": {
                self.project_id: {
                    "owner_gate": {
                        "gate_id": "gate-xyz-123",
                        "gate_source": "watchdog",
                        "created_at": "2026-09-25T00:00:00Z",
                    }
                }
            }
        }
        (self.runtime_root / "watchdog.json").write_text(json.dumps(gate_data), encoding="utf-8")

        snapshot = {"project_id": self.project_id, "state": "EXECUTING"}
        res = resolve_owner_gate_authority(self.runtime_root, snapshot)
        self.assertTrue(res["resolved"])
        self.assertTrue(res["pending"])
        self.assertEqual(res["gate_id"], "gate-xyz-123")
        self.assertEqual(res["gate_source"], "watchdog")

    def test_resolve_owner_gate_missing_project(self):
        res = resolve_owner_gate_authority(self.runtime_root, {})
        self.assertFalse(res["resolved"])
        self.assertTrue(res["pending"])
        self.assertEqual(res["reason"], "missing_project_id")

    def test_pre_command_identity_persistence(self):
        from dev_orchestrator.control.surface import project_identity
        coord = ControlCommandCoordinator(self.runtime_root)
        config_path = self.runtime_root / "projects.json"
        config_path.write_text(json.dumps({"projects": [{
            "project_id": self.project_id,
            "repo_path": str(self.temp_dir),
            "adapter": "agent_files",
        }]}), encoding="utf-8")

        snapshot = {"project_id": self.project_id, "state": "EXECUTING"}
        expected = project_identity(snapshot, self.runtime_root)
        submit_control_command(
            runtime_root=self.runtime_root,
            project_id=self.project_id,
            action="pause",
            command_id="cmd-pause-1",
            expected=expected,
        )
        summary = {"projects": [snapshot]}
        results = coord.advance(config_path, summary, executor=None)
        self.assertEqual(len(results), 1)
        res = results[0]
        self.assertIn("pre_command_identity", res)
        self.assertEqual(res["pre_command_identity"].get("project_id"), self.project_id)

        # Check latest_control_result also has pre_command_identity
        latest = latest_control_result(self.runtime_root, self.project_id)
        self.assertIsNotNone(latest)
        self.assertIn("pre_command_identity", latest)
        self.assertEqual(latest["pre_command_identity"].get("project_id"), self.project_id)

    def test_classify_unconfirmed_progress_when_after_is_empty_or_shows_no_progress(self):
        before = {
            "lifecycle_state": "REVIEW_FAILED",
            "git": {"head": "abc1234", "branch": "main"},
            "gate_id": "gate-1",
        }
        cmd_record = {
            "action": "continue",
            "parameters": {},
            "expected": {"head": "abc1234", "branch": "main"},
            "target": {"gate_id": "gate-1"},
        }
        # 1. Empty after
        self.assertEqual(classify_owner_action("continue", cmd_record, before, {}), "UNCONFIRMED_PROGRESS")

        # 2. None after
        self.assertEqual(classify_owner_action("continue", cmd_record, before, None), "UNCONFIRMED_PROGRESS")

        # 3. Unchanged state without proven worker/progress
        stale_after = {
            "lifecycle_state": "REVIEW_FAILED",
            "worker_running": False,
        }
        self.assertEqual(classify_owner_action("continue", cmd_record, before, stale_after), "UNCONFIRMED_PROGRESS")

    def test_reviewer_owner_gate_recognized(self):
        rev_data = {
            "reviews": {
                "rev-1": {
                    "review_id": "rev-1",
                    "project_id": self.project_id,
                    "state": "completed",
                    "decision": "owner_gate",
                    "started_at": "2026-09-25T10:00:00Z",
                    "completed_at": "2026-09-25T10:05:00Z",
                }
            }
        }
        (self.runtime_root / "ai-reviewer.json").write_text(json.dumps(rev_data), encoding="utf-8")
        gate = latest_owner_gate(self.runtime_root, self.project_id)
        self.assertIsNotNone(gate)
        self.assertEqual(gate.get("gate_source"), "reviewer")
        self.assertEqual(gate.get("review_id"), "rev-1")

        snapshot = {"project_id": self.project_id, "state": "WAITING_REVIEW"}
        view = project_control_view(snapshot, self.runtime_root)
        self.assertEqual(view.get("lifecycle_state"), "OWNER_GATE")
        self.assertEqual(view["control_identity"].get("gate_id"), "rev-1")
        self.assertEqual(view["control_identity"].get("lifecycle_state"), "OWNER_GATE")

    def test_reviewer_owner_gate_not_sticky_after_later_next_review(self):
        rev_data = {
            "reviews": {
                "rev-1": {
                    "review_id": "rev-1",
                    "project_id": self.project_id,
                    "state": "completed",
                    "decision": "owner_gate",
                    "started_at": "2026-09-25T10:00:00Z",
                    "completed_at": "2026-09-25T10:05:00Z",
                },
                "rev-2": {
                    "review_id": "rev-2",
                    "project_id": self.project_id,
                    "state": "completed",
                    "decision": "next",
                    "started_at": "2026-09-25T11:00:00Z",
                    "completed_at": "2026-09-25T11:05:00Z",
                },
            }
        }
        (self.runtime_root / "ai-reviewer.json").write_text(json.dumps(rev_data), encoding="utf-8")
        gate = latest_owner_gate(self.runtime_root, self.project_id)
        self.assertIsNone(gate)

        snapshot = {"project_id": self.project_id, "state": "READY_TO_RUN"}
        view = project_control_view(snapshot, self.runtime_root)
        self.assertNotEqual(view.get("lifecycle_state"), "OWNER_GATE")
        self.assertIsNone(view["control_identity"].get("gate_id"))
        self.assertNotEqual(view["control_identity"].get("lifecycle_state"), "OWNER_GATE")

    def test_reviewer_owner_gate_not_sticky_when_consumed_by_rereview(self):
        rev_data = {
            "reviews": {
                "rev-1": {
                    "review_id": "rev-1",
                    "project_id": self.project_id,
                    "state": "completed",
                    "decision": "owner_gate",
                    "started_at": "2026-09-25T10:00:00Z",
                    "completed_at": "2026-09-25T10:05:00Z",
                },
                "rev-2": {
                    "review_id": "rev-2",
                    "project_id": self.project_id,
                    "state": "running",
                    "rereview_of": "rev-1",
                    "started_at": "2026-09-25T11:00:00Z",
                },
            }
        }
        (self.runtime_root / "ai-reviewer.json").write_text(json.dumps(rev_data), encoding="utf-8")
        gate = latest_owner_gate(self.runtime_root, self.project_id)
        self.assertIsNone(gate)

        snapshot = {"project_id": self.project_id, "state": "WAITING_REVIEW"}
        view = project_control_view(snapshot, self.runtime_root)
        self.assertNotEqual(view.get("lifecycle_state"), "OWNER_GATE")

    def test_reviewer_owner_gate_scoped_out_for_executing_and_terminal(self):
        rev_data = {
            "reviews": {
                "rev-1": {
                    "review_id": "rev-1",
                    "project_id": self.project_id,
                    "state": "completed",
                    "decision": "owner_gate",
                    "started_at": "2026-09-25T10:00:00Z",
                    "completed_at": "2026-09-25T10:05:00Z",
                }
            }
        }
        (self.runtime_root / "ai-reviewer.json").write_text(json.dumps(rev_data), encoding="utf-8")
        # 1. Executing snapshot
        exec_snapshot = {"project_id": self.project_id, "state": "WORKER_RUNNING", "lifecycle_state": "EXECUTING"}
        view_exec = project_control_view(exec_snapshot, self.runtime_root)
        self.assertEqual(view_exec.get("lifecycle_state"), "EXECUTING")

        # 2. Terminal snapshot
        term_snapshot = {"project_id": self.project_id, "state": "DONE", "lifecycle_state": "DONE", "next_status": "DONE"}
        view_term = project_control_view(term_snapshot, self.runtime_root)
        self.assertEqual(view_term.get("lifecycle_state"), "DONE")


if __name__ == "__main__":
    unittest.main()
