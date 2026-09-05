import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from dev_orchestrator.agents.base import AgentBackend
from dev_orchestrator.agents.models import (
    AgentRequest,
    AgentResult,
    AgentRole,
    AgentRun,
    AgentRunState,
    BackendStatus,
    CapabilitySet,
    QuotaState,
)
from dev_orchestrator.core.transition_executor import (
    TransitionExecutor,
    _execution_policy,
)


class FakeBackend(AgentBackend):
    def __init__(
        self, backend_id: str, *, available: bool = True,
        terminal_state: AgentRunState = AgentRunState.COMPLETED, exit_code: int = 0,
    ) -> None:
        self._backend_id = backend_id
        self.available = available
        self.terminal_state = terminal_state
        self.exit_code = exit_code
        self.started: list[AgentRequest] = []
        self._counter = 0

    @property
    def backend_id(self) -> str:
        return self._backend_id
    def capabilities(self) -> CapabilitySet:
        return CapabilitySet(
            roles=frozenset({AgentRole.WORKER}),
            tags=frozenset({"code", "repository"}),
            provider=self.backend_id,
            independence_domain=self.backend_id,
            cancellation_supported=True,
        )

    def probe(self) -> BackendStatus:
        return BackendStatus(
            self.backend_id,
            self.available,
            "" if self.available else "offline",
            QuotaState.UNKNOWN,
        )

    async def start(self, request: AgentRequest) -> AgentRun:
        self.started.append(request)
        self._counter += 1
        run_id = "{0}-run-{1}".format(self.backend_id, self._counter)
        return AgentRun(run_id, self.backend_id, AgentRunState.RUNNING, None, 4242, request.project_id)

    async def status(self, run_id: str) -> AgentRun:
        return AgentRun(run_id, self.backend_id, self.terminal_state, self.exit_code)

    async def cancel(self, run_id: str) -> AgentRun:
        return AgentRun(run_id, self.backend_id, AgentRunState.CANCELLED, -1)

    async def collect(self, run_id: str) -> AgentResult:
        return AgentResult(run_id, self.backend_id, self.terminal_state, self.exit_code)


def valid_project() -> dict:
    return {
        "execution": {
            "enabled": True,
            "owner_authorized": True,
            "allowed_next_actions": ["next_task"],
            "preferred_backends": ["agy", "dsh"],
            "backends": {
                "agy": {
                    "executable": r"C:\tools\agy.exe",
                    "model": "claude-sonnet-4-6",
                    "effort": "high",
                    "mode": "accept-edits",
                    "print_timeout": 600,
                },
                "dsh": {"executable": r"C:\tools\dsh.cmd"},
            },
            "bootstrap": {"request_id": "owner-p1", "task_id": "P1"},
        }
    }


