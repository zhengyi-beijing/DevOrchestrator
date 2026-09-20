import copy
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dev_orchestrator.ai.contracts import AIRoleRequest, AIRoleResult, ResourceContext
from dev_orchestrator.control.surface import project_control_view
from dev_orchestrator.core.ai_planner import AIPlannerCoordinator
from dev_orchestrator.core.ai_reviewer import AIReviewerCoordinator
from dev_orchestrator.core.control_commands import ControlCommandCoordinator
from dev_orchestrator.core.project_status import build_project_status, project_runtime_status
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.core.staged_roadmap import read_successor
from dev_orchestrator.core.transition_executor import TransitionExecutor
from dev_orchestrator.core.watchdog import resolve_recovery_epoch
from dev_orchestrator.storage.json_store import write_json
from tests_py.test_transition_executor import FakeBackend


class FakeResourceFailoverReviewPort:
    def __init__(self, fail_count=1, failure_classification="quota_exhausted"):
        self.fail_count = fail_count
        self.failure_classification = failure_classification
        self.requests: list[AIRoleRequest] = []
        self.calls = 0

    def execute(self, request: AIRoleRequest) -> AIRoleResult:
        self.requests.append(request)
        self.calls += 1
        if self.calls <= self.fail_count:
            return AIRoleResult(
                request_id=request.request_id,
                role_run_id=request.role_run_id,
                status="failed",
                failure_classification=self.failure_classification,
                error=f"Resource error: {self.failure_classification}",
                resource_context=ResourceContext(
                    f"res-fail-{self.calls}", "provider-x", "acct-1", "model-a"
                ),
            )
        return AIRoleResult(
            request_id=request.request_id,
            role_run_id=request.role_run_id,
            status="succeeded",
            output=json.dumps({
                "decision": "next",
                "next_action": "next_task",
                "reason": "pass after failover",
            }),
            dispatch_id="dispatch-ok",
            decision_id="decision-ok",
            execution_id="exec-ok",
            resource_context=ResourceContext(
                "res-success", "provider-y", "acct-2", "model-b"
            ),
        )


class FakeResourceFailoverWorkerPort:
    def __init__(self, repo_dir: Path | None = None, dirty_on_fail: bool = False):
        self.repo_dir = repo_dir
        self.dirty_on_fail = dirty_on_fail
        self.requests: list[AIRoleRequest] = []
        self.calls = 0

    def execute(self, request: AIRoleRequest) -> AIRoleResult:
        self.requests.append(request)
        self.calls += 1
        if self.calls == 1 and self.repo_dir is not None and self.dirty_on_fail:
            (self.repo_dir / "dirty_leak.txt").write_text("unexpected leak\n", encoding="utf-8")
        if self.calls == 1 and (self.dirty_on_fail or self.repo_dir is not None):
            return AIRoleResult(
                request_id=request.request_id,
                role_run_id=request.role_run_id,
                status="failed",
                failure_classification="quota_exhausted",
                error="Quota limit reached for model-a",
                resource_context=ResourceContext(
                    "worker-res-1", "provider-a", "acct-1", "model-a"
                ),
            )
        return AIRoleResult(
            request_id=request.request_id,
            role_run_id=request.role_run_id,
            status="succeeded",
            output="WORKER_SUCCESS_AFTER_FAILOVER",
            dispatch_id="worker-disp-ok",
            decision_id="worker-dec-ok",
            execution_id="worker-exec-ok",
            resource_context=ResourceContext(
                "worker-res-2", "provider-b", "acct-2", "model-b"
            ),
        )


