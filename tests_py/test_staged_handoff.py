import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dev_orchestrator.ai.contracts import AIRoleRequest, AIRoleResult, ResourceContext
from dev_orchestrator.core.ai_planner import AIPlannerCoordinator
from dev_orchestrator.core.control_commands import ControlCommandCoordinator
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.core.staged_roadmap import read_successor, sha256_bytes
from dev_orchestrator.core.transition_executor import TransitionExecutor
from dev_orchestrator.platform.process import hidden_subprocess_kwargs
from tests_py.test_transition_executor import FakeBackend


def make_git_repo(repo: Path, task_id: str = "P11x") -> str:
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


def setup_staged_spec(repo: Path, successor_id: str = "P11b") -> tuple[str, bytes]:
    spec_path = f"agent/staged/{successor_id}.md"
    spec_bytes = (
        f"# {successor_id} Successor Task\n\n"
        "Status: **PENDING DESIGN**\n\n"
        "Goal: implement successor.\n"
    ).encode("utf-8")
    (repo / spec_path).write_bytes(spec_bytes)
    return spec_path, spec_bytes


def setup_roadmap(repo: Path, task_id: str = "P11x", successor_id: str = "P11b", spec_path: str = "agent/staged/P11b.md") -> None:
    data = {
        "schema_version": 1,
        "tasks": [
            {"task_id": task_id, "successor": successor_id, "successor_spec_path": spec_path},
            {"task_id": successor_id, "successor": None, "successor_spec_path": None},
        ],
    }
    (repo / "agent" / "staged" / "roadmap.json").write_text(json.dumps(data), encoding="utf-8")


class FakeHandoffPort:
    def __init__(self, plan_task_id: str = "P11b"):
        self.requests = []
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


class TestStagedHandoffPrompts(unittest.TestCase):
    def test_prompts_for_deferred_record(self):
        staged_text = (
            "# P11b Successor Task\n\n"
            "Status: **PENDING DESIGN**\n\n"
            "Goal: implement successor.\n"
        )
        record = {
            "project_id": "p1",
            "task_id": "P11b",
            "branch": "main",
            "head": "commit-head-123",
            "deferred": True,
            "predecessor_task_id": "P11x",
            "staged_spec_path": "agent/staged/P11b.md",
            "staged_spec_sha256": "abc123sha",
            "staged_spec_text": staged_text,
            "next_text": "# P11x Predecessor Task\nStatus: **COMPLETE**\n",
        }
        plan = {
            "task_id": "P11b",
            "summary": "Plan summary.",
            "implementation_steps": ["step"],
            "interfaces": ["iface"],
            "validation": ["val"],
            "risks": ["risk"],
            "out_of_scope": ["none"],
        }

        # Initial planner prompt
        p_prompt = AIPlannerCoordinator._planner_prompt(record)
        self.assertIn(staged_text, p_prompt)
        self.assertIn("Staged successor task spec (agent/staged/P11b.md;", p_prompt)
        self.assertNotIn("Predecessor Task", p_prompt)
        self.assertNotIn("Status: **COMPLETE**", p_prompt)

        # Retry planner prompt
        p_retry_prompt = AIPlannerCoordinator._planner_prompt(record, retry_reason="JSON bad", attempt=2)
        self.assertIn(staged_text, p_retry_prompt)
        self.assertIn("[PLANNER_RETRY]", p_retry_prompt)
        self.assertNotIn("Predecessor Task", p_retry_prompt)

        # Remediation planner prompt
        p_rem_prompt = AIPlannerCoordinator._planner_prompt(
            record,
            remediation={"round": 1, "rejection": "add more tests", "prior_plan": plan},
        )
        self.assertIn(staged_text, p_rem_prompt)
        self.assertIn("[PLAN_REMEDIATION]", p_rem_prompt)
        self.assertIn("add more tests", p_rem_prompt)
        self.assertNotIn("Predecessor Task", p_rem_prompt)

        # Initial review prompt
        r_prompt = AIPlannerCoordinator._review_prompt(record, plan)
        self.assertIn(staged_text, r_prompt)
        self.assertIn("Staged successor task spec (agent/staged/P11b.md;", r_prompt)
        self.assertNotIn("Predecessor Task", r_prompt)
        self.assertNotIn("Original task:\n---BEGIN NEXT---\n# P11x", r_prompt)

        # Normal record prompts byte-identical test
        normal_record = {
            "project_id": "p1",
            "task_id": "P11x",
            "branch": "main",
            "head": "commit-head-123",
            "next_text": "# P11x\nStatus: **PENDING DESIGN**\n",
        }
        normal_p_prompt = AIPlannerCoordinator._planner_prompt(normal_record)
        expected_p = (
            "Current agent/next.md:\n---BEGIN NEXT---\n"
            + normal_record["next_text"]
            + "\n---END NEXT---\n"
        )
        self.assertTrue(normal_p_prompt.endswith(expected_p))

        normal_r_prompt = AIPlannerCoordinator._review_prompt(normal_record, plan)
        expected_r_head = "Original task:\n---BEGIN NEXT---\n" + normal_record["next_text"] + "\n---END NEXT---\n\n"
        self.assertIn(expected_r_head, normal_r_prompt)