class TransitionExecutorPolicyTests(unittest.TestCase):
    def test_provider_neutral_policy_normalizes_agy_and_dsh(self):
        policy, error = _execution_policy(valid_project())
        self.assertEqual(error, "")
        self.assertEqual(policy["preferred_backends"], ("agy", "dsh"))
        self.assertEqual(policy["backends"]["agy"]["model"], "claude-sonnet-4-6")
        self.assertEqual(policy["backends"]["agy"]["print_timeout"], 600)
        self.assertEqual(policy["backends"]["dsh"]["executable"], r"C:\tools\dsh.cmd")
        self.assertEqual(policy["bootstrap"], {"request_id": "owner-p1", "task_id": "P1"})

    def test_malformed_authority_or_backend_policy_fails_closed(self):
        cases = []
        p = valid_project(); p["execution"]["allowed_next_actions"] = ["next_task", "next_stage"]
        cases.append(p)
        p = valid_project(); p["execution"]["preferred_backends"] = []
        cases.append(p)
        p = valid_project(); p["execution"]["preferred_backends"] = ["agy", "agy"]
        cases.append(p)
        p = valid_project(); p["execution"]["preferred_backends"] = ["unknown"]
        cases.append(p)
        p = valid_project(); p["execution"]["backends"]["agy"]["print_timeout"] = 0
        cases.append(p)
        p = valid_project(); p["execution"]["owner_authorized"] = False
        cases.append(p)
        for project in cases:
            with self.subTest(project=project):
                policy, error = _execution_policy(project)
                self.assertIsNone(policy)
                self.assertTrue(error)
    def _request(self, root: Path, preferred=("agy", "dsh")) -> AgentRequest:
        return AgentRequest(
            project_id="p1",
            role=AgentRole.WORKER,
            prompt="do one task",
            working_directory=root,
            required_capabilities=frozenset({"code", "repository"}),
            preferred_backends=preferred,
        )

    def test_router_prefers_agy_then_falls_back_to_dsh(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            policy, _ = _execution_policy(valid_project())
            executor = TransitionExecutor(
                root / "runtime",
                backend_overrides={
                    "agy": FakeBackend("agy", available=True),
                    "dsh": FakeBackend("dsh", available=True),
                },
            )
            router, _ = executor._router_for_policy(policy)
            self.assertEqual(router.route(self._request(root)).selected_backend_id, "agy")
            executor = TransitionExecutor(
                root / "runtime2",
                backend_overrides={
                    "agy": FakeBackend("agy", available=False),
                    "dsh": FakeBackend("dsh", available=True),
                },
            )
            router, _ = executor._router_for_policy(policy)
            self.assertEqual(router.route(self._request(root)).selected_backend_id, "dsh")

    def test_production_backend_options_are_forwarded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            policy, _ = _execution_policy(valid_project())
            seen = {}

            def make_agy(**kwargs):
                seen["agy"] = kwargs
                return FakeBackend("agy")

            def make_dsh(**kwargs):
                seen["dsh"] = kwargs
                return FakeBackend("dsh")

            executor = TransitionExecutor(root / "runtime")
            with patch("dev_orchestrator.core.transition_executor.AgyBackend", side_effect=make_agy), \
                 patch("dev_orchestrator.core.transition_executor.DshBackend", side_effect=make_dsh):
                executor._router_for_policy(policy)
        self.assertEqual(seen["agy"]["command_prefix"], (r"C:\tools\agy.exe",))
        self.assertEqual(seen["agy"]["model"], "claude-sonnet-4-6")
        self.assertEqual(seen["agy"]["effort"], "high")
        self.assertEqual(seen["agy"]["mode"], "accept-edits")
        self.assertEqual(seen["agy"]["print_timeout"], 600)
        self.assertEqual(seen["dsh"]["command_prefix"], (r"C:\tools\dsh.cmd",))


def make_repo(root: Path, task_id: str = "P1") -> str:
    root.mkdir(parents=True)
    (root / "agent").mkdir()
    (root / "agent" / "CURRENT.md").write_text("# current\n", encoding="utf-8")
    (root / "agent" / "next.md").write_text(
        "# {0} bounded task\nStatus: DESIGN READY / EXECUTABLE\n".format(task_id),
        encoding="utf-8",
    )
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "init"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "fixture"], check=True, stdout=subprocess.DEVNULL)
    return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()


def ready_summary(repo: Path, task_id: str) -> dict:
    return {"projects": [{
        "project_id": "p1", "repo_path": str(repo), "state": "READY_TO_RUN",
        "worker": {"kind": "none", "state": "not_started", "process_alive": False},
        "telemetry": {"task_id": task_id}, "next_title": "{0} bounded task".format(task_id),
    }]}


def write_config(path: Path, repo: Path, *, bootstrap: bool) -> None:
    execution = {
        "enabled": True,
        "owner_authorized": True,
        "allowed_next_actions": ["next_task"],
        "preferred_backends": ["agy"],
        "backends": {"agy": {}},
    }
    if bootstrap:
        execution["bootstrap"] = {"request_id": "owner-p1", "task_id": "P1"}
    path.write_text(json.dumps({"projects": [{
        "project_id": "p1", "repo_path": str(repo), "execution": execution,
    }]}), encoding="utf-8")


