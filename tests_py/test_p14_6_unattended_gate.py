import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dev_orchestrator.ai.contracts import AIRoleRequest, AIRoleResult, ResourceContext
from dev_orchestrator.control.surface import project_control_view
from dev_orchestrator.core.ai_planner import AIPlannerCoordinator
from dev_orchestrator.core.ai_reviewer import AIReviewerCoordinator
from dev_orchestrator.core.control_commands import ControlCommandCoordinator
from dev_orchestrator.core.project_status import build_project_status, project_runtime_status, _watchdog_view
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.core.staged_roadmap import read_successor
from dev_orchestrator.core.transition_executor import TransitionExecutor
from dev_orchestrator.core.watchdog import resolve_recovery_epoch, WatchdogCoordinator, canonical_path
from dev_orchestrator.storage.json_store import write_json, utc_now_iso
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


class E2EQualificationPort:
    def __init__(self, repo: Path):
        self.repo = repo
        self.calls = 0
        self.requests: list[AIRoleRequest] = []

    def execute(self, request: AIRoleRequest) -> AIRoleResult:
        self.calls += 1
        self.requests.append(request)
        role = request.role
        run_id = request.role_run_id or ""

        if role == "reviewer" and "plan" in run_id:
            return AIRoleResult(
                request_id=request.request_id,
                role_run_id=request.role_run_id,
                status="succeeded",
                output=json.dumps({"decision": "approve", "reason": "Plan approved."}),
                dispatch_id="disp-plan-rev",
                execution_id="exec-plan-rev",
                resource_context=ResourceContext("res-plan-rev", "prov", "acc", "mod"),
            )

        if role == "planner":
            return AIRoleResult(
                request_id=request.request_id,
                role_run_id=request.role_run_id,
                status="succeeded",
                output=json.dumps({
                    "task_id": "P14.6",
                    "summary": "Successor plan",
                    "implementation_steps": ["step 1"],
                    "interfaces": ["iface 1"],
                    "validation": ["val 1"],
                    "risks": ["none"],
                    "out_of_scope": ["none"],
                }),
                dispatch_id="disp-plan",
                execution_id="exec-plan",
                resource_context=ResourceContext("res-plan", "prov", "acc", "mod"),
            )

        if role == "worker":
            if self.calls == 1:
                return AIRoleResult(
                    request_id=request.request_id,
                    role_run_id=request.role_run_id,
                    status="failed",
                    failure_classification="quota_exhausted",
                    error="Worker model quota exhausted",
                    resource_context=ResourceContext("worker-res-1", "prov-a", "acc-1", "mod-a"),
                )
            if self.calls == 2:
                (self.repo / "agent" / "next.md").write_bytes(b"# P14.5 Task\nStatus: **COMPLETE**\n\nGoal: done\n")
                (self.repo / "p14_5.txt").write_text("p14.5 work done", encoding="utf-8")
                subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
                subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "worker complete P14.5"], check=True, capture_output=True)
                return AIRoleResult(
                    request_id=request.request_id,
                    role_run_id=request.role_run_id,
                    status="succeeded",
                    output="P14.5 initial worker complete",
                    dispatch_id="disp-w1",
                    execution_id="exec-w1",
                    resource_context=ResourceContext("worker-res-2", "prov-b", "acc-2", "mod-b"),
                )
            if "remediation" in request.request_id or self.calls == 5:
                (self.repo / "remediation.txt").write_text("remediation applied", encoding="utf-8")
                subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
                subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "worker remediate P14.5"], check=True, capture_output=True)
                return AIRoleResult(
                    request_id=request.request_id,
                    role_run_id=request.role_run_id,
                    status="succeeded",
                    output="P14.5 remediation worker complete",
                    dispatch_id="disp-w-rem",
                    execution_id="exec-w-rem",
                    resource_context=ResourceContext("worker-res-3", "prov-b", "acc-2", "mod-b"),
                )
            # Successor P14.6 worker
            (self.repo / "p14_6.txt").write_text("p14.6 work done", encoding="utf-8")
            subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "worker complete P14.6"], check=True, capture_output=True)
            return AIRoleResult(
                request_id=request.request_id,
                role_run_id=request.role_run_id,
                status="succeeded",
                output="P14.6 worker complete",
                dispatch_id="disp-w-p146",
                execution_id="exec-w-p146",
                resource_context=ResourceContext("worker-res-4", "prov-c", "acc-3", "mod-c"),
            )

        if role == "reviewer":
            if self.calls == 3:
                return AIRoleResult(
                    request_id=request.request_id,
                    role_run_id=request.role_run_id,
                    status="failed",
                    failure_classification="rate_limited",
                    error="Reviewer rate limited",
                    resource_context=ResourceContext("rev-res-1", "prov-r1", "acc-r1", "mod-r1"),
                )
            if self.calls == 4:
                return AIRoleResult(
                    request_id=request.request_id,
                    role_run_id=request.role_run_id,
                    status="succeeded",
                    output=json.dumps({
                        "decision": "remediate",
                        "next_action": "continue_current_stage",
                        "reason": "Needs remediation for edge case.",
                    }),
                    dispatch_id="disp-r1",
                    execution_id="exec-r1",
                    resource_context=ResourceContext("rev-res-2", "prov-r2", "acc-r2", "mod-r2"),
                )
            return AIRoleResult(
                request_id=request.request_id,
                role_run_id=request.role_run_id,
                status="succeeded",
                output=json.dumps({
                    "decision": "next",
                    "next_action": "next_task",
                    "reason": "Remediation accepted, next task approved.",
                }),
                dispatch_id="disp-r2",
                execution_id="exec-r2",
                resource_context=ResourceContext("rev-res-3", "prov-r3", "acc-r3", "mod-r3"),
            )
        raise ValueError(f"Unexpected request: {request}")


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
            self.assertEqual(rev_rec.get("failover_from_resource_ids"), ["res-fail-1", "res-fail-2"])

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
            self.assertEqual(port.requests[1].request_id, "ai-worker:worker-launch-1:failover-1")
            self.assertIn("worker-res-1", port.requests[1].excluded_resource_ids)

            ledger = executor.state()["executions"]
            exec_row = ledger["worker-launch-1"]
            self.assertEqual(exec_row["state"], "completed")
            self.assertEqual(exec_row.get("failover_from_resource_ids"), ["worker-res-1"])
            self.assertEqual(exec_row.get("broker_request_id"), "ai-worker:worker-launch-1:failover-1")

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

            projects_cfg = {
                "projects": [{
                    "project_id": "p1",
                    "repo_path": str(repo),
                    "execution": {"enabled": True, "engine": "aibroker"},
                    "watchdog": {"enabled": True},
                }]
            }
            config_path = base / "projects.json"
            config_path.write_text(json.dumps(projects_cfg), encoding="utf-8")

            repo_fp = hashlib.sha256(canonical_path(repo).encode("utf-8")).hexdigest()[:16]

            # 1. Baseline check on empty runtime: all components agree on None for role evidence
            empty_snapshot = {
                "project_id": "p1",
                "repo_path": str(repo),
                "state": "READY_TO_RUN",
                "telemetry": {"task_id": "P14.6"},
                "git": {"head": head, "branch": truth.branch},
                "next_title": "P14.6 Unattended Gate",
            }
            empty_epoch = resolve_recovery_epoch(empty_snapshot, runtime_root=runtime)
            self.assertIsNotNone(empty_epoch)
            self.assertIsNone(empty_epoch["evidence"]["plan_id"])
            self.assertIsNone(empty_epoch["evidence"]["review_id"])
            self.assertIsNone(empty_epoch["evidence"]["execution_id"])
            self.assertIsNone(empty_epoch["evidence"]["control_id"])

            # 2. Discriminating check: active plan, review, execution, and control records
            write_json(runtime / "ai-planner.json", {
                "version": 1,
                "plans": {
                    "plan-act-1": {
                        "project_id": "p1",
                        "plan_id": "plan-act-1",
                        "state": "planning",
                        "started_at": "2026-09-20T01:00:00+00:00",
                    }
                },
            })
            write_json(runtime / "ai-reviewer.json", {
                "version": 1,
                "reviews": {
                    "rev-act-1": {
                        "project_id": "p1",
                        "review_id": "rev-act-1",
                        "state": "running",
                        "started_at": "2026-09-20T01:05:00+00:00",
                    }
                },
            })
            write_json(runtime / "transition-executor.json", {
                "version": 1,
                "executions": {
                    "exec-act-1": {
                        "project_id": "p1",
                        "execution_id": "exec-act-1",
                        "state": "running",
                        "started_at": "2026-09-20T01:10:00+00:00",
                    }
                },
            })

            snapshot = {
                "project_id": "p1",
                "repo_path": str(repo),
                "state": "READY_TO_RUN",
                "telemetry": {"task_id": "P14.6"},
                "git": {"head": head, "branch": truth.branch},
                "source_request_id": "ctrl-req-1",
                "activity": {
                    "watchdog_safe": {
                        "repo_scope": "canonical",
                        "repo_root_fingerprint": repo_fp,
                        "last_activity_at": utc_now_iso(),
                        "sources": {},
                    }
                },
            }
            summary = {"projects": [snapshot]}

            # Tick WatchdogCoordinator to update watchdog.json
            wd = WatchdogCoordinator(runtime)
            wd.advance(config_path, summary)
            for t in wd._threads.values():
                t.join(timeout=10.0)

            # Component 1: direct resolver
            ep_direct = resolve_recovery_epoch(snapshot, runtime_root=runtime)
            self.assertIsNotNone(ep_direct)
            epoch_id_direct = ep_direct["id"]

            # Component 2: watchdog view
            wd_view = _watchdog_view(runtime, "p1")
            self.assertIsNotNone(wd_view)
            epoch_id_wd = wd_view["recovery_epoch_id"]

            # Component 3: runtime status
            runtime_status = project_runtime_status(snapshot, runtime)
            epoch_id_runtime = runtime_status["recovery_epoch_id"]

            # Component 4: project status (monitor/daemon)
            proj_status = build_project_status(
                snapshot, runtime, phase="monitor", daemon_state="running", pid=999
            )
            epoch_id_proj = proj_status["recovery_epoch_id"]

            # Component 5: control view
            ctrl_view = project_control_view(
                snapshot, runtime, project_config=projects_cfg["projects"][0]
            )
            epoch_id_ctrl = ctrl_view["recovery_epoch_id"]

            # All 5 components must agree exactly on epoch dict and epoch_id hash
            self.assertEqual(epoch_id_direct, epoch_id_wd)
            self.assertEqual(epoch_id_direct, epoch_id_runtime)
            self.assertEqual(epoch_id_direct, epoch_id_proj)
            self.assertEqual(epoch_id_direct, epoch_id_ctrl)

            # All evidence fields must be non-trivial and present
            evidence = ep_direct["evidence"]
            self.assertEqual(evidence["project_id"], "p1")
            self.assertEqual(evidence["task_id"], "P14.6")
            self.assertEqual(evidence["head"], head)
            self.assertEqual(evidence["plan_id"], "plan-act-1")
            self.assertEqual(evidence["review_id"], "rev-act-1")
            self.assertEqual(evidence["execution_id"], "exec-act-1")
            self.assertEqual(evidence["control_id"], "ctrl-req-1")
            self.assertNotIn(None, evidence.values())

            # Sensitivity: mutating any evidence field must change epoch id
            mutated_snap = copy.deepcopy(snapshot)
            mutated_snap["telemetry"]["task_id"] = "P14.7"
            ep_mutated = resolve_recovery_epoch(mutated_snap, runtime_root=runtime)
            self.assertNotEqual(ep_mutated["id"], epoch_id_direct)

    def test_unattended_successor_promotion_does_not_bypass_review(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "repo"
            init_head = make_git_repo(repo, "P14.5")
            spec_path, _ = setup_staged_spec(repo, "P14.6")
            setup_roadmap(repo, "P14.5", "P14.6", spec_path)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "setup staged successor"], check=True, capture_output=True)

            # Worker executes and commits changes to repo, advancing HEAD
            (repo / "work.txt").write_text("worker did work\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "worker commit"], check=True, capture_output=True)

            runtime = base / "runtime"
            runtime.mkdir(parents=True, exist_ok=True)
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
                        "allowed_next_actions": ["next_task", "continue_current_stage"],
                        "worker_prompt": "worker prompt",
                        "remediation_prompt": "remediation prompt",
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

            # 1. Pre-review state: Worker is completed in transition-executor.json, but review has NOT occurred
            write_json(runtime / "transition-executor.json", {
                "version": 1,
                "executions": {
                    "worker-run-1": {
                        "project_id": "p1",
                        "source_request_id": "worker-run-1",
                        "engine": "aibroker",
                        "state": "completed",
                        "task_id": "P14.5",
                        "branch": "master",
                        "head": init_head,
                        "repo_path": str(repo),
                        "completed_at": "2026-09-20T01:00:00+00:00",
                    }
                },
            })

            truth = read_repository_truth(repo)
            summary = {
                "projects": [{
                    "project_id": "p1",
                    "state": "IDLE",
                    "next_status": "**COMPLETE**",
                    "telemetry": {"task_id": "P14.5"},
                    "git": {"head": truth.head, "branch": truth.branch},
                }]
            }

            port = FakeResourceFailoverWorkerPort()
            executor = TransitionExecutor(runtime, ai_execution_port=port)
            executor.advance(summary, config_path)
            ledger = executor.state()["executions"]
            auto_handoffs = [k for k in ledger if k.startswith("auto-handoff:")]
            self.assertEqual(len(auto_handoffs), 0, "Predecessor promotion must NOT bypass mandatory review")

            # 1b. Failed worker state: Worker previously failed on task; repo advertises COMPLETE
            write_json(runtime / "transition-executor.json", {
                "version": 1,
                "executions": {
                    "worker-run-failed": {
                        "project_id": "p1",
                        "source_request_id": "worker-run-failed",
                        "engine": "aibroker",
                        "state": "failed",
                        "task_id": "P14.5",
                        "branch": "master",
                        "head": init_head,
                        "repo_path": str(repo),
                        "completed_at": "2026-09-20T01:00:00+00:00",
                    }
                },
            })
            executor.advance(summary, config_path)
            ledger = executor.state()["executions"]
            auto_handoffs = [k for k in ledger if k.startswith("auto-handoff:")]
            self.assertEqual(len(auto_handoffs), 0, "Predecessor promotion must NOT occur when worker previously failed without accepted review")

            # 2. In-flight review state: review is currently running
            write_json(runtime / "ai-reviewer.json", {
                "version": 1,
                "reviews": {
                    "ai_review:worker-run-1": {
                        "project_id": "p1",
                        "review_id": "ai_review:worker-run-1",
                        "source_request_id": "worker-run-1",
                        "task_id": "P14.5",
                        "state": "running",
                        "started_at": "2026-09-20T01:05:00+00:00",
                    }
                },
            })
            executor.advance(summary, config_path)
            ledger = executor.state()["executions"]
            auto_handoffs = [k for k in ledger if k.startswith("auto-handoff:")]
            self.assertEqual(len(auto_handoffs), 0, "Auto handoff must NOT be created while review is in flight")

            # 3. Remediation state: review produces remediation decision
            write_json(runtime / "review-decisions.json", {
                "version": 1,
                "decisions": {
                    "ai_review:worker-run-1": {
                        "decision_id": "dec-1",
                        "review_id": "ai_review:worker-run-1",
                        "request_id": "ai_review:worker-run-1",
                        "project_id": "p1",
                        "task_id": "P14.5",
                        "branch": truth.branch,
                        "head": truth.head,
                        "role": "reviewer",
                        "event": "worker_done",
                        "disposition": "apply",
                        "decision": "remediate",
                        "next_action": "continue_current_stage",
                        "review_status_hash": truth.status_hash,
                        "consumed_at": utc_now_iso(),
                        "created_at": "2026-09-20T01:10:00+00:00",
                    }
                },
            })
            remediation_launches = executor.advance(summary, config_path)
            self.assertEqual(len(remediation_launches), 1)
            self.assertEqual(remediation_launches[0].task_id, "P14.5")
            rem_req_id = remediation_launches[0].source_request_id
            ledger = executor.state()["executions"]
            self.assertEqual(ledger[rem_req_id]["source_kind"], "remediation")
            auto_handoffs = [k for k in ledger if k.startswith("auto-handoff:")]
            self.assertEqual(len(auto_handoffs), 0, "Auto handoff must NOT be created on remediation")

            rem_thread = executor._threads.get(rem_req_id)
            if rem_thread is not None:
                rem_thread.join(timeout=10.0)

            # 4. Accepted re-review state: review produces next/next_task
            truth = read_repository_truth(repo)
            summary["projects"][0]["git"]["head"] = truth.head
            write_json(runtime / "review-decisions.json", {
                "version": 1,
                "decisions": {
                    "ai_review:worker-run-rem": {
                        "decision_id": "dec-2",
                        "review_id": "ai_review:worker-run-rem",
                        "request_id": "ai_review:worker-run-rem",
                        "project_id": "p1",
                        "task_id": "P14.5",
                        "branch": truth.branch,
                        "head": truth.head,
                        "role": "reviewer",
                        "event": "worker_done",
                        "disposition": "apply",
                        "decision": "next",
                        "next_action": "next_task",
                        "review_status_hash": truth.status_hash,
                        "consumed_at": utc_now_iso(),
                        "created_at": "2026-09-20T01:15:00+00:00",
                    }
                },
            })
            executor.advance(summary, config_path)
            ledger = executor.state()["executions"]
            handoff_entries = [v for v in ledger.values() if v.get("state") == "handoff"]
            self.assertEqual(len(handoff_entries), 1)
            self.assertEqual(handoff_entries[0]["staged_successor"], "P14.6")

    def test_end_to_end_unattended_multi_task_with_failure_injection_and_failover(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "repo"
            head = make_git_repo(repo, "P14.5")
            spec_path, _ = setup_staged_spec(repo, "P14.6")
            setup_roadmap(repo, "P14.5", "P14.6", spec_path)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "setup staged successor"], check=True, capture_output=True)

            # Start P14.5 with READY_TO_RUN
            (repo / "agent" / "next.md").write_bytes(b"# P14.5 Task\nStatus: READY_TO_RUN\n\nGoal: task 14.5\n")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "p14.5 ready"], check=True, capture_output=True)

            runtime = base / "runtime"
            runtime.mkdir(parents=True, exist_ok=True)
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
                        "allowed_next_actions": ["next_task", "continue_current_stage"],
                        "worker_prompt": "worker prompt",
                        "remediation_prompt": "remediation prompt",
                    },
                    "ai_roles": {
                        "planner": {"enabled": True},
                        "reviewer": {"enabled": True, "quality": "high", "independence": "resource"},
                    },
                }]
            }
            config_path.write_text(json.dumps(config_data), encoding="utf-8")

            port = E2EQualificationPort(repo)
            planner = AIPlannerCoordinator(runtime, port)
            reviewer = AIReviewerCoordinator(runtime, port)
            executor = TransitionExecutor(runtime, ai_execution_port=port)
            control = ControlCommandCoordinator(runtime, planner)

            # Step A: Unattended launch of P14.5 worker with quota failover
            truth = read_repository_truth(repo)
            summary = {
                "projects": [{
                    "project_id": "p1",
                    "state": "READY_TO_RUN",
                    "next_status": "READY_TO_RUN",
                    "telemetry": {"task_id": "P14.5"},
                    "git": {"head": truth.head, "branch": truth.branch},
                }]
            }
            launches = executor.advance(summary, config_path)
            self.assertEqual(len(launches), 1)
            w_req_id = launches[0].source_request_id
            executor._threads[w_req_id].join(timeout=10.0)
            exec_row = executor.state()["executions"][w_req_id]
            self.assertEqual(exec_row["state"], "completed")
            self.assertEqual(exec_row.get("failover_from_resource_ids"), ["worker-res-1"])

            # Step B: Technical Reviewer launch with rate-limit failover and remediation decision
            truth = read_repository_truth(repo)
            summary = {
                "projects": [{
                    "project_id": "p1",
                    "state": "IDLE",
                    "next_status": "**COMPLETE**",
                    "telemetry": {"task_id": "P14.5"},
                    "git": {"head": truth.head, "branch": truth.branch},
                }]
            }
            rev_launches = reviewer.advance(config_path)
            self.assertEqual(len(rev_launches), 1)
            rev_id = rev_launches[0]
            reviewer._threads[rev_id].join(timeout=10.0)
            rev_row = reviewer.state()["reviews"][rev_id]
            self.assertEqual(rev_row["state"], "completed")
            self.assertEqual(rev_row["decision"], "remediate")
            self.assertEqual(rev_row.get("failover_from_resource_ids"), ["rev-res-1"])

            # Step C: Remediation worker launch
            rem_launches = executor.advance(summary, config_path)
            self.assertEqual(len(rem_launches), 1)
            rem_id = rem_launches[0].source_request_id
            executor._threads[rem_id].join(timeout=10.0)
            self.assertEqual(executor.state()["executions"][rem_id]["state"], "completed")

            # Step D: Re-review launch and accept
            truth = read_repository_truth(repo)
            summary["projects"][0]["git"]["head"] = truth.head
            rerev_launches = reviewer.advance(config_path)
            self.assertEqual(len(rerev_launches), 1)
            rerev_id = rerev_launches[0]
            reviewer._threads[rerev_id].join(timeout=10.0)
            self.assertEqual(reviewer.state()["reviews"][rerev_id]["decision"], "next")

            # Step E: Decision advance to handoff
            executor.advance(summary, config_path)
            handoffs = [v for v in executor.state()["executions"].values() if v.get("state") == "handoff"]
            self.assertEqual(len(handoffs), 1)
            self.assertEqual(handoffs[0]["staged_successor"], "P14.6")

            # Step F: Planner and Plan Review for P14.6
            outcomes = control.advance(config_path, summary, executor)
            self.assertEqual(len(outcomes), 1)
            plan_id = outcomes[0]["plan_id"]
            for _ in range(50):
                if planner.state()["plans"].get(plan_id, {}).get("state") == "ready":
                    break
                time.sleep(0.05)
            self.assertEqual(planner.state()["plans"][plan_id]["state"], "ready")
            next_text = (repo / "agent" / "next.md").read_text(encoding="utf-8")
            self.assertIn("P14.6", next_text)
            self.assertIn("READY_TO_RUN", next_text)

            # Step G: Automatic launch and execution of P14.6 worker
            truth = read_repository_truth(repo)
            summary = {
                "projects": [{
                    "project_id": "p1",
                    "state": "READY_TO_RUN",
                    "next_status": "READY_TO_RUN",
                    "telemetry": {"task_id": "P14.6"},
                    "git": {"head": truth.head, "branch": truth.branch},
                }]
            }
            succ_launches = executor.advance(summary, config_path)
            self.assertEqual(len(succ_launches), 1)
            self.assertEqual(succ_launches[0].task_id, "P14.6")
            succ_id = succ_launches[0].source_request_id
            executor._threads[succ_id].join(timeout=10.0)
            self.assertEqual(executor.state()["executions"][succ_id]["state"], "completed")
            self.assertTrue((repo / "p14_6.txt").is_file())

    def test_worker_failover_crash_recovery_reconciliation(self):
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
                }]
            }
            config_path.write_text(json.dumps(config_data), encoding="utf-8")

            attempt_2_dispatched = threading.Event()
            can_finish_attempt_2 = threading.Event()

            class CrashFailoverPort:
                def __init__(self):
                    self.calls = 0
                    self.requests: list[AIRoleRequest] = []
                    self.status_queries: list[str] = []

                def execute(self, request: AIRoleRequest) -> AIRoleResult:
                    self.calls += 1
                    self.requests.append(request)
                    if self.calls == 1:
                        return AIRoleResult(
                            request_id=request.request_id,
                            role_run_id=request.role_run_id,
                            status="failed",
                            failure_classification="quota_exhausted",
                            error="Quota limit reached for model-a",
                            resource_context=ResourceContext("res-a", "prov-a", "acc-a", "mod-a"),
                        )
                    attempt_2_dispatched.set()
                    can_finish_attempt_2.wait(timeout=10.0)
                    return AIRoleResult(
                        request_id=request.request_id,
                        role_run_id=request.role_run_id,
                        status="succeeded",
                        output="WORKER_FAILOVER_SUCCESS",
                        dispatch_id="disp-f2",
                        execution_id="exec-f2",
                        resource_context=ResourceContext("res-b", "prov-b", "acc-b", "mod-b"),
                    )

                def status(self, broker_request_id: str) -> dict[str, Any]:
                    self.status_queries.append(broker_request_id)
                    if broker_request_id == "ai-worker:worker-failover-run:failover-1":
                        return {
                            "request_id": broker_request_id,
                            "status": "running",
                            "dispatch_id": "disp-f2",
                            "execution_id": "exec-f2",
                        }
                    return {
                        "request_id": broker_request_id,
                        "status": "failed",
                        "execution_error": "Quota limit reached for model-a",
                    }

            port = CrashFailoverPort()
            executor = TransitionExecutor(runtime, ai_execution_port=port)

            launch = executor._launch(
                config_data["projects"][0],
                source_request_id="worker-failover-run",
                source_kind="ready",
                task_id="P14.5",
                source_task_id=None,
                branch=truth.branch,
                head=head,
                worker_prompt="do task",
                policy=config_data["projects"][0]["execution"],
            )
            self.assertIsNotNone(launch)

            self.assertTrue(attempt_2_dispatched.wait(timeout=10.0))

            ledger_on_disk = executor.state()["executions"]["worker-failover-run"]
            self.assertEqual(
                ledger_on_disk["broker_request_id"],
                "ai-worker:worker-failover-run:failover-1",
            )
            self.assertEqual(ledger_on_disk["state"], "running")

            recovery_port = CrashFailoverPort()
            recovery_executor = TransitionExecutor(runtime, ai_execution_port=recovery_port)

            self.assertIn("ai-worker:worker-failover-run:failover-1", recovery_port.status_queries)
            self.assertNotIn("ai-worker:worker-failover-run", recovery_port.status_queries)

            recovered_ledger = recovery_executor.state()["executions"]["worker-failover-run"]
            self.assertNotEqual(recovered_ledger["state"], "failed")
            self.assertEqual(recovered_ledger["state"], "recovery_required")
            self.assertFalse(recovered_ledger.get("recovery_safe_retry"))
            self.assertIn("forbidden", recovered_ledger.get("reason", ""))

            can_finish_attempt_2.set()
            thread = executor._threads.get("worker-failover-run")
            if thread is not None:
                thread.join(timeout=10.0)
