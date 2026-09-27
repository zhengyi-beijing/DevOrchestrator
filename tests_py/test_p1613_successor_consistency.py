"""P16.13 lifecycle fault-injection matrix.

Each test names the incident class from the task's A--J matrix.  The fixtures
use real git repositories and durable runtime ledgers so restart/idempotency
behavior is exercised at the same boundaries as the daemon.
"""
from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dev_orchestrator.core.ai_reviewer import AIReviewerCoordinator
from dev_orchestrator.daemon import _run_orchestration_tick
from dev_orchestrator.core.lifecycle_authority import (
    active_owners,
    evaluate_lifecycle_invariants,
    source_ownership_blockers,
)
from dev_orchestrator.core.successor_consistency import (
    reconcile_roadmap_successor,
    resolve_successor,
)
from dev_orchestrator.core.transition_executor import TransitionExecutor
from dev_orchestrator.core.repository import read_repository_truth


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _commit(repo: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", message],
        check=True, capture_output=True, text=True,
    )
    return _git(repo, "rev-parse", "HEAD")


def _repo(root: Path, *, roadmap_target: str | None = "P2", current: str = "P2") -> Path:
    repo = root / "repo"
    (repo / "agent" / "staged").mkdir(parents=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    (repo / "agent" / "next.md").write_text(
        f"# {current} task\n\nStatus: **PENDING DESIGN**\n", encoding="utf-8",
    )
    # Successor specs deliberately require byte-stable LF even on Windows.
    (repo / "agent" / "staged" / "P2.md").write_bytes(
        b"# P2 task\n\nStatus: **PENDING DESIGN**\n\nPredecessor: P1\n"
    )
    entry: dict[str, object] = {"task_id": "P1"}
    if roadmap_target is None:
        entry.update({"successor": None, "successor_spec_path": None})
    else:
        entry.update({
            "successor": roadmap_target,
            "successor_spec_path": f"agent/staged/{roadmap_target}.md",
        })
        if roadmap_target != "P2":
            (repo / "agent" / "staged" / f"{roadmap_target}.md").write_bytes(
                f"# {roadmap_target} task\n\nStatus: **PENDING DESIGN**\n".encode("utf-8")
            )
    (repo / "agent" / "staged" / "roadmap.json").write_text(
        json.dumps({"schema_version": 1, "tasks": [entry]}, indent=2) + "\n",
        encoding="utf-8",
    )
    _commit(repo, "fixture")
    return repo


def _summary(repo: Path, task_id: str = "P2") -> dict:
    return {"projects": [{
        "project_id": "p1",
        "repo_path": str(repo),
        "state": "IDLE",
        "next_title": f"{task_id} task",
        "next_status": "**PENDING DESIGN**",
        "telemetry": {"task_id": task_id},
        "git": {"head": _git(repo, "rev-parse", "HEAD")},
    }]}


class SuccessorConsistencyFaultMatrixTests(unittest.TestCase):
    def test_A_and_H_stale_predecessor_worker_fences_published_successor(self):
        """A/H: exact P16.12-running/P16.13-published incident cannot leak."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); repo = _repo(root); runtime = root / "runtime"
            executor = TransitionExecutor(runtime)
            ledger = executor.state()
            ledger["executions"]["p1612-remediation"] = {
                "project_id": "p1", "source_request_id": "p1612-remediation",
                "source_kind": "remediation", "task_id": "P1", "source_task_id": "P1",
                "state": "running", "started_at": "2026-09-26T07:23:00+00:00",
            }
            executor._save_ledger(ledger)

            first = executor.reconcile_lifecycle_authority(_summary(repo))
            authority = first["lifecycle"]["p1"]
            self.assertEqual((authority["current_task_id"], authority["lifecycle_state"]), ("P1", "EXECUTING"))
            self.assertEqual(authority["repository_projection"]["task_id"], "P2")
            projected = executor.overlay_lifecycle_authority(_summary(repo))["projects"][0]
            self.assertEqual(projected["telemetry"]["task_id"], "P1")
            self.assertEqual(projected["state"], "WORKER_RUNNING")
            self.assertEqual(len(first["transitions"]), 1)

            # Repeated daemon ticks retain one generation/transition.
            second = executor.reconcile_lifecycle_authority(_summary(repo))
            self.assertEqual(set(second["transitions"]), set(first["transitions"]))
            self.assertEqual(second["lifecycle"]["p1"]["generation"], 0)

            # The Watchdog is the other component that could relabel the
            # published successor as current.  Drive the real coordinator over
            # the same state and assert it neither relabels the authority nor
            # actuates recovery while the predecessor Worker is still running.
            from dev_orchestrator.core.watchdog import WatchdogCoordinator

            config_path = runtime / "projects.json"
            config_path.write_text(json.dumps({"projects": [{
                "project_id": "p1", "repo_path": str(repo),
                "watchdog": {"enabled": True, "auto_recovery": True,
                             "no_progress_threshold_minutes": 1},
            }]}), encoding="utf-8")
            watchdog = WatchdogCoordinator(runtime)
            overlaid = executor.overlay_lifecycle_authority(_summary(repo))
            watchdog.advance(str(config_path), overlaid, executor=executor)
            for thread in getattr(watchdog, "_threads", {}).values():
                thread.join(timeout=10.0)

            after = executor.state()["lifecycle"]["p1"]
            self.assertEqual(
                (after["current_task_id"], after["lifecycle_state"]), ("P1", "EXECUTING"),
                "the Watchdog relabelled the authority while the predecessor was running",
            )
            self.assertIsNone(after.get("owner_gate"))
            self.assertEqual(
                len(executor.state()["transitions"]), 1,
                "the Watchdog created a second transition for the published successor",
            )
            self.assertEqual(
                [row for row in executor.state()["executions"].values()
                 if isinstance(row, dict) and row.get("state") == "handoff"], [],
                "the Watchdog published a handoff while source ownership was live",
            )
            wrow = (watchdog.state().get("projects") or {}).get("p1") or {}
            # Guard against a vacuous pass: the Watchdog must actually have
            # evaluated this project rather than skipped it.
            self.assertEqual(
                len(wrow.get("lifecycle_invariants") or []), 6,
                f"the Watchdog did not evaluate this project: {wrow.get('last_error')}",
            )
            self.assertNotIn(
                "PENDING_DESIGN_NOT_EXECUTING", wrow.get("unresolved_invariants") or [],
            )
            self.assertEqual(
                [f["code"] for f in wrow["lifecycle_invariants"] if not f["holds"]], [],
                "the Watchdog saw a lifecycle violation in the fenced-successor state",
            )

    def test_B_review_verdict_without_handoff_recovers_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); repo = _repo(root, current="P1"); runtime = root / "runtime"
            executor = TransitionExecutor(runtime)
            result = executor.reconcile_successor_handoff(
                {"project_id": "p1", "repo_path": str(repo)}, _summary(repo, "P1"),
                completed_task_id="P1", trigger="fault-B",
            )
            replay = executor.reconcile_successor_handoff(
                {"project_id": "p1", "repo_path": str(repo)}, _summary(repo, "P1"),
                completed_task_id="P1", trigger="fault-B",
            )
            self.assertEqual(result["status"], "applied")
            self.assertEqual(replay["status"], "noop")
            state = executor.state()
            handoffs = [row for row in state["executions"].values() if row.get("state") == "handoff"]
            self.assertEqual(len(handoffs), 1)
            self.assertEqual((handoffs[0]["source_task_id"], handoffs[0]["target_task_id"]), ("P1", "P2"))
            self.assertEqual(len(state["transitions"]), 1)

    def test_C_restart_mid_transition_replays_missing_handoff(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); repo = _repo(root, current="P1"); runtime = root / "runtime"
            executor = TransitionExecutor(runtime)
            resolved = resolve_successor(repo, "P1")
            self.assertTrue(executor._record_handoff(
                "review-next", "p1", "P1", "P2", "accepted",
                staged=resolved, reviewed_branch=_git(repo, "branch", "--show-current"),
                reviewed_head=_git(repo, "rev-parse", "HEAD"), successor_evidence="roadmap",
            ))
            ledger = executor.state()
            del ledger["executions"]["review-next"]  # crash after journal, before work-item durability
            executor._save_ledger(ledger)

            recovered = TransitionExecutor(runtime).state()
            self.assertEqual(recovered["executions"]["review-next"]["state"], "handoff")
            transition = next(iter(recovered["transitions"].values()))
            self.assertEqual(transition["state"], "ready")
            self.assertIn("replayed_at", transition)

    def test_D_dirty_reconcile_waits_then_advances_without_owner_continue(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); repo = _repo(root, roadmap_target=None, current="P1")
            resolution = resolve_successor(repo, "P1")
            self.assertEqual(resolution.kind, "inconsistent")
            (repo / "README.md").write_text("owner work\n", encoding="utf-8")
            blocked = reconcile_roadmap_successor(repo, "P1", resolution)
            self.assertEqual(blocked.status, "rejected")
            self.assertIn("clean repository", blocked.reason or "")

            subprocess.run(["git", "-C", str(repo), "restore", "README.md"], check=True)
            applied = reconcile_roadmap_successor(repo, "P1")
            self.assertEqual(applied.status, "applied")
            self.assertEqual(resolve_successor(repo, "P1").kind, "successor")
            self.assertEqual(_git(repo, "status", "--porcelain"), "")

    def test_acceptance_2_inconsistent_successor_never_settles_project_complete(self):
        """Acceptance 2: detect ROADMAP_SUCCESSOR_INCONSISTENT and do not settle.

        Drives the real decision actuation rather than the resolver, because the
        terminal-settle branch that must not be taken lives there.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _repo(root, roadmap_target=None, current="P1")
            runtime = root / "runtime"; runtime.mkdir()
            self.assertEqual(resolve_successor(repo, "P1").kind, "inconsistent")

            truth = read_repository_truth(repo)
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {
                    "engine": "aibroker", "enabled": True, "owner_authorized": True,
                    "allowed_next_actions": ["next_task"],
                },
            }
            config = root / "projects.json"
            config.write_text(json.dumps({"projects": [project]}), encoding="utf-8")
            snapshot = {
                "project_id": "p1", "repo_path": str(repo), "state": "WAITING_REVIEW",
                "next_title": "P1 task", "next_status": "**COMPLETE**",
                "telemetry": {"task_id": "P1"},
                "git": {"branch": truth.branch, "head": truth.head, "dirty": False,
                        "status_hash": truth.status_hash},
            }
            (runtime / "review-decisions.json").write_text(json.dumps({"version": 1, "decisions": {
                "ai_review:d1": {
                    "project_id": "p1", "request_id": "ai_review:d1", "disposition": "apply",
                    "decision": "next", "next_action": "next_task",
                    "reason": "reviewed task complete", "task_id": "P1",
                    "branch": truth.branch, "head": truth.head,
                    "review_status_hash": truth.status_hash,
                    "role": "reviewer", "event": "worker_done",
                    "consumed_at": "2026-09-26T00:00:00+00:00",
                },
            }}), encoding="utf-8")

            executor = TransitionExecutor(runtime)
            executor.advance({"projects": [snapshot]}, config,
                             decision_summary={"projects": [snapshot]})

            rows = [row for row in executor.state()["executions"].values() if isinstance(row, dict)]
            self.assertEqual(
                [row for row in rows if row.get("outcome") == "task_complete"], [],
                "an inconsistent successor was terminal-settled as project complete",
            )
            handoffs = [row for row in rows if row.get("state") == "handoff"]
            self.assertEqual(
                len(handoffs), 1,
                f"reconciliation did not produce exactly one handoff: {rows}",
            )
            self.assertEqual(
                (handoffs[0].get("source_task_id"), handoffs[0].get("target_task_id")),
                ("P1", "P2"),
            )
            # The repair is committed, so the recovery precondition survives.
            self.assertEqual(_git(repo, "status", "--porcelain"), "")
            self.assertEqual(resolve_successor(repo, "P1").kind, "successor")

    def test_E_roadmap_and_staged_claim_disagreement_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); repo = _repo(root, roadmap_target="P3", current="P1")
            resolution = resolve_successor(repo, "P1")
            self.assertEqual(resolution.kind, "ambiguous")
            executor = TransitionExecutor(root / "runtime")
            outcome = executor.reconcile_successor_handoff(
                {"project_id": "p1", "repo_path": str(repo)}, _summary(repo, "P1"),
                completed_task_id="P1", trigger="fault-E",
            )
            self.assertEqual(outcome["status"], "conflict")
            self.assertEqual(executor.state()["transitions"], {})

    def test_F_duplicate_review_handoff_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); repo = _repo(root, current="P1"); executor = TransitionExecutor(root / "runtime")
            resolved = resolve_successor(repo, "P1")
            kwargs = dict(
                staged=resolved, reviewed_branch=_git(repo, "branch", "--show-current"),
                reviewed_head=_git(repo, "rev-parse", "HEAD"), successor_evidence="roadmap",
            )
            self.assertTrue(executor._record_handoff("review-1", "p1", "P1", "P2", "ok", **kwargs))
            self.assertTrue(executor._record_handoff("review-1", "p1", "P1", "P2", "ok", **kwargs))
            state = executor.state()
            self.assertEqual(len(state["executions"]), 1)
            self.assertEqual(len(state["transitions"]), 1)

    def test_G_exactly_one_diff_localized_remediation_extension(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); repo = _repo(root, current="P1"); runtime = root / "runtime"; runtime.mkdir()
            base = _git(repo, "rev-parse", "HEAD")
            (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
            head = _commit(repo, "implementation")
            executions = {
                "root": {"project_id": "p1", "task_id": "P1", "source_kind": "decision", "state": "completed"},
                "rem1": {"project_id": "p1", "task_id": "P1", "source_kind": "remediation", "review_decision_id": "r1", "state": "completed"},
                "rem2": {"project_id": "p1", "task_id": "P1", "source_kind": "remediation", "review_decision_id": "r2", "state": "completed", "repo_path": str(repo), "head": base},
            }
            reviews = {
                "r1": {"review_id": "r1", "project_id": "p1", "task_id": "P1", "source_request_id": "root", "state": "completed"},
                "r2": {"review_id": "r2", "project_id": "p1", "task_id": "P1", "source_request_id": "rem1", "state": "completed"},
                "r3": {"review_id": "r3", "project_id": "p1", "task_id": "P1", "source_request_id": "rem2", "state": "completed", "repo_path": str(repo), "head": head},
            }
            (runtime / "transition-executor.json").write_text(json.dumps({"version": 2, "executions": executions, "lifecycle": {}, "transitions": {}}), encoding="utf-8")
            (runtime / "ai-reviewer.json").write_text(json.dumps({"version": 1, "reviews": reviews}), encoding="utf-8")
            reviewer = AIReviewerCoordinator(runtime, None)
            finding = ({"file": "app.py", "summary": "local bug", "fix": "correct value", "regression_test": "test_value"},)
            first = reviewer._apply_remediation_budget(
                "r3", "p1", "P1", "remediate", "continue_current_stage", "bug", 2,
                findings=finding, max_extensions=1, max_extension_findings=3,
            )
            self.assertEqual((first[0], first[4]), ("remediate", True))

            executions["rem3"] = {"project_id": "p1", "task_id": "P1", "source_kind": "remediation", "review_decision_id": "r3", "state": "completed", "repo_path": str(repo), "head": base}
            state = reviewer.state(); state["reviews"]["r4"] = {"review_id": "r4", "project_id": "p1", "task_id": "P1", "source_request_id": "rem3", "state": "completed", "repo_path": str(repo), "head": head}
            reviewer._save_state(state)
            (runtime / "transition-executor.json").write_text(json.dumps({"version": 2, "executions": executions, "lifecycle": {}, "transitions": {}}), encoding="utf-8")
            second = reviewer._apply_remediation_budget(
                "r4", "p1", "P1", "remediate", "continue_current_stage", "still broken", 2,
                findings=finding, max_extensions=1, max_extension_findings=3,
            )
            self.assertEqual((second[0], second[1], second[4]), ("owner_gate", "stop", False))

    def test_G_replayed_tick_reuses_the_grant_and_never_grants_a_second(self):
        """Acceptance 10: repeated ticks never grant a duplicate extension."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); repo = _repo(root, current="P1"); runtime = root / "runtime"; runtime.mkdir()
            base = _git(repo, "rev-parse", "HEAD")
            (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
            head = _commit(repo, "implementation")
            executions = {
                "root": {"project_id": "p1", "task_id": "P1", "source_kind": "decision", "state": "completed"},
                "rem1": {"project_id": "p1", "task_id": "P1", "source_kind": "remediation", "review_decision_id": "r1", "state": "completed"},
                "rem2": {"project_id": "p1", "task_id": "P1", "source_kind": "remediation", "review_decision_id": "r2", "state": "completed", "repo_path": str(repo), "head": base},
            }
            reviews = {
                "r1": {"review_id": "r1", "project_id": "p1", "task_id": "P1", "source_request_id": "root", "state": "completed"},
                "r2": {"review_id": "r2", "project_id": "p1", "task_id": "P1", "source_request_id": "rem1", "state": "completed"},
                "r3": {"review_id": "r3", "project_id": "p1", "task_id": "P1", "source_request_id": "rem2", "state": "completed", "repo_path": str(repo), "head": head},
            }
            (runtime / "transition-executor.json").write_text(json.dumps(
                {"version": 2, "executions": executions, "lifecycle": {}, "transitions": {}}), encoding="utf-8")
            (runtime / "ai-reviewer.json").write_text(json.dumps(
                {"version": 1, "reviews": reviews}), encoding="utf-8")
            reviewer = AIReviewerCoordinator(runtime, None)
            finding = ({"file": "app.py", "summary": "local bug", "fix": "correct value",
                        "regression_test": "test_value"},)

            args = ("r3", "p1", "P1", "remediate", "continue_current_stage", "bug", 2)
            kwargs = dict(findings=finding, max_extensions=1, max_extension_findings=3)
            first = reviewer._apply_remediation_budget(*args, **kwargs)
            self.assertEqual((first[0], first[4]), ("remediate", True))
            granted_at = reviewer.state()["reviews"]["r3"]["remediation_extension_granted_at"]

            # Replay the same review identity, as a repeated daemon tick would.
            for _ in range(3):
                replay = reviewer._apply_remediation_budget(*args, **kwargs)
                self.assertEqual(
                    (replay[0], replay[4]), ("remediate", True),
                    "a replayed tick did not reuse the already-granted extension",
                )
            row = reviewer.state()["reviews"]["r3"]
            self.assertEqual(
                row["remediation_extension_granted_at"], granted_at,
                "a replayed tick re-granted the extension instead of reusing it",
            )
            self.assertEqual(row["remediation_extension_review_id"], "r3")

            # The single grant is still consumed for the next descendant review,
            # which must gate rather than receive a second extension.
            executions["rem3"] = {"project_id": "p1", "task_id": "P1", "source_kind": "remediation",
                                  "review_decision_id": "r3", "state": "completed",
                                  "repo_path": str(repo), "head": base}
            state = reviewer.state()
            state["reviews"]["r4"] = {"review_id": "r4", "project_id": "p1", "task_id": "P1",
                                      "source_request_id": "rem3", "state": "completed",
                                      "repo_path": str(repo), "head": head}
            reviewer._save_state(state)
            (runtime / "transition-executor.json").write_text(json.dumps(
                {"version": 2, "executions": executions, "lifecycle": {}, "transitions": {}}), encoding="utf-8")
            descendant = reviewer._apply_remediation_budget(
                "r4", "p1", "P1", "remediate", "continue_current_stage", "still broken", 2, **kwargs,
            )
            self.assertEqual(
                (descendant[0], descendant[1], descendant[4]), ("owner_gate", "stop", False),
                "the replayed grant was double-counted into a second extension",
            )

    def test_I_and_J_repeated_recovery_after_intent_is_single_and_live(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); repo = _repo(root); runtime = root / "runtime"; executor = TransitionExecutor(runtime)
            ledger = executor.state()
            ledger["lifecycle"]["p1"] = {
                "schema_version": 1, "project_id": "p1", "generation": 0,
                "current_task_id": "P1", "lifecycle_state": "TRANSITIONING",
                "source_task_id": "P1", "active_transition_id": "t1",
                "active_owner": None, "owner_gate": None,
            }
            ledger["transitions"]["t1"] = {
                "transition_id": "t1", "idempotency_key": "t1", "project_id": "p1",
                "source_task_id": "P1", "target_task_id": "P2", "generation": 1,
                "state": "intent",
            }
            executor._save_ledger(ledger)
            for _ in range(3):
                state = executor.reconcile_lifecycle_authority(_summary(repo))
                self.assertEqual(state["lifecycle"]["p1"]["current_task_id"], "P1")
                self.assertEqual(state["transitions"]["t1"]["state"], "intent")

            recovery = executor.reconcile_successor_handoff(
                {"project_id": "p1", "repo_path": str(repo)}, _summary(repo),
                completed_task_id="P1", trigger="fault-J",
            )
            self.assertEqual(recovery["status"], "applied")
            request_id = recovery["source_request_id"]
            executor.mark_handoff_consumed(request_id, "continue-1", "plan-1")
            final = executor.reconcile_lifecycle_authority(_summary(repo))
            self.assertEqual(final["lifecycle"]["p1"]["current_task_id"], "P2")
            self.assertEqual(final["lifecycle"]["p1"]["generation"], 1)
            self.assertEqual(len(final["transitions"]), 1)

    def test_pending_design_and_single_owner_invariants_are_centralized(self):
        snapshot = _summary(Path("."), "P2")["projects"][0]
        executor_state = {
            "lifecycle": {"p1": {"current_task_id": "P2", "lifecycle_state": "PENDING_DESIGN"}},
            "executions": {"bad": {"project_id": "p1", "source_request_id": "bad", "task_id": "P2", "state": "running"}},
            "transitions": {},
        }
        findings = {item.code: item for item in evaluate_lifecycle_invariants(snapshot=snapshot, executor_state=executor_state)}
        self.assertFalse(findings["PENDING_DESIGN_NOT_EXECUTING"].holds)
        _, reason = TransitionExecutor._lifecycle_launch_guard(executor_state, "p1", "P2", "P1")
        self.assertIn("PENDING_DESIGN", reason or "")

    def test_legacy_consumed_history_is_not_current_ownership_or_bad_lineage(self):
        executor_state = {
            "lifecycle": {"p1": {"current_task_id": "P2", "lifecycle_state": "READY_TO_RUN"}},
            "executions": {
                "worker-old": {
                    "project_id": "p1", "source_request_id": "worker-old",
                    "task_id": "P1", "state": "completed", "review_state": "pending",
                    "completed_at": "2026-09-20T01:00:00+00:00",
                },
                "review-old": {
                    "project_id": "p1", "source_request_id": "review-old",
                    "task_id": "P1.5", "next_task_id": "P2", "state": "handoff",
                    "recorded_at": "2026-09-20T02:00:00+00:00",
                    "handoff_consumed": True,
                },
            },
            "transitions": {},
        }
        snapshot = {
            "project_id": "p1", "state": "READY_TO_RUN",
            "next_status": "READY TO RUN", "telemetry": {"task_id": "P2"},
        }
        decisions = {"decisions": {"review-final": {
            "project_id": "p1", "request_id": "review-final", "task_id": "P1.5",
            "disposition": "apply", "decision": "next", "next_action": "next_task",
            "consumed_at": "2026-09-20T01:30:00+00:00",
        }}}
        self.assertEqual(active_owners("p1", executor_state), [])
        findings = {
            item.code: item for item in evaluate_lifecycle_invariants(
                snapshot=snapshot, executor_state=executor_state,
                decisions_state=decisions,
            )
        }
        self.assertTrue(findings["SUCCESSOR_HANDOFF_LINEAGE_VALID"].holds)
        self.assertTrue(findings["NEXT_TASK_WITHOUT_HANDOFF"].holds)
        self.assertTrue(findings["SINGLE_ACTIVE_LIFECYCLE_OWNER"].holds)

    def test_false_legacy_review_obligation_gate_reconciles_without_deleting_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); repo = _repo(root); executor = TransitionExecutor(root / "runtime")
            ledger = executor.state()
            ledger["lifecycle"]["p1"] = {
                "schema_version": 1, "project_id": "p1", "generation": 0,
                "current_task_id": "P1", "lifecycle_state": "OWNER_GATE",
                "source_task_id": "P1", "active_transition_id": "bad-bootstrap",
                "active_owner": None,
                "owner_gate": {
                    "code": "SINGLE_ACTIVE_LIFECYCLE_OWNER",
                    "owners": [{"role": "review_obligation", "id": "old", "task_id": "P1"}],
                },
            }
            ledger["transitions"]["bad-bootstrap"] = {
                "transition_id": "bad-bootstrap", "project_id": "p1",
                "source_task_id": "P1", "target_task_id": "P2",
                "generation": 1, "state": "intent",
            }
            executor._save_ledger(ledger)
            reconciled = executor.reconcile_lifecycle_authority(_summary(repo))
            authority = reconciled["lifecycle"]["p1"]
            self.assertEqual(authority["current_task_id"], "P2")
            self.assertIsNone(authority["owner_gate"])
            self.assertIn("legacy_bootstrap_reconciled", authority)
            self.assertEqual(reconciled["transitions"]["bad-bootstrap"]["state"], "owner_gate")


class _StalePredecessorExecutor:
    """Authority owns the successor while a stale predecessor run still projects.

    This reproduces the live P16.12 -> P16.13 incident shape at the projection
    boundary: the authoritative lifecycle names the successor as PENDING_DESIGN
    while ``overlay_managed_runs`` still selects the predecessor execution and
    relabels the snapshot as an executing P16.12 Worker.
    """

    def __init__(self):
        self.authority = {
            "schema_version": 1, "project_id": "p1", "generation": 4,
            "current_task_id": "P16.13", "source_task_id": "P16.12",
            "lifecycle_state": "PENDING_DESIGN", "active_owner": None,
            "owner_gate": None,
        }
        self.reconcile_calls = 0
        self.overlay_authority_calls = 0

    def reconcile_lifecycle_authority(self, _summary, *, planner_state=None, reviewer_state=None):
        self.reconcile_calls += 1
        return {"lifecycle": {"p1": dict(self.authority)}}

    def overlay_lifecycle_authority(self, summary):
        self.overlay_authority_calls += 1
        projected = copy.deepcopy(summary)
        if not isinstance(projected, dict) or not isinstance(projected.get("projects"), list):
            return projected
        for snapshot in projected["projects"]:
            telemetry = dict(snapshot.get("telemetry") or {})
            telemetry["task_id"] = self.authority["current_task_id"]
            snapshot["telemetry"] = telemetry
            snapshot["authoritative_lifecycle"] = dict(self.authority)
            snapshot["lifecycle_state"] = self.authority["lifecycle_state"]
            snapshot["state"] = "IDLE"
        return projected

    def overlay_managed_runs(self, summary):
        projected = copy.deepcopy(summary)
        for snapshot in projected.get("projects") or []:
            snapshot["state"] = "WORKER_RUNNING"
            snapshot["lifecycle_state"] = "EXECUTING"
            snapshot["telemetry"] = {"task_id": "P16.12"}
        return projected

    def advance(self, _summary, _config, *, decision_summary=None):
        return []


class _CapturingConsumer:
    def __init__(self):
        self.summary = None
        self.calls = 0

    def advance(self, _config, summary, *, executor=None, watchdog=None, planner=None, reviewer=None):
        self.calls += 1
        self.summary = copy.deepcopy(summary)
        return []

    def record_tick_error(self, exc):  # pragma: no cover - failure path guard
        raise AssertionError("consumer should not fail: " + str(exc))


class AuthorityProjectionDerivationTests(unittest.TestCase):
    """Every tick consumer must derive lifecycle truth from the one authority.

    Regression for the projection half of SINGLE_ACTIVE_LIFECYCLE_OWNER and
    CURRENT_TASK_MATCHES_ACTIVE_EXECUTION: a stale predecessor managed run must
    not be able to relabel the authoritative successor in the Watchdog summary,
    the Activation Supervisor summary, the dispatch view, or the published
    projection.
    """

    def _tick(self, executor, watchdog, supervisor):
        raw = {"projects": [{
            "project_id": "p1", "repo_path": "repo", "state": "IDLE",
            "lifecycle_state": "PENDING_DESIGN", "telemetry": {"task_id": "P16.13"},
        }]}
        dispatched: list = []
        with tempfile.TemporaryDirectory() as td, \
             patch("dev_orchestrator.daemon.run_monitor_once", return_value=raw), \
             patch("dev_orchestrator.daemon.write_project_statuses", return_value=[]), \
             patch(
                 "dev_orchestrator.daemon.dispatch_worker_done_events",
                 side_effect=lambda s, *_a, **_k: dispatched.append(copy.deepcopy(s)),
             ), \
             patch("dev_orchestrator.daemon.consume_websol_responses", return_value=None):
            result = _run_orchestration_tick(
                "config.json", Path(td), object(), executor,
                watchdog=watchdog, supervisor=supervisor, pid=123,
            )
        return result, dispatched

    def _assert_authoritative(self, snapshot, *, label):
        self.assertEqual(
            snapshot.get("telemetry", {}).get("task_id"), "P16.13",
            label + " observed a non-authoritative task id",
        )
        self.assertEqual(
            snapshot.get("lifecycle_state"), "PENDING_DESIGN",
            label + " observed a non-authoritative lifecycle state",
        )
        self.assertNotEqual(
            snapshot.get("state"), "WORKER_RUNNING",
            label + " reported a pending-design successor as executing",
        )
        self.assertEqual(
            snapshot.get("authoritative_lifecycle", {}).get("current_task_id"), "P16.13",
            label + " was not derived from the authoritative lifecycle record",
        )

    def test_watchdog_supervisor_and_published_views_derive_from_authority(self):
        executor = _StalePredecessorExecutor()
        watchdog = _CapturingConsumer()
        supervisor = _CapturingConsumer()

        result, dispatched = self._tick(executor, watchdog, supervisor)

        self.assertEqual(watchdog.calls, 1)
        self.assertEqual(supervisor.calls, 1)
        self.assertEqual(len(dispatched), 1)
        for label, snapshot in (
            ("watchdog", watchdog.summary["projects"][0]),
            ("supervisor", supervisor.summary["projects"][0]),
            ("dispatch view", dispatched[0]["projects"][0]),
            ("published projection", result["projects"][0]),
        ):
            with self.subTest(consumer=label):
                self._assert_authoritative(snapshot, label=label)

    def test_stale_role_projection_cannot_relabel_authoritative_lifecycle(self):
        """A stale predecessor plan must not relabel the authoritative successor.

        ``overlay_orchestration_lifecycle`` rewrites ``lifecycle_state`` from the
        Planner/Reviewer ledgers.  Those ledgers are projections, so a leftover
        P16.12 plan in ``planning`` must not make the authoritative
        PENDING_DESIGN P16.13 successor look like it is being planned.
        """
        executor = _StalePredecessorExecutor()
        watchdog = _CapturingConsumer()
        supervisor = _CapturingConsumer()
        raw = {"projects": [{
            "project_id": "p1", "repo_path": "repo", "state": "IDLE",
            "lifecycle_state": "PENDING_DESIGN", "telemetry": {"task_id": "P16.13"},
        }]}
        stale_plan = {"plans": {"old": {
            "project_id": "p1", "task_id": "P16.12", "state": "planning",
            "created_at": "2026-09-25T00:00:00+00:00",
        }}}

        class _Planner:
            def state(self):
                return stale_plan

        class _Controls:
            planner = _Planner()

            def advance(self, _config, _summary, _executor):
                return []

        with tempfile.TemporaryDirectory() as td, \
             patch("dev_orchestrator.daemon.run_monitor_once", return_value=raw), \
             patch("dev_orchestrator.daemon.write_project_statuses", return_value=[]), \
             patch("dev_orchestrator.daemon.dispatch_worker_done_events", return_value=None), \
             patch("dev_orchestrator.daemon.consume_websol_responses", return_value=None):
            result = _run_orchestration_tick(
                "config.json", Path(td), object(), executor, controls=_Controls(),
                watchdog=watchdog, supervisor=supervisor, pid=123,
            )

        for label, snapshot in (
            ("watchdog", watchdog.summary["projects"][0]),
            ("supervisor", supervisor.summary["projects"][0]),
            ("published projection", result["projects"][0]),
        ):
            with self.subTest(consumer=label):
                self._assert_authoritative(snapshot, label=label)

    def test_authority_overlay_is_idempotent_across_repeated_ticks(self):
        executor = _StalePredecessorExecutor()
        first, _ = self._tick(executor, _CapturingConsumer(), _CapturingConsumer())
        second, _ = self._tick(executor, _CapturingConsumer(), _CapturingConsumer())

        self.assertEqual(first["projects"][0]["telemetry"], second["projects"][0]["telemetry"])
        self.assertEqual(
            first["projects"][0]["authoritative_lifecycle"],
            second["projects"][0]["authoritative_lifecycle"],
        )
        self.assertEqual(first["projects"][0]["state"], second["projects"][0]["state"])
        self.assertNotIn("_watchdog_tick_error", second)
        self.assertNotIn("_supervisor_tick_error", second)

    def test_tick_without_authority_capable_executor_is_unchanged(self):
        """Executors predating the authority record keep their legacy projection."""

        class LegacyExecutor:
            def overlay_managed_runs(self, summary):
                projected = copy.deepcopy(summary)
                for snapshot in projected.get("projects") or []:
                    snapshot["view"] = "legacy-overlay"
                return projected

            def advance(self, _summary, _config, *, decision_summary=None):
                return []

        watchdog = _CapturingConsumer()
        supervisor = _CapturingConsumer()
        result, _ = self._tick(LegacyExecutor(), watchdog, supervisor)

        self.assertEqual(result["projects"][0]["view"], "legacy-overlay")
        self.assertNotIn("authoritative_lifecycle", result["projects"][0])
        self.assertEqual(watchdog.calls, 1)
        self.assertEqual(supervisor.calls, 1)


class ReviewFindingRegressionTests(unittest.TestCase):
    """Technical Review findings B2-B4 from the P16.13 independent review."""

    # -- B3: barrier scoping ----------------------------------------------
    def _pending_obligation_state(self, barrier: dict | None) -> dict:
        executions = {"run-pred": {
            "project_id": "p1", "task_id": "P16.12", "state": "completed",
            "review_state": "pending", "source_request_id": "wd-pred",
            "completed_at": "2026-09-26T05:00:00+00:00",
        }}
        if barrier is not None:
            executions["barrier"] = barrier
        return {"executions": executions, "lifecycle": {}, "transitions": {}}

    def test_B3_unconsumed_cross_task_barrier_cannot_drain_an_obligation(self):
        """P16.13's own recovery mints freshly stamped cross-task barriers."""
        state = self._pending_obligation_state({
            "project_id": "p1", "task_id": "P12.6", "source_task_id": "P12.6",
            "target_task_id": "P12.7", "state": "handoff",
            "recorded_at": "2026-09-26T06:00:00+00:00",
        })
        self.assertEqual(
            [o.get("task_id") for o in active_owners("p1", state)], ["P16.12"],
            "an unconsumed barrier for an unrelated task drained a live obligation",
        )
        self.assertEqual(
            [o.get("task_id") for o in source_ownership_blockers("p1", "P16.12", state)],
            ["P16.12"],
        )

    def test_B3_consumed_history_and_same_task_barriers_still_supersede(self):
        """The accepted legacy-migration behavior must be preserved."""
        consumed = self._pending_obligation_state({
            "project_id": "p1", "task_id": "P12.6", "state": "handoff",
            "handoff_consumed": True,
            "handoff_consumed_at": "2026-09-26T06:00:00+00:00",
        })
        self.assertEqual(active_owners("p1", consumed), [])
        same_task = self._pending_obligation_state({
            "project_id": "p1", "task_id": "P16.12", "state": "settled",
            "recorded_at": "2026-09-26T06:00:00+00:00",
        })
        self.assertEqual(active_owners("p1", same_task), [])

    def test_B3_unknown_ordering_fails_closed(self):
        state = self._pending_obligation_state({
            "project_id": "p1", "task_id": "P16.12", "state": "settled",
        })
        state["executions"]["run-pred"].pop("completed_at")
        self.assertEqual(
            [o.get("task_id") for o in active_owners("p1", state)], ["P16.12"],
            "an unparseable barrier ordering drained the obligation",
        )

    # -- B2 / B4: evidence and disposition --------------------------------
    def _next_decision_fixture(self, actuation_state: str | None):
        snapshot = {"project_id": "p1", "telemetry": {"task_id": "P2"}, "state": "IDLE"}
        executions = {}
        if actuation_state is not None:
            executions["ai_review:d1"] = {
                "project_id": "p1", "task_id": "P1", "state": actuation_state,
            }
        state = {
            "lifecycle": {"p1": {"current_task_id": "P1", "lifecycle_state": "REVIEWING"}},
            "executions": executions, "transitions": {},
        }
        decisions = {"decisions": {"d1": {
            "project_id": "p1", "task_id": "P1", "disposition": "apply",
            "decision": "next", "next_action": "next_task",
            "request_id": "ai_review:d1", "consumed_at": "2026-09-22T20:51:00+00:00",
        }}}
        return snapshot, state, decisions

    def _finding(self, snapshot, state, decisions=None):
        kwargs = {"snapshot": snapshot, "executor_state": state}
        if decisions is not None:
            kwargs["decisions_state"] = decisions
        return {
            item.code: item for item in evaluate_lifecycle_invariants(**kwargs)
        }["NEXT_TASK_WITHOUT_HANDOFF"]

    def test_B2_missing_decisions_evidence_is_not_reported_as_satisfied(self):
        snapshot, state, _ = self._next_decision_fixture(None)
        finding = self._finding(snapshot, state)
        self.assertFalse(
            finding.holds,
            "the evaluator claimed an invariant held that it had no evidence to test",
        )
        self.assertTrue(finding.evidence.get("evidence_unavailable"))
        self.assertFalse(finding.recoverable, "absent evidence must not drive recovery")

    def test_B2_reconciliation_records_the_same_verdict_as_an_independent_check(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _repo(root)
            runtime = root / "runtime"
            runtime.mkdir(parents=True, exist_ok=True)
            executor = TransitionExecutor(runtime)
            (runtime / "review-decisions.json").write_text(json.dumps({"decisions": {"d1": {
                "project_id": "p1", "task_id": "P1", "disposition": "apply",
                "decision": "next", "next_action": "next_task",
                "request_id": "ai_review:d1",
                "consumed_at": "2026-09-22T20:51:00+00:00",
            }}}), encoding="utf-8")

            snapshot = _summary(repo, "P2")["projects"][0]
            executor.reconcile_lifecycle_authority({"projects": [snapshot]})
            recorded = {
                row["code"]: row
                for row in executor.state()["lifecycle"]["p1"]["invariants"]
            }["NEXT_TASK_WITHOUT_HANDOFF"]

            independent = self._finding(
                snapshot, executor.state(),
                json.loads((runtime / "review-decisions.json").read_text(encoding="utf-8")),
            )
            self.assertEqual(
                recorded["holds"], independent.holds,
                "the authority of record disagreed with an independent evaluation",
            )
            self.assertFalse(
                recorded["holds"],
                "an unsatisfied NEXT_TASK decision was attested as satisfied",
            )
            # Pin the executor half: the verdict must come from the ledger, not
            # from the evaluator's absent-evidence fallback.
            self.assertFalse(
                recorded["evidence"]["evidence_unavailable"],
                "reconciliation reached its verdict without reading the decisions ledger",
            )
            self.assertEqual(
                [row["task_id"] for row in recorded["evidence"]["next_decisions"]],
                ["P1"],
            )

    def test_B4_durably_blocked_actuation_gates_instead_of_recovering(self):
        snapshot, state, decisions = self._next_decision_fixture("blocked")
        finding = self._finding(snapshot, state, decisions)
        self.assertFalse(finding.holds)
        self.assertFalse(
            finding.recoverable,
            "a durably blocked actuation was offered to recovery, which cannot converge",
        )
        self.assertTrue(finding.evidence["next_decisions"][0]["actuation_blocked"])

    def test_B4_missing_actuation_remains_zero_touch_recoverable(self):
        """Liveness: a genuinely lost handoff must still heal automatically."""
        snapshot, state, decisions = self._next_decision_fixture(None)
        finding = self._finding(snapshot, state, decisions)
        self.assertFalse(finding.holds)
        self.assertTrue(finding.recoverable)

    def test_B4_blocked_obligation_gates_the_authority_idempotently(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _repo(root)
            executor = TransitionExecutor(root / "runtime")
            ledger = executor.state()
            ledger["executions"]["run-p1"] = {
                "project_id": "p1", "task_id": "P1", "state": "completed",
                "review_state": "pending", "source_request_id": "req-p1",
                "completed_at": "2026-09-22T20:00:00+00:00",
            }
            ledger["executions"]["ai_review:req-p1"] = {
                "project_id": "p1", "task_id": "P1", "state": "blocked",
                "reason": "project is not READY_TO_RUN",
            }
            executor._save_ledger(ledger)

            snapshot = _summary(repo, "P2")["projects"][0]
            reviewer_state = {"reviews": {"ai_review:req-p1": {
                "project_id": "p1", "task_id": "P1", "state": "completed",
            }}}
            executor.reconcile_lifecycle_authority(
                {"projects": [snapshot]}, reviewer_state=reviewer_state)
            authority = executor.state()["lifecycle"]["p1"]
            self.assertEqual(authority["lifecycle_state"], "OWNER_GATE")
            self.assertEqual(
                (authority.get("owner_gate") or {}).get("code"),
                "NEXT_TASK_WITHOUT_HANDOFF",
                "a blocked obligation pinned the authority with no owner-visible gate",
            )

            first = copy.deepcopy(authority["owner_gate"])
            for _ in range(3):
                executor.reconcile_lifecycle_authority(
                    {"projects": [snapshot]}, reviewer_state=reviewer_state)
            again = executor.state()["lifecycle"]["p1"]["owner_gate"]
            self.assertEqual(again.get("code"), first.get("code"))
            self.assertEqual(again.get("authority_task_id"), first.get("authority_task_id"))
            self.assertEqual(
                again.get("recorded_at"), first.get("recorded_at"),
                "the authority gate was re-stamped on repeated ticks",
            )


class BoundedRecoveryActuationTests(unittest.TestCase):
    """B1/N4: drive the real Watchdog so recovery actuation itself is covered."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.runtime = root / "runtime"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.repo = _repo(root)
        self.config_path = self.runtime / "projects.json"
        self.config_path.write_text(json.dumps({"projects": [{
            "project_id": "p1",
            "repo_path": str(self.repo),
            "watchdog": {"enabled": True, "auto_recovery": True,
                         "no_progress_threshold_minutes": 1},
        }]}), encoding="utf-8")
        (self.runtime / "review-decisions.json").write_text(json.dumps({"decisions": {"d1": {
            "project_id": "p1", "task_id": "P1", "disposition": "apply",
            "decision": "next", "next_action": "next_task",
            "request_id": "ai_review:d1", "consumed_at": "2026-09-22T20:51:00+00:00",
        }}}), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    class _CountingExecutor:
        """Handoff recovery that always fails for the given reason."""

        def __init__(self, reason, waiting=False):
            self.calls = 0
            self.reason = reason
            self.waiting = waiting
            self.ledger = {
                "executions": {}, "transitions": {},
                "lifecycle": {"p1": {
                    "current_task_id": "P1", "lifecycle_state": "REVIEWING",
                }},
            }

        def state(self):
            return copy.deepcopy(self.ledger)

        def reconcile_successor_handoff(self, _pcfg, _snapshot, *, completed_task_id, trigger):
            self.calls += 1
            return {
                "status": "blocked", "reason": self.reason,
                "waiting": self.waiting,
            }

    def _drive(self, reason, ticks, waiting=False):
        from dev_orchestrator.core.watchdog import WatchdogCoordinator

        executor = self._CountingExecutor(reason, waiting=waiting)
        watchdog = WatchdogCoordinator(self.runtime)
        snapshot = _summary(self.repo, "P2")["projects"][0]
        for _ in range(ticks):
            watchdog.advance(str(self.config_path), {"projects": [snapshot]},
                             executor=executor)
            for thread in getattr(watchdog, "_threads", {}).values():
                thread.join(timeout=10.0)
        row = (watchdog.state().get("projects") or {}).get("p1") or {}
        return executor, row

    def test_B1_non_transient_failure_gates_and_stops_actuating(self):
        executor, row = self._drive("agent/staged/roadmap.json does not exist", ticks=6)
        self.assertGreaterEqual(executor.calls, 1, "recovery never ran at all")
        self.assertLessEqual(
            executor.calls, 2,
            f"recovery kept actuating after the owner gate was declared "
            f"({executor.calls} calls over 6 ticks)",
        )
        gate = row.get("owner_gate")
        self.assertIsInstance(gate, dict, "no durable owner gate was recorded")
        self.assertEqual(gate.get("code"), "NEXT_TASK_WITHOUT_HANDOFF")
        self.assertTrue(gate.get("gate_id"), "owner gate carries no stable identity")
        attempts = row.get("lifecycle_recovery_attempts") or {}
        self.assertTrue(attempts, "no recovery attempt was recorded")
        self.assertEqual(
            {a.get("state") for a in attempts.values()}, {"gated"},
            "the gated recovery attempt was not marked gated",
        )

    def test_B1_owner_gate_recorded_at_is_durable_across_ticks(self):
        """The gate must be declared once, not re-stamped every tick."""
        from dev_orchestrator.core.watchdog import WatchdogCoordinator

        executor = self._CountingExecutor("agent/staged/roadmap.json does not exist")
        watchdog = WatchdogCoordinator(self.runtime)
        snapshot = _summary(self.repo, "P2")["projects"][0]
        stamps = []
        for _ in range(6):
            watchdog.advance(str(self.config_path), {"projects": [snapshot]},
                             executor=executor)
            for thread in getattr(watchdog, "_threads", {}).values():
                thread.join(timeout=10.0)
            gate = ((watchdog.state().get("projects") or {}).get("p1") or {}).get("owner_gate")
            if isinstance(gate, dict) and gate.get("recorded_at"):
                stamps.append(gate["recorded_at"])
        self.assertTrue(stamps, "no owner gate was ever declared")
        self.assertEqual(
            len(set(stamps)), 1,
            f"the owner gate was re-stamped across ticks: {sorted(set(stamps))}",
        )

    def test_B1_attempt_counter_stops_climbing_past_the_gate(self):
        _, row = self._drive("agent/staged/roadmap.json does not exist", ticks=8)
        attempts = row.get("lifecycle_recovery_attempts") or {}
        counts = [int(a.get("count") or 0) for a in attempts.values()]
        self.assertTrue(
            counts and max(counts) <= 2,
            f"recovery attempt counter kept climbing past the gate: {counts}",
        )

    def test_B1_legacy_failed_attempt_is_fenced_when_actuation_is_already_blocked(self):
        """Restart migrates old loop evidence without retrying blocked work."""
        from dev_orchestrator.core.watchdog import WatchdogCoordinator

        head = _git(self.repo, "rev-parse", "HEAD")
        key = f"P1:{head}"
        snapshot = _summary(self.repo, "P2")["projects"][0]
        seed_executor = self._CountingExecutor(
            "agent/staged/roadmap.json does not exist",
        )
        seed_watchdog = WatchdogCoordinator(self.runtime)
        seed_watchdog.advance(
            str(self.config_path), {"projects": [snapshot]}, executor=seed_executor,
        )
        legacy_state = json.loads(
            (self.runtime / "watchdog.json").read_text(encoding="utf-8"),
        )
        legacy_state["projects"]["p1"]["lifecycle_recovery_attempts"][key] = {
            "count": 244,
            "state": "failed",
            "result": {
                "status": "blocked",
                "reason": "agent/staged/roadmap.json does not exist",
            },
        }
        (self.runtime / "watchdog.json").write_text(
            json.dumps(legacy_state), encoding="utf-8",
        )
        executor = self._CountingExecutor("must not be called")
        executor.ledger["executions"]["ai_review:d1"] = {
            "project_id": "p1",
            "task_id": "P1",
            "state": "blocked",
        }
        watchdog = WatchdogCoordinator(self.runtime)

        watchdog.advance(
            str(self.config_path), {"projects": [snapshot]}, executor=executor,
        )

        row = (watchdog.state().get("projects") or {}).get("p1") or {}
        attempt = (row.get("lifecycle_recovery_attempts") or {}).get(key) or {}
        self.assertEqual(executor.calls, 0, "blocked actuation was retried during migration")
        self.assertEqual(attempt.get("count"), 244, "historical attempt evidence changed")
        self.assertEqual(attempt.get("state"), "gated")
        self.assertTrue(attempt.get("fenced"))
        self.assertEqual(
            (row.get("owner_gate") or {}).get("code"),
            "NEXT_TASK_WITHOUT_HANDOFF",
        )

    def test_B1_malformed_legacy_attempts_still_records_nonrecoverable_gate(self):
        """Malformed legacy evidence is preserved while the gate fails closed."""
        from dev_orchestrator.core.watchdog import WatchdogCoordinator

        snapshot = _summary(self.repo, "P2")["projects"][0]
        seed_executor = self._CountingExecutor(
            "agent/staged/roadmap.json does not exist",
        )
        seed_watchdog = WatchdogCoordinator(self.runtime)
        seed_watchdog.advance(
            str(self.config_path), {"projects": [snapshot]}, executor=seed_executor,
        )
        seed_state = json.loads(
            (self.runtime / "watchdog.json").read_text(encoding="utf-8"),
        )
        for malformed in (None, ["preserve-this-legacy-evidence"], "legacy-scalar"):
            with self.subTest(malformed=malformed):
                legacy_state = copy.deepcopy(seed_state)
                legacy_state["projects"]["p1"]["lifecycle_recovery_attempts"] = malformed
                (self.runtime / "watchdog.json").write_text(
                    json.dumps(legacy_state), encoding="utf-8",
                )
                executor = self._CountingExecutor("must not be called")
                executor.ledger["executions"]["ai_review:d1"] = {
                    "project_id": "p1",
                    "task_id": "P1",
                    "state": "blocked",
                }
                watchdog = WatchdogCoordinator(self.runtime)

                watchdog.advance(
                    str(self.config_path), {"projects": [snapshot]}, executor=executor,
                )

                row = (watchdog.state().get("projects") or {}).get("p1") or {}
                self.assertEqual(executor.calls, 0, "malformed evidence triggered recovery")
                self.assertEqual(row.get("lifecycle_recovery_attempts"), malformed)
                self.assertEqual(
                    row.get("lifecycle_recovery_evidence_error"),
                    "lifecycle_recovery_attempts is not an object",
                )
                self.assertEqual(
                    (row.get("owner_gate") or {}).get("code"),
                    "NEXT_TASK_WITHOUT_HANDOFF",
                )
                self.assertFalse(
                    str(row.get("last_error") or "").startswith(
                        "lifecycle_invariant_evaluation_failed",
                    ),
                )

    def test_B1_resolved_lifecycle_invariant_archives_and_clears_owner_gate(self):
        """A recovered invariant must not leave a sticky published gate."""
        from dev_orchestrator.core.watchdog import WatchdogCoordinator

        executor = self._CountingExecutor(
            "agent/staged/roadmap.json does not exist",
        )
        watchdog = WatchdogCoordinator(self.runtime)
        snapshot = _summary(self.repo, "P2")["projects"][0]
        for _ in range(2):
            watchdog.advance(
                str(self.config_path), {"projects": [snapshot]}, executor=executor,
            )
        gated = (watchdog.state().get("projects") or {}).get("p1") or {}
        gate = gated.get("owner_gate") or {}
        self.assertEqual(gate.get("code"), "NEXT_TASK_WITHOUT_HANDOFF")

        executor.ledger["executions"]["ai_review:d1"] = {
            "project_id": "p1",
            "task_id": "P1",
            "state": "settled",
            "outcome": "task_complete",
            "recorded_at": "2026-09-22T21:00:00+00:00",
        }
        watchdog.advance(
            str(self.config_path), {"projects": [snapshot]}, executor=executor,
        )
        recovered = (watchdog.state().get("projects") or {}).get("p1") or {}
        self.assertIsNone(recovered.get("owner_gate"))
        self.assertEqual(recovered.get("unresolved_invariants"), [])
        history = recovered.get("resolved_lifecycle_owner_gates") or []
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].get("gate_id"), gate.get("gate_id"))
        self.assertEqual(
            history[0].get("resolved_reason"),
            "lifecycle invariant now holds",
        )

        watchdog.advance(
            str(self.config_path), {"projects": [snapshot]}, executor=executor,
        )
        replayed = (watchdog.state().get("projects") or {}).get("p1") or {}
        self.assertIsNone(replayed.get("owner_gate"))
        self.assertEqual(len(replayed.get("resolved_lifecycle_owner_gates") or []), 1)

    def test_NA_undrained_source_ownership_is_a_wait_not_a_refusal(self):
        """Review finding N-A: only _record_handoff rebuilds the durable handoff.

        Gating the "ownership is not terminal" wait would fence off the only
        code path that can ever publish it, converting a self-healing wait into
        a permanent stall.
        """
        executor, row = self._drive(
            "source-task lifecycle ownership is not terminal", ticks=6, waiting=True)
        self.assertEqual(
            executor.calls, 6,
            f"a source-ownership wait was gated after {executor.calls} of 6 ticks",
        )
        self.assertIsNone(
            row.get("owner_gate"),
            "a transient ownership wait was escalated to an owner gate",
        )
        states = {a.get("state") for a in (row.get("lifecycle_recovery_attempts") or {}).values()}
        self.assertNotIn("gated", states)

    def test_NB_non_mapping_decisions_ledger_does_not_gate_every_project(self):
        """Review finding N-B: a malformed ledger must normalize, not gate."""
        from dev_orchestrator.core.watchdog import WatchdogCoordinator

        (self.runtime / "review-decisions.json").write_text("[]", encoding="utf-8")
        executor = self._CountingExecutor("unused")
        watchdog = WatchdogCoordinator(self.runtime)
        snapshot = _summary(self.repo, "P2")["projects"][0]
        watchdog.advance(str(self.config_path), {"projects": [snapshot]}, executor=executor)
        for thread in getattr(watchdog, "_threads", {}).values():
            thread.join(timeout=10.0)
        row = (watchdog.state().get("projects") or {}).get("p1") or {}
        codes = {
            item["code"] for item in (row.get("lifecycle_invariants") or [])
            if not item.get("holds")
        }
        self.assertNotIn(
            "NEXT_TASK_WITHOUT_HANDOFF", codes,
            "a non-mapping decisions ledger was treated as absent evidence and gated",
        )

    def test_B1_transient_dirty_worktree_keeps_retrying(self):
        """A zero-touch dirty-tree wait must not be gated away."""
        executor, row = self._drive(
            "successor repair requires a clean repository", ticks=5)
        self.assertEqual(
            executor.calls, 5,
            f"a transient dirty-tree wait stopped retrying ({executor.calls}/5)",
        )
        self.assertIsNone(
            row.get("owner_gate"), "a transient condition was gated to the owner")


class _PlanningPort:
    """Approves one bounded plan, so the real Planner can run end to end."""

    def __init__(self):
        self.requests = []

    def execute(self, request):
        from dev_orchestrator.ai.contracts import AIRoleResult, ResourceContext

        self.requests.append(request)
        if request.role == "planner":
            payload = json.dumps({
                "task_id": request.task_run_id,
                "summary": "Freeze a bounded implementation plan.",
                "implementation_steps": ["Add the seam", "Implement the bounded change"],
                "interfaces": ["Keep the public ABI stable"],
                "validation": ["Run the focused tests", "Run the regression"],
                "risks": ["Do not expand scope"],
                "out_of_scope": ["No unrelated refactor"],
            })
            suffix = "plan"
        else:
            payload = json.dumps({"decision": "approve", "reason": "bounded and testable"})
            suffix = "review"
        return AIRoleResult(
            request_id=request.request_id,
            role_run_id=request.role_run_id,
            status="succeeded",
            output=payload,
            dispatch_id="dispatch-" + suffix,
            decision_id="decision-" + suffix,
            execution_id="execution-" + suffix,
            resource_context=ResourceContext("r-" + suffix, "p", "a", "m"),
        )


class ZeroTouchSuccessorHandoffEndToEndTests(unittest.TestCase):
    """Acceptance 4, 5, 6, 7 and 8 across the real component boundary.

    Every other P16.13 test calls the executor or the resolver directly. This one
    drives the real WatchdogCoordinator, TransitionExecutor,
    ControlCommandCoordinator and AIPlannerCoordinator in the order the daemon
    tick runs them, starting from the matrix-B fault (an accepted NEXT_TASK
    decision whose durable handoff was lost) and asserting that the successor
    Planner starts with no owner command anywhere in the loop.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.runtime = root / "runtime"
        self.runtime.mkdir(parents=True, exist_ok=True)
        # Predecessor P1 is COMPLETE; P2 is staged and named by the roadmap.
        self.repo = _repo(root, roadmap_target="P2", current="P1")
        (self.repo / "agent" / "next.md").write_bytes(
            b"# P1 task\n\nStatus: **COMPLETE**\n"
        )
        _commit(self.repo, "predecessor complete")

        self.project = {
            "project_id": "p1",
            "repo_path": str(self.repo),
            "execution": {"engine": "aibroker"},
            "ai_roles": {"planner": {
                "enabled": True, "quality": "high", "review_quality": "high",
                "review_independence": "resource",
            }},
            "watchdog": {"enabled": True, "auto_recovery": True,
                         "no_progress_threshold_minutes": 1},
        }
        self.config_path = self.runtime / "projects.json"
        self.config_path.write_text(
            json.dumps({"projects": [self.project]}), encoding="utf-8")

        # Matrix B: the reviewer's NEXT_TASK verdict was accepted, but the
        # durable handoff never landed.
        (self.runtime / "review-decisions.json").write_text(json.dumps({"decisions": {
            "ai_review:d1": {
                "project_id": "p1", "task_id": "P1", "disposition": "apply",
                "decision": "next", "next_action": "next_task",
                "request_id": "ai_review:d1",
                "consumed_at": "2026-09-26T00:00:00+00:00",
            },
        }}), encoding="utf-8")

        self.executor = TransitionExecutor(self.runtime)
        self.port = _PlanningPort()

    def tearDown(self):
        self._tmp.cleanup()

    def _snapshot(self):
        """What the monitor would publish, with the authority overlaid."""
        summary = {"projects": [{
            "project_id": "p1", "repo_path": str(self.repo),
            "state": "IDLE",
            "next_title": "P1 task", "next_status": "**COMPLETE**",
            "telemetry": {"task_id": "P1"},
            "git": {"head": _git(self.repo, "rev-parse", "HEAD"),
                    "branch": _git(self.repo, "rev-parse", "--abbrev-ref", "HEAD")},
        }]}
        return self.executor.overlay_lifecycle_authority(summary)

    def _owner_commands(self):
        """Any owner/continue command that reached the control plane."""
        inbox = self.runtime / "control" / "inbox"
        history = self.runtime / "control" / "history"
        found = []
        for base in (inbox, history):
            if not base.exists():
                continue
            for path in base.glob("*.json"):
                row = json.loads(path.read_text(encoding="utf-8"))
                if str(row.get("source") or "") != "automatic_review_handoff":
                    found.append({"path": path.name, "source": row.get("source"),
                                  "action": row.get("action")})
        return found

    def _tick(self, watchdog, controls):
        """One ordered tick: watchdog recovery, then control-plane actuation."""
        snapshot_summary = self._snapshot()
        watchdog.advance(str(self.config_path), snapshot_summary, executor=self.executor)
        for thread in getattr(watchdog, "_threads", {}).values():
            thread.join(timeout=10.0)
        controls.advance(str(self.config_path), self._snapshot(), self.executor)

    def test_lost_handoff_converges_and_starts_the_successor_planner(self):
        from dev_orchestrator.core.ai_planner import AIPlannerCoordinator
        from dev_orchestrator.core.control_commands import ControlCommandCoordinator
        from dev_orchestrator.core.watchdog import WatchdogCoordinator

        planner = AIPlannerCoordinator(self.runtime, self.port)
        controls = ControlCommandCoordinator(self.runtime, planner)
        watchdog = WatchdogCoordinator(self.runtime, planner=planner)

        # Acceptance 2: the lost handoff is a detected violation, not a
        # terminal-settled complete project.
        findings = {
            item.code: item for item in evaluate_lifecycle_invariants(
                snapshot=self._snapshot()["projects"][0],
                executor_state=self.executor.state(),
                decisions_state=json.loads(
                    (self.runtime / "review-decisions.json").read_text(encoding="utf-8")),
            )
        }
        self.assertFalse(findings["NEXT_TASK_WITHOUT_HANDOFF"].holds)
        self.assertTrue(findings["NEXT_TASK_WITHOUT_HANDOFF"].recoverable)

        self._tick(watchdog, controls)
        for thread in getattr(planner, "_threads", {}).values():
            thread.join(timeout=15.0)

        # Acceptance 3: exactly one handoff was created automatically.
        handoffs = [
            row for row in self.executor.state()["executions"].values()
            if isinstance(row, dict) and row.get("project_id") == "p1"
            and row.get("next_task_id") == "P2"
        ]
        self.assertEqual(len(handoffs), 1, f"expected one handoff, got {len(handoffs)}")

        # Acceptance 5: lineage is source=P1, target=P2.
        self.assertEqual(handoffs[0]["source_task_id"], "P1")
        self.assertEqual(handoffs[0]["target_task_id"], "P2")

        # Acceptance 4: the successor Planner started with no owner continue.
        plans = [
            row for row in planner.state()["plans"].values()
            if isinstance(row, dict) and row.get("project_id") == "p1"
        ]
        self.assertEqual(
            len(plans), 1,
            f"the successor Planner did not start automatically: {plans}",
        )
        self.assertEqual(plans[0].get("task_id"), "P2")
        self.assertEqual(
            self._owner_commands(), [],
            "an owner command was required, so recovery was not zero-touch",
        )

        # Acceptance 5 (authority half): the handoff was consumed and the
        # authority advanced to the successor, keeping the predecessor as source.
        authority = self.executor.state()["lifecycle"]["p1"]
        self.assertEqual(authority["current_task_id"], "P2")
        self.assertEqual(authority["source_task_id"], "P1")
        self.assertIsNone(authority.get("owner_gate"), "recovery stopped at an owner gate")
        self.assertTrue(handoffs[0].get("handoff_consumed"))

        # Acceptance 8: the repository actually advanced to the successor.
        self.assertIn("P2", (self.repo / "agent" / "next.md").read_text(encoding="utf-8"))

        # Acceptance 6: the lost-handoff violation is resolved afterwards.
        after = {
            item.code: item for item in evaluate_lifecycle_invariants(
                snapshot=self._snapshot()["projects"][0],
                executor_state=self.executor.state(),
                decisions_state=json.loads(
                    (self.runtime / "review-decisions.json").read_text(encoding="utf-8")),
            )
        }
        unresolved = [code for code, item in after.items() if not item.holds]
        self.assertEqual(
            unresolved, [],
            f"lifecycle invariants did not converge after the handoff: {unresolved}",
        )

        # Acceptance 7: repeated ticks are idempotent -- no second handoff, no
        # second plan, no owner gate, no duplicate planner launch.
        for _ in range(3):
            self._tick(watchdog, controls)
            for thread in getattr(planner, "_threads", {}).values():
                thread.join(timeout=15.0)
        self.assertEqual(
            len([
                row for row in self.executor.state()["executions"].values()
                if isinstance(row, dict) and row.get("project_id") == "p1"
                and row.get("next_task_id") == "P2"
            ]), 1, "repeated ticks duplicated the handoff")
        self.assertEqual(
            len([
                row for row in planner.state()["plans"].values()
                if isinstance(row, dict) and row.get("project_id") == "p1"
            ]), 1, "repeated ticks launched a duplicate Planner")
        self.assertEqual(self._owner_commands(), [])


class DeferredReviewFindingTests(unittest.TestCase):
    """Deferred NON_BLOCKING review findings N1 and N2."""

    def test_N1_executor_state_loss_does_not_publish_a_clean_bill_of_health(self):
        """Absent ownership evidence must be recorded, not answered."""
        from dev_orchestrator.core.watchdog import WatchdogCoordinator

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / "runtime"
            runtime.mkdir(parents=True, exist_ok=True)
            repo = _repo(root)
            config_path = runtime / "projects.json"
            config_path.write_text(json.dumps({"projects": [{
                "project_id": "p1", "repo_path": str(repo),
                "watchdog": {"enabled": True, "auto_recovery": True,
                             "no_progress_threshold_minutes": 1},
            }]}), encoding="utf-8")

            class _BrokenExecutor:
                def state(self):
                    raise RuntimeError("ledger is unreadable")

            watchdog = WatchdogCoordinator(runtime)
            snapshot = _summary(repo, "P2")["projects"][0]
            watchdog.advance(str(config_path), {"projects": [snapshot]},
                             executor=_BrokenExecutor())
            for thread in getattr(watchdog, "_threads", {}).values():
                thread.join(timeout=10.0)

            row = (watchdog.state().get("projects") or {}).get("p1") or {}
            self.assertEqual(
                row.get("lifecycle_invariants"), [],
                "invariants were evaluated from an empty ledger",
            )
            self.assertIn(
                "executor_state_unavailable", str(row.get("lifecycle_evidence_unavailable")),
                "the loss of ownership evidence was not recorded",
            )
            self.assertEqual(
                row.get("unresolved_invariants"), [],
                "absent evidence was reported as a set of resolved invariants",
            )

    def test_N2_absent_roadmap_with_staged_claim_fails_closed(self):
        """A missing roadmap is not evidence that the project is complete."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _repo(root)
            # Drop the roadmap entirely; agent/staged/P2.md still declares
            # "Predecessor: P1".
            (repo / "agent" / "staged" / "roadmap.json").unlink()
            _commit(repo, "remove roadmap")

            resolution = resolve_successor(str(repo), "P1")
            self.assertEqual(
                resolution.kind, "inconsistent",
                f"an unambiguous staged claim was ignored: kind={resolution.kind}",
            )
            self.assertEqual(resolution.successor_task_id, "P2")
            self.assertIn("ROADMAP_SUCCESSOR_INCONSISTENT", str(resolution.reason))

    def test_N2_absent_roadmap_without_staged_claim_stays_absent(self):
        """With no staged evidence there is nothing to fail closed on."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _repo(root)
            (repo / "agent" / "staged" / "roadmap.json").unlink()
            (repo / "agent" / "staged" / "P2.md").unlink()
            _commit(repo, "remove roadmap and staged successor")
            self.assertEqual(resolve_successor(str(repo), "P1").kind, "absent")

    def test_N2_failed_reconcile_does_not_leave_a_created_roadmap_behind(self):
        """A rollback must not dirty the tree and block later recovery."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _repo(root)
            roadmap = repo / "agent" / "staged" / "roadmap.json"
            roadmap.unlink()
            _commit(repo, "remove roadmap")
            before = read_repository_truth(repo)
            self.assertFalse(before.dirty, "fixture did not start clean")

            resolution = resolve_successor(str(repo), "P1")
            self.assertEqual(resolution.kind, "inconsistent")

            # Fail the validation stage after the roadmap has been written.
            with patch(
                "dev_orchestrator.core.successor_consistency.read_successor",
                side_effect=RuntimeError("validate failed"),
            ):
                outcome = reconcile_roadmap_successor(str(repo), "P1", resolution)

            self.assertEqual(outcome.status, "rejected")
            self.assertFalse(
                roadmap.exists(),
                "a roadmap created by the failed attempt was left in the worktree",
            )
            after = read_repository_truth(repo)
            self.assertFalse(
                after.dirty,
                "the failed reconcile dirtied the tree, blocking later recovery",
            )


def _complete_repo(root: Path, **kwargs) -> Path:
    """Predecessor P1 COMPLETE in repository truth; roadmap per ``kwargs``."""
    repo = _repo(root, current="P1", **kwargs)
    (repo / "agent" / "next.md").write_bytes(b"# P1 task\n\nStatus: **COMPLETE**\n")
    _commit(repo, "predecessor complete")
    return repo


def _repo_summary(repo: Path) -> dict:
    """What the monitor publishes: current agent/next.md truth, not a fixture."""
    lines = (repo / "agent" / "next.md").read_text(encoding="utf-8").splitlines()
    title = next(line[2:].strip() for line in lines if line.startswith("# "))
    status = next(
        (line.split(":", 1)[1].strip() for line in lines
         if line.strip().casefold().startswith("status:")), "",
    )
    return {"projects": [{
        "project_id": "p1", "repo_path": str(repo), "state": "IDLE",
        "next_title": title, "next_status": status,
        "telemetry": {"task_id": title.split()[0]},
        "git": {"head": _git(repo, "rev-parse", "HEAD"),
                "branch": _git(repo, "rev-parse", "--abbrev-ref", "HEAD")},
    }]}


# The reviewer settled P1 task_complete before the roadmap named a successor:
# terminal history, never a successor handoff.
_STALE_SETTLE = {
    "project_id": "p1", "source_request_id": "ai_review:settled",
    "source_kind": "decision", "task_id": "P1", "state": "settled",
    "outcome": "task_complete",
    "reason": "reviewed task is COMPLETE and no next executable task is advertised",
    "recorded_at": "2026-09-26T06:30:29+00:00",
}


def _terminal_state(executions=None, transitions=None) -> dict:
    return {
        "lifecycle": {"p1": {
            "project_id": "p1", "current_task_id": "P1",
            "lifecycle_state": "COMPLETE", "owner_gate": None,
        }},
        "executions": {"ai_review:settled": dict(_STALE_SETTLE), **(executions or {})},
        "transitions": transitions or {},
    }


def _next_finding(repo: Path, executor_state: dict, decisions=None):
    return {
        item.code: item for item in evaluate_lifecycle_invariants(
            snapshot=_repo_summary(repo)["projects"][0],
            executor_state=executor_state,
            decisions_state={"decisions": {}} if decisions is None else decisions,
        )
    }["NEXT_TASK_WITHOUT_HANDOFF"]


class TerminalRoadmapSuccessorInvariantTests(unittest.TestCase):
    """P16.13 -> P16.14 live incident: COMPLETE authority, valid roadmap
    successor, no surviving NEXT decision and no durable handoff."""

    def test_missing_handoff_is_a_recoverable_violation_without_a_decision(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _complete_repo(Path(td))
            finding = _next_finding(repo, _terminal_state())
            self.assertFalse(finding.holds, "the roadmap successor obligation was ignored")
            self.assertTrue(finding.recoverable)
            self.assertEqual(finding.evidence["next_decisions"], [])
            self.assertEqual(
                (finding.evidence["roadmap_successor"]["source_task_id"],
                 finding.evidence["roadmap_successor"]["target_task_id"],
                 finding.evidence["roadmap_successor"]["kind"]),
                ("P1", "P2", "successor"),
            )

    def test_unique_staged_claim_repairs_null_roadmap_without_human_clock(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _complete_repo(Path(td), roadmap_target=None)

            finding = _next_finding(repo, _terminal_state())

            self.assertFalse(finding.holds)
            self.assertTrue(finding.recoverable)
            successor = finding.evidence["roadmap_successor"]
            self.assertEqual((successor["kind"], successor["target_task_id"]),
                             ("inconsistent", "P2"))

    def test_roadmap_only_successor_without_matching_predecessor_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _complete_repo(Path(td))
            spec = repo / "agent" / "staged" / "P2.md"
            spec.write_bytes(b"# P2 task\n\nStatus: **PENDING DESIGN**\n")
            _commit(repo, "remove predecessor claim")

            finding = _next_finding(repo, _terminal_state())

            self.assertFalse(finding.holds)
            self.assertFalse(finding.recoverable)
            successor = finding.evidence["roadmap_successor"]
            self.assertEqual(successor["kind"], "invalid")
            self.assertIn("exactly one valid staged successor", successor["reason"])

    def test_existing_or_in_flight_handoff_satisfies_the_obligation(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _complete_repo(Path(td))
            handoff = {"h": {"project_id": "p1", "task_id": "P1", "source_task_id": "P1",
                             "target_task_id": "P2", "state": "handoff"}}
            self.assertTrue(_next_finding(repo, _terminal_state(executions=handoff)).holds)
            intent = {"t": {"transition_id": "t", "project_id": "p1", "source_task_id": "P1",
                            "target_task_id": "P2", "state": "intent"}}
            self.assertTrue(_next_finding(repo, _terminal_state(transitions=intent)).holds)

    def test_refused_transition_gates_instead_of_recovering(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _complete_repo(Path(td))
            refused = {"t": {"transition_id": "t", "project_id": "p1", "source_task_id": "P1",
                             "target_task_id": "P2", "state": "waiting_recovery"}}
            finding = _next_finding(repo, _terminal_state(transitions=refused))
            self.assertFalse(finding.holds)
            self.assertFalse(finding.recoverable)

    def test_ambiguous_successor_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            # Roadmap names P3 while staged P2 claims P1 as its predecessor.
            repo = _complete_repo(Path(td), roadmap_target="P3")
            finding = _next_finding(repo, _terminal_state())
            self.assertFalse(finding.holds)
            self.assertFalse(finding.recoverable)
            self.assertEqual(finding.evidence["roadmap_successor"]["kind"], "ambiguous")

    def test_invalid_successor_spec_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _complete_repo(Path(td))
            (repo / "agent" / "staged" / "P2.md").write_bytes(
                b"# P2 task\n\nStatus: **COMPLETE**\n\nPredecessor: P1\n")
            _commit(repo, "successor spec no longer pending design")
            finding = _next_finding(repo, _terminal_state())
            self.assertFalse(finding.holds)
            self.assertFalse(finding.recoverable)
            self.assertEqual(finding.evidence["roadmap_successor"]["kind"], "invalid")

    def test_zero_successors_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _complete_repo(Path(td), roadmap_target=None)
            (repo / "agent" / "staged" / "P2.md").unlink()
            _commit(repo, "no staged successor")
            finding = _next_finding(repo, _terminal_state())
            self.assertFalse(finding.holds)
            self.assertFalse(finding.recoverable)
            self.assertEqual(
                finding.evidence["roadmap_successor"]["kind"], "end_of_roadmap",
            )

    def test_no_human_clock_uses_terminal_authority_not_markdown_status(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _complete_repo(Path(td))
            (repo / "agent" / "next.md").write_bytes(
                b"# stale projection\n\nStatus: **PENDING DESIGN**\n"
            )
            _commit(repo, "stale markdown projection")

            finding = _next_finding(repo, _terminal_state())

            self.assertFalse(finding.holds)
            self.assertTrue(finding.recoverable)
            self.assertEqual(
                finding.evidence["roadmap_successor"]["target_task_id"], "P2",
            )

    def test_staged_not_started_status_is_a_valid_successor_declaration(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _complete_repo(Path(td))
            spec = repo / "agent" / "staged" / "P2.md"
            spec.write_bytes(
                b"# P2 task\n\nStatus: STAGED / NOT STARTED\n\nPredecessor: P1\n"
            )
            _commit(repo, "stage successor without activating it")

            finding = _next_finding(repo, _terminal_state())

            self.assertFalse(finding.holds)
            self.assertTrue(finding.recoverable)
            successor = finding.evidence["roadmap_successor"]
            self.assertEqual((successor["kind"], successor["target_task_id"]),
                             ("successor", "P2"))

    def test_successor_determinism_ignores_projection_churn(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _complete_repo(Path(td))
            state = _terminal_state()
            first_snapshot = _repo_summary(repo)["projects"][0]
            second_snapshot = copy.deepcopy(first_snapshot)
            second_snapshot.update({
                "next_title": "unrelated stale title",
                "next_status": "**READY_TO_RUN**",
                "telemetry": {"task_id": "STALE"},
            })

            first = {item.code: item for item in evaluate_lifecycle_invariants(
                snapshot=first_snapshot, executor_state=state,
                decisions_state={"decisions": {}},
            )}["NEXT_TASK_WITHOUT_HANDOFF"]
            second = {item.code: item for item in evaluate_lifecycle_invariants(
                snapshot=second_snapshot, executor_state=state,
                decisions_state={"decisions": {}},
            )}["NEXT_TASK_WITHOUT_HANDOFF"]

            self.assertEqual(first.evidence["roadmap_successor"],
                             second.evidence["roadmap_successor"])
            self.assertEqual(first.recoverable, second.recoverable)

    def test_active_ownership_is_not_a_missing_handoff(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _complete_repo(Path(td))
            running = {"w": {"project_id": "p1", "source_request_id": "w",
                             "task_id": "P1", "state": "running"}}
            self.assertTrue(_next_finding(repo, _terminal_state(executions=running)).holds)

    def test_surviving_decision_path_is_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _complete_repo(Path(td))
            # Consumed after the stale settle, so that settle cannot satisfy it.
            decisions = {"decisions": {"d": {
                "project_id": "p1", "task_id": "P1", "request_id": "d",
                "disposition": "apply", "decision": "next", "next_action": "next_task",
                "consumed_at": "2026-09-26T07:00:00+00:00",
            }}}
            finding = _next_finding(repo, _terminal_state(), decisions)
            self.assertFalse(finding.holds)
            self.assertTrue(finding.recoverable)
            self.assertEqual([row["request_id"] for row in finding.evidence["next_decisions"]], ["d"])
            self.assertNotIn("roadmap_successor", finding.evidence)


class TerminalRoadmapSuccessorEndToEndTests(unittest.TestCase):
    """COMPLETE P1 + roadmap P1->P2 + valid staged P2 + no NEXT decision and
    no handoff, driven through the real daemon-ordered components."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.runtime = root / "runtime"
        self.runtime.mkdir(parents=True, exist_ok=True)
        # Match the P17 closure defect: P18 is uniquely staged with a matching
        # predecessor while the roadmap still has a null P17 edge.
        self.repo = _complete_repo(root, roadmap_target=None)
        self.project = {
            "project_id": "p1",
            "repo_path": str(self.repo),
            "execution": {"engine": "aibroker"},
            "ai_roles": {"planner": {
                "enabled": True, "quality": "high", "review_quality": "high",
                "review_independence": "resource",
            }},
            "watchdog": {"enabled": True, "auto_recovery": True,
                         "no_progress_threshold_minutes": 1},
        }
        self.config_path = self.runtime / "projects.json"
        self.config_path.write_text(json.dumps({"projects": [self.project]}), encoding="utf-8")
        # No surviving NEXT decision at all.
        (self.runtime / "review-decisions.json").write_text(
            json.dumps({"decisions": {}}), encoding="utf-8")
        self.executor = TransitionExecutor(self.runtime)
        ledger = self.executor.state()
        ledger["executions"]["ai_review:settled"] = dict(_STALE_SETTLE)
        self.executor._save_ledger(ledger)
        self.port = _PlanningPort()

    def tearDown(self):
        self._tmp.cleanup()

    def _owner_commands(self):
        found = []
        for base in (self.runtime / "control" / "inbox", self.runtime / "control" / "history"):
            if not base.exists():
                continue
            for path in base.glob("*.json"):
                row = json.loads(path.read_text(encoding="utf-8"))
                if str(row.get("source") or "") != "automatic_review_handoff":
                    found.append({"path": path.name, "source": row.get("source")})
        return found

    def _handoffs(self):
        return [
            row for row in self.executor.state()["executions"].values()
            if isinstance(row, dict) and row.get("project_id") == "p1"
            and row.get("state") == "handoff"
        ]

    def _tick(self, planner, controls, watchdog):
        """The daemon's order: authority, control plane, authority, Watchdog."""
        def reconcile():
            self.executor.reconcile_lifecycle_authority(
                _repo_summary(self.repo), planner_state=planner.state())

        reconcile()
        controls.advance(str(self.config_path),
                         self.executor.overlay_lifecycle_authority(_repo_summary(self.repo)),
                         self.executor)
        for thread in getattr(planner, "_threads", {}).values():
            thread.join(timeout=15.0)
        reconcile()
        watchdog.advance(str(self.config_path),
                         self.executor.overlay_lifecycle_authority(_repo_summary(self.repo)),
                         executor=self.executor)
        for thread in getattr(watchdog, "_threads", {}).values():
            thread.join(timeout=10.0)

    def test_terminal_authority_recovers_roadmap_successor_zero_touch(self):
        from dev_orchestrator.core.ai_planner import AIPlannerCoordinator
        from dev_orchestrator.core.control_commands import ControlCommandCoordinator
        from dev_orchestrator.core.watchdog import WatchdogCoordinator

        planner = AIPlannerCoordinator(self.runtime, self.port)
        controls = ControlCommandCoordinator(self.runtime, planner)
        watchdog = WatchdogCoordinator(self.runtime, planner=planner)

        # Tick 1: authority is P1/COMPLETE; the Watchdog recovers the handoff.
        self._tick(planner, controls, watchdog)
        authority = self.executor.state()["lifecycle"]["p1"]
        self.assertEqual(authority["current_task_id"], "P1")
        handoffs = self._handoffs()
        self.assertEqual(len(handoffs), 1, "exactly one handoff must be recovered")
        self.assertEqual((handoffs[0]["source_task_id"], handoffs[0]["target_task_id"]),
                         ("P1", "P2"))
        self.assertTrue(handoffs[0]["source_request_id"].startswith("recover-handoff:p1:P1:P2:"))
        self.assertTrue(
            handoffs[0]["source_request_id"].endswith(
                str(handoffs[0]["staged_spec_sha256"])[:12]
            ),
            "recovery identity must derive from stable successor evidence, not HEAD",
        )

        # Tick 2: the normal control plane consumes it and starts the Planner.
        self._tick(planner, controls, watchdog)
        plans = [row for row in planner.state()["plans"].values()
                 if isinstance(row, dict) and row.get("project_id") == "p1"]
        self.assertEqual(len(plans), 1, f"successor Planner did not start: {plans}")
        self.assertEqual(plans[0].get("task_id"), "P2")
        self.assertTrue(self._handoffs()[0].get("handoff_consumed"))
        authority = self.executor.state()["lifecycle"]["p1"]
        self.assertEqual((authority["source_task_id"], authority["current_task_id"]), ("P1", "P2"))
        self.assertIsNone(authority.get("owner_gate"))
        self.assertIn("P2", (self.repo / "agent" / "next.md").read_text(encoding="utf-8"))
        self.assertEqual(self._owner_commands(), [], "recovery required an owner command")

        # Repeated ticks are idempotent.
        for _ in range(3):
            self._tick(planner, controls, watchdog)
        self.assertEqual(len(self._handoffs()), 1, "repeated ticks duplicated the handoff")
        self.assertEqual(
            len([row for row in planner.state()["plans"].values()
                 if isinstance(row, dict) and row.get("project_id") == "p1"]),
            1, "repeated ticks launched a duplicate Planner")
        self.assertEqual(len(self.executor.state()["transitions"]), 1)
        self.assertEqual(self._owner_commands(), [])
        wrow = (watchdog.state().get("projects") or {}).get("p1") or {}
        self.assertNotIn("NEXT_TASK_WITHOUT_HANDOFF", wrow.get("unresolved_invariants") or [])
        attempts = wrow.get("lifecycle_recovery_attempts") or {}
        self.assertEqual([a.get("state") for a in attempts.values()], ["recovered"])

    def test_dirty_worktree_waits_without_gating_then_recovers(self):
        from dev_orchestrator.core.ai_planner import AIPlannerCoordinator
        from dev_orchestrator.core.control_commands import ControlCommandCoordinator
        from dev_orchestrator.core.watchdog import WatchdogCoordinator

        planner = AIPlannerCoordinator(self.runtime, self.port)
        controls = ControlCommandCoordinator(self.runtime, planner)
        watchdog = WatchdogCoordinator(self.runtime, planner=planner)
        (self.repo / "stray").write_text("", encoding="utf-8")
        for _ in range(3):
            self._tick(planner, controls, watchdog)
        self.assertEqual(self._handoffs(), [], "recovery ran on a dirty worktree")
        wrow = (watchdog.state().get("projects") or {}).get("p1") or {}
        self.assertIsNone(wrow.get("owner_gate"), "a dirty-tree wait was gated")

        (self.repo / "stray").unlink()
        self._tick(planner, controls, watchdog)
        self.assertEqual(len(self._handoffs()), 1)


if __name__ == "__main__":
    unittest.main()
