"""Bounded autonomous recovery for cleared automatic planning failures."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.core.watchdog import WatchdogCoordinator, resolve_watchdog_policy
from dev_orchestrator.control.owner_store import OwnerControlStore
from dev_orchestrator.core.control_commands import ControlCommandCoordinator
from dev_orchestrator.incidents.fingerprint import incident_fingerprint


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return result.stdout.strip()


class PlanningConvergenceRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.runtime = self.root / "runtime"
        self.repo.mkdir()
        self.runtime.mkdir()
        _git(self.repo, "init")
        _git(self.repo, "config", "user.email", "test@example.invalid")
        _git(self.repo, "config", "user.name", "Test")
        (self.repo / "README.md").write_text("initial\n", encoding="utf-8")
        _git(self.repo, "add", "README.md")
        _git(self.repo, "commit", "-m", "initial")
        self.project = {
            "project_id": "p1",
            "repo_path": str(self.repo),
            "watchdog": {
                "enabled": True,
                "auto_recovery": True,
                "max_attempts_per_run": 2,
            },
        }
        self.coordinator = WatchdogCoordinator(self.runtime)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _snapshot(self, *, head: str | None = None, owner_gate: object = None) -> dict:
        truth = read_repository_truth(self.repo)
        return {
            "project_id": "p1",
            "repo_path": str(self.repo),
            "lifecycle_state": "PENDING_DESIGN",
            "next_status": "PENDING_DESIGN",
            "git": {
                "branch": truth.branch,
                "head": head if head is not None else truth.head,
                "dirty": truth.dirty,
            },
            "telemetry": {"task_id": "P18"},
            "authoritative_lifecycle": {
                "current_task_id": "P18",
                "lifecycle_state": "PENDING_DESIGN",
                "active_owner": None,
                "owner_gate": owner_gate,
                "repository_projection": {
                    "task_id": "P18",
                    "state": "PENDING_DESIGN",
                    "matches_authority": True,
                },
            },
        }

    def _failed_plan(
        self, suffix: str = "one", *, reason: str = "repository changed during planning",
        source: str = "automatic_review_handoff", completed_at: str = "2026-09-28T00:00:00+00:00",
    ) -> dict:
        command_id = f"auto-{suffix}"
        plan_id = f"ai_plan:{command_id}"
        record = {
            "plan_id": plan_id,
            "command_id": command_id,
            "project_id": "p1",
            "task_id": "P18",
            "repo_path": str(self.repo),
            "branch": read_repository_truth(self.repo).branch,
            "head": "superseded-head",
            "state": "failed",
            "reason": reason,
            "completed_at": completed_at,
        }
        history_dir = self.runtime / "control" / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        (history_dir / f"{command_id}.json").write_text(json.dumps({
            "command_id": command_id,
            "project_id": "p1",
            "plan_id": plan_id,
            "source": source,
            "state": "blocked",
        }), encoding="utf-8")
        return record

    def _recover(self, snapshot: dict, plans: dict, *, project: dict | None = None) -> dict | None:
        prow = self.coordinator._cached_state["projects"].setdefault("p1", {"attempts": {}})
        pcfg = project or self.project
        return self.coordinator._recover_cleared_automatic_planning_failure(
            pcfg, snapshot, prow, {"plans": plans}, resolve_watchdog_policy(pcfg),
        )

    def test_repository_changed_during_planning_recovers_only_after_clean(self) -> None:
        failed = self._failed_plan()
        (self.repo / "README.md").write_text("changed\n", encoding="utf-8")
        dirty = self._recover(self._snapshot(), {failed["plan_id"]: failed})
        self.assertEqual(dirty["status"], "planning_recovery_waiting")
        self.assertFalse((self.runtime / "control" / "inbox").exists())

        _git(self.repo, "add", "README.md")
        _git(self.repo, "commit", "-m", "clear transient planning race")
        recovered = self._recover(self._snapshot(), {failed["plan_id"]: failed})
        self.assertEqual(recovered["status"], "planning_recovery_requested")
        command = json.loads(
            (self.runtime / "control" / "inbox" / f"{recovered['command_id']}.json").read_text(encoding="utf-8")
        )
        self.assertEqual(command["action"], "continue")
        self.assertEqual(command["source"], "watchdog_planning_recovery")

    def test_no_duplicate_retry_across_ticks_or_restart(self) -> None:
        failed = self._failed_plan()
        plans = {failed["plan_id"]: failed}
        first = self._recover(self._snapshot(), plans)
        second = self._recover(self._snapshot(), plans)
        self.assertEqual(first["status"], "planning_recovery_requested")
        self.assertEqual(second["status"], "planning_recovery_already_reserved")

        restarted = WatchdogCoordinator(self.runtime)
        prow = restarted._cached_state["projects"]["p1"]
        third = restarted._recover_cleared_automatic_planning_failure(
            self.project, self._snapshot(), prow, {"plans": plans},
            resolve_watchdog_policy(self.project),
        )
        self.assertEqual(third["status"], "planning_recovery_already_reserved")
        self.assertEqual(len(list((self.runtime / "control" / "inbox").glob("wd-plan-*.json"))), 1)

    def test_recovery_command_is_consumed_by_the_single_control_authority(self) -> None:
        failed = self._failed_plan()
        recovered = self._recover(self._snapshot(), {failed["plan_id"]: failed})

        class Planner:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def terminal_records(self) -> list[dict]:
                return []

            def ready_records(self) -> list[dict]:
                return []

            def resume_exhausted_technical_gate(self, project, snapshot, gate_id):
                return False, "no recoverable gate"

            def continue_owner_approved(self, project, snapshot, command_id):
                return False, None, "no owner-approved plan"

            def continue_failed_plan_review(self, project, snapshot, command_id):
                return False, None, "no failed plan review"

            def start(self, project, snapshot, command_id):
                self.calls.append((project["project_id"], command_id))
                return f"ai_plan:{command_id}", "planning started"

        class Executor:
            @staticmethod
            def state() -> dict:
                return {"executions": {}}

        config = self.root / "projects.json"
        config.write_text(json.dumps({"projects": [{
            **self.project,
            "adapter": "agent_files",
            "execution": {
                "enabled": True,
                "owner_authorized": True,
                "allowed_next_actions": ["next_task"],
            },
        }]}), encoding="utf-8")
        planner = Planner()
        outcomes = ControlCommandCoordinator(self.runtime, planner=planner).advance(
            config, {"projects": [self._snapshot()]}, Executor(),
        )
        accepted = [row for row in outcomes if row.get("command_id") == recovered["command_id"]]
        self.assertEqual(accepted[0]["state"], "accepted")
        self.assertEqual(planner.calls, [("p1", recovered["command_id"])])

    def test_stable_problem_budget_exhaustion_blocks_a_new_failure(self) -> None:
        first_failure = self._failed_plan("one", completed_at="2026-09-28T00:00:00+00:00")
        one_attempt_project = {
            **self.project,
            "watchdog": {**self.project["watchdog"], "max_attempts_per_run": 1},
        }
        first = self._recover(
            self._snapshot(), {first_failure["plan_id"]: first_failure}, project=one_attempt_project,
        )
        self.assertEqual(first["status"], "planning_recovery_requested")

        second_failure = self._failed_plan("two", completed_at="2026-09-28T00:01:00+00:00")
        exhausted = self._recover(
            self._snapshot(), {
                first_failure["plan_id"]: first_failure,
                second_failure["plan_id"]: second_failure,
            }, project=one_attempt_project,
        )
        self.assertEqual(exhausted["status"], "planning_recovery_exhausted")
        problems = self.coordinator._cached_state["projects"]["p1"]["planning_recovery_problems"]
        self.assertEqual(len(problems), 1, "HEAD and attempt identity must not split the problem budget")
        self.assertEqual(next(iter(problems.values()))["state"], "exhausted")
        self.assertEqual(len(list((self.runtime / "control" / "inbox").glob("wd-plan-*.json"))), 1)

    def test_nonretryable_cases_fail_closed(self) -> None:
        cases: list[tuple[str, dict, dict]] = []
        permanent = self._failed_plan("permanent", reason="planner output invalid")
        cases.append(("permanent", self._snapshot(), {permanent["plan_id"]: permanent}))
        owner = self._failed_plan("owner", completed_at="2026-09-28T00:01:00+00:00")
        cases.append(("owner_gate", self._snapshot(owner_gate={"code": "OWNER_APPROVAL"}), {owner["plan_id"]: owner}))
        nonautomatic = self._failed_plan("manual", source="control_api", completed_at="2026-09-28T00:02:00+00:00")
        cases.append(("nonautomatic", self._snapshot(), {nonautomatic["plan_id"]: nonautomatic}))
        stale = self._failed_plan("stale", completed_at="2026-09-28T00:03:00+00:00")
        cases.append(("stale_repo", self._snapshot(head="stale-monitor-head"), {stale["plan_id"]: stale}))
        ambiguous = self._failed_plan("ambiguous", completed_at="2026-09-28T00:04:00+00:00")
        ambiguous_snapshot = self._snapshot()
        ambiguous_snapshot["authoritative_lifecycle"]["repository_projection"]["matches_authority"] = False
        cases.append(("ambiguous_authority", ambiguous_snapshot, {ambiguous["plan_id"]: ambiguous}))

        for name, snapshot, plans in cases:
            with self.subTest(name=name):
                result = self._recover(snapshot, plans)
                self.assertNotEqual((result or {}).get("status"), "planning_recovery_requested")
        self.assertEqual(len(list((self.runtime / "control" / "inbox").glob("wd-plan-*.json"))), 0)

    def test_problem_identity_excludes_head_and_retry_ids(self) -> None:
        semantic = {
            "failure_class": "RESOURCE_TRANSIENT",
            "diagnosis_code": "planner_failed",
            "blocker_code": "repository_changed_during_planning",
            "lifecycle_class": "automatic_planning",
            "contract_class": "automatic_planning_progress",
            "role_class": "planner",
            "continuation_relation": "goal:P18",
            "invariant_identifier": "AUTOMATIC_PLANNING_PROGRESS",
        }
        first = incident_fingerprint(semantic)
        second = incident_fingerprint(dict(reversed(list(semantic.items()))))
        self.assertEqual(first, second)

    def test_owner_pause_is_a_hard_retry_fence(self) -> None:
        failed = self._failed_plan("paused")
        OwnerControlStore(self.runtime).set_paused(
            "p1", True, command_id="owner-stop", action="pause",
        )
        result = self._recover(self._snapshot(), {failed["plan_id"]: failed})
        self.assertIsNone(result)
        self.assertEqual(len(list((self.runtime / "control" / "inbox").glob("*.json"))), 0)


if __name__ == "__main__":
    unittest.main()
