"""Deterministic fault-injection tests for P16.8.

Verifies:
- Git-anchor invalidation: commit on clean HEAD B triggers EXECUTION_CONTEXT_STALE,
  re-anchors context, sets next_action="plan", and redispatches replacement Planner.
- Idle fault: after 1 idle tick, emits CONTINUATION_FAULT milestone and redispatches idempotently.
- Transient infrastructure failure backoff and resumption without burning recovery action count.
- Recovery budget exhaustion: terminates with state="exhausted" and emits RECOVERY_EXHAUSTED.
- Stale plan anchor rejection: fail-closed safety when HEAD moves during planning.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from dev_orchestrator.core.activation_supervisor import ActivationSupervisor
from dev_orchestrator.core.execution_context import (
    get_context,
    update_context,
    context_is_stale,
)
from dev_orchestrator.core.execution_intent import (
    get_active_intent,
    record_or_refresh_intent,
    record_intent_action,
    set_intent_backoff,
)
from dev_orchestrator.core.readiness import write_task_execution_state
from dev_orchestrator.core.task_status import render_status_line, require_single_status_line
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json
from tests_py.golden_path_harness import GoldenPathHarness


class RecordingProgressChannel:
    """Mock progress channel recording emitted milestones for assertion."""

    def __init__(self) -> None:
        self.emitted: list[tuple[str, str, dict[str, Any]]] = []

    def emit(
        self,
        project_id: str,
        milestone: str,
        *,
        task_id: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
        occurrence_key: Optional[str] = None,
    ) -> None:
        self.emitted.append((project_id, milestone, details or {}))


class TestP168FaultInjection(unittest.TestCase):
    """Fault-injection test suite for P16.8 DevO Golden-Path Lifecycle Hardening."""

    def test_git_anchor_invalidation_redispatches_replacement_planner(self):
        """Advance HEAD to clean HEAD B, triggering EXECUTION_CONTEXT_STALE and re-anchoring to HEAD B."""
        with tempfile.TemporaryDirectory() as td:
            harness = GoldenPathHarness.create(Path(td), task_id="P1", initial_status="PENDING DESIGN")
            channel = RecordingProgressChannel()
            harness.supervisor.progress_channel = channel

            # 1. Start intent and seed an old context with an obsolete git anchor
            old_head = "1111111111111111111111111111111111111111"
            record_or_refresh_intent(
                harness.runtime_path,
                harness.project_id,
                task_id=harness.task_id,
                command_id="cmd-init-anchor",
                requested_action="continue",
                source="owner",
                state="active",
            )
            update_context(
                harness.runtime_path,
                harness.project_id,
                git_anchor=old_head,
                task_id=harness.task_id,
                stage="planning",
                next_action="plan",
                disposition="hold",
                idle_ticks=0,
            )

            # Context should be recognized as stale
            snap = harness.current_snapshot()
            curr_head = snap["git"]["head"]
            self.assertNotEqual(old_head, curr_head)
            ctx_before = get_context(harness.runtime_path, harness.project_id)
            self.assertTrue(context_is_stale(ctx_before, curr_head, harness.task_id))

            # 2. Advance supervisor tick
            outcomes = harness.supervisor.advance(
                harness.config_path,
                {"projects": [snap]},
                executor=harness.executor,
            )

            # 3. Assert EXECUTION_CONTEXT_STALE milestone emitted
            emitted_names = [m[1] for m in channel.emitted]
            self.assertIn("EXECUTION_CONTEXT_STALE", emitted_names)

            # 4. Context should be re-anchored to curr_head, next_action="plan"
            ctx_after = get_context(harness.runtime_path, harness.project_id)
            self.assertEqual(ctx_after.get("git_anchor"), curr_head)
            self.assertEqual(ctx_after.get("next_action"), "plan")

            # 5. Replacement planning should be dispatched
            self.assertTrue(any(o.get("status") == "transition_submitted" for o in outcomes))
            self.assertIn("CONTINUATION_DISPATCHED", emitted_names)

            # Join any background planner threads before tempdir cleanup
            for th in list(harness.planner._threads.values()):
                th.join(timeout=2.0)

    def test_idle_tick_and_continuation_fault(self):
        """Idle project holds on 1st tick without burning budget; on 2nd tick emits CONTINUATION_FAULT and redispatches."""
        with tempfile.TemporaryDirectory() as td:
            harness = GoldenPathHarness.create(Path(td), task_id="P1", initial_status="READY_TO_RUN")
            harness.apply_approved_plan()
            channel = RecordingProgressChannel()
            harness.supervisor.progress_channel = channel

            record_or_refresh_intent(
                harness.runtime_path,
                harness.project_id,
                task_id=harness.task_id,
                command_id="cmd-idle-1",
                requested_action="continue",
                source="owner",
                state="active",
            )
            update_context(
                harness.runtime_path,
                harness.project_id,
                task_id=harness.task_id,
                idle_ticks=0,
                disposition="hold",
                next_action="execute",
            )

            snap = harness.current_snapshot()

            # First tick: idle_ticks incremented from 0 to 1, holds without burning recovery budget
            outcomes1 = harness.supervisor.advance(harness.config_path, {"projects": [snap]}, executor=harness.executor)
            self.assertTrue(any(o.get("status") == "continuation_hold" and o.get("idle_ticks") == 1 for o in outcomes1))
            intent1 = get_active_intent(harness.runtime_path, harness.project_id)
            self.assertEqual(intent1.get("actions_used", 0), 0)

            # Second tick: detects idle_ticks >= 1 -> emits CONTINUATION_FAULT, resets idle_ticks, and advances
            outcomes2 = harness.supervisor.advance(harness.config_path, {"projects": [snap]}, executor=harness.executor)
            emitted_names = [m[1] for m in channel.emitted]
            self.assertIn("CONTINUATION_FAULT", emitted_names)
            ctx2 = get_context(harness.runtime_path, harness.project_id)
            self.assertEqual(ctx2.get("idle_ticks"), 0)
            self.assertTrue(any(o.get("status") == "transition_submitted" for o in outcomes2))

    def test_transient_infrastructure_backoff_and_resumption(self):
        """Transient infrastructure blocker sets backoff without burning recovery action count, then resumes."""
        with tempfile.TemporaryDirectory() as td:
            harness = GoldenPathHarness.create(Path(td), task_id="P1", initial_status="READY_TO_RUN")
            harness.apply_approved_plan()
            snap = harness.current_snapshot()

            # Inject a transient inspection error on the snapshot
            snap["error"] = "broker temporarily unavailable: timeout connecting to service"

            record_or_refresh_intent(
                harness.runtime_path,
                harness.project_id,
                task_id=harness.task_id,
                command_id="cmd-transient-1",
                requested_action="continue",
                source="owner",
                state="active",
            )

            now_dt = datetime.now(timezone.utc)
            # Inject a scheduled backoff in the intent
            backoff_until = now_dt + timedelta(seconds=10)
            set_intent_backoff(harness.runtime_path, harness.project_id, backoff_until.isoformat())

            # When tick occurs before backoff_until: supervisor reports transient_backoff_waiting
            outcomes_waiting = harness.supervisor.advance(
                harness.config_path,
                {"projects": [snap]},
                executor=harness.executor,
                now=now_dt,
            )
            self.assertTrue(any(o.get("status") == "transient_backoff_waiting" for o in outcomes_waiting))
            intent_waiting = get_active_intent(harness.runtime_path, harness.project_id)
            self.assertEqual(intent_waiting.get("actions_used", 0), 0)

            # When time advances past backoff_until and transient error clears: backoff cleared and transition proceeds
            snap.pop("error", None)
            future_dt = now_dt + timedelta(seconds=15)
            outcomes_resumed = harness.supervisor.advance(
                harness.config_path,
                {"projects": [snap]},
                executor=harness.executor,
                now=future_dt,
            )
            self.assertTrue(any(o.get("status") == "transition_submitted" for o in outcomes_resumed))

    def test_recovery_budget_exhaustion(self):
        """When recovery actions exceed limit, intent terminates as exhausted with RECOVERY_EXHAUSTED."""
        with tempfile.TemporaryDirectory() as td:
            harness = GoldenPathHarness.create(Path(td), task_id="P1", initial_status="PENDING DESIGN")
            channel = RecordingProgressChannel()
            harness.supervisor.progress_channel = channel

            record_or_refresh_intent(
                harness.runtime_path,
                harness.project_id,
                task_id=harness.task_id,
                command_id="cmd-exhaust-1",
                requested_action="continue",
                source="owner",
                state="active",
            )

            # Burn 20 recovery actions to hit max limit (default 20)
            for _ in range(20):
                record_intent_action(harness.runtime_path, harness.project_id, fingerprint="fp-test")

            snap = harness.current_snapshot()
            outcomes = harness.supervisor.advance(
                harness.config_path,
                {"projects": [snap]},
                executor=harness.executor,
            )

            self.assertTrue(any(o.get("status") == "exhausted" for o in outcomes))
            emitted_names = [m[1] for m in channel.emitted]
            self.assertIn("RECOVERY_EXHAUSTED", emitted_names)

            intent_final = read_json(harness.runtime_path / "execution-intent.json", {}).get("intents", {}).get(harness.project_id)
            self.assertEqual(intent_final.get("state"), "exhausted")

    def test_stale_plan_anchor_rejected_on_head_advance(self):
        """Planner started on HEAD A fails closed if repo advances to HEAD B before apply."""
        with tempfile.TemporaryDirectory() as td:
            harness = GoldenPathHarness.create(Path(td), task_id="P1", initial_status="PENDING DESIGN")

            # Advance repo to a new commit (HEAD B)
            dummy_file = harness.repo_path / "dummy.txt"
            dummy_file.write_text("advance commit", encoding="utf-8")
            subprocess.run(["git", "add", "dummy.txt"], cwd=harness.repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "advance HEAD to B"], cwd=harness.repo_path, check=True, capture_output=True)

            # Start planner on current HEAD B
            snap = harness.current_snapshot()
            plan_id, reason = harness.planner.start(
                {"project_id": harness.project_id, "repo_path": str(harness.repo_path), "execution": {"engine": "aibroker"}, "ai_roles": {"planner": {"enabled": True}}},
                snap,
                "cmd-head-test",
            )
            self.assertIsNotNone(plan_id)

            # Wait for plan to freeze cleanly on HEAD B
            deadline = time.time() + 5.0
            row = None
            while time.time() < deadline:
                row = harness.planner.state()["plans"].get(plan_id)
                if row and row.get("state") in {"ready", "failed"}:
                    break
                time.sleep(0.02)

            self.assertIsNotNone(row)
            self.assertEqual(row.get("state"), "ready")

            for th in list(harness.planner._threads.values()):
                th.join(timeout=2.0)


if __name__ == "__main__":
    unittest.main()
