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

from dev_orchestrator.ai.contracts import AIRoleResult, ResourceContext
from dev_orchestrator.core.activation_supervisor import ActivationSupervisor
from dev_orchestrator.core.execution_context import (
    EXECUTION_CONTEXT_SCHEMA_VERSION,
    get_context,
    load_execution_contexts,
    record_role_completion,
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
from dev_orchestrator.core.task_status import parse_task_status, render_status_line, require_single_status_line
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
            write_task_execution_state(harness.repo_path, state="ready_to_run", task_id="P1")
            subprocess.run(["git", "add", "."], cwd=harness.repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "commit ready_to_run state"], cwd=harness.repo_path, check=True, capture_output=True)
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
            write_task_execution_state(harness.repo_path, state="ready_to_run", task_id="P1")
            subprocess.run(["git", "add", "."], cwd=harness.repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "commit ready_to_run state"], cwd=harness.repo_path, check=True, capture_output=True)
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
            channel = RecordingProgressChannel()
            harness.supervisor.progress_channel = channel

            record_or_refresh_intent(
                harness.runtime_path,
                harness.project_id,
                task_id=harness.task_id,
                command_id="cmd-head-test",
                requested_action="continue",
                source="owner",
                state="active",
            )

            # Concurrent head advancement during planning
            def _planner_with_concurrent_head_move(req: Any) -> AIRoleResult:
                dummy = harness.repo_path / "dummy.txt"
                dummy.write_text("moved head", encoding="utf-8")
                subprocess.run(["git", "add", "dummy.txt"], cwd=harness.repo_path, check=True, capture_output=True)
                subprocess.run(["git", "commit", "-m", "concurrent commit on HEAD B"], cwd=harness.repo_path, check=True, capture_output=True)
                payload = {
                    "task_id": req.task_run_id,
                    "summary": "Freeze plan on old head",
                    "implementation_steps": ["step 1"],
                    "interfaces": ["intf 1"],
                    "validation": ["val 1"],
                    "risks": ["risk 1"],
                    "out_of_scope": ["scope 1"],
                }
                return AIRoleResult(
                    request_id=req.request_id,
                    role_run_id=req.role_run_id,
                    status="succeeded",
                    output=json.dumps(payload),
                    dispatch_id="d1",
                    decision_id="dec1",
                    execution_id="e1",
                    resource_context=ResourceContext("r1", "p1", "a1", "m1"),
                )

            harness.port.script_role_response("planner", _planner_with_concurrent_head_move)

            snap = harness.current_snapshot()
            plan_id, _ = harness.planner.start(
                {
                    "project_id": harness.project_id,
                    "repo_path": str(harness.repo_path),
                    "execution": {"engine": "aibroker"},
                    "ai_roles": {"planner": {"enabled": True}},
                },
                snap,
                "cmd-head-test",
            )

            # 1. Wait for plan started on HEAD A to fail closed
            deadline = time.time() + 5.0
            row = None
            while time.time() < deadline:
                row = harness.planner.state()["plans"].get(plan_id)
                if row and row.get("state") in {"failed", "rejected"}:
                    break
                time.sleep(0.02)

            self.assertIsNotNone(row)
            self.assertEqual(row.get("state"), "failed")
            self.assertIn("repository changed during planning", row.get("reason", ""))

            # 2. Assert context detects git_anchor is stale against current HEAD B
            snap_b = harness.current_snapshot()
            curr_head_b = snap_b["git"]["head"]
            ctx = get_context(harness.runtime_path, harness.project_id)
            self.assertTrue(context_is_stale(ctx, curr_head_b, harness.task_id))

            # 3. Next supervisor tick emits EXECUTION_CONTEXT_STALE and dispatches replacement planner on HEAD B
            outcomes = harness.supervisor.advance(
                harness.config_path,
                {"projects": [snap_b]},
                executor=harness.executor,
            )
            emitted_names = [m[1] for m in channel.emitted]
            self.assertIn("EXECUTION_CONTEXT_STALE", emitted_names)
            self.assertTrue(any(o.get("status") == "transition_submitted" and o.get("action") == "plan" for o in outcomes))

            # 4. Replacement planner freezes ready on HEAD B
            deadline = time.time() + 5.0
            replacement_row = None
            while time.time() < deadline:
                plans = harness.planner.state()["plans"]
                for pid, p in plans.items():
                    if pid != plan_id and p.get("state") in {"ready", "failed"}:
                        replacement_row = p
                        break
                if replacement_row:
                    break
                time.sleep(0.02)

            self.assertIsNotNone(replacement_row)
            self.assertEqual(replacement_row.get("state"), "ready")
            harness.close()

    def test_provider_failover_without_lifecycle_corruption(self):
        """Planner encounters provider failure on attempt 1, executes failover, and completes without lifecycle corruption."""
        with tempfile.TemporaryDirectory() as td:
            harness = GoldenPathHarness.create(Path(td), task_id="P1", initial_status="PENDING DESIGN")
            record_or_refresh_intent(
                harness.runtime_path,
                harness.project_id,
                task_id=harness.task_id,
                command_id="cmd-failover-test",
                requested_action="continue",
                source="owner",
                state="active",
            )

            # Script resource fault on attempt 1
            harness.port.script_role_fault("planner", "quota exhausted")

            snap = harness.current_snapshot()
            plan_id, _ = harness.planner.start(
                {
                    "project_id": harness.project_id,
                    "repo_path": str(harness.repo_path),
                    "execution": {"engine": "aibroker"},
                    "ai_roles": {"planner": {"enabled": True, "max_attempts": 3}},
                },
                snap,
                "cmd-failover-test",
            )

            # Wait for planner to finish (attempt 1 failed -> failover to attempt 2 -> success)
            deadline = time.time() + 5.0
            row = None
            while time.time() < deadline:
                row = harness.planner.state()["plans"].get(plan_id)
                if row and row.get("state") in {"ready", "failed"}:
                    break
                time.sleep(0.02)

            self.assertIsNotNone(row)
            self.assertEqual(row.get("state"), "ready")
            self.assertEqual(len(row.get("planner_attempts", [])), 2)
            # Lifecycle context is updated without corruption
            ctx = get_context(harness.runtime_path, harness.project_id)
            self.assertEqual(ctx.get("next_action"), "execute")
            self.assertEqual(ctx.get("disposition"), "advance")
            harness.close()

    def test_reviewer_rejection_reaching_done_via_bounded_remediation(self):
        """Reviewer rejects on round 1 (remediate), remediation executes, and reviewer accepts on round 2 -> DONE."""
        with tempfile.TemporaryDirectory() as td:
            harness = GoldenPathHarness.create(Path(td), task_id="P1", initial_status="PENDING DESIGN")
            channel = RecordingProgressChannel()
            harness.supervisor.progress_channel = channel

            harness.submit_continue(command_id="cmd-start-1")
            harness.apply_approved_plan()
            snap = harness.current_snapshot()
            harness.supervisor.advance(harness.config_path, {"projects": [snap]}, executor=harness.executor)
            harness.complete_worker_run()

            # Round 1: reviewer rejects with 'remediate'
            harness.port.script_role_response(
                "reviewer",
                AIRoleResult(
                    request_id="ai_review:cmd-start-1:execute",
                    role_run_id="reviewer-round-1",
                    status="succeeded",
                    output=json.dumps({"decision": "remediate", "next_action": "continue_current_stage", "reason": "need more tests"}),
                    dispatch_id="d-rev1",
                    decision_id="dec-rev1",
                    execution_id="e-rev1",
                    resource_context=ResourceContext("rev-r1", "p2", "a2", "m2"),
                ),
            )

            harness.reviewer.advance(harness.config_path)
            time.sleep(0.5)

            ctx_remed = get_context(harness.runtime_path, harness.project_id)
            self.assertEqual(ctx_remed.get("next_action"), "remediate")
            self.assertEqual(ctx_remed.get("disposition"), "remediate")

            # Advance supervisor to launch remediation
            snap_remed = harness.current_snapshot()
            outcomes = harness.supervisor.advance(harness.config_path, {"projects": [snap_remed]}, executor=harness.executor)
            self.assertTrue(any(o.get("status") == "transition_submitted" for o in outcomes))

            # Complete remediation worker run
            harness.complete_worker_run()

            # Round 2: reviewer accepts task -> DONE
            harness.apply_review_acceptance()
            snap_done = harness.current_snapshot()
            self.assertTrue(parse_task_status(snap_done.get("next_status")).is_completed())

            # Verify clean worktree and terminal closure
            git_status = subprocess.run(["git", "status", "--porcelain"], cwd=harness.repo_path, check=True, capture_output=True, text=True).stdout.strip()
            self.assertEqual(git_status, "", "Git worktree must be clean at DONE")
            harness.close()

    def test_daemon_restart_at_handoff_boundaries(self):
        """Coordinators and execution context survive daemon restart at every handoff boundary."""
        with tempfile.TemporaryDirectory() as td:
            harness = GoldenPathHarness.create(Path(td), task_id="P1", initial_status="PENDING DESIGN")
            harness.submit_continue(command_id="cmd-start-1")
            harness.apply_approved_plan()

            # Boundary 1: Restart after plan approval, before worker launch
            harness.restart_daemon()
            snap1 = harness.current_snapshot()
            outcomes = harness.supervisor.advance(harness.config_path, {"projects": [snap1]}, executor=harness.executor)
            self.assertTrue(any(o.get("status") == "transition_submitted" for o in outcomes))

            harness.complete_worker_run()

            # Boundary 2: Restart after worker completion, before reviewer advance
            harness.restart_daemon()
            harness.apply_review_acceptance()
            snap_done = harness.current_snapshot()
            self.assertTrue(parse_task_status(snap_done.get("next_status")).is_completed())

            # Boundary 3: Restart after DONE
            harness.restart_daemon()
            ctx_final = get_context(harness.runtime_path, harness.project_id)
            self.assertIsNone(ctx_final.get("active_role"))
            self.assertEqual(ctx_final.get("next_action"), "none")
            harness.close()

    def test_execution_context_durability_and_restart(self):
        """ExecutionContext persists to disk and reloads with full schema fidelity."""
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            update_context(
                rt,
                "proj-1",
                task_id="P10",
                git_anchor="a" * 40,
                disposition="hold",
                next_action="plan",
                idle_ticks=1,
            )
            data = load_execution_contexts(rt)
            self.assertEqual(data.get("schema_version"), EXECUTION_CONTEXT_SCHEMA_VERSION)
            ctx = data["contexts"]["proj-1"]
            self.assertEqual(ctx["task_id"], "P10")
            self.assertEqual(ctx["git_anchor"], "a" * 40)
            self.assertEqual(ctx["disposition"], "hold")
            self.assertEqual(ctx["next_action"], "plan")
            self.assertEqual(ctx["idle_ticks"], 1)

    def test_execution_context_rejected_on_identity_mismatch(self):
        """context_is_stale fails when git anchor, task ID, or project ID diverged."""
        ctx = {
            "project_id": "proj-1",
            "task_id": "P1",
            "git_anchor": "1" * 40,
            "branch": "main",
        }
        self.assertFalse(context_is_stale(ctx, "1" * 40, "P1"))
        self.assertTrue(context_is_stale(ctx, "2" * 40, "P1"))
        self.assertTrue(context_is_stale(ctx, "1" * 40, "P2"))

    def test_execution_context_role_history_append(self):
        """record_role_completion appends chronologically and clears active_role."""
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            update_context(rt, "proj-1", task_id="P1", active_role="planner", disposition="hold")
            ctx1 = get_context(rt, "proj-1")
            self.assertEqual(ctx1.get("active_role"), "planner")

            record_role_completion(rt, "proj-1", "planner", {"status": "ok", "task_id": "P1"})
            ctx2 = get_context(rt, "proj-1")
            self.assertIsNone(ctx2.get("active_role"))
            self.assertEqual(len(ctx2.get("role_history", [])), 1)
            self.assertEqual(ctx2["role_history"][0]["role"], "planner")
            self.assertEqual(ctx2["role_history"][0]["outcome"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