class FakeHandoffPort:
    def __init__(self, plan_task_id: str = "P14.6"):
        self.requests: list[AIRoleRequest] = []
        self.plan_task_id = plan_task_id

    def execute(self, request: AIRoleRequest) -> AIRoleResult:
        self.requests.append(request)
        if request.role == "planner":
            plan = {
                "task_id": self.plan_task_id,
                "summary": "Plan for successor task.",
                "implementation_steps": ["Step 1", "Step 2"],
                "interfaces": ["Contract 1"],
                "validation": ["Val 1"],
                "risks": ["Risk 1"],
                "out_of_scope": ["Scope 1"],
            }
            return AIRoleResult(
                request_id=request.request_id,
                role_run_id=request.role_run_id,
                status="succeeded",
                output=json.dumps(plan),
                dispatch_id="disp-plan",
                execution_id="exec-plan",
                resource_context=ResourceContext("res-plan", "prov", "acc", "mod"),
            )
        return AIRoleResult(
            request_id=request.request_id,
            role_run_id=request.role_run_id,
            status="succeeded",
            output=json.dumps({"decision": "approve", "reason": "Approved design."}),
            dispatch_id="disp-rev",
            execution_id="exec-rev",
            resource_context=ResourceContext("res-rev", "prov", "acc", "mod"),
        )


