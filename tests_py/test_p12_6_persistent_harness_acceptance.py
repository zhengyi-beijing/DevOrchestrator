"""P12.6 Persistent Harness Acceptance Suite.

Verifies the documented AIBroker persistent service transport, lifecycle neutrality,
pause barrier behavior, capability-qualified interruption, restart reconciliation,
and CLI compatibility path using an ephemeral loopback HTTP server fixture.
"""

from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any
import unittest
from urllib.parse import unquote, urlsplit

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dev_orchestrator.ai import (
    AIBrokerClientConfig,
    AIBrokerExecutionPort,
    AIBrokerInvocationError,
    AIRoleRequest,
    ResourceContext,
)
from dev_orchestrator.ai.execution_port import MANAGED_INTERRUPT_REASON
from dev_orchestrator.ai.runtime_config import load_aibroker_execution_port
from dev_orchestrator.control.owner_store import OwnerControlStore
from dev_orchestrator.control.surface import project_identity
from dev_orchestrator.core.control_commands import ControlCommandCoordinator, submit_control_command
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.core.transition_executor import TransitionExecutor
from tests_py.test_control_commands import write_config


class EphemeralBrokerServer:
    """Ephemeral loopback HTTP server simulating the persistent AIBroker service."""

    def __init__(self, expected_token: str = "test-broker-token"):
        self.expected_token = expected_token
        self.dispatches: dict[str, dict[str, Any]] = {}
        self.interrupt_responses: dict[str, dict[str, Any]] = {}
        self.recorded_requests: list[dict[str, Any]] = []
        self.route_overrides: dict[str, Any] = {}
        self.dispatch_delay_seconds: float = 0.0
        self.dispatch_gate: threading.Event | None = None
        outer = self

        class BrokerHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass  # Suppress stderr logging

            def _read_body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", 0))
                if length == 0:
                    return {}
                raw = self.rfile.read(length).decode("utf-8")
                try:
                    return json.loads(raw)
                except Exception:
                    return {"_raw": raw}

            def _send_json(self, status: int, data: Any):
                body = json.dumps(data).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parsed = urlsplit(self.path)
                path = parsed.path
                token = self.headers.get("X-AIResourceBroker-Token")
                outer.recorded_requests.append({
                    "method": "GET",
                    "path": path,
                    "token": token,
                    "headers": dict(self.headers),
                })
                if path in outer.route_overrides:
                    status, data = outer.route_overrides[path]
                    self._send_json(status, data)
                    return

                if path.startswith("/api/dispatches/"):
                    req_id = unquote(path.removeprefix("/api/dispatches/"))
                    if req_id in outer.dispatches:
                        self._send_json(200, outer.dispatches[req_id])
                    else:
                        self._send_json(404, {"request_id": req_id, "status": "not_found"})
                    return

                self._send_json(404, {"error": "not found"})

            def do_POST(self):
                parsed = urlsplit(self.path)
                path = parsed.path
                token = self.headers.get("X-AIResourceBroker-Token")
                body = self._read_body()
                outer.recorded_requests.append({
                    "method": "POST",
                    "path": path,
                    "token": token,
                    "headers": dict(self.headers),
                    "body": body,
                })

                if outer.expected_token and token != outer.expected_token:
                    self._send_json(403, {"error": "invalid control token"})
                    return

                if path in outer.route_overrides:
                    status, data = outer.route_overrides[path]
                    self._send_json(status, data)
                    return

                if path == "/api/dispatch":
                    if outer.dispatch_gate is not None:
                        outer.dispatch_gate.wait(timeout=10.0)
                    if outer.dispatch_delay_seconds > 0:
                        time.sleep(outer.dispatch_delay_seconds)
                    req_id = body.get("request_id", "req-1")
                    result = {
                        "dispatch_id": f"disp-{req_id}",
                        "request_id": req_id,
                        "decision_id": f"dec-{req_id}",
                        "execution_id": f"exec-{req_id}",
                        "session_id": f"sess-{req_id}",
                        "status": "succeeded",
                        "output": "PERSISTENT_BROKER_WORKER_OK",
                        "error": None,
                        "resource_context": {
                            "resource_id": "deepseek/default/deepseek-v4",
                            "provider": "deepseek",
                            "account": "default",
                            "model": "deepseek-v4",
                        },
                        "started_at": "2026-09-17T01:00:00+00:00",
                        "finished_at": "2026-09-17T01:05:00+00:00",
                    }
                    outer.dispatches[req_id] = result
                    self._send_json(200, result)
                    return

                if path.startswith("/api/dispatches/") and path.endswith("/interrupt"):
                    req_id = unquote(path[len("/api/dispatches/") : -len("/interrupt")].rstrip("/"))
                    if req_id in outer.interrupt_responses:
                        self._send_json(200, outer.interrupt_responses[req_id])
                        return
                    if req_id in outer.dispatches:
                        record = dict(outer.dispatches[req_id])
                        record["status"] = "failed"
                        record["interrupt_supported"] = True
                        record["execution_error"] = body.get("reason", "interrupted")
                        outer.dispatches[req_id] = record
                        self._send_json(200, record)
                        return
                    self._send_json(404, {"request_id": req_id, "status": "not_found"})
                    return

                self._send_json(404, {"error": "not found"})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), BrokerHandler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=2.0)


