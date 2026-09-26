"""P16.13 lifecycle fault-injection matrix.

Each test names the incident class from the task's A--J matrix.  The fixtures
use real git repositories and durable runtime ledgers so restart/idempotency
behavior is exercised at the same boundaries as the daemon.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.core.ai_reviewer import AIReviewerCoordinator
from dev_orchestrator.core.lifecycle_authority import active_owners, evaluate_lifecycle_invariants
from dev_orchestrator.core.successor_consistency import (
    reconcile_roadmap_successor,
    resolve_successor,
)
from dev_orchestrator.core.transition_executor import TransitionExecutor


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


if __name__ == "__main__":
    unittest.main()
