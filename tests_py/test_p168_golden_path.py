"""Golden-Path lifecycle tests for P16.8.

Verifies end-to-end task progression from PENDING_DESIGN to DONE from one continue command,
structured readiness authority over Markdown, terminal closure enforcement, and activity telemetry.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.control.surface import project_control_view, project_identity
from dev_orchestrator.core.activity_telemetry import resolve_activity_telemetry
from dev_orchestrator.core.ai_planner import PlannerProtocolError, _validate_plan_schema
from dev_orchestrator.core.control_commands import submit_control_command
from dev_orchestrator.core.execution_context import (
    get_context,
    load_execution_contexts,
    resolve_next_action,
)
from dev_orchestrator.core.execution_intent import (
    get_active_intent,
    record_or_refresh_intent,
)
from dev_orchestrator.core.readiness import (
    resolve_readiness,
    resolve_task_state,
    write_task_execution_state,
)
from dev_orchestrator.core.task_status import (
    parse_task_status,
    render_status_line,
    require_single_status_line,
)
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json
from tests_py.golden_path_harness import GoldenPathHarness


class TestP168GoldenPath(unittest.TestCase):
    """End-to-end and component tests for P16.8 Golden Path."""

    def test_structured_readiness_precedence_rules(self):
        """Verify the 3 strict precedence rules of resolve_task_state."""
        with tempfile.TemporaryDirectory() as td:
            harness = GoldenPathHarness.create(Path(td), task_id="P1", initial_status="PENDING DESIGN")
            repo = harness.repo_path

            # Rule 1: Valid structured readiness takes authority even if Markdown disagrees
            write_task_execution_state(repo, state="ready_to_run", task_id="P1", head="abc", source="planner")
            res1 = resolve_task_state(
                {"project_id": harness.project_id},
                {"next_status": "Status: **PENDING DESIGN**"},
                repo,
                current_task_id="P1",
            )
            self.assertTrue(res1.valid)
            self.assertTrue(res1.is_ready_to_run())
            self.assertEqual(res1.source, "structured")
            self.assertEqual(res1.consistency, "mismatch")

            # Rule 2: Absent structured readiness falls back to Markdown
            state_file = repo / "agent" / "execution-state.json"
            if state_file.exists():
                state_file.unlink()
            res2 = resolve_task_state(
                {"project_id": harness.project_id},
                {"next_status": "Status: **PENDING DESIGN**"},
                repo,
                current_task_id="P1",
            )
            self.assertTrue(res2.valid)
            self.assertTrue(res2.is_pending_design())
            self.assertEqual(res2.source, "markdown_fallback")

            # Rule 3: Corrupt or invalid structured readiness fails closed
            state_file.write_text("{invalid json", encoding="utf-8")
            res3 = resolve_task_state(
                {"project_id": harness.project_id},
                {"next_status": "Status: **READY_TO_RUN**"},
                repo,
                current_task_id="P1",
            )
            self.assertFalse(res3.valid)
            self.assertTrue(res3.is_blocked())
            self.assertEqual(res3.code, "READINESS_SCHEMA_INVALID")

    def test_actionable_artifact_error_payload(self):
        """Verify artifact violation error specifies field, expected, actual, and correction."""
        oversized_list = [f"item_{i}" for i in range(25)]
        plan_doc = {
            "task_id": "P1",
            "summary": "Test Plan",
            "implementation_steps": oversized_list,
            "interfaces": ["intf1"],
            "validation": ["val1"],
            "risks": ["risk1"],
            "out_of_scope": ["scope1"],
        }
        with self.assertRaises(PlannerProtocolError) as ctx:
            _validate_plan_schema(plan_doc)

        err = ctx.exception
        self.assertEqual(err.field, "implementation_steps")
        self.assertEqual(err.expected, "at most 24 items")
        self.assertEqual(err.actual, "25 items")
        self.assertIn("exceeding the limit of 24", err.actionable_message)
        self.assertIn("combine or remove 1 item(s)", err.correction)

    def test_golden_path_lifecycle_progression_and_terminal_closure(self):
        """Full lifecycle: PENDING_DESIGN -> READY_TO_RUN -> EXECUTING -> WAITING_REVIEW -> DONE -> terminal closure."""
        with tempfile.TemporaryDirectory() as td:
            harness = GoldenPathHarness.create(Path(td), task_id="P1", initial_status="PENDING DESIGN")

            # 1. Start from PENDING_DESIGN: issue owner continue
            hist = harness.submit_continue(command_id="cmd-start-1")
            self.assertEqual(hist.get("state"), "accepted")
            self.assertEqual(hist.get("lifecycle_action"), "plan")

            # 2. Simulate planner approving plan -> READY_TO_RUN
            harness.apply_approved_plan()
            snap = harness.current_snapshot()
            self.assertEqual(snap.get("state"), "READY_TO_RUN")
            self.assertTrue(parse_task_status(snap.get("next_status")).is_ready_to_run())

            # 3. Next supervisor tick advances to worker launch
            outcomes = harness.supervisor.advance(harness.config_path, {"projects": [snap]}, executor=harness.executor)
            self.assertTrue(any(o.get("status") == "transition_submitted" for o in outcomes))

            # 4. Worker executes and completes -> WAITING_REVIEW
            harness.complete_worker_run()

            # 5. Reviewer accepts task -> DONE
            harness.apply_review_acceptance()
            snap_done = harness.current_snapshot()
            parsed_done = parse_task_status(snap_done.get("next_status"))
            self.assertTrue(parsed_done.is_completed())

            # 6. Verify Terminal Closure on Control Surface
            surface = project_control_view(
                snap_done,
                harness.runtime_path,
                project_config={"project_id": harness.project_id, "ai_roles": {"reviewer": {"enabled": True}}},
            )
            ctrl_map = {c["action"]: c for c in surface["controls"]}

            for action in ["continue", "retry", "rereview", "reconcile"]:
                self.assertFalse(ctrl_map[action]["available"], f"{action} should be unavailable on completed task")
                self.assertIn("current task is terminal; stale review/continue is audit history only", ctrl_map[action]["reason"])

            # 7. Verify Control Commands rejects continue on terminal task
            identity = project_identity(snap_done, harness.runtime_path)
            submit_control_command(
                harness.runtime_path,
                project_id=harness.project_id,
                action="continue",
                command_id="cmd-stale-cont",
                expected=identity,
                source="owner",
            )
            harness.coordinator.advance(harness.config_path, {"projects": [snap_done]}, harness.executor)
            stale_hist = read_json(harness.runtime_path / "control" / "history" / "cmd-stale-cont.json", {})
            self.assertEqual(stale_hist.get("state"), "blocked")
            self.assertIn("current task is terminal; stale review/continue is audit history only", stale_hist.get("reason", ""))

    def test_activity_telemetry_schema(self):
        """Verify additive activity telemetry object produces all 9 required fields."""
        with tempfile.TemporaryDirectory() as td:
            harness = GoldenPathHarness.create(Path(td), task_id="P1", initial_status="PENDING DESIGN")
            snap = harness.current_snapshot()

            activity = resolve_activity_telemetry(snap, harness.runtime_path, project_id=harness.project_id)
            required_fields = {
                "stage",
                "active_role",
                "worker_state",
                "code_execution",
                "orchestrator_activity",
                "next_action",
                "intent_state",
                "disposition",
                "task_state_source",
            }
            self.assertTrue(required_fields.issubset(set(activity.keys())))
            self.assertFalse(activity["code_execution"])
            self.assertIn(activity["stage"], {"idle", "planning", "executing", "completed"})
            self.assertEqual(activity["task_state_source"], "markdown_fallback")


if __name__ == "__main__":
    unittest.main()
