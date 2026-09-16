import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from dev_orchestrator.ai.contracts import AIRoleResult, ResourceContext
from dev_orchestrator.ai.execution_port import MANAGED_INTERRUPT_REASON
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.core.transition_executor import TransitionExecutor, _execution_policy


class FakePort:
    def __init__(self):
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        return AIRoleResult(
            request_id=request.request_id,
            role_run_id=request.role_run_id,
            status="succeeded",
            output="WORKER_OK",
            dispatch_id="dispatch-1",
            decision_id="decision-1",
            execution_id="execution-1",
            resource_context=ResourceContext(
                "dsh/default/model", "deepseek", "default", "model"
            ),
        )



class RecoveryPort(FakePort):
    def __init__(self, fact):
        super().__init__(); self.fact = fact; self.status_requests = []
    def status(self, request_id):
        self.status_requests.append(request_id); return dict(self.fact) if self.fact else None


class WorktreeUnsafeError(Exception):
    pass


class PreExecutionWorktreePort(FakePort):
    def __init__(self):
        super().__init__()
        self.fail_first = True

    def execute(self, request):
        self.requests.append(request)
        if self.fail_first:
            self.fail_first = False
            return AIRoleResult(
                request_id=request.request_id, role_run_id=request.role_run_id,
                status="failed",
                error="WorktreeUnsafeError: dirty worktree requires deterministic recovery before writable reuse",
                dispatch_id="fa9af4c6-incident", decision_id="17fd00d7-incident",
                execution_id="c44f65e9-2da2-41d3-9eab-cc1a03241114",
                resource_context=ResourceContext(
                    "agy/agy-1/gemini-3.8-flash-high", "agy", "agy-1", "gemini-3.8-flash-high",
                ),
            )
        return AIRoleResult(
            request_id=request.request_id, role_run_id=request.role_run_id,
            status="succeeded", output="REMEDIATED",
            dispatch_id="dispatch-retry", decision_id="decision-retry",
            execution_id="execution-retry",
        )


class BlockingRetryPort(PreExecutionWorktreePort):
    def __init__(self):
        super().__init__()
        self.retry_started = threading.Event()
        self.allow_retry = threading.Event()

    def execute(self, request):
        if not self.fail_first:
            self.retry_started.set()
            if not self.allow_retry.wait(timeout=2):
                raise RuntimeError("test retry release timed out")
        return super().execute(request)


def broker_project(repo: Path):
    return {
        "project_id": "p1",
        "repo_path": str(repo),
        "execution": {
            "enabled": True,
            "owner_authorized": True,
            "engine": "aibroker",
            "worker_quality": "balanced",
            "allowed_next_actions": ["next_task"],
        },
    }