class TestStagedHandoffPlannerGuards(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.base = Path(self.td.name)
        self.repo = self.base / "repo"
        self.head = make_git_repo(self.repo, "P11x")
        self.spec_path, self.spec_bytes = setup_staged_spec(self.repo, "P11b")
        setup_roadmap(self.repo, "P11x", "P11b", self.spec_path)
        # Commit staged files
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "add staged files"], check=True, capture_output=True)
        self.head = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()

        self.runtime = self.base / "runtime"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.port = FakeHandoffPort("P11b")
        self.planner = AIPlannerCoordinator(self.runtime, self.port)
        self.project = {
            "project_id": "p1",
            "repo_path": str(self.repo),
            "execution": {"engine": "aibroker"},
            "ai_roles": {"planner": {"enabled": True}},
        }
        self.snapshot = {
            "project_id": "p1",
            "state": "IDLE",
            "next_status": "**COMPLETE**",
            "telemetry": {"task_id": "P11x"},
        }
        self.handoff = {
            "task_id": "P11x",
            "next_task_id": "P11b",
            "staged_successor": "P11b",
            "staged_spec_path": self.spec_path,
            "staged_spec_sha256": sha256_bytes(self.spec_bytes),
            "reviewed_branch": "master" if "master" in subprocess.check_output(["git", "-C", str(self.repo), "branch"], text=True) else "main",
            "reviewed_head": self.head,
        }

    def tearDown(self):
        self.td.cleanup()

    def test_start_deferred_missing_metadata_fails(self):
        bad_handoff = dict(self.handoff)
        bad_handoff.pop("staged_spec_sha256")
        plan_id, reason = self.planner.start_deferred(self.project, self.snapshot, "cmd1", bad_handoff)
        self.assertIsNone(plan_id)
        self.assertEqual(reason, "staged handoff metadata incomplete")
        self.assertEqual(len(self.planner.state()["plans"]), 0)

    def test_start_deferred_dirty_repo_fails(self):
        (self.repo / "untracked.txt").write_text("dirty", encoding="utf-8")
        plan_id, reason = self.planner.start_deferred(self.project, self.snapshot, "cmd1", self.handoff)
        self.assertIsNone(plan_id)
        self.assertEqual(reason, "planner requires a clean repository")
        self.assertEqual(len(self.planner.state()["plans"]), 0)

    def test_start_deferred_head_moved_fails(self):
        bad_handoff = dict(self.handoff, reviewed_head="0000000000000000000000000000000000000000")
        plan_id, reason = self.planner.start_deferred(self.project, self.snapshot, "cmd1", bad_handoff)
        self.assertIsNone(plan_id)
        self.assertEqual(reason, "repository moved since review")
        self.assertEqual(len(self.planner.state()["plans"]), 0)

    def test_start_deferred_branch_mismatch_fails(self):
        bad_handoff = dict(self.handoff, reviewed_branch="nonexistent-branch")
        plan_id, reason = self.planner.start_deferred(self.project, self.snapshot, "cmd1", bad_handoff)
        self.assertIsNone(plan_id)
        self.assertEqual(reason, "repository moved since review")
        self.assertEqual(len(self.planner.state()["plans"]), 0)

    def test_start_deferred_spec_digest_mismatch_fails(self):
        bad_handoff = dict(self.handoff, staged_spec_sha256="deadbeefdeadbeef")
        plan_id, reason = self.planner.start_deferred(self.project, self.snapshot, "cmd1", bad_handoff)
        self.assertIsNone(plan_id)
        self.assertEqual(reason, "staged spec changed since handoff")
        self.assertEqual(len(self.planner.state()["plans"]), 0)

    def test_deferred_apply_crlf_change_to_next_md_fails(self):
        record = {
            "plan_id": "ai_plan:cmd1",
            "command_id": "cmd1",
            "project_id": "p1",
            "task_id": "P11b",
            "repo_path": str(self.repo),
            "branch": self.handoff["reviewed_branch"],
            "head": self.head,
            "status_hash": read_repository_truth(self.repo).status_hash,
            "deferred": True,
            "predecessor_task_id": "P11x",
            "predecessor_next_sha256": sha256_bytes((self.repo / "agent" / "next.md").read_bytes()),
            "staged_spec_path": self.spec_path,
            "staged_spec_sha256": self.handoff["staged_spec_sha256"],
        }
        plan = {
            "task_id": "P11b", "summary": "s", "implementation_steps": ["i"],
            "interfaces": ["c"], "validation": ["v"], "risks": ["r"], "out_of_scope": ["o"],
        }
        # Simulate CRLF change on next.md
        orig_bytes = (self.repo / "agent" / "next.md").read_bytes()
        (self.repo / "agent" / "next.md").write_bytes(orig_bytes.replace(b"\n", b"\r\n"))

        self.planner._save_state({"version": 1, "plans": {"ai_plan:cmd1": copy.deepcopy(record)}})
        self.planner._apply_deferred_plan("ai_plan:cmd1", record, plan, "Approved")
        state = self.planner.state()["plans"].get("ai_plan:cmd1")
        self.assertEqual(state["state"], "failed")
        self.assertIn("agent/next.md changed during planning", state["reason"])

    def test_deferred_apply_staged_spec_edit_fails(self):
        record = {
            "plan_id": "ai_plan:cmd1",
            "command_id": "cmd1",
            "project_id": "p1",
            "task_id": "P11b",
            "repo_path": str(self.repo),
            "branch": self.handoff["reviewed_branch"],
            "head": self.head,
            "status_hash": read_repository_truth(self.repo).status_hash,
            "deferred": True,
            "predecessor_task_id": "P11x",
            "predecessor_next_sha256": sha256_bytes((self.repo / "agent" / "next.md").read_bytes()),
            "staged_spec_path": self.spec_path,
            "staged_spec_sha256": self.handoff["staged_spec_sha256"],
        }
        plan = {
            "task_id": "P11b", "summary": "s", "implementation_steps": ["i"],
            "interfaces": ["c"], "validation": ["v"], "risks": ["r"], "out_of_scope": ["o"],
        }
        # Modify staged spec
        (self.repo / self.spec_path).write_bytes(b"# modified spec")
        self.planner._save_state({"version": 1, "plans": {"ai_plan:cmd1": copy.deepcopy(record)}})
        self.planner._apply_deferred_plan("ai_plan:cmd1", record, plan, "Approved")
        state = self.planner.state()["plans"].get("ai_plan:cmd1")
        self.assertEqual(state["state"], "failed")
        self.assertIn("staged spec changed during planning", state["reason"])

    def test_deferred_apply_pre_write_truth_race_fails(self):
        truth = read_repository_truth(self.repo)
        record = {
            "plan_id": "ai_plan:cmd1",
            "command_id": "cmd1",
            "project_id": "p1",
            "task_id": "P11b",
            "repo_path": str(self.repo),
            "branch": self.handoff["reviewed_branch"],
            "head": self.head,
            "status_hash": truth.status_hash,
            "deferred": True,
            "predecessor_task_id": "P11x",
            "predecessor_next_sha256": sha256_bytes((self.repo / "agent" / "next.md").read_bytes()),
            "staged_spec_path": self.spec_path,
            "staged_spec_sha256": self.handoff["staged_spec_sha256"],
        }
        plan = {
            "task_id": "P11b", "summary": "s", "implementation_steps": ["i"],
            "interfaces": ["c"], "validation": ["v"], "risks": ["r"], "out_of_scope": ["o"],
        }
        orig_next_bytes = (self.repo / "agent" / "next.md").read_bytes()

        # Variant 1: status_hash / untracked file race
        orig_read_truth = read_repository_truth
        call_count = [0]
        def racing_read_truth(path):
            call_count[0] += 1
            res = orig_read_truth(path)
            if call_count[0] == 1:
                # First call is the pre_write_truth in _apply_deferred_plan
                # Create untracked file to change status_hash
                (self.repo / "race.txt").write_text("race", encoding="utf-8")
                return orig_read_truth(path)
            return res

        self.planner._save_state({"version": 1, "plans": {"ai_plan:cmd1": copy.deepcopy(record)}})
        with patch("dev_orchestrator.core.ai_planner.read_repository_truth", side_effect=racing_read_truth):
            self.planner._apply_deferred_plan("ai_plan:cmd1", record, plan, "Approved")
        state = self.planner.state()["plans"].get("ai_plan:cmd1")
        self.assertEqual(state["state"], "failed")
        self.assertIn("repository changed during planning", state["reason"])
        self.assertEqual((self.repo / "agent" / "next.md").read_bytes(), orig_next_bytes)

        # Variant 2: branch race
        (self.repo / "race.txt").unlink()
        record["status_hash"] = read_repository_truth(self.repo).status_hash
        call_count[0] = 0
        def branch_race_read_truth(path):
            res = orig_read_truth(path)
            from dev_orchestrator.core.repository import RepositoryTruth
            return RepositoryTruth(res.valid, "other-branch", res.head, res.dirty, res.status_hash, res.dirty_entries)

        self.planner._save_state({"version": 1, "plans": {"ai_plan:cmd2": copy.deepcopy(record)}})
        with patch("dev_orchestrator.core.ai_planner.read_repository_truth", side_effect=branch_race_read_truth):
            self.planner._apply_deferred_plan("ai_plan:cmd2", record, plan, "Approved")
        state2 = self.planner.state()["plans"].get("ai_plan:cmd2")
        self.assertEqual(state2["state"], "failed")
        self.assertIn("repository changed during planning", state2["reason"])
        self.assertEqual((self.repo / "agent" / "next.md").read_bytes(), orig_next_bytes)

    def test_deferred_recovery_without_prohibited_git_commands(self):
        truth = read_repository_truth(self.repo)
        record = {
            "plan_id": "ai_plan:cmd1",
            "command_id": "cmd1",
            "project_id": "p1",
            "task_id": "P11b",
            "repo_path": str(self.repo),
            "branch": self.handoff["reviewed_branch"],
            "head": self.head,
            "status_hash": truth.status_hash,
            "deferred": True,
            "predecessor_task_id": "P11x",
            "predecessor_next_sha256": sha256_bytes((self.repo / "agent" / "next.md").read_bytes()),
            "staged_spec_path": self.spec_path,
            "staged_spec_sha256": self.handoff["staged_spec_sha256"],
        }
        plan = {
            "task_id": "P11b", "summary": "s", "implementation_steps": ["i"],
            "interfaces": ["c"], "validation": ["v"], "risks": ["r"], "out_of_scope": ["o"],
        }
        orig_bytes = (self.repo / "agent" / "next.md").read_bytes()

        recorded_subprocesses = []

        def failing_commit(repo, task_id):
            # Stage the file like real commit_plan does
            subprocess.run(["git", "-C", str(repo), "add", "--", "agent/next.md"], check=True)
            raise RuntimeError("simulated commit failure")

        orig_sp_run = subprocess.run
        def logged_sp_run(*args, **kwargs):
            if args:
                recorded_subprocesses.append(list(args[0]))
            return orig_sp_run(*args, **kwargs)

        self.planner._save_state({"version": 1, "plans": {"ai_plan:cmd1": copy.deepcopy(record)}})
        with patch.object(self.planner, "_commit_plan", side_effect=failing_commit), \
             patch("subprocess.run", side_effect=logged_sp_run):
            self.planner._apply_deferred_plan("ai_plan:cmd1", record, plan, "Approved")

        state = self.planner.state()["plans"].get("ai_plan:cmd1")
        self.assertEqual(state["state"], "failed")
        self.assertIn("plan apply failed: simulated commit failure", state["reason"])

        # Check no reset, restore, or checkout was called
        for cmd in recorded_subprocesses:
            self.assertNotIn("reset", cmd)
            self.assertNotIn("restore", cmd)
            self.assertNotIn("checkout", cmd)

        # Check agent/next.md restored exactly
        self.assertEqual((self.repo / "agent" / "next.md").read_bytes(), orig_bytes)
        restored_truth = read_repository_truth(self.repo)
        self.assertTrue(restored_truth.valid)
        self.assertFalse(restored_truth.dirty)
        self.assertEqual(restored_truth.head, self.head)
        self.assertEqual(restored_truth.status_hash, truth.status_hash)