class P12_6_PersistentHarnessAcceptanceTests(unittest.TestCase):
    """P12.6 Persistent Harness Acceptance and Closure Test Suite."""

    def setUp(self):
        self.server = EphemeralBrokerServer(expected_token="secret-p12-6-token")
        self.root = Path(self._create_temp_dir())
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.repo = self._init_repo(self.root / "repo")
        self.config_file = self.root / "projects.json"
        self.config_file.write_text(json.dumps({"projects": [self._project_spec()]}), encoding="utf-8")
        self._write_broker_config(self.server.url, "secret-p12-6-token")

    def tearDown(self):
        self.server.shutdown()

    def _create_temp_dir(self) -> str:
        import tempfile
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return td.name

    def _init_repo(self, repo_dir: Path) -> Path:
        repo_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "p12_6@example.com"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "user.name", "P12_6 Acceptance"], cwd=repo_dir, check=True)
        (repo_dir / "README.md").write_text("# Repo\n", encoding="utf-8")
        (repo_dir / "agent").mkdir(parents=True, exist_ok=True)
        (repo_dir / "agent" / "next.md").write_text("# P12.6\nStatus: **READY_TO_RUN**\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, check=True, capture_output=True)
        return repo_dir

    def _write_broker_config(self, service_url: str | None, service_token: str | None):
        cfg: dict[str, Any] = {
            "python_executable": sys.executable,
            "broker_repo": str(self.root / "broker_dummy"),
            "config_path": str(self.root / "resources_dummy.yaml"),
            "process_timeout_seconds": 600.0,
            "probe_before_dispatch": True,
        }
        if service_url is not None:
            cfg["service_url"] = service_url
        if service_token is not None:
            cfg["service_token"] = service_token
        (self.runtime / "aibroker-execution.json").write_text(json.dumps(cfg), encoding="utf-8")

    def _project_spec(self) -> dict[str, Any]:
        return {
            "project_id": "devorchestrator",
            "repo_path": str(self.repo),
            "execution": {
                "enabled": True,
                "owner_authorized": True,
                "engine": "aibroker",
                "worker_quality": "high",
                "allowed_next_actions": ["next_task"],
            },
        }

    # =========================================================================
    # Acceptance Criterion 1 & 2: Transport & Correlation Evidence
    # =========================================================================

    def test_persistent_transport_e2e_dispatch_status_interrupt(self):
        """Exercises authenticated dispatch, status, and interrupt via ephemeral loopback."""
        port = load_aibroker_execution_port(self.runtime)
        self.assertIsNotNone(port)

        # 1. Execute dispatch
        request = AIRoleRequest(
            project_id="devorchestrator",
            role="worker",
            prompt="Implement P12.6 acceptance",
            working_directory=self.repo,
            request_id="req-p12-6-test",
            task_run_id="P12.6",
            stage_run_id="stage-1",
            role_run_id="role-1",
            timeout_seconds=1800.0,
            metadata={"managed_worktree": True},
            previous_resource_context=ResourceContext("prev-res", "deepseek", "default", "v3"),
        )
        result = port.execute(request)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.output, "PERSISTENT_BROKER_WORKER_OK")
        self.assertEqual(result.dispatch_id, "disp-req-p12-6-test")
        self.assertEqual(result.execution_id, "exec-req-p12-6-test")
        self.assertEqual(result.session_id, "sess-req-p12-6-test")
        self.assertIsNotNone(result.resource_context)
        self.assertEqual(result.resource_context.provider, "deepseek")

        # Verify server observed exact headers and body
        dispatch_reqs = [r for r in self.server.recorded_requests if r["path"] == "/api/dispatch"]
        self.assertEqual(len(dispatch_reqs), 1)
        dis_req = dispatch_reqs[0]
        self.assertEqual(dis_req["method"], "POST")
        self.assertEqual(dis_req["token"], "secret-p12-6-token")
        self.assertEqual(dis_req["body"]["project_id"], "devorchestrator")
        self.assertEqual(dis_req["body"]["role"], "worker")
        self.assertEqual(dis_req["body"]["task_id"], "P12.6")
        self.assertTrue(dis_req["body"]["managed_worktree"])
        self.assertEqual(dis_req["body"]["timeout_seconds"], 1800.0)
        self.assertEqual(dis_req["body"]["previous_resource_context"]["resource_id"], "prev-res")

        # 2. Status query
        status_res = port.status("req-p12-6-test")
        self.assertIsNotNone(status_res)
        self.assertEqual(status_res["status"], "succeeded")

        # Status 404 for unknown request returns None
        self.assertIsNone(port.status("nonexistent-req"))

        # 3. Interrupt query
        interrupt_res = port.interrupt("req-p12-6-test", "explicit test interrupt")
        self.assertIsNotNone(interrupt_res)
        self.assertEqual(interrupt_res["status"], "failed")
        self.assertTrue(interrupt_res["interrupt_supported"])

        # Interrupt 404 for unknown request returns None
        self.assertIsNone(port.interrupt("nonexistent-req", "reason"))

    def test_one_execute_call_produces_exactly_one_dispatch_and_one_ledger_record(self):
        """Assert one DevOrchestrator execute call produces exactly one Broker dispatch and one execution-ledger record."""
        port = load_aibroker_execution_port(self.runtime)
        executor = TransitionExecutor(self.runtime, ai_execution_port=port)
        truth = read_repository_truth(self.repo)

        snapshot = {
            "project_id": "devorchestrator",
            "state": "READY_TO_RUN",
            "telemetry": {"task_id": "P12.6"},
            "worker": {"state": "not_started", "process_alive": False},
        }
        launch = executor.start_control(self._project_spec(), snapshot, "launch-exact-1")
        self.assertIsNotNone(launch)
        executor._threads["launch-exact-1"].join(timeout=5.0)

        # Assert exactly one Broker dispatch was made
        dispatch_reqs = [r for r in self.server.recorded_requests if r["path"] == "/api/dispatch"]
        self.assertEqual(len(dispatch_reqs), 1)

        # Assert exactly one execution record exists in ledger
        state = executor.state()
        executions = state.get("executions", {})
        self.assertEqual(len(executions), 1)
        record = executions["launch-exact-1"]
        self.assertEqual(record["state"], "completed")
        self.assertEqual(record["dispatch_id"], "disp-ai-worker:launch-exact-1")
        self.assertEqual(record["execution_id"], "exec-ai-worker:launch-exact-1")
        self.assertEqual(record["session_id"], "sess-ai-worker:launch-exact-1")
        self.assertEqual(record["resource_context"]["provider"], "deepseek")

    # =========================================================================
    # Acceptance Criterion 2: Lifecycle Neutrality (Broker is NOT a controller)
    # =========================================================================

    def test_lifecycle_neutrality_broker_output_never_advances_or_mutates_state(self):
        """Exercise TransitionExecutor with lifecycle-looking Broker output and prove it is recorded ONLY as evidence."""
        # Broker returns output that mimics lifecycle decisions
        lifecycle_payload = {
            "dispatch_id": "disp-neutral-1",
            "request_id": "ai-worker:launch-lifecycle-1",
            "decision_id": "dec-neutral-1",
            "execution_id": "exec-neutral-1",
            "session_id": "sess-neutral-1",
            "status": "succeeded",
            "output": json.dumps({
                "decision": "NEXT",
                "next_action": "TASK_COMPLETE",
                "remediation": False,
                "owner_gate": "approved",
                "stage_transition": "settled",
            }),
            "error": None,
            "resource_context": {"resource_id": "r1", "provider": "deepseek", "account": "a", "model": "m"},
        }
        self.server.route_overrides["/api/dispatch"] = (200, lifecycle_payload)

        port = load_aibroker_execution_port(self.runtime)
        executor = TransitionExecutor(self.runtime, ai_execution_port=port)
        snapshot = {
            "project_id": "devorchestrator",
            "state": "READY_TO_RUN",
            "telemetry": {"task_id": "P12.6"},
            "worker": {"state": "not_started", "process_alive": False},
        }

        # Snapshot before launch
        before_truth = read_repository_truth(self.repo)
        before_next_md = (self.repo / "agent" / "next.md").read_text(encoding="utf-8")

        launch = executor.start_control(self._project_spec(), snapshot, "launch-lifecycle-1")
        self.assertIsNotNone(launch)
        executor._threads["launch-lifecycle-1"].join(timeout=5.0)

        # 1. State in executor is 'completed' (worker execution ended), NOT advanced to next task
        record = executor.state()["executions"]["launch-lifecycle-1"]
        self.assertEqual(record["state"], "completed")
        self.assertEqual(record["task_id"], "P12.6")

        # 2. Output is recorded strictly as evidence, never parsed as lifecycle commands
        self.assertEqual(record["broker_status"], "succeeded")

        # 3. Repository state and agent/next.md are completely untouched
        after_truth = read_repository_truth(self.repo)
        after_next_md = (self.repo / "agent" / "next.md").read_text(encoding="utf-8")
        self.assertEqual(before_truth.head, after_truth.head)
        self.assertEqual(before_next_md, after_next_md)

        # 4. Review decisions store is not created or mutated by worker output
        self.assertFalse((self.runtime / "review-decisions.json").exists())

    # =========================================================================
    # Acceptance Criterion 3: Pause & Stop Barriers
    # =========================================================================

    def test_pause_blocks_launch_before_execute(self):
        """Pause blocks every later launch before AIBrokerExecutionPort.execute."""
        owner_store = OwnerControlStore(self.runtime)
        owner_store.set_paused("devorchestrator", True, command_id="p-1", action="pause", reason="test pause")

        port = load_aibroker_execution_port(self.runtime)
        executor = TransitionExecutor(self.runtime, ai_execution_port=port, owner_store=owner_store)
        snapshot = {
            "project_id": "devorchestrator",
            "state": "READY_TO_RUN",
            "telemetry": {"task_id": "P12.6"},
            "worker": {"state": "not_started", "process_alive": False},
        }

        launch = executor.start_control(self._project_spec(), snapshot, "launch-paused-1")
        self.assertIsNone(launch)

        # Ledger records blocked reason
        record = executor.state()["executions"]["launch-paused-1"]
        self.assertEqual(record["state"], "blocked")
        self.assertIn("owner pause blocked Worker launch", record["reason"])

        # Broker was never invoked
        dispatch_reqs = [r for r in self.server.recorded_requests if r["path"] == "/api/dispatch"]
        self.assertEqual(len(dispatch_reqs), 0)

    def test_pause_during_active_execution_does_not_cancel_active_harness(self):
        """Pause does not claim to cancel an already active harness."""
        gate = threading.Event()
        self.server.dispatch_gate = gate

        port = load_aibroker_execution_port(self.runtime)
        owner_store = OwnerControlStore(self.runtime)
        executor = TransitionExecutor(self.runtime, ai_execution_port=port, owner_store=owner_store)
        snapshot = {
            "project_id": "devorchestrator",
            "state": "READY_TO_RUN",
            "telemetry": {"task_id": "P12.6"},
            "worker": {"state": "not_started", "process_alive": False},
        }

        launch = executor.start_control(self._project_spec(), snapshot, "launch-active-1")
        self.assertIsNotNone(launch)

        # Wait until dispatch request is received by server
        for _ in range(50):
            if any(r["path"] == "/api/dispatch" for r in self.server.recorded_requests):
                break
            time.sleep(0.05)

        # Now owner pauses the project
        owner_store.set_paused("devorchestrator", True, command_id="p-during-run", action="pause")
        self.assertTrue(owner_store.is_paused("devorchestrator"))

        # Confirm no interrupt was sent to the server for pause
        interrupt_reqs = [r for r in self.server.recorded_requests if "/interrupt" in r["path"]]
        self.assertEqual(len(interrupt_reqs), 0)

        # Release the dispatch gate so it can finish
        gate.set()
        executor._threads["launch-active-1"].join(timeout=5.0)

        record = executor.state()["executions"]["launch-active-1"]
        self.assertEqual(record["state"], "completed")

    def test_stop_exact_correlated_interrupt_success(self):
        """Stop with confirmed correlated interrupt evidence succeeds with pause_and_interrupt."""
        truth = read_repository_truth(self.repo)
        snapshot = {
            "project_id": "devorchestrator",
            "state": "READY_TO_RUN",
            "lifecycle_state": "READY_TO_RUN",
            "next_status": "**READY_TO_RUN**",
            "git": {"branch": truth.branch, "head": truth.head},
            "telemetry": {"task_id": "P12.6"},
        }

        # Seed active running execution in executor
        executor = TransitionExecutor(self.runtime, ai_execution_port=load_aibroker_execution_port(self.runtime))
        broker_req_id = "ai-worker:run-stop-1"
        self.server.dispatches[broker_req_id] = {
            "request_id": broker_req_id,
            "status": "running",
            "execution_id": "exec-stop-1",
        }

        with executor._lock:
            ledger = executor._load_ledger()
            ledger["executions"]["run-stop-1"] = {
                "source_request_id": "run-stop-1",
                "project_id": "devorchestrator",
                "state": "running",
                "engine": "aibroker",
                "broker_request_id": broker_req_id,
                "started_at": "2026-09-17T01:00:00Z",
            }
            executor._save_ledger(ledger)

        submit_control_command(
            self.runtime, "devorchestrator", "stop",
            expected=project_identity(snapshot, self.runtime),
        )

        coordinator = ControlCommandCoordinator(self.runtime)
        outcomes = coordinator.advance(self.config_file, {"projects": [snapshot]}, executor)

        self.assertEqual(len(outcomes), 1)
        outcome = outcomes[0]
        self.assertEqual(outcome["state"], "accepted")
        self.assertEqual(outcome["effect"], "pause_and_interrupt")
        self.assertTrue(OwnerControlStore(self.runtime).is_paused("devorchestrator"))
        self.assertEqual(outcome["interruption"]["request_id"], broker_req_id)
        self.assertEqual(outcome["interruption"]["status"], "failed")
        self.assertTrue(outcome["interruption"]["interrupt_supported"])

    def test_stop_fails_closed_when_interrupt_evidence_is_unsupported_or_unconfirmed(self):
        """Stop retains durable pause but fails closed when interrupt evidence is unsupported, not found, or mismatched."""
        truth = read_repository_truth(self.repo)
        snapshot = {
            "project_id": "devorchestrator",
            "state": "READY_TO_RUN",
            "lifecycle_state": "READY_TO_RUN",
            "next_status": "**READY_TO_RUN**",
            "git": {"branch": truth.branch, "head": truth.head},
            "telemetry": {"task_id": "P12.6"},
        }
        port = load_aibroker_execution_port(self.runtime)
        executor = TransitionExecutor(self.runtime, ai_execution_port=port)
        broker_req_id = "ai-worker:run-failclose-1"

        with executor._lock:
            ledger = executor._load_ledger()
            ledger["executions"]["run-failclose-1"] = {
                "source_request_id": "run-failclose-1",
                "project_id": "devorchestrator",
                "state": "running",
                "engine": "aibroker",
                "broker_request_id": broker_req_id,
                "started_at": "2026-09-17T01:00:00Z",
            }
            executor._save_ledger(ledger)

        # Case 1: Interrupt unsupported by persistent harness
        self.server.interrupt_responses[broker_req_id] = {
            "request_id": broker_req_id,
            "status": "running",
            "interrupt_supported": False,
        }
        OwnerControlStore(self.runtime).set_paused("devorchestrator", False, command_id="res-1", action="resume")
        submit_control_command(
            self.runtime, "devorchestrator", "stop",
            expected=project_identity(snapshot, self.runtime),
        )
        coordinator = ControlCommandCoordinator(self.runtime)
        outcome = coordinator.advance(self.config_file, {"projects": [snapshot]}, executor)[0]

        self.assertEqual(outcome["state"], "failed")
        self.assertEqual(outcome["effect"], "pause_future_launches")
        self.assertIn("interruption unsupported", outcome["reason"])
        self.assertTrue(OwnerControlStore(self.runtime).is_paused("devorchestrator"))

        # Case 2: Target not found in broker (404)
        del self.server.interrupt_responses[broker_req_id]
        if broker_req_id in self.server.dispatches:
            del self.server.dispatches[broker_req_id]

        OwnerControlStore(self.runtime).set_paused("devorchestrator", False, command_id="res-2", action="resume")
        submit_control_command(
            self.runtime, "devorchestrator", "stop",
            expected=project_identity(snapshot, self.runtime),
        )
        outcome2 = coordinator.advance(self.config_file, {"projects": [snapshot]}, executor)[0]

        self.assertEqual(outcome2["state"], "failed")
        self.assertEqual(outcome2["effect"], "pause_future_launches")
        self.assertIn("target not found", outcome2["reason"])
        self.assertTrue(OwnerControlStore(self.runtime).is_paused("devorchestrator"))

        # Case 3: Mismatched request_id in response
        self.server.interrupt_responses[broker_req_id] = {
            "request_id": "wrong-request-id",
            "status": "failed",
            "interrupt_supported": True,
        }
        OwnerControlStore(self.runtime).set_paused("devorchestrator", False, command_id="res-3", action="resume")
        submit_control_command(
            self.runtime, "devorchestrator", "stop",
            expected=project_identity(snapshot, self.runtime),
        )
        outcome3 = coordinator.advance(self.config_file, {"projects": [snapshot]}, executor)[0]

        self.assertEqual(outcome3["state"], "failed")
        self.assertEqual(outcome3["effect"], "pause_future_launches")
        self.assertIn("request_id mismatch", outcome3["reason"])
        self.assertTrue(OwnerControlStore(self.runtime).is_paused("devorchestrator"))

        # Case 4: Missing interrupt_supported in response (ambiguous capability evidence fails closed)
        self.server.interrupt_responses[broker_req_id] = {
            "request_id": broker_req_id,
            "status": "interrupted",
        }
        OwnerControlStore(self.runtime).set_paused("devorchestrator", False, command_id="res-4", action="resume")
        submit_control_command(
            self.runtime, "devorchestrator", "stop",
            expected=project_identity(snapshot, self.runtime),
        )
        outcome4 = coordinator.advance(self.config_file, {"projects": [snapshot]}, executor)[0]

        self.assertEqual(outcome4["state"], "failed")
        self.assertEqual(outcome4["effect"], "pause_future_launches")
        self.assertIn("interruption unsupported", outcome4["reason"])
        self.assertTrue(OwnerControlStore(self.runtime).is_paused("devorchestrator"))

    def test_stop_cross_project_isolation(self):
        """Stopping project A interrupts only project A; project B's execution and unpaused state are untouched."""
        truth = read_repository_truth(self.repo)
        repo_p2 = self.root / "repo_p2"
        self._init_repo(repo_p2)

        multi_config = self.root / "multi_projects.json"
        multi_config.write_text(json.dumps({"projects": [
            {
                "project_id": pid,
                "repo_path": str(self.repo if pid == "p1" else repo_p2),
                "adapter": "agent_files",
                "execution": {
                    "enabled": True, "owner_authorized": True,
                    "engine": "aibroker", "allowed_next_actions": ["next_task"],
                },
            }
            for pid in ("p1", "p2")
        ]}), encoding="utf-8")

        port = load_aibroker_execution_port(self.runtime)
        executor = TransitionExecutor(self.runtime, ai_execution_port=port)

        self.server.dispatches["broker-p1"] = {"request_id": "broker-p1", "status": "running"}
        self.server.dispatches["broker-p2"] = {"request_id": "broker-p2", "status": "running"}

        with executor._lock:
            ledger = executor._load_ledger()
            ledger["executions"]["run-p1"] = {
                "source_request_id": "run-p1", "project_id": "p1", "state": "running",
                "engine": "aibroker", "broker_request_id": "broker-p1", "started_at": "2026-09-17T01:00:00Z",
            }
            ledger["executions"]["run-p2"] = {
                "source_request_id": "run-p2", "project_id": "p2", "state": "running",
                "engine": "aibroker", "broker_request_id": "broker-p2", "started_at": "2026-09-17T01:00:00Z",
            }
            executor._save_ledger(ledger)

        snap_p1 = {"project_id": "p1", "state": "READY_TO_RUN", "lifecycle_state": "READY_TO_RUN",
                   "next_status": "**READY_TO_RUN**", "git": {"branch": truth.branch, "head": truth.head},
                   "telemetry": {"task_id": "P1"}}
        snap_p2 = {"project_id": "p2", "state": "READY_TO_RUN", "lifecycle_state": "READY_TO_RUN",
                   "next_status": "**READY_TO_RUN**", "git": {"branch": truth.branch, "head": truth.head},
                   "telemetry": {"task_id": "P2"}}

        submit_control_command(self.runtime, "p1", "stop", expected=project_identity(snap_p1, self.runtime))

        coordinator = ControlCommandCoordinator(self.runtime)
        outcomes = coordinator.advance(multi_config, {"projects": [snap_p1, snap_p2]}, executor)

        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["execution_id"], "run-p1")
        self.assertEqual(outcomes[0]["effect"], "pause_and_interrupt")

        # p1 is paused, p2 is NOT paused
        owner_store = OwnerControlStore(self.runtime)
        self.assertTrue(owner_store.is_paused("p1"))
        self.assertFalse(owner_store.is_paused("p2"))

        # Server observed interrupt ONLY for broker-p1, NEVER for broker-p2
        interrupt_reqs = [r for r in self.server.recorded_requests if "/interrupt" in r["path"]]
        self.assertEqual(len(interrupt_reqs), 1)
        self.assertIn("broker-p1", interrupt_reqs[0]["path"])
        self.assertNotIn("broker-p2", interrupt_reqs[0]["path"])

    # =========================================================================
    # Acceptance Criterion 3: Restart Reconciliation Projection
    # =========================================================================

    def test_restart_reconciliation_projections(self):
        """Verify restart reconciliation through persistent status route:
        succeeded -> completed
        explicit failure -> failed
        running/unknown/unavailable -> recovery_required without replay
        managed interrupt with unchanged repo -> recovery_safe_retry=True
        managed interrupt with changed repo -> recovery_safe_retry=False
        """
        truth = read_repository_truth(self.repo)

        # 1. Succeeded projection
        runtime_succeeded = self.root / "runtime_succ"
        runtime_succeeded.mkdir()
        (runtime_succeeded / "aibroker-execution.json").write_text(
            (self.runtime / "aibroker-execution.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.server.dispatches["ai-worker:w-succ"] = {
            "request_id": "ai-worker:w-succ", "status": "succeeded", "finished_at": "2026-09-17T02:00:00Z",
            "dispatch_id": "d-s", "decision_id": "q-s", "execution_id": "e-s", "resource_id": "r-s",
            "provider": "deepseek", "account": "a", "model": "m",
        }
        (runtime_succeeded / "transition-executor.json").write_text(json.dumps({"version": 1, "executions": {
            "w-succ": {"project_id": "p1", "source_request_id": "w-succ", "engine": "aibroker",
                       "broker_request_id": "ai-worker:w-succ", "state": "running", "repo_path": str(self.repo),
                       "branch": truth.branch, "head": truth.head, "launch_status_hash": truth.status_hash,
                       "started_at": "2026-09-17T01:00:00Z"},
        }}), encoding="utf-8")

        port = load_aibroker_execution_port(runtime_succeeded)
        rec_succ = TransitionExecutor(runtime_succeeded, ai_execution_port=port).state()["executions"]["w-succ"]
        self.assertEqual(rec_succ["state"], "completed")
        self.assertEqual(rec_succ["execution_id"], "e-s")

        # 2. Explicit provider failure projection
        runtime_fail = self.root / "runtime_fail"
        runtime_fail.mkdir()
        (runtime_fail / "aibroker-execution.json").write_text(
            (self.runtime / "aibroker-execution.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.server.dispatches["ai-worker:w-fail"] = {
            "request_id": "ai-worker:w-fail", "status": "failed", "execution_error": "model error",
            "finished_at": "2026-09-17T02:00:00Z", "resource_id": "r-f",
        }
        (runtime_fail / "transition-executor.json").write_text(json.dumps({"version": 1, "executions": {
            "w-fail": {"project_id": "p1", "source_request_id": "w-fail", "engine": "aibroker",
                       "broker_request_id": "ai-worker:w-fail", "state": "running", "repo_path": str(self.repo),
                       "branch": truth.branch, "head": truth.head, "launch_status_hash": truth.status_hash,
                       "started_at": "2026-09-17T01:00:00Z"},
        }}), encoding="utf-8")

        rec_fail = TransitionExecutor(runtime_fail, ai_execution_port=load_aibroker_execution_port(runtime_fail)).state()["executions"]["w-fail"]
        self.assertEqual(rec_fail["state"], "failed")
        self.assertEqual(rec_fail["reason"], "model error")

        # 3. Running / unavailable projection -> recovery_required, no auto replay
        runtime_running = self.root / "runtime_run"
        runtime_running.mkdir()
        (runtime_running / "aibroker-execution.json").write_text(
            (self.runtime / "aibroker-execution.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.server.dispatches["ai-worker:w-run"] = {
            "request_id": "ai-worker:w-run", "status": "running", "resource_id": "r-r",
        }
        (runtime_running / "transition-executor.json").write_text(json.dumps({"version": 1, "executions": {
            "w-run": {"project_id": "p1", "source_request_id": "w-run", "engine": "aibroker",
                      "broker_request_id": "ai-worker:w-run", "state": "running", "repo_path": str(self.repo),
                      "branch": truth.branch, "head": truth.head, "launch_status_hash": truth.status_hash,
                      "started_at": "2026-09-17T01:00:00Z"},
        }}), encoding="utf-8")

        rec_run = TransitionExecutor(runtime_running, ai_execution_port=load_aibroker_execution_port(runtime_running)).state()["executions"]["w-run"]
        self.assertEqual(rec_run["state"], "recovery_required")
        self.assertFalse(rec_run["recovery_safe_retry"])

        # 4. Managed interrupt with unchanged repository -> recovery_safe_retry=True
        runtime_managed = self.root / "runtime_managed"
        runtime_managed.mkdir()
        (runtime_managed / "aibroker-execution.json").write_text(
            (self.runtime / "aibroker-execution.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.server.dispatches["ai-worker:w-managed"] = {
            "request_id": "ai-worker:w-managed", "status": "failed",
            "execution_error": MANAGED_INTERRUPT_REASON, "resource_id": "r-m",
        }
        (runtime_managed / "transition-executor.json").write_text(json.dumps({"version": 1, "executions": {
            "w-managed": {"project_id": "p1", "source_request_id": "w-managed", "engine": "aibroker",
                          "broker_request_id": "ai-worker:w-managed", "state": "running", "repo_path": str(self.repo),
                          "branch": truth.branch, "head": truth.head, "launch_status_hash": truth.status_hash,
                          "started_at": "2026-09-17T01:00:00Z"},
        }}), encoding="utf-8")

        rec_managed = TransitionExecutor(runtime_managed, ai_execution_port=load_aibroker_execution_port(runtime_managed)).state()["executions"]["w-managed"]
        self.assertEqual(rec_managed["state"], "recovery_required")
        self.assertTrue(rec_managed["recovery_safe_retry"])
        self.assertIn("managed Broker interruption confirmed", rec_managed["reason"])

        # 5. Managed interrupt with changed repository -> recovery_safe_retry=False
        repo_changed = self.root / "repo_changed"
        self._init_repo(repo_changed)
        truth_changed = read_repository_truth(repo_changed)
        (repo_changed / "README.md").write_text("dirtied\n", encoding="utf-8")

        runtime_changed = self.root / "runtime_changed"
        runtime_changed.mkdir()
        (runtime_changed / "aibroker-execution.json").write_text(
            (self.runtime / "aibroker-execution.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.server.dispatches["ai-worker:w-changed"] = {
            "request_id": "ai-worker:w-changed", "status": "failed",
            "execution_error": MANAGED_INTERRUPT_REASON, "resource_id": "r-c",
        }
        (runtime_changed / "transition-executor.json").write_text(json.dumps({"version": 1, "executions": {
            "w-changed": {"project_id": "p1", "source_request_id": "w-changed", "engine": "aibroker",
                          "broker_request_id": "ai-worker:w-changed", "state": "running", "repo_path": str(repo_changed),
                          "branch": truth_changed.branch, "head": truth_changed.head, "launch_status_hash": truth_changed.status_hash,
                          "started_at": "2026-09-17T01:00:00Z"},
        }}), encoding="utf-8")

        rec_changed = TransitionExecutor(runtime_changed, ai_execution_port=load_aibroker_execution_port(runtime_changed)).state()["executions"]["w-changed"]
        self.assertEqual(rec_changed["state"], "recovery_required")
        self.assertFalse(rec_changed["recovery_safe_retry"])

    # =========================================================================
    # Acceptance Criterion 1: CLI Compatibility Fallback
    # =========================================================================

    def test_cli_compatibility_fallback_when_service_url_absent(self):
        """When service_url is absent, the execution port uses subprocess CLI transport."""
        runtime_cli = self.root / "runtime_cli"
        runtime_cli.mkdir()
        # Write config without service_url
        (runtime_cli / "aibroker-execution.json").write_text(json.dumps({
            "python_executable": sys.executable,
            "broker_repo": str(self.root / "broker_dummy"),
            "config_path": str(self.root / "resources_dummy.yaml"),
            "process_timeout_seconds": 300.0,
            "probe_before_dispatch": False,
        }), encoding="utf-8")

        port = load_aibroker_execution_port(runtime_cli)
        self.assertIsNotNone(port)
        self.assertIsNone(port.config.service_url)

        with unittest.mock.patch("dev_orchestrator.ai.aibroker_subprocess.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=json.dumps({
                    "dispatch_id": "disp-cli", "request_id": "req-cli-1", "decision_id": "dec-cli",
                    "execution_id": "exec-cli", "status": "succeeded", "output": "CLI_OK",
                }),
                stderr="",
            )
            req = AIRoleRequest(
                project_id="devorchestrator", role="worker", prompt="work",
                working_directory=self.repo, request_id="req-cli-1",
            )
            res = port.execute(req)
            self.assertEqual(res.status, "succeeded")
            self.assertEqual(res.output, "CLI_OK")
            self.assertTrue(mock_run.called)
            # Ephemeral server should have received NO requests
            self.assertEqual(len([r for r in self.server.recorded_requests if r.get("token")]), 0)

    def test_stop_cli_fallback_retains_pause_and_fails_closed_without_persistent_capability(self):
        """CLI fallback interrupt dispatch lacks persistent capability proof and fails closed with retained pause."""
        runtime_cli = self.root / "runtime_stop_cli"
        runtime_cli.mkdir()
        (runtime_cli / "aibroker-execution.json").write_text(json.dumps({
            "python_executable": sys.executable,
            "broker_repo": str(self.root / "broker_dummy"),
            "config_path": str(self.root / "resources_dummy.yaml"),
            "process_timeout_seconds": 300.0,
            "probe_before_dispatch": False,
        }), encoding="utf-8")

        port = load_aibroker_execution_port(runtime_cli)
        self.assertIsNotNone(port)
        self.assertIsNone(port.config.service_url)

        truth = read_repository_truth(self.repo)
        snapshot = {
            "project_id": "devorchestrator",
            "state": "READY_TO_RUN",
            "lifecycle_state": "READY_TO_RUN",
            "next_status": "**READY_TO_RUN**",
            "git": {"branch": truth.branch, "head": truth.head},
            "telemetry": {"task_id": "P12.6"},
        }
        executor = TransitionExecutor(runtime_cli, ai_execution_port=port)
        broker_req_id = "ai-worker:run-cli-stop-1"

        with executor._lock:
            ledger = executor._load_ledger()
            ledger["executions"]["run-cli-stop-1"] = {
                "source_request_id": "run-cli-stop-1",
                "project_id": "devorchestrator",
                "state": "running",
                "engine": "aibroker",
                "broker_request_id": broker_req_id,
                "started_at": "2026-09-17T01:00:00Z",
            }
            executor._save_ledger(ledger)

        submit_control_command(
            runtime_cli, "devorchestrator", "stop",
            expected=project_identity(snapshot, runtime_cli),
        )

        with unittest.mock.patch("dev_orchestrator.ai.aibroker_subprocess.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=json.dumps({
                    "request_id": broker_req_id,
                    "status": "failed",
                    "resource_id": "r1",
                    "execution_error": "stopped",
                }),
                stderr="",
            )
            coordinator = ControlCommandCoordinator(runtime_cli)
            outcome = coordinator.advance(self.config_file, {"projects": [snapshot]}, executor)[0]

        self.assertEqual(outcome["state"], "failed")
        self.assertEqual(outcome["effect"], "pause_future_launches")
        self.assertIn("interruption unsupported", outcome["reason"])
        self.assertTrue(OwnerControlStore(runtime_cli).is_paused("devorchestrator"))


if __name__ == "__main__":
    unittest.main()