def write_decision(runtime: Path, *, head: str, task_id: str = "P1", request_id: str = "worker_done:p1:r1") -> None:
    runtime.mkdir(parents=True, exist_ok=True)
    record = {
        "project_id": "p1", "request_id": request_id,
        "disposition": "apply", "next_action": "next_task",
        "task_id": task_id, "stage_id": None,
        "branch": "master", "head": head,
        "role": "reviewer", "event": "worker_done",
        "consumed_at": "2026-09-05T00:00:00+00:00",
    }
    (runtime / "websol-decisions.json").write_text(
        json.dumps({"version": 1, "decisions": {request_id: record}}), encoding="utf-8"
    )


def wait_terminal(executor: TransitionExecutor, request_id: str) -> dict:
    deadline = time.time() + 3
    while time.time() < deadline:
        record = executor.state()["executions"].get(request_id)
        if isinstance(record, dict) and record.get("state") in {
            "completed", "failed", "cancelled", "blocked", "recovery_required"
        }:
            return record
        time.sleep(0.02)
    raise AssertionError("execution did not reach terminal state")


class TransitionExecutorActuationTests(unittest.TestCase):
    def test_bootstrap_launches_once(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo, "P1")
            config = base / "projects.json"; write_config(config, repo, bootstrap=True)
            backend = FakeBackend("agy")
            executor = TransitionExecutor(runtime, backend_overrides={"agy": backend})
            launches = executor.advance(ready_summary(repo, "P1"), config)
            self.assertEqual(len(launches), 1)
            record = wait_terminal(executor, "owner-p1")
            self.assertEqual(record["state"], "completed")
            self.assertEqual(record["task_id"], "P1")
            self.assertEqual(len(backend.started), 1)
            self.assertEqual(executor.advance(ready_summary(repo, "P1"), config), [])
            self.assertEqual(len(backend.started), 1)
    def test_durable_decision_launches_advanced_task_once(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            head = make_repo(repo, "P2")
            config = base / "projects.json"; write_config(config, repo, bootstrap=False)
            write_decision(runtime, head=head, task_id="P1")
            backend = FakeBackend("agy")
            executor = TransitionExecutor(runtime, backend_overrides={"agy": backend})
            launches = executor.advance(ready_summary(repo, "P2"), config)
            self.assertEqual(len(launches), 1)
            record = wait_terminal(executor, "worker_done:p1:r1")
            self.assertEqual(record["task_id"], "P2")
            self.assertEqual(record["source_task_id"], "P1")
            self.assertEqual(len(backend.started), 1)
            self.assertEqual(executor.advance(ready_summary(repo, "P2"), config), [])
            self.assertEqual(len(backend.started), 1)

    def test_same_task_stale_and_dirty_are_blocked(self):
        cases = ("same_task", "stale", "dirty")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as td:
                base = Path(td); repo = base / "repo"; runtime = base / "runtime"
                current_task = "P1" if case == "same_task" else "P2"
                head = make_repo(repo, current_task)
                config = base / "projects.json"; write_config(config, repo, bootstrap=False)
                decision_head = ("0" * 40) if case == "stale" else head
                write_decision(runtime, head=decision_head, task_id="P1")
                if case == "dirty":
                    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
                backend = FakeBackend("agy")
                executor = TransitionExecutor(runtime, backend_overrides={"agy": backend})
                launches = executor.advance(ready_summary(repo, current_task), config)
                self.assertEqual(launches, [])
                record = executor.state()["executions"]["worker_done:p1:r1"]
                self.assertEqual(record["state"], "blocked")
                self.assertTrue(record["reason"])
                self.assertEqual(len(backend.started), 0)

    def test_restart_marks_active_execution_recovery_required(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td) / "runtime"; runtime.mkdir()
            path = runtime / "transition-executor.json"
            path.write_text(json.dumps({
                "version": 1,
                "executions": {"owner-p1": {
                    "project_id": "p1", "source_request_id": "owner-p1",
                    "source_kind": "bootstrap", "task_id": "P1", "state": "running",
                }},
            }), encoding="utf-8")
            executor = TransitionExecutor(runtime)
            record = executor.state()["executions"]["owner-p1"]
            self.assertEqual(record["state"], "recovery_required")
            self.assertIn("automatic replay is forbidden", record["reason"])


if __name__ == "__main__":
    unittest.main()