class TestStagedHandoffEndToEnd(unittest.TestCase):
    def test_end_to_end_and_restart_idempotency(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "repo"
            head = make_git_repo(repo, "P11x")
            spec_path, spec_bytes = setup_staged_spec(repo, "P11b")
            setup_roadmap(repo, "P11x", "P11b", spec_path)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "setup staged"], check=True, capture_output=True)
            reviewed_head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

            runtime = base / "runtime"
            runtime.mkdir(parents=True, exist_ok=True)
            port = FakeHandoffPort("P11b")
            planner = AIPlannerCoordinator(runtime, port)
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
                    },
                    "ai_roles": {"planner": {"enabled": True}},
                }]
            }
            config_path.write_text(json.dumps(config_data), encoding="utf-8")

            # Write websol-decisions.json for P11x COMPLETE
            repo_truth = read_repository_truth(repo)
            record = {
                "project_id": "p1", "request_id": "worker_done:p1:r1",
                "disposition": "apply", "decision": "next", "next_action": "next_task",
                "task_id": "P11x", "stage_id": None,
                "branch": repo_truth.branch, "head": reviewed_head,
                "role": "reviewer", "event": "worker_done",
                "consumed_at": "2026-09-05T00:00:00+00:00",
            }
            (runtime / "websol-decisions.json").write_text(
                json.dumps({"version": 1, "decisions": {"worker_done:p1:r1": record}}), encoding="utf-8"
            )

            executor = TransitionExecutor(runtime, backend_overrides={"agy": FakeBackend("agy")})
            control = ControlCommandCoordinator(runtime, planner)

            summary = {
                "projects": [{
                    "project_id": "p1",
                    "state": "IDLE",
                    "next_status": "**COMPLETE**",
                    "telemetry": {"task_id": "P11x"},
                    "git": {"head": reviewed_head},
                }]
            }

            # 1. Executor tick records staged handoff
            executor.advance(summary, config_path)
            exec_row = executor.state()["executions"]["worker_done:p1:r1"]
            self.assertEqual(exec_row["state"], "handoff")
            self.assertEqual(exec_row["next_task_id"], "P11b")
            self.assertEqual(exec_row["staged_successor"], "P11b")

            # Repository state before plan start: unchanged
            cur_head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            self.assertEqual(cur_head, reviewed_head)
            self.assertEqual(
                (repo / "agent" / "next.md").read_bytes(),
                b"# P11x Predecessor Task\nStatus: **COMPLETE**\n\nGoal: completed.\n",
            )

            # 2. Control tick resumes handoff and starts deferred planner
            outcomes = control.advance(config_path, summary, executor)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0]["lifecycle_action"], "plan")
            self.assertTrue(executor.state()["executions"]["worker_done:p1:r1"]["handoff_consumed"])

            # Wait for planner thread to complete
            plan_id = outcomes[0]["plan_id"]
            for _ in range(50):
                plan_state = planner.state()["plans"].get(plan_id, {})
                if plan_state.get("state") == "ready":
                    break
                time.sleep(0.1)

            self.assertEqual(plan_state.get("state"), "ready")

            # 3. Verify exactly one commit produced
            new_head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            self.assertNotEqual(new_head, reviewed_head)
            rev_list = subprocess.check_output(
                ["git", "-C", str(repo), "rev-list", f"{reviewed_head}..HEAD"], text=True
            ).strip().splitlines()
            self.assertEqual(len(rev_list), 1)

            commit_msg = subprocess.check_output(
                ["git", "-C", str(repo), "log", "-1", "--pretty=%B"], text=True
            ).strip()
            self.assertEqual(commit_msg, "plan(P11b): freeze executable design")

            diff_files = subprocess.check_output(
                ["git", "-C", str(repo), "diff", "--name-only", f"{reviewed_head}", "HEAD"], text=True
            ).strip().splitlines()
            self.assertEqual(diff_files, ["agent/next.md"])

            new_next = (repo / "agent" / "next.md").read_text(encoding="utf-8")
            self.assertTrue(new_next.startswith("# P11b Successor Task"))
            self.assertIn("Status: **READY_TO_RUN**", new_next)
            self.assertIn("## Approved executable design", new_next)

            # Staged spec unchanged
            self.assertEqual((repo / spec_path).read_bytes(), spec_bytes)

            # 4. Restart & Idempotency: Re-create executor & coordinators and tick again
            new_planner = AIPlannerCoordinator(runtime, port)
            new_executor = TransitionExecutor(runtime, backend_overrides={"agy": FakeBackend("agy")})
            new_control = ControlCommandCoordinator(runtime, new_planner)

            post_summary = {
                "projects": [{
                    "project_id": "p1",
                    "state": "IDLE",
                    "next_status": "**READY_TO_RUN**",
                    "telemetry": {"task_id": "P11b"},
                    "git": {"head": new_head},
                }]
            }

            initial_port_call_count = len(port.requests)
            new_control.advance(config_path, post_summary, new_executor)

            # No new port calls, no new commits
            self.assertEqual(len(port.requests), initial_port_call_count)
            head_after_restart = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            self.assertEqual(head_after_restart, new_head)
            self.assertEqual(len(new_planner.state()["plans"]), 1)


if __name__ == "__main__":
    unittest.main()
