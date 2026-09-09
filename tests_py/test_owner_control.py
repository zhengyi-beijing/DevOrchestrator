import asyncio
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from dev_orchestrator.agents.base import AgentBackend
from dev_orchestrator.agents.models import (
    AgentRequest, AgentResult, AgentRole, AgentRun, AgentRunState,
    BackendStatus, CapabilitySet, QuotaState,
)
from dev_orchestrator.bridge.store import BrowserBridgeStore
from dev_orchestrator.control.owner_store import OwnerControlStore
from dev_orchestrator.control.service import ControlPlaneService
from dev_orchestrator.control.store import ConversationControlStore, ControlConflictError
from dev_orchestrator.core.transition_executor import TransitionExecutor, _execution_policy
from dev_orchestrator.core.websol import WebSolEvent, WebSolRequest, WebSolRole


class FakeOwnerBackend(AgentBackend):
    backend_id = "agy"
    def __init__(self):
        self.started = []
        self.counter = 0
    def capabilities(self):
        return CapabilitySet(roles=frozenset({AgentRole.WORKER}), tags=frozenset({"code", "repository"}),
                             provider="agy", independence_domain="agy", cancellation_supported=True)
    def probe(self):
        return BackendStatus("agy", True, "", QuotaState.UNKNOWN)
    async def start(self, request: AgentRequest):
        self.started.append(request); self.counter += 1
        return AgentRun(f"owner-run-{self.counter}", "agy", AgentRunState.RUNNING,
                        None, 4242, request.project_id)
    async def status(self, run_id):
        return AgentRun(run_id, "agy", AgentRunState.COMPLETED, 0)
    async def cancel(self, run_id):
        return AgentRun(run_id, "agy", AgentRunState.CANCELLED, -1)
    async def collect(self, run_id):
        return AgentResult(run_id, "agy", AgentRunState.COMPLETED, 0)


def make_repo(root: Path, task_id="P2", status="DESIGN READY / EXECUTABLE"):
    root.mkdir(); (root / "agent").mkdir()
    (root / "agent" / "CURRENT.md").write_text("# current\n", encoding="utf-8")
    (root / "agent" / "next.md").write_text(
        f"# {task_id} bounded task\nStatus: **{status}**\n", encoding="utf-8")
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "init"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "fixture"], check=True, stdout=subprocess.DEVNULL)
    branch = subprocess.check_output(["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"], text=True).strip()
    head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    return branch, head
def write_config(path: Path, repo: Path, *, owner_start=None):
    execution = {
        "enabled": True, "owner_authorized": True,
        "allowed_next_actions": ["continue_current_stage", "next_task"],
        "preferred_backends": ["agy"], "backends": {"agy": {}},
        "worker_prompt": "Execute exactly the current bounded software-only task; do not push.",
    }
    if owner_start:
        execution["owner_start"] = owner_start
    path.write_text(json.dumps({"projects": [{
        "project_id": "p1", "name": "P1", "repo_path": str(repo),
        "adapter": "agent_files", "conversation_binding": {
            "transport": "browser_bridge", "adapter": "chatgpt_web", "binding_id": "conv-A"},
        "execution": execution,
    }]}), encoding="utf-8")


def write_snapshot(runtime: Path, repo: Path, branch: str, head: str,
                   task_id: str, state: str):
    (runtime / "projects").mkdir(parents=True, exist_ok=True)
    payload = {"project_id": "p1", "id": "p1", "state": state,
               "repo_path": str(repo), "git": {"branch": branch, "head": head,
               "dirty": False, "changed_entries": 0},
               "worker": {"kind": "none", "state": "not_started", "process_alive": False},
               "telemetry": {"task_id": task_id}, "next_title": f"{task_id} bounded task"}
    (runtime / "projects" / "p1.json").write_text(json.dumps(payload), encoding="utf-8")
    return {"projects": [payload]}


def write_gate(runtime: Path, branch: str, head: str, task_id="P1", request_id="gate-p1"):
    runtime.mkdir(parents=True, exist_ok=True)
    record = {"project_id": "p1", "request_id": request_id,
              "disposition": "owner_gate", "decision": "owner_gate", "next_action": "stop",
              "task_id": task_id, "branch": branch, "head": head,
              "consumed_at": "2026-09-09T00:00:00+00:00"}
    (runtime / "websol-decisions.json").write_text(
        json.dumps({"version": 1, "decisions": {request_id: record}}), encoding="utf-8")
