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
                    "project": "agy-p1",
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
        self.assertEqual(policy["backends"]["agy"]["project"], "agy-p1")
        self.assertEqual(policy["backends"]["agy"]["model"], "claude-sonnet-4-6")
        self.assertEqual(policy["backends"]["agy"]["print_timeout"], 600)
        self.assertEqual(policy["backends"]["dsh"]["executable"], r"C:\tools\dsh.cmd")
        self.assertEqual(policy["bootstrap"], {"request_id": "owner-p1", "task_id": "P1"})

    def test_unprovisioned_agy_is_policy_valid_but_backend_unavailable(self):
        project = valid_project()
        del project["execution"]["backends"]["agy"]["project"]
        policy, error = _execution_policy(project)
        self.assertEqual(error, "")
        self.assertNotIn("project", policy["backends"]["agy"])
        with tempfile.TemporaryDirectory() as td:
            backend = TransitionExecutor(Path(td))._backend_for_policy(
                "agy", policy["backends"]["agy"]
            )
            status = backend.probe()
            self.assertFalse(status.available)
            self.assertIn("provider project not configured", status.reason)

    def test_unprovisioned_agy_routes_to_other_available_backend(self):
        project = valid_project()
        del project["execution"]["backends"]["agy"]["project"]
        policy, error = _execution_policy(project)
        self.assertEqual(error, "")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            executor = TransitionExecutor(
                root / "runtime",
                backend_overrides={"dsh": FakeBackend("dsh", available=True)},
            )
            router, _ = executor._router_for_policy(policy)
            route = router.route(self._request(root))
            self.assertEqual(route.selected_backend_id, "dsh")

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
        p = valid_project(); p["execution"]["backends"]["agy"]["project"] = "   "
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
        self.assertEqual(seen["agy"]["project"], "agy-p1")
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
        "backends": {"agy": {"project": "agy-p1"}},
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
        "disposition": "apply", "decision": "next", "next_action": "next_task",
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
            thread = executor._threads.get(request_id)
            if thread is not None:
                thread.join(timeout=1.0)
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

    def test_projected_review_identity_still_launches_advertised_ready_next_task(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            head = make_repo(repo, "P2")
            config = base / "projects.json"; write_config(config, repo, bootstrap=False)
            write_decision(runtime, head=head, task_id="P1")
            summary = ready_summary(repo, "P2")
            summary["projects"][0]["state"] = "WAITING_REVIEW"
            summary["projects"][0]["telemetry"]["task_id"] = "P1"
            summary["projects"][0]["next_title"] = "P2 bounded task"
            summary["projects"][0]["next_status"] = "DESIGN READY / EXECUTABLE"
            backend = FakeBackend("agy"); executor = TransitionExecutor(runtime, backend_overrides={"agy": backend})
            launches = executor.advance(summary, config)
            self.assertEqual(len(launches), 1)
            record = wait_terminal(executor, "worker_done:p1:r1")
            self.assertEqual(record["task_id"], "P2")
            self.assertEqual(record["source_task_id"], "P1")
            self.assertEqual(len(backend.started), 1)

    def test_next_pending_design_task_emits_planner_handoff_instead_of_blocking(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo, "P2")
            (repo / "agent" / "next.md").write_text(
                "# P2 bounded task\nStatus: **PENDING DESIGN**\n", encoding="utf-8"
            )
            subprocess.run(["git", "-C", str(repo), "add", "agent/next.md"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "pending P2"], check=True, stdout=subprocess.DEVNULL)
            head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            config = base / "projects.json"; write_config(config, repo, bootstrap=False)
            write_decision(runtime, head=head, task_id="P1")
            summary = ready_summary(repo, "P2"); summary["projects"][0]["state"] = "WAITING_REVIEW"
            summary["projects"][0]["telemetry"]["task_id"] = "P1"
            summary["projects"][0]["next_title"] = "P2 bounded task"
            summary["projects"][0]["next_status"] = "**PENDING DESIGN**"
            backend = FakeBackend("agy"); executor = TransitionExecutor(runtime, backend_overrides={"agy": backend})
            self.assertEqual(executor.advance(summary, config), [])
            record = executor.state()["executions"]["worker_done:p1:r1"]
            self.assertEqual((record["state"], record["outcome"], record["next_task_id"]), ("handoff", "planning_required", "P2"))
            self.assertEqual(len(backend.started), 0)

    def test_reviewed_complete_task_settles_without_starting_phantom_next_worker(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo, "P1")
            (repo / "agent" / "next.md").write_text(
                "# P1 bounded task\nStatus: **COMPLETE**\n", encoding="utf-8"
            )
            subprocess.run(["git", "-C", str(repo), "add", "agent/next.md"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "complete P1"], check=True, stdout=subprocess.DEVNULL)
            head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            config = base / "projects.json"; write_config(config, repo, bootstrap=False)
            write_decision(runtime, head=head, task_id="P1")
            summary = ready_summary(repo, "P1"); summary["projects"][0]["state"] = "IDLE"
            summary["projects"][0]["next_status"] = "**COMPLETE**"
            backend = FakeBackend("agy"); executor = TransitionExecutor(runtime, backend_overrides={"agy": backend})
            self.assertEqual(executor.advance(summary, config), [])
            record = executor.state()["executions"]["worker_done:p1:r1"]
            self.assertEqual(record["state"], "settled")
            self.assertEqual(record["outcome"], "task_complete")
            self.assertEqual(len(backend.started), 0)
            self.assertEqual(executor.advance(summary, config), [])

    def test_legacy_not_ready_block_reconciles_to_settled_when_complete_truth_matches(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td); repo=base/"repo"; runtime=base/"runtime"
            make_repo(repo,"P1")
            (repo/"agent"/"next.md").write_text("# P1 bounded task\nStatus: **COMPLETE**\n",encoding="utf-8")
            subprocess.run(["git","-C",str(repo),"add","agent/next.md"],check=True)
            subprocess.run(["git","-C",str(repo),"commit","-m","complete P1"],check=True,stdout=subprocess.DEVNULL)
            head=subprocess.check_output(["git","-C",str(repo),"rev-parse","HEAD"],text=True).strip()
            config=base/"projects.json"; write_config(config,repo,bootstrap=False); write_decision(runtime,head=head,task_id="P1")
            (runtime/"transition-executor.json").write_text(json.dumps({"version":1,"executions":{"worker_done:p1:r1":{"project_id":"p1","source_request_id":"worker_done:p1:r1","source_kind":"decision","task_id":"P1","state":"blocked","reason":"project is not READY_TO_RUN"}}}),encoding="utf-8")
            summary=ready_summary(repo,"P1"); summary["projects"][0]["state"]="IDLE"; summary["projects"][0]["next_status"]="**COMPLETE**"
            executor=TransitionExecutor(runtime,backend_overrides={"agy":FakeBackend("agy")}); self.assertEqual(executor.advance(summary,config),[])
            row=executor.state()["executions"]["worker_done:p1:r1"]; self.assertEqual(row["state"],"settled"); self.assertIn("legacy_reconciled_from",row)

    def test_owner_start_launches_current_task_once_after_prior_history(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo, "P2")
            config = base / "projects.json"; write_config(config, repo, bootstrap=False)
            data = json.loads(config.read_text(encoding="utf-8")); data["projects"][0]["execution"]["owner_start"] = {"request_id":"owner-start-p2","task_id":"P2"}; config.write_text(json.dumps(data), encoding="utf-8")
            runtime.mkdir(); (runtime / "transition-executor.json").write_text(json.dumps({"version":1,"executions":{"old":{"project_id":"p1","source_request_id":"old","source_kind":"decision","task_id":"P1","state":"completed"}}}), encoding="utf-8")
            backend = FakeBackend("agy"); executor = TransitionExecutor(runtime, backend_overrides={"agy": backend})
            launches = executor.advance(ready_summary(repo, "P2"), config)
            self.assertEqual(len(launches), 1)
            record = wait_terminal(executor, "owner-start-p2")
            self.assertEqual((record["state"], record["source_kind"], record["task_id"]), ("completed", "owner_start", "P2"))
            self.assertEqual(len(backend.started), 1)
            self.assertEqual(executor.advance(ready_summary(repo, "P2"), config), [])

    def test_owner_start_still_uses_raw_monitor_gate_when_decisions_use_projected_truth(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo, "P2")
            config = base / "projects.json"; write_config(config, repo, bootstrap=False)
            data = json.loads(config.read_text(encoding="utf-8"))
            data["projects"][0]["execution"]["owner_start"] = {
                "request_id": "owner-start-p2", "task_id": "P2"
            }
            config.write_text(json.dumps(data), encoding="utf-8")
            backend = FakeBackend("agy")
            executor = TransitionExecutor(runtime, backend_overrides={"agy": backend})
            raw = ready_summary(repo, "P2")
            raw["projects"][0]["state"] = "WAITING_PHASE_GATE"
            projected = ready_summary(repo, "P2")

            self.assertEqual(
                executor.advance(raw, config, decision_summary=projected), []
            )
            record = executor.state()["executions"]["owner-start-p2"]
            self.assertEqual(record["state"], "blocked")
            self.assertIn("READY_TO_RUN", record["reason"])
            self.assertEqual(len(backend.started), 0)

    def test_owner_start_task_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo, "P2"); config = base / "projects.json"; write_config(config, repo, bootstrap=False)
            data = json.loads(config.read_text(encoding="utf-8")); data["projects"][0]["execution"]["owner_start"] = {"request_id":"owner-start-p3","task_id":"P3"}; config.write_text(json.dumps(data), encoding="utf-8")
            backend = FakeBackend("agy"); executor = TransitionExecutor(runtime, backend_overrides={"agy": backend})
            self.assertEqual(executor.advance(ready_summary(repo, "P2"), config), [])
            record = executor.state()["executions"]["owner-start-p3"]
            self.assertEqual(record["state"], "blocked"); self.assertIn("task id", record["reason"]); self.assertEqual(len(backend.started), 0)

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

    def test_project_context_injected_into_legacy_worker_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo, "P2")
            ctx_payload = {
                "schema_version": 1,
                "project_id": "p1",
                "goals": ["Ship worker context"],
                "architecture": ["Core modules"],
                "protected_scope": ["Production infra"],
                "safety_constraints": ["Fail closed"],
                "validation_commands": ["python -m unittest"],
                "runtime_assumptions": ["Python 3.11"],
                "key_decisions": ["D1 schema"],
            }
            (repo / "agent" / "project-context.json").write_text(json.dumps(ctx_payload), encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "ctx"], check=True)

            config = base / "projects.json"; write_config(config, repo, bootstrap=False)
            data = json.loads(config.read_text(encoding="utf-8"))
            data["projects"][0]["execution"]["owner_start"] = {"request_id": "owner-start-p2", "task_id": "P2"}
            data["projects"][0]["project_context"] = {
                "enabled": True,
                "document_path": "agent/project-context.json",
                "require_valid": True,
            }
            config.write_text(json.dumps(data), encoding="utf-8")
            runtime.mkdir()
            backend = FakeBackend("agy")
            executor = TransitionExecutor(runtime, backend_overrides={"agy": backend})
            launches = executor.advance(ready_summary(repo, "P2"), config)
            self.assertEqual(len(launches), 1)
            record = wait_terminal(executor, "owner-start-p2")
            self.assertEqual(record["state"], "completed")
            self.assertEqual(record["context_state"], "ready")
            self.assertIsNotNone(record["context_digest"])
            self.assertEqual(len(backend.started), 1)
            request = backend.started[0]
            self.assertIn("[PROJECT_CONTEXT_BEGIN]", request.prompt)
            self.assertIn("## goals\n- Ship worker context", request.prompt)
            self.assertIn("[PROJECT_CONTEXT_END]", request.prompt)

    def test_invalid_project_context_blocks_worker_launch(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo, "P2")
            (repo / "agent" / "project-context.json").write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "invalid-ctx"], check=True)

            config = base / "projects.json"; write_config(config, repo, bootstrap=False)
            data = json.loads(config.read_text(encoding="utf-8"))
            data["projects"][0]["execution"]["owner_start"] = {"request_id": "owner-start-p2", "task_id": "P2"}
            data["projects"][0]["project_context"] = {
                "enabled": True,
                "document_path": "agent/project-context.json",
                "require_valid": True,
            }
            config.write_text(json.dumps(data), encoding="utf-8")
            runtime.mkdir()
            backend = FakeBackend("agy")
            executor = TransitionExecutor(runtime, backend_overrides={"agy": backend})
            launches = executor.advance(ready_summary(repo, "P2"), config)
            self.assertEqual(len(launches), 0)
            record = executor.state()["executions"]["owner-start-p2"]
            self.assertEqual(record["state"], "blocked")
            self.assertIn("durable project context is invalid", record["reason"])
            self.assertEqual(len(backend.started), 0)

    def test_absent_project_context_leaves_worker_prompt_byte_identical(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo, "P2")
            config = base / "projects.json"; write_config(config, repo, bootstrap=False)
            data = json.loads(config.read_text(encoding="utf-8"))
            data["projects"][0]["execution"]["owner_start"] = {"request_id": "owner-start-p2", "task_id": "P2"}
            config.write_text(json.dumps(data), encoding="utf-8")
            runtime.mkdir()
            backend = FakeBackend("agy")
            executor = TransitionExecutor(runtime, backend_overrides={"agy": backend})
            launches = executor.advance(ready_summary(repo, "P2"), config)
            self.assertEqual(len(launches), 1)
            record = wait_terminal(executor, "owner-start-p2")
            self.assertEqual(record["state"], "completed")
            self.assertNotIn("[PROJECT_CONTEXT_BEGIN]", backend.started[0].prompt)


if __name__ == "__main__":
    unittest.main()
