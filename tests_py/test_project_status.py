import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dev_orchestrator.core.project_status import (
    _watchdog_view,
    project_runtime_status,
    write_execution_status,
    write_project_status,
    write_review_status,
)


def make_repo(root: Path) -> str:
    root.mkdir(parents=True)
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "init"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "fixture"], check=True, stdout=subprocess.DEVNULL)
    return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()


class ProjectStatusTests(unittest.TestCase):
    def test_project_local_status_is_atomic_and_git_clean(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            head = make_repo(repo); runtime.mkdir()
            snapshot = {
                "project_id": "p1", "repo_path": str(repo), "state": "READY_TO_RUN",
                "orchestration_ready": True, "next_title": "P1 bounded task",
                "telemetry": {"task_id": "P1", "run_id": None},
                "worker": {"kind": "none", "state": "not_started", "process_alive": False},
                "git": {"branch": "master", "head": head, "dirty": False, "changed_entries": 0},
            }
            target = write_project_status(
                snapshot, runtime, phase="monitor", daemon_state="running", pid=123,
            )
            self.assertEqual(target, repo / ".devorch" / "status.json")
            status = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(status["project_id"], "p1")
            self.assertEqual(status["phase"], "monitor")
            self.assertEqual(status["task_id"], "P1")
            self.assertEqual(status["git"]["head"], head)
            porcelain = subprocess.check_output(
                ["git", "-C", str(repo), "status", "--porcelain"], text=True
            ).strip()
            self.assertEqual(porcelain, "")
            exclude = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "--git-path", "info/exclude"], text=True
            ).strip()
            exclude_path = Path(exclude) if Path(exclude).is_absolute() else repo / exclude
            self.assertIn(".devorch/", exclude_path.read_text(encoding="utf-8"))

    def test_execution_state_updates_status_without_waiting_for_monitor_tick(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo); runtime.mkdir()
            record = {
                "project_id": "p1", "repo_path": str(repo),
                "source_request_id": "owner-p1", "source_kind": "bootstrap",
                "task_id": "P1", "backend_id": "agy", "state": "running",
                "pid": 42, "started_at": "2026-09-05T01:00:00+00:00",
            }
            target = write_execution_status(record, runtime)
            status = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(status["phase"], "worker")
            self.assertEqual(status["worker"]["state"], "running")
            self.assertTrue(status["worker"]["process_alive"])
            record.update({
                "state": "completed", "exit_code": 0,
                "completed_at": "2026-09-05T01:05:00+00:00",
            })
            write_execution_status(record, runtime)
            status = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(status["worker"]["state"], "completed")
            self.assertFalse(status["worker"]["process_alive"])
            self.assertEqual(status["actuation"]["exit_code"], 0)

    def test_web_sol_decision_and_disposition_are_distinct(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            head = make_repo(repo); runtime.mkdir()
            (runtime / "websol-decisions.json").write_text(json.dumps({
                "version": 1, "decisions": {"r1": {
                    "project_id": "p1", "request_id": "r1",
                    "decision": "remediate", "disposition": "apply",
                    "next_action": "continue_current_stage",
                    "consumed_at": "2026-09-05T01:00:00+00:00",
                }}
            }), encoding="utf-8")
            snapshot = {
                "project_id": "p1", "repo_path": str(repo), "state": "READY_TO_RUN",
                "orchestration_ready": True, "next_title": "P1 bounded task",
                "telemetry": {"task_id": "P1", "run_id": None},
                "worker": {"kind": "none", "state": "not_started", "process_alive": False},
                "git": {"branch": "master", "head": head, "dirty": False, "changed_entries": 0},
            }
            target = write_project_status(
                snapshot, runtime, phase="decision", daemon_state="running", pid=123
            )
            status = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(status["web_sol"]["decision"], "remediate")
            self.assertEqual(status["web_sol"]["disposition"], "apply")
            self.assertEqual(status["web_sol"]["next_action"], "continue_current_stage")

    def test_broker_native_running_state_in_execution_status(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo); runtime.mkdir()
            record = {
                "project_id": "p1", "repo_path": str(repo),
                "source_request_id": "owner-p1", "source_kind": "bootstrap",
                "task_id": "P1", "backend_id": "aibroker", "engine": "aibroker",
                "broker_request_id": "brk-100", "role_run_id": "role-worker-1",
                "state": "running", "pid": 1234, "started_at": "2026-09-11T01:00:00+00:00",
            }
            target = write_execution_status(record, runtime)
            self.assertIsNotNone(target)
            status = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(status["phase"], "worker")
            self.assertEqual(status["status"], "EXECUTING")
            self.assertEqual(status["lifecycle_state"], "EXECUTING")
            self.assertEqual(status["worker"]["engine"], "aibroker")
            self.assertEqual(status["worker"]["state"], "running")
            self.assertTrue(status["worker"]["process_alive"])
            self.assertIn("broker_execution", status)
            self.assertEqual(status["broker_execution"]["broker_request_id"], "brk-100")
            self.assertEqual(status["broker_execution"]["role_run_id"], "role-worker-1")
            self.assertEqual(status["broker_execution"]["engine"], "aibroker")

    def test_project_runtime_status_projects_active_broker_worker(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            # Base snapshot with stale READY_TO_RUN
            snapshot = {
                "project_id": "p1",
                "state": "READY_TO_RUN",
                "lifecycle_state": "READY_TO_RUN",
                "worker": {"kind": "none", "state": "not_started", "process_alive": False},
            }
            # Record an active aibroker execution in transition-executor.json
            (runtime / "transition-executor.json").write_text(json.dumps({
                "version": 1,
                "executions": {
                    "req-1": {
                        "project_id": "p1",
                        "source_request_id": "req-1",
                        "state": "running",
                        "engine": "aibroker",
                        "backend_id": "aibroker",
                        "broker_request_id": "brk-42",
                        "role_run_id": "role-run-42",
                        "started_at": "2026-09-11T12:00:00+00:00",
                    }
                }
            }), encoding="utf-8")

            projected = project_runtime_status(snapshot, runtime)
            # Projected state must show active broker execution instead of stale READY_TO_RUN
            self.assertEqual(projected["state"], "WORKER_RUNNING")
            self.assertEqual(projected["lifecycle_state"], "EXECUTING")
            self.assertEqual(projected["worker"]["engine"], "aibroker")
            self.assertEqual(projected["worker"]["state"], "running")
            self.assertTrue(projected["worker"]["process_alive"])
            self.assertIn("broker_execution", projected)
            self.assertEqual(projected["broker_execution"]["broker_request_id"], "brk-42")

    def test_project_runtime_status_projects_active_reviewer_and_planner(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            snapshot = {
                "project_id": "p1",
                "state": "READY_TO_RUN",
                "lifecycle_state": "READY_TO_RUN",
            }
            # Active reviewer
            (runtime / "ai-reviewer.json").write_text(json.dumps({
                "version": 1,
                "reviews": {
                    "rev-1": {
                        "project_id": "p1",
                        "review_id": "rev-1",
                        "state": "running",
                        "started_at": "2026-09-11T12:00:00+00:00",
                    }
                }
            }), encoding="utf-8")

            projected = project_runtime_status(snapshot, runtime)
            self.assertEqual(projected["lifecycle_state"], "REVIEWING")
            self.assertEqual(projected["state"], "REVIEWING")
            self.assertEqual(projected["reviewer"]["review_id"], "rev-1")

            # Active planner (when no reviewer active)
            (runtime / "ai-reviewer.json").write_text(json.dumps({"version": 1, "reviews": {}}), encoding="utf-8")
            (runtime / "ai-planner.json").write_text(json.dumps({
                "version": 1,
                "plans": {
                    "plan-1": {
                        "project_id": "p1",
                        "plan_id": "plan-1",
                        "state": "planning",
                        "started_at": "2026-09-11T12:05:00+00:00",
                    }
                }
            }), encoding="utf-8")

            projected_plan = project_runtime_status(snapshot, runtime)
            self.assertEqual(projected_plan["lifecycle_state"], "PLANNING")
            self.assertEqual(projected_plan["state"], "PLANNING")
            self.assertEqual(projected_plan["planner"]["plan_id"], "plan-1")

    def test_project_context_reaches_status_json_and_survives_project_runtime_status(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            head = make_repo(repo); runtime.mkdir()
            snapshot = {
                "project_id": "p1", "repo_path": str(repo), "state": "READY_TO_RUN",
                "orchestration_ready": True, "next_title": "P1 bounded task",
                "telemetry": {"task_id": "P1", "run_id": None},
                "worker": {"kind": "none", "state": "not_started", "process_alive": False},
                "git": {"branch": "master", "head": head, "dirty": False, "changed_entries": 0},
                "project_context": {
                    "schema_version": 1,
                    "state": "ready",
                    "reason": "context resolved",
                    "digest": "0123456789abcdef",
                    "updated_at": "2026-09-12T00:00:00+00:00",
                    "document_path": "agent/project-context.json",
                    "supplement_used": False,
                    "domains": {"goals": 2, "architecture": 3},
                },
            }
            target = write_project_status(
                snapshot, runtime, phase="monitor", daemon_state="running", pid=123,
            )
            status = json.loads(target.read_text(encoding="utf-8"))
            self.assertIn("project_context", status)
            self.assertEqual(status["project_context"]["state"], "ready")
            self.assertEqual(status["project_context"]["digest"], "0123456789abcdef")
            self.assertEqual(status["project_context"]["domains"]["goals"], 2)

            projected = project_runtime_status(snapshot, runtime)
            self.assertIn("project_context", projected)
            self.assertEqual(projected["project_context"]["state"], "ready")
            self.assertEqual(projected["project_context"]["digest"], "0123456789abcdef")

    def test_watchdog_view_uses_newest_source_timestamp_not_dict_order(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            (runtime / "watchdog.json").write_text(json.dumps({
                "version": 1,
                "degraded": False,
                "projects": {
                    "p1": {
                        "signal_sources": {
                            "sources": {
                                "late-in-order": {
                                    "path": "agent/older.md",
                                    "last_activity_at": "2026-09-12T10:00:00Z",
                                },
                                "early-in-order": {
                                    "path": "agent/newer.md",
                                    "last_activity_at": "2026-09-12T11:00:00Z",
                                },
                            }
                        },
                        "activity_evidence": "available",
                        "activity_evidence_reason": None,
                        "attempts": {},
                        "attempt_counts": {},
                    }
                }
            }), encoding="utf-8")
            projects_dir = runtime / "projects"
            projects_dir.mkdir()
            (projects_dir / "p1.json").write_text(json.dumps({
                "activity": {"watchdog_safe": {"runtime_scope": "canonical"}}
            }), encoding="utf-8")

            view = _watchdog_view(runtime, "p1")

            self.assertIsNotNone(view)
            self.assertEqual(view["activity_evidence"]["newest_path"], "agent/newer.md")


if __name__ == "__main__":
    unittest.main()