def wait_done(executor: TransitionExecutor, source_request_id: str):
    deadline = time.time() + 2
    while time.time() < deadline:
        record = executor.state()["executions"].get(source_request_id)
        if record and record.get("state") in {"completed", "failed", "cancelled", "blocked"}:
            thread = executor._threads.get(source_request_id)
            if thread is not None:
                thread.join(timeout=1.0)
                if thread.is_alive():
                    raise AssertionError("owner execution thread did not terminate")
            return record
        time.sleep(0.02)
    raise AssertionError("owner execution did not finish")


class OwnerControlTests(unittest.TestCase):
    def setup_fixture(self, *, task_id="P2", status="DESIGN READY / EXECUTABLE", owner_start=None):
        tmp = tempfile.TemporaryDirectory(); base = Path(tmp.name)
        repo = base / "repo"; runtime = base / "runtime"; config = base / "projects.json"
        branch, head = make_repo(repo, task_id, status); write_config(config, repo, owner_start=owner_start)
        store = ConversationControlStore(runtime); store.heartbeat(
            "chatgpt_web", "conv-A", title="P1 conversation",
            url="https://chatgpt.com/c/conv-A", tab_instance_id="tab-1")
        owner_store = OwnerControlStore(runtime); backend = FakeOwnerBackend()
        executor = TransitionExecutor(runtime, backend_overrides={"agy": backend}, owner_store=owner_store)
        service = ControlPlaneService(config, runtime, store, BrowserBridgeStore(runtime / "bridge"),
                                      transition_executor=executor, owner_store=owner_store)
        return tmp, repo, runtime, config, branch, head, store, owner_store, backend, executor, service

    @staticmethod
    def base_payload(action, action_id, branch, head):
        return {"project_id": "p1", "action": action, "action_id": action_id,
                "adapter": "chatgpt_web", "binding_id": "conv-A",
                "expected_branch": branch, "expected_head": head}
    def test_exact_gate_approval_is_explicit_and_non_executing(self):
        f = self.setup_fixture(task_id="P2"); tmp, repo, runtime, config, branch, head, _, owner_store, backend, executor, service = f
        try:
            write_snapshot(runtime, repo, branch, head, "P2", "READY_TO_RUN")
            write_gate(runtime, branch, head, task_id="P1")
            payload = self.base_payload("approve_next_stage", "approve-1", branch, head)
            payload.update({"gate_request_id": "gate-p1", "gate_task_id": "P1"})
            result = service.owner_action(payload)
            self.assertEqual(result["owner_action"]["effect"], "gate_approved_no_worker_started")
            self.assertEqual(len(backend.started), 0)
            self.assertEqual(service._latest_owner_gate("p1")["owner_state"], "approved")
            self.assertFalse(owner_store.is_paused("p1"))
        finally: tmp.cleanup()

    def test_start_current_task_requires_approved_gate_then_launches_once(self):
        f = self.setup_fixture(task_id="P2"); tmp, repo, runtime, config, branch, head, _, _, backend, executor, service = f
        try:
            write_snapshot(runtime, repo, branch, head, "P2", "READY_TO_RUN")
            write_gate(runtime, branch, head, task_id="P1")
            start = self.base_payload("start_current_task", "start-1", branch, head); start["expected_task_id"] = "P2"
            with self.assertRaises(ControlConflictError): service.owner_action(start)
            approve = self.base_payload("approve_next_stage", "approve-1", branch, head)
            approve.update({"gate_request_id": "gate-p1", "gate_task_id": "P1"})
            service.owner_action(approve)
            result = service.owner_action(start)
            self.assertEqual(result["launch"]["task_id"], "P2")
            record = wait_done(executor, "owner-control:start-1")
            self.assertEqual(record["state"], "completed")
            self.assertEqual(len(backend.started), 1)
            again = service.owner_action(start)
            self.assertTrue(again["idempotent"])
            self.assertEqual(len(backend.started), 1)
        finally: tmp.cleanup()
    def test_unadvanced_phase_gate_cannot_be_started(self):
        f = self.setup_fixture(task_id="P1", status="AWAITING REVIEW"); tmp, repo, runtime, config, branch, head, _, _, backend, executor, service = f
        try:
            write_snapshot(runtime, repo, branch, head, "P1", "WAITING_PHASE_GATE")
            write_gate(runtime, branch, head, task_id="P1")
            approve = self.base_payload("approve_next_stage", "approve-1", branch, head)
            approve.update({"gate_request_id": "gate-p1", "gate_task_id": "P1"})
            service.owner_action(approve)
            start = self.base_payload("start_current_task", "start-1", branch, head); start["expected_task_id"] = "P1"
            with self.assertRaises(ControlConflictError): service.owner_action(start)
            self.assertEqual(len(backend.started), 0)
        finally: tmp.cleanup()

    def test_stop_persists_pause_and_suppresses_static_owner_start(self):
        f = self.setup_fixture(task_id="P2", owner_start={"request_id": "legacy-start", "task_id": "P2"})
        tmp, repo, runtime, config, branch, head, _, owner_store, backend, executor, service = f
        try:
            summary = write_snapshot(runtime, repo, branch, head, "P2", "READY_TO_RUN")
            stop = self.base_payload("stop", "stop-1", branch, head)
            result = service.owner_action(stop)
            self.assertEqual(result["owner_action"]["effect"], "pause_future_launches")
            self.assertTrue(owner_store.is_paused("p1"))
            self.assertTrue(owner_store.suppress_static_starts("p1"))
            self.assertEqual(executor.advance(summary, config), [])
            self.assertEqual(len(backend.started), 0)
            restored = OwnerControlStore(runtime)
            self.assertTrue(restored.is_paused("p1"))
        finally: tmp.cleanup()

    def test_owner_action_rejects_wrong_conversation_and_stale_head(self):
        f = self.setup_fixture(task_id="P2"); tmp, repo, runtime, config, branch, head, _, _, _, _, service = f
        try:
            write_snapshot(runtime, repo, branch, head, "P2", "READY_TO_RUN")
            wrong = self.base_payload("stop", "stop-wrong", branch, head); wrong["binding_id"] = "conv-other"
            with self.assertRaises(ControlConflictError): service.owner_action(wrong)
            stale = self.base_payload("start_current_task", "start-stale", branch, "0" * 40)
            stale["expected_task_id"] = "P2"
            with self.assertRaises(ControlConflictError): service.owner_action(stale)
        finally: tmp.cleanup()

    def test_stop_remains_available_when_repo_identity_has_moved(self):
        f = self.setup_fixture(task_id="P2"); tmp, repo, runtime, config, branch, head, _, owner_store, _, _, service = f
        try:
            write_snapshot(runtime, repo, branch, head, "P2", "READY_TO_RUN")
            payload = self.base_payload("stop", "stop-stale-ok", branch, head)
            (repo / "README.md").write_text("changed after UI render\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "move head"], check=True, stdout=subprocess.DEVNULL)
            result = service.owner_action(payload)
            self.assertEqual(result["owner_action"]["effect"], "pause_future_launches")
            self.assertTrue(owner_store.is_paused("p1"))
        finally: tmp.cleanup()

    def test_action_id_replay_requires_exact_caller_identity(self):
        f = self.setup_fixture(task_id="P2"); tmp, repo, runtime, config, branch, head, _, _, _, _, service = f
        try:
            write_snapshot(runtime, repo, branch, head, "P2", "READY_TO_RUN")
            payload = self.base_payload("stop", "stop-replay", branch, head)
            self.assertFalse(service.owner_action(payload)["idempotent"])
            replay = dict(payload); replay["binding_id"] = "conv-other"
            with self.assertRaises(ControlConflictError): service.owner_action(replay)
        finally: tmp.cleanup()

    def test_active_websol_claim_blocks_start_but_not_stop(self):
        f = self.setup_fixture(task_id="P2"); tmp, repo, runtime, config, branch, head, _, owner_store, backend, _, service = f
        try:
            write_snapshot(runtime, repo, branch, head, "P2", "READY_TO_RUN")
            request = WebSolRequest(
                project_id="p1", request_id="claim-active", task_id="P1", stage_id=None,
                branch=branch, head=head, role=WebSolRole.REVIEWER,
                event=WebSolEvent.WORKER_DONE, nonce="claim-nonce")
            service.bridge_store.submit("chatgpt_web", "conv-A", request, "review")
            self.assertIsNotNone(service.bridge_store.claim("chatgpt_web", "conv-A"))
            start = self.base_payload("start_current_task", "start-claimed", branch, head)
            start["expected_task_id"] = "P2"
            with self.assertRaises(ControlConflictError): service.owner_action(start)
            stop = self.base_payload("stop", "stop-claimed", branch, head)
            self.assertEqual(service.owner_action(stop)["owner_action"]["effect"], "pause_future_launches")
            self.assertTrue(owner_store.is_paused("p1"))
            self.assertEqual(len(backend.started), 0)
        finally: tmp.cleanup()

    def test_dirty_repository_blocks_gate_approval_server_side(self):
        f = self.setup_fixture(task_id="P2"); tmp, repo, runtime, config, branch, head, _, _, _, _, service = f
        try:
            write_snapshot(runtime, repo, branch, head, "P2", "READY_TO_RUN")
            write_gate(runtime, branch, head, task_id="P1")
            (repo / "README.md").write_text("dirty\n", encoding="utf-8")
            approve = self.base_payload("approve_next_stage", "approve-dirty", branch, head)
            approve.update({"gate_request_id": "gate-p1", "gate_task_id": "P1"})
            with self.assertRaises(ControlConflictError): service.owner_action(approve)
        finally: tmp.cleanup()

    def test_start_crash_window_reconciles_existing_launch(self):
        f = self.setup_fixture(task_id="P2"); tmp, repo, runtime, config, branch, head, _, owner_store, backend, executor, service = f
        try:
            write_snapshot(runtime, repo, branch, head, "P2", "READY_TO_RUN")
            write_gate(runtime, branch, head, task_id="P1")
            approve = self.base_payload("approve_next_stage", "approve-before-crash", branch, head)
            approve.update({"gate_request_id": "gate-p1", "gate_task_id": "P1"})
            service.owner_action(approve)
            start = self.base_payload("start_current_task", "start-crash", branch, head)
            start["expected_task_id"] = "P2"
            owner_store.record_action(
                "start-crash", "p1", "start_current_task", "launch_requested",
                adapter="chatgpt_web", binding_id="conv-A", expected_branch=branch,
                expected_head=head, expected_task_id="P2")
            launch = executor.owner_control_launch(
                config, project_id="p1", action_id="start-crash", expected_task_id="P2",
                expected_branch=branch, expected_head=head)
            wait_done(executor, launch.source_request_id)
            result = service.owner_action(start)
            self.assertTrue(result["idempotent"])
            self.assertEqual(result["owner_action"]["state"], "accepted")
            self.assertEqual(len(backend.started), 1)
        finally: tmp.cleanup()

    def test_start_intent_adopts_runtime_control_before_launch_result(self):
        f = self.setup_fixture(task_id="P1", status="AWAITING REVIEW", owner_start={"request_id": "legacy", "task_id": "P1"})
        tmp, repo, runtime, config, branch, head, _, owner_store, backend, executor, service = f
        try:
            write_snapshot(runtime, repo, branch, head, "P1", "READY_TO_RUN")
            start = self.base_payload("start_current_task", "start-adopt", branch, head)
            start["expected_task_id"] = "P1"
            with self.assertRaises(ControlConflictError): service.owner_action(start)
            self.assertTrue(owner_store.suppress_static_starts("p1"))
            self.assertEqual(len(backend.started), 0)
        finally: tmp.cleanup()

    def test_final_launch_gate_rechecks_owner_pause_and_static_suppression(self):
        f = self.setup_fixture(task_id="P2"); tmp, _, _, config, branch, head, _, owner_store, backend, executor, _ = f
        try:
            project = json.loads(config.read_text(encoding="utf-8"))["projects"][0]
            policy, error = _execution_policy(project)
            self.assertEqual(error, "")
            owner_store.set_paused("p1", True, action_id="pause-final", action="stop")
            launch = executor._launch(
                project, source_request_id="decision-race", source_kind="decision", task_id="P2",
                source_task_id="P1", branch=branch, head=head, worker_prompt=policy["worker_prompt"], policy=policy)
            self.assertIsNone(launch)
            self.assertIn("owner pause", executor.state()["executions"]["decision-race"]["reason"])
            owner_store.set_paused("p1", False, action_id="resume-final", action="approve_next_stage")
            launch = executor._launch(
                project, source_request_id="legacy-race", source_kind="owner_start", task_id="P2",
                source_task_id=None, branch=branch, head=head, worker_prompt=policy["worker_prompt"], policy=policy)
            self.assertIsNone(launch)
            self.assertIn("suppresses legacy", executor.state()["executions"]["legacy-race"]["reason"])
            self.assertEqual(len(backend.started), 0)
        finally: tmp.cleanup()

    def test_pause_barrier_fails_closed_without_consumed_timestamp(self):
        f = self.setup_fixture(task_id="P2"); tmp, _, _, _, _, _, _, owner_store, _, executor, _ = f
        try:
            owner_store.set_paused("p1", True, action_id="pause-1", action="stop")
            owner_store.set_paused("p1", False, action_id="resume-1", action="approve_next_stage")
            self.assertFalse(executor._automatic_decision_allowed("p1", None))
            self.assertFalse(executor._automatic_decision_allowed("p1", "not-a-date"))
            self.assertTrue(executor._automatic_decision_allowed("p1", "2999-01-01T00:00:00+00:00"))
        finally: tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