class AIBrokerTransitionTests(unittest.TestCase):
    def make_repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        (repo / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "fixture"], cwd=repo, check=True, capture_output=True)
        return repo

    def _write_remediation_decision(self, runtime: Path, truth, *, head=None, reason="Fix the stale broker evidence"):
        runtime.mkdir(parents=True, exist_ok=True)
        decision_id = "review-remediate-1"
        (runtime / "review-decisions.json").write_text(json.dumps({"version": 1, "decisions": {
            decision_id: {
                "project_id": "p1", "request_id": decision_id,
                "disposition": "apply", "decision": "remediate",
                "next_action": "continue_current_stage", "task_id": "P1",
                "branch": truth.branch, "head": head or truth.head,
                "role": "reviewer", "event": "worker_done", "reason": reason,
                "review_status_hash": truth.status_hash, "consumed_at": "2026-09-16T00:00:00+00:00",
            },
        }}), encoding="utf-8")
        return decision_id

    @staticmethod
    def _remediation_snapshot(task_id="P1"):
        return {
            "project_id": "p1", "state": "READY_TO_RUN", "telemetry": {"task_id": task_id},
            "worker": {"state": "not_started", "process_alive": False},
        }


    def _write_active_broker_record(self, runtime: Path, repo: Path, truth, *, request_id="worker-1"):
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "transition-executor.json").write_text(json.dumps({"version": 1, "executions": {
            request_id: {"project_id": "p1", "source_request_id": request_id, "engine": "aibroker",
                         "broker_request_id": "ai-worker:" + request_id, "state": "running",
                         "branch": truth.branch, "head": truth.head, "launch_status_hash": truth.status_hash,
                         "repo_path": str(repo), "started_at": "2026-09-10T01:00:00+00:00",
                         "review_state": "pending"}
        }}), encoding="utf-8")

    def test_restart_recovers_succeeded_broker_as_completed_with_resource_facts(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); repo=self.make_repo(root); runtime=root/"runtime"; truth=read_repository_truth(repo)
            self._write_active_broker_record(runtime, repo, truth)
            port=RecoveryPort({"status":"succeeded", "dispatch_id":"d1", "decision_id":"q1",
                               "execution_id":"e1", "resource_id":"r1", "provider":"deepseek",
                               "account":"a", "model":"m", "finished_at":"2026-09-10T01:02:00+00:00"})
            record=TransitionExecutor(runtime, ai_execution_port=port).state()["executions"]["worker-1"]
            self.assertEqual(record["state"], "completed")
            self.assertEqual(record["execution_id"], "e1")
            self.assertEqual(record["resource_context"]["resource_id"], "r1")

    def test_restart_managed_interrupt_is_safe_retry_only_when_repo_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); repo=self.make_repo(root); runtime=root/"runtime"; truth=read_repository_truth(repo)
            self._write_active_broker_record(runtime, repo, truth)
            fact={"status":"failed", "execution_error":MANAGED_INTERRUPT_REASON, "resource_id":"r1"}
            record=TransitionExecutor(runtime, ai_execution_port=RecoveryPort(fact)).state()["executions"]["worker-1"]
            self.assertEqual(record["state"], "recovery_required")
            self.assertTrue(record["recovery_safe_retry"])

    def test_restart_running_or_changed_repo_never_auto_replays(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); repo=self.make_repo(root); runtime=root/"runtime"; truth=read_repository_truth(repo)
            self._write_active_broker_record(runtime, repo, truth)
            (repo/"README.md").write_text("changed\n", encoding="utf-8")
            fact={"status":"failed", "execution_error":MANAGED_INTERRUPT_REASON, "resource_id":"r1"}
            record=TransitionExecutor(runtime, ai_execution_port=RecoveryPort(fact)).state()["executions"]["worker-1"]
            self.assertFalse(record["recovery_safe_retry"])
            runtime2=root/"runtime2"; truth2=read_repository_truth(repo)
            self._write_active_broker_record(runtime2, repo, truth2, request_id="worker-2")
            running=TransitionExecutor(runtime2, ai_execution_port=RecoveryPort({"status":"running", "resource_id":"r1"})).state()["executions"]["worker-2"]
            self.assertEqual(running["state"], "recovery_required")
            self.assertFalse(running["recovery_safe_retry"])

    def test_start_control_ignores_unsafe_recovery_from_stale_head(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); repo = self.make_repo(root); runtime = root / "runtime"
            truth = read_repository_truth(repo); runtime.mkdir()
            stale = {"project_id": "p1", "source_request_id": "old-worker", "engine": "aibroker",
                     "task_id": "P11x", "state": "recovery_required", "recovery_safe_retry": False,
                     "branch": truth.branch, "head": "stale-head", "started_at": "2026-09-13T01:00:00+00:00"}
            (runtime / "transition-executor.json").write_text(json.dumps({"version": 1, "executions": {"old-worker": stale}}), encoding="utf-8")
            port = FakePort(); executor = TransitionExecutor(runtime, ai_execution_port=port)
            snapshot = {"state": "READY_TO_RUN", "telemetry": {"task_id": "P11x"},
                        "worker": {"state": "not_started", "process_alive": False}}
            launch = executor.start_control(broker_project(repo), snapshot, "new-control")
            self.assertIsNotNone(launch)
            executor._threads["new-control"].join(timeout=2)
            self.assertEqual(len(port.requests), 1)

    def test_start_control_blocks_unsafe_recovery_for_same_task_and_head(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); repo = self.make_repo(root); runtime = root / "runtime"
            truth = read_repository_truth(repo); runtime.mkdir()
            current = {"project_id": "p1", "source_request_id": "old-worker", "engine": "aibroker",
                       "task_id": "P11x", "state": "recovery_required", "recovery_safe_retry": False,
                       "branch": truth.branch, "head": truth.head, "started_at": "2026-09-13T01:00:00+00:00"}
            (runtime / "transition-executor.json").write_text(json.dumps({"version": 1, "executions": {"old-worker": current}}), encoding="utf-8")
            port = FakePort(); executor = TransitionExecutor(runtime, ai_execution_port=port)
            snapshot = {"state": "READY_TO_RUN", "telemetry": {"task_id": "P11x"},
                        "worker": {"state": "not_started", "process_alive": False}}
            launch = executor.start_control(broker_project(repo), snapshot, "new-control")
            self.assertIsNone(launch)
            self.assertEqual(port.requests, [])

    def test_owner_continue_retries_only_exact_pre_execution_remediation_with_review_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); repo = self.make_repo(root); runtime = root / "runtime"
            project = broker_project(repo)
            project["execution"]["allowed_next_actions"] = ["next_task", "continue_current_stage"]
            policy, error = _execution_policy(project); self.assertFalse(error)
            port = PreExecutionWorktreePort(); executor = TransitionExecutor(runtime, ai_execution_port=port)
            (repo / "agent").mkdir()
            (repo / "agent" / "next.md").write_text("# P2 next task\nStatus: READY_TO_RUN\n", encoding="utf-8")
            subprocess.run(["git", "add", "agent/next.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "advance to P2"], cwd=repo, check=True, capture_output=True)
            (repo / "graphify-out").mkdir()
            (repo / "graphify-out" / "generated.json").write_text("{}\n", encoding="utf-8")
            reviewed_truth = read_repository_truth(repo)
            self.assertEqual(reviewed_truth.status_hash, "c955036d0817f8f2dbb4e79ccf010d1206eee1694ffa4f2d951b1a9e3ffe3d20")
            decision_id = self._write_remediation_decision(runtime, reviewed_truth)
            config = root / "projects.json"
            config.write_text(json.dumps({"projects": [project]}), encoding="utf-8")
            launches = executor.advance({"projects": [self._remediation_snapshot("P2")]}, config)
            self.assertEqual(len(launches), 1)
            executor._threads[decision_id].join(timeout=2)
            original = executor.state()["executions"][decision_id]
            self.assertEqual(original["state"], "failed")
            self.assertIn("WorktreeUnsafeError", original["reason"])
            self.assertEqual(original["broker_status"], "failed")
            self.assertEqual(original["dispatch_id"], "fa9af4c6-incident")
            self.assertEqual(original["decision_id"], "17fd00d7-incident")
            self.assertEqual(original["execution_id"], "c44f65e9-2da2-41d3-9eab-cc1a03241114")
            self.assertEqual(original["resource_context"]["resource_id"], "agy/agy-1/gemini-3.8-flash-high")
            self.assertIsNone(original["session_id"])
            self.assertFalse(original["provider_output_observed"])
            self.assertEqual(original["review_dirty_entries"], ["?? graphify-out/"])
            self.assertEqual(original["review_status_hash"], reviewed_truth.status_hash)
            # Model the historical incident before explicit no-output fields
            # were added: the exact Broker worktree-safety contract is the
            # only accepted compatibility proof.
            legacy = executor.state()
            legacy["executions"][decision_id].pop("provider_output_observed")
            legacy["executions"][decision_id].pop("first_output_at")
            (runtime / "transition-executor.json").write_text(json.dumps(legacy), encoding="utf-8")
            original_before_retry = legacy["executions"][decision_id]

            (repo / "graphify-out" / "generated.json").unlink()
            (repo / "graphify-out").rmdir()
            retry = executor.start_control(project, self._remediation_snapshot("P2"), "owner-continue-2")
            self.assertIsNotNone(retry)
            executor._threads["owner-continue-2"].join(timeout=2)
            ledger = executor.state()["executions"]
            self.assertEqual(ledger[decision_id], original_before_retry)
            self.assertEqual(ledger["owner-continue-2"]["state"], "completed")
            self.assertEqual(ledger["owner-continue-2"]["source_kind"], "remediation")
            self.assertEqual(ledger["owner-continue-2"]["recovery_of"], decision_id)
            self.assertEqual(ledger["owner-continue-2"]["review_decision_id"], decision_id)
            self.assertEqual(len(port.requests), 2)
            self.assertEqual(port.requests[1].stage_run_id, "remediation")
            self.assertEqual(port.requests[1].task_run_id, "P1")
            self.assertIn("Remediate the current bounded task", port.requests[1].prompt)
            self.assertIn("Reviewer reason: Fix the stale broker evidence", port.requests[1].prompt)
            self.assertIsNone(executor.start_control(project, self._remediation_snapshot("P2"), "owner-continue-2"))
            self.assertEqual(len(port.requests), 2)

    def test_remediation_recovery_refuses_dirty_mismatched_or_usable_provider_work(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); repo = self.make_repo(root); runtime = root / "runtime"
            project = broker_project(repo)
            project["execution"]["allowed_next_actions"] = ["next_task", "continue_current_stage"]
            policy, error = _execution_policy(project); self.assertFalse(error)
            port = PreExecutionWorktreePort(); executor = TransitionExecutor(runtime, ai_execution_port=port)
            truth = read_repository_truth(repo); decision_id = self._write_remediation_decision(runtime, truth)
            initial = executor._launch(project, source_request_id=decision_id, source_kind="remediation",
                task_id="P1", source_task_id="P1", branch=truth.branch, head=truth.head,
                worker_prompt="INITIAL", policy=policy)
            self.assertIsNotNone(initial); executor._threads[decision_id].join(timeout=2)

            (repo / "generated.txt").write_text("dirty\n", encoding="utf-8")
            self.assertIsNone(executor.start_control(project, self._remediation_snapshot(), "dirty-continue"))
            self.assertIn("requires a clean current worktree", executor.state()["executions"]["dirty-continue"]["reason"])
            (repo / "generated.txt").unlink()

            decision_path = runtime / "review-decisions.json"
            decisions = json.loads(decision_path.read_text(encoding="utf-8"))
            decisions["decisions"][decision_id]["head"] = "mismatched-head"
            decision_path.write_text(json.dumps(decisions), encoding="utf-8")
            self.assertIsNone(executor.start_control(project, self._remediation_snapshot(), "mismatched-continue"))
            self.assertIn("decision no longer matches", executor.state()["executions"]["mismatched-continue"]["reason"])
            decisions["decisions"][decision_id]["head"] = truth.head
            decision_path.write_text(json.dumps(decisions), encoding="utf-8")

            baseline = executor.state()
            for command_id, field, value in (
                ("session-continue", "session_id", "provider-session"),
                ("output-continue", "provider_output_observed", True),
                ("provider-failure-continue", "reason", "provider timeout"),
            ):
                bad = json.loads(json.dumps(baseline)); bad["executions"][decision_id][field] = value
                (runtime / "transition-executor.json").write_text(json.dumps(bad), encoding="utf-8")
                self.assertIsNone(executor.start_control(project, self._remediation_snapshot(), command_id))
                self.assertIn("not an exact no-usable-provider-work WorktreeUnsafeError", executor.state()["executions"][command_id]["reason"])
            self.assertEqual(len(port.requests), 1)

    def test_reviewed_source_cleanup_cannot_use_generated_only_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); repo = self.make_repo(root); runtime = root / "runtime"
            project = broker_project(repo)
            project["execution"]["allowed_next_actions"] = ["next_task", "continue_current_stage"]
            policy, error = _execution_policy(project); self.assertFalse(error)
            port = PreExecutionWorktreePort(); executor = TransitionExecutor(runtime, ai_execution_port=port)
            (repo / "README.md").write_text("reviewed source change\n", encoding="utf-8")
            reviewed_truth = read_repository_truth(repo)
            decision_id = self._write_remediation_decision(runtime, reviewed_truth)
            initial = executor._launch(project, source_request_id=decision_id, source_kind="remediation",
                task_id="P1", source_task_id="P1", branch=reviewed_truth.branch, head=reviewed_truth.head,
                worker_prompt="INITIAL", policy=policy)
            self.assertIsNotNone(initial); executor._threads[decision_id].join(timeout=2)
            (repo / "README.md").write_text("fixture\n", encoding="utf-8")
            self.assertIsNone(executor.start_control(project, self._remediation_snapshot(), "source-revert-continue"))
            record = executor.state()["executions"]["source-revert-continue"]
            self.assertIn("generated-only proof", record["reason"])
            self.assertEqual(len(port.requests), 1)

    def test_missing_negative_provider_evidence_is_ambiguous_without_exact_legacy_contract(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); repo = self.make_repo(root); runtime = root / "runtime"
            project = broker_project(repo)
            project["execution"]["allowed_next_actions"] = ["next_task", "continue_current_stage"]
            policy, error = _execution_policy(project); self.assertFalse(error)
            port = PreExecutionWorktreePort(); executor = TransitionExecutor(runtime, ai_execution_port=port)
            (repo / "graphify-out").mkdir()
            (repo / "graphify-out" / "generated.json").write_text("{}\n", encoding="utf-8")
            reviewed_truth = read_repository_truth(repo)
            decision_id = self._write_remediation_decision(runtime, reviewed_truth)
            initial = executor._launch(project, source_request_id=decision_id, source_kind="remediation",
                task_id="P1", source_task_id="P1", branch=reviewed_truth.branch, head=reviewed_truth.head,
                worker_prompt="INITIAL", policy=policy)
            self.assertIsNotNone(initial); executor._threads[decision_id].join(timeout=2)
            (repo / "graphify-out" / "generated.json").unlink()
            (repo / "graphify-out").rmdir()
            ambiguous = executor.state()
            row = ambiguous["executions"][decision_id]
            row.pop("provider_output_observed")
            row.pop("first_output_at")
            row["reason"] = "WorktreeUnsafeError: non-contract legacy worktree message"
            (runtime / "transition-executor.json").write_text(json.dumps(ambiguous), encoding="utf-8")
            self.assertIsNone(executor.start_control(project, self._remediation_snapshot(), "missing-evidence-continue"))
            self.assertIn("not an exact no-usable-provider-work", executor.state()["executions"]["missing-evidence-continue"]["reason"])
            self.assertEqual(len(port.requests), 1)

    def test_recovery_lineage_is_persisted_before_the_retry_executes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); repo = self.make_repo(root); runtime = root / "runtime"
            project = broker_project(repo)
            project["execution"]["allowed_next_actions"] = ["next_task", "continue_current_stage"]
            policy, error = _execution_policy(project); self.assertFalse(error)
            port = BlockingRetryPort(); executor = TransitionExecutor(runtime, ai_execution_port=port)
            (repo / "graphify-out").mkdir()
            (repo / "graphify-out" / "generated.json").write_text("{}\n", encoding="utf-8")
            reviewed_truth = read_repository_truth(repo)
            decision_id = self._write_remediation_decision(runtime, reviewed_truth)
            initial = executor._launch(project, source_request_id=decision_id, source_kind="remediation",
                task_id="P1", source_task_id="P1", branch=reviewed_truth.branch, head=reviewed_truth.head,
                worker_prompt="INITIAL", policy=policy)
            self.assertIsNotNone(initial); executor._threads[decision_id].join(timeout=2)
            (repo / "graphify-out" / "generated.json").unlink()
            (repo / "graphify-out").rmdir()
            retry_id = "crash-safe-retry"
            self.assertIsNotNone(executor.start_control(project, self._remediation_snapshot(), retry_id))
            self.assertTrue(port.retry_started.wait(timeout=2))
            launching = executor.state()["executions"][retry_id]
            self.assertEqual(launching["source_kind"], "remediation")
            self.assertEqual(launching["recovery_of"], decision_id)
            self.assertEqual(launching["review_decision_id"], decision_id)
            self.assertEqual(launching["review_status_hash"], reviewed_truth.status_hash)
            self.assertEqual(launching["reviewed_fingerprint_evidence"]["kind"], "legacy_generated_only_fingerprint")
            port.allow_retry.set()
            executor._threads[retry_id].join(timeout=2)
            self.assertEqual(executor.state()["executions"][retry_id]["state"], "completed")

    def test_policy_accepts_aibroker_without_legacy_backends(self):
        with tempfile.TemporaryDirectory() as td:
            policy, error = _execution_policy(broker_project(Path(td)))
        self.assertEqual(error, "")
        self.assertEqual(policy["engine"], "aibroker")
        self.assertEqual(policy["preferred_backends"], ())
        self.assertEqual(policy["worker_quality"], "balanced")

    def test_aibroker_worker_records_exact_dispatch_resource_facts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self.make_repo(root)
            project = broker_project(repo)
            policy, error = _execution_policy(project)
            self.assertFalse(error)
            truth = read_repository_truth(repo)
            port = FakePort()
            executor = TransitionExecutor(root / "runtime", ai_execution_port=port)
            launch = executor._launch(
                project, source_request_id="owner-p1", source_kind="owner_start",
                task_id="P1", source_task_id=None, branch=truth.branch, head=truth.head,
                worker_prompt="Return exactly WORKER_OK", policy=policy,
            )
            self.assertIsNotNone(launch)
            deadline = time.time() + 3
            record = {}
            while time.time() < deadline:
                record = executor.state()["executions"]["owner-p1"]
                if record.get("state") == "completed":
                    break
                time.sleep(0.02)
            self.assertEqual(record["state"], "completed")
            self.assertEqual(record["engine"], "aibroker")
            self.assertEqual(record["dispatch_id"], "dispatch-1")
            self.assertEqual(record["execution_id"], "execution-1")
            executor._threads["owner-p1"].join(timeout=2)
            self.assertFalse(executor._threads["owner-p1"].is_alive())
            self.assertEqual(record["resource_context"]["provider"], "deepseek")
            self.assertEqual(port.requests[0].role, "worker")
            self.assertEqual(port.requests[0].timeout_seconds, 14400.0)
            self.assertTrue(port.requests[0].metadata["managed_worktree"])

    def test_project_context_injected_into_aibroker_worker_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self.make_repo(root)
            ctx_payload = {
                "schema_version": 1,
                "project_id": "p1",
                "goals": ["Ship broker context"],
                "architecture": ["Broker worker module"],
                "protected_scope": ["Production infra"],
                "safety_constraints": ["Fail closed"],
                "validation_commands": ["python -m unittest"],
                "runtime_assumptions": ["Python 3.11"],
                "key_decisions": ["D1 schema"],
            }
            (repo / "agent").mkdir(parents=True, exist_ok=True)
            (repo / "agent" / "project-context.json").write_text(json.dumps(ctx_payload), encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "ctx"], check=True)

            project = broker_project(repo)
            project["project_context"] = {
                "enabled": True,
                "document_path": "agent/project-context.json",
                "require_valid": True,
            }
            policy, error = _execution_policy(project)
            self.assertFalse(error)
            truth = read_repository_truth(repo)
            port = FakePort()
            executor = TransitionExecutor(root / "runtime", ai_execution_port=port)
            launch = executor._launch(
                project, source_request_id="owner-p1", source_kind="owner_start",
                task_id="P1", source_task_id=None, branch=truth.branch, head=truth.head,
                worker_prompt="Return exactly WORKER_OK", policy=policy,
            )
            self.assertIsNotNone(launch)
            deadline = time.time() + 3
            record = {}
            while time.time() < deadline:
                record = executor.state()["executions"]["owner-p1"]
                if record.get("state") == "completed":
                    break
                time.sleep(0.02)
            self.assertEqual(record["state"], "completed")
            self.assertEqual(record["context_state"], "ready")
            self.assertIsNotNone(record["context_digest"])
            executor._threads["owner-p1"].join(timeout=2)
            self.assertFalse(executor._threads["owner-p1"].is_alive())
            prompt = port.requests[0].prompt
            self.assertIn("[PROJECT_CONTEXT_BEGIN]", prompt)
            self.assertIn("## goals\n- Ship broker context", prompt)
            self.assertIn("[PROJECT_CONTEXT_END]", prompt)