def make_git_repo(repo: Path, task_id: str = "P14.5") -> str:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "DevO Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "devo@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "commit.gpgsign", "false"], check=True)
    (repo / "agent").mkdir(parents=True, exist_ok=True)
    (repo / "agent" / "staged").mkdir(parents=True, exist_ok=True)
    (repo / "agent" / "next.md").write_bytes(
        f"# {task_id} Predecessor Task\nStatus: **COMPLETE**\n\nGoal: completed.\n".encode("utf-8")
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init repo"], check=True, capture_output=True)
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    return head


def setup_staged_spec(repo: Path, successor_id: str = "P14.6") -> tuple[str, bytes]:
    spec_path = f"agent/staged/{successor_id}.md"
    spec_bytes = (
        f"# {successor_id} Successor Task\n\n"
        "Status: **PENDING DESIGN**\n\n"
        "Goal: implement successor.\n"
    ).encode("utf-8")
    (repo / spec_path).write_bytes(spec_bytes)
    return spec_path, spec_bytes


def setup_roadmap(repo: Path, predecessor_id: str, successor_id: str, spec_path: str) -> None:
    roadmap_path = repo / "agent" / "staged" / "roadmap.json"
    roadmap_data = {
        "schema_version": 1,
        "tasks": [
            {"task_id": predecessor_id, "successor": successor_id, "successor_spec_path": spec_path},
            {"task_id": successor_id, "successor": None, "successor_spec_path": None},
        ],
    }
    roadmap_path.write_bytes(json.dumps(roadmap_data, indent=2).encode("utf-8"))


class TestP146UnattendedGate(unittest.TestCase):
    def test_reviewer_quota_failover_success(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "repo"
            head = make_git_repo(repo, "P14.5")
            truth = read_repository_truth(repo)
            runtime = base / "runtime"
            runtime.mkdir(parents=True, exist_ok=True)

            config_path = base / "projects.json"
            config_data = {
                "projects": [{
                    "project_id": "p1",
                    "repo_path": str(repo),
                    "execution": {
                        "enabled": True,
                        "engine": "aibroker",
                        "owner_authorized": True,
                        "allowed_next_actions": ["next_task"],
                    },
                    "ai_roles": {
                        "reviewer": {
                            "enabled": True,
                            "quality": "high",
                            "independence": "resource",
                        }
                    },
                }]
            }
            config_path.write_text(json.dumps(config_data), encoding="utf-8")

            # Completed worker in transition-executor.json
            (runtime / "transition-executor.json").write_text(json.dumps({
                "version": 1,
                "executions": {
                    "worker-run-1": {
                        "project_id": "p1",
                        "source_request_id": "worker-run-1",
                        "engine": "aibroker",
                        "state": "completed",
                        "task_id": "P14.5",
                        "branch": truth.branch,
                        "head": head,
                        "launch_status_hash": truth.status_hash,
                        "repo_path": str(repo),
                        "completed_at": "2026-09-20T01:00:00+00:00",
                        "resource_context": {
                            "resource_id": "prev-worker-res",
                        },
                    }
                }
            }), encoding="utf-8")

            port = FakeResourceFailoverReviewPort(fail_count=1, failure_classification="quota_exhausted")
            reviewer = AIReviewerCoordinator(runtime, port)

            launched = reviewer.advance(config_path)
            self.assertEqual(launched, ["ai_review:worker-run-1"])
            review_id = launched[0]

            thread = reviewer._threads.get(review_id)
            self.assertIsNotNone(thread)
            thread.join(timeout=10.0)

            self.assertEqual(port.calls, 2)
            self.assertEqual(port.requests[0].request_id, review_id)
            self.assertEqual(port.requests[1].request_id, f"{review_id}:failover-1")
            self.assertIn("res-fail-1", port.requests[1].excluded_resource_ids)

            state = reviewer.state()
            rev_rec = state["reviews"][review_id]
            self.assertEqual(rev_rec["state"], "completed")
            self.assertEqual(rev_rec["decision"], "next")
            self.assertEqual(rev_rec["next_action"], "next_task")
            self.assertEqual(rev_rec.get("failover_from_resource_ids"), ["res-fail-1"])

    def test_reviewer_resource_failover_exhaustion(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "repo"
            head = make_git_repo(repo, "P14.5")
            truth = read_repository_truth(repo)
            runtime = base / "runtime"
            runtime.mkdir(parents=True, exist_ok=True)

            config_path = base / "projects.json"
            config_data = {
                "projects": [{
                    "project_id": "p1",
                    "repo_path": str(repo),
                    "execution": {
                        "enabled": True,
                        "engine": "aibroker",
                        "owner_authorized": True,
                        "allowed_next_actions": ["next_task"],
                    },
                    "ai_roles": {
                        "reviewer": {
                            "enabled": True,
                            "quality": "high",
                            "independence": "resource",
                        }
                    },
                }]
            }
            config_path.write_text(json.dumps(config_data), encoding="utf-8")

            (runtime / "transition-executor.json").write_text(json.dumps({
                "version": 1,
                "executions": {
                    "worker-run-exhausted": {
                        "project_id": "p1",
                        "source_request_id": "worker-run-exhausted",
                        "engine": "aibroker",
                        "state": "completed",
                        "task_id": "P14.5",
                        "branch": truth.branch,
                        "head": head,
                        "launch_status_hash": truth.status_hash,
                        "repo_path": str(repo),
                        "completed_at": "2026-09-20T01:00:00+00:00",
                        "resource_context": {
                            "resource_id": "prev-worker-res",
                        },
                    }
                }
            }), encoding="utf-8")

            port = FakeResourceFailoverReviewPort(fail_count=10, failure_classification="rate_limited")
            reviewer = AIReviewerCoordinator(runtime, port)

            launched = reviewer.advance(config_path)
            self.assertEqual(launched, ["ai_review:worker-run-exhausted"])
            review_id = launched[0]

            thread = reviewer._threads.get(review_id)
            self.assertIsNotNone(thread)
            thread.join(timeout=10.0)

            self.assertEqual(port.calls, 3)
            state = reviewer.state()
            rev_rec = state["reviews"][review_id]
            self.assertEqual(rev_rec["state"], "failed")
            self.assertIn("rate_limited", rev_rec.get("reason", ""))

    def test_worker_quota_failover_on_clean_repo(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "repo"
            head = make_git_repo(repo, "P14.5")
            truth = read_repository_truth(repo)
            runtime = base / "runtime"
            runtime.mkdir(parents=True, exist_ok=True)

            port = FakeResourceFailoverWorkerPort(repo_dir=repo, dirty_on_fail=False)
            executor = TransitionExecutor(runtime, ai_execution_port=port)

            config_path = base / "projects.json"
            config_data = {
                "projects": [{
                    "project_id": "p1",
                    "repo_path": str(repo),
                    "execution": {
                        "enabled": True,
                        "engine": "aibroker",
                        "owner_authorized": True,
                        "allowed_next_actions": ["next_task"],
                    },
                }]
            }
            config_path.write_text(json.dumps(config_data), encoding="utf-8")

            snapshot = {
                "project_id": "p1",
                "state": "READY_TO_RUN",
                "telemetry": {"task_id": "P14.5"},
                "git": {"head": head, "branch": truth.branch},
            }

            launch = executor._launch(
                config_data["projects"][0],
                source_request_id="worker-launch-1",
                source_kind="ready",
                task_id="P14.5",
                source_task_id=None,
                branch=truth.branch,
                head=head,
                worker_prompt="do task",
                policy=config_data["projects"][0]["execution"],
            )
            self.assertIsNotNone(launch)

            thread = executor._threads.get("worker-launch-1")
            self.assertIsNotNone(thread)
            thread.join(timeout=10.0)

            self.assertEqual(port.calls, 2)
            self.assertEqual(port.requests[0].request_id, "ai-worker:worker-launch-1")
            self.assertEqual(port.requests[1].request_id, "worker-launch-1:failover-1")
            self.assertIn("worker-res-1", port.requests[1].excluded_resource_ids)

            ledger = executor.state()["executions"]
            exec_row = ledger["worker-launch-1"]
            self.assertEqual(exec_row["state"], "completed")
            self.assertEqual(exec_row.get("failover_from_resource_ids"), ["worker-res-1"])

    def test_worker_quota_failover_refused_on_dirty_repo(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "repo"
            head = make_git_repo(repo, "P14.5")
            truth = read_repository_truth(repo)
            runtime = base / "runtime"
            runtime.mkdir(parents=True, exist_ok=True)

            port = FakeResourceFailoverWorkerPort(repo_dir=repo, dirty_on_fail=True)
            executor = TransitionExecutor(runtime, ai_execution_port=port)

            config_path = base / "projects.json"
            config_data = {
                "projects": [{
                    "project_id": "p1",
                    "repo_path": str(repo),
                    "execution": {
                        "enabled": True,
                        "engine": "aibroker",
                        "owner_authorized": True,
                        "allowed_next_actions": ["next_task"],
                    },
                }]
            }
            config_path.write_text(json.dumps(config_data), encoding="utf-8")

            launch = executor._launch(
                config_data["projects"][0],
                source_request_id="worker-launch-dirty",
                source_kind="ready",
                task_id="P14.5",
                source_task_id=None,
                branch=truth.branch,
                head=head,
                worker_prompt="do task",
                policy=config_data["projects"][0]["execution"],
            )
            self.assertIsNotNone(launch)

            thread = executor._threads.get("worker-launch-dirty")
            self.assertIsNotNone(thread)
            thread.join(timeout=10.0)

            # Only 1 call because failover must be refused when repository is dirty
            self.assertEqual(port.calls, 1)

            ledger = executor.state()["executions"]
            exec_row = ledger["worker-launch-dirty"]
            self.assertEqual(exec_row["state"], "failed")
            self.assertIn("refused", exec_row.get("reason", ""))

    def test_unattended_successor_promotion_and_launch(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "repo"
            head = make_git_repo(repo, "P14.5")
            spec_path, _ = setup_staged_spec(repo, "P14.6")
            setup_roadmap(repo, "P14.5", "P14.6", spec_path)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "setup staged successor"], check=True, capture_output=True)
            completed_head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

            runtime = base / "runtime"
            runtime.mkdir(parents=True, exist_ok=True)
            planner_port = FakeHandoffPort("P14.6")
            planner = AIPlannerCoordinator(runtime, planner_port)
            worker_port = FakeResourceFailoverWorkerPort()
            executor = TransitionExecutor(runtime, ai_execution_port=worker_port)
            control = ControlCommandCoordinator(runtime, planner)

            config_path = base / "projects.json"
            config_data = {
                "projects": [{
                    "project_id": "p1",
                    "repo_path": str(repo),
                    "adapter": "agent_files",
                    "execution": {
                        "enabled": True,
                        "engine": "aibroker",
                        "owner_authorized": True,
                        "allowed_next_actions": ["next_task"],
                        "worker_prompt": "run worker prompt",
                    },
                    "ai_roles": {"planner": {"enabled": True}},
                }]
            }
            config_path.write_text(json.dumps(config_data), encoding="utf-8")

            summary = {
                "projects": [{
                    "project_id": "p1",
                    "state": "IDLE",
                    "next_status": "**COMPLETE**",
                    "telemetry": {"task_id": "P14.5"},
                    "git": {"head": completed_head},
                }]
            }

            # 1. Advance executor: creates auto-handoff for completed predecessor
            executor.advance(summary, config_path)
            auto_key = f"auto-handoff:P14.5:{completed_head[:12]}"
            ledger = executor.state()["executions"]
            self.assertIn(auto_key, ledger)
            self.assertEqual(ledger[auto_key]["state"], "handoff")
            self.assertEqual(ledger[auto_key]["staged_successor"], "P14.6")

            # 2. Control advance resumes handoff and initiates deferred planner
            outcomes = control.advance(config_path, summary, executor)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0]["lifecycle_action"], "plan")
            plan_id = outcomes[0]["plan_id"]

            # Wait for planner thread to complete
            for _ in range(50):
                plan_state = planner.state()["plans"].get(plan_id, {})
                if plan_state.get("state") == "ready":
                    break
                time.sleep(0.05)

            self.assertEqual(planner.state()["plans"][plan_id]["state"], "ready")

            # Successor P14.6 is now written into agent/next.md with READY_TO_RUN
            new_truth = read_repository_truth(repo)
            next_text = (repo / "agent" / "next.md").read_text(encoding="utf-8")
            self.assertIn("P14.6", next_text)
            self.assertIn("READY_TO_RUN", next_text)

            # 3. Executor advance automatically launches unlaunched READY_TO_RUN successor
            new_summary = {
                "projects": [{
                    "project_id": "p1",
                    "state": "READY_TO_RUN",
                    "next_status": "READY_TO_RUN",
                    "telemetry": {"task_id": "P14.6"},
                    "git": {"head": new_truth.head, "branch": new_truth.branch},
                }]
            }
            launches = executor.advance(new_summary, config_path)
            self.assertEqual(len(launches), 1)
            self.assertEqual(launches[0].task_id, "P14.6")
            self.assertTrue(launches[0].source_request_id.startswith("auto-ready:"))

            # Wait for the launched worker thread to finish before tempdir cleanup
            worker_thread = executor._threads.get(launches[0].source_request_id)
            if worker_thread is not None:
                worker_thread.join(timeout=10.0)

    def test_recovery_epoch_cross_component_agreement(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "repo"
            head = make_git_repo(repo, "P14.6")
            truth = read_repository_truth(repo)
            runtime = base / "runtime"
            runtime.mkdir(parents=True, exist_ok=True)

            snapshot = {
                "project_id": "p1",
                "state": "READY_TO_RUN",
                "telemetry": {"task_id": "P14.6"},
                "git": {"head": head, "branch": truth.branch},
                "next_title": "P14.6 Unattended Gate",
            }
            summary = {"projects": [snapshot]}

            projects_cfg = {
                "projects": [{
                    "project_id": "p1",
                    "repo_path": str(repo),
                    "execution": {"enabled": True, "engine": "aibroker"},
                }]
            }

            # 1. Watchdog resolver
            epoch_watchdog = resolve_recovery_epoch(snapshot, summary, runtime_root=runtime)
            epoch_id_watchdog = epoch_watchdog["id"]

            # 2. Runtime status
            runtime_status = project_runtime_status(snapshot, runtime)
            epoch_runtime = runtime_status["recovery_epoch"]
            epoch_id_runtime = runtime_status["recovery_epoch_id"]

            # 3. Build project status (dashboard/monitor)
            project_status = build_project_status(
                snapshot, runtime, phase="monitor", daemon_state="running", pid=999
            )
            epoch_proj = project_status["recovery_epoch"]
            epoch_id_proj = project_status["recovery_epoch_id"]

            # 4. Control overview
            control_view = project_control_view(
                snapshot, runtime, project_config=projects_cfg["projects"][0]
            )
            epoch_ctrl = control_view["recovery_epoch"]
            epoch_id_ctrl = control_view["recovery_epoch_id"]

            # All 4 components must agree exactly on epoch dict and epoch_id hash
            self.assertEqual(epoch_id_watchdog, epoch_id_runtime)
            self.assertEqual(epoch_id_watchdog, epoch_id_proj)
            self.assertEqual(epoch_id_watchdog, epoch_id_ctrl)
            self.assertEqual(epoch_watchdog["evidence"]["task_id"], "P14.6")
            self.assertEqual(epoch_watchdog["evidence"]["head"], head)
