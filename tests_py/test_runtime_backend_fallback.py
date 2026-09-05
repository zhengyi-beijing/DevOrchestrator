import json
import tempfile
import time
import unittest
from pathlib import Path

from dev_orchestrator.agents.models import AgentResult, AgentRunState
from dev_orchestrator.core.transition_executor import TransitionExecutor
from tests_py.test_transition_executor import FakeBackend, make_repo, ready_summary


class QuotaBackend(FakeBackend):
    async def collect(self, run_id: str) -> AgentResult:
        return AgentResult(
            run_id,
            self.backend_id,
            AgentRunState.FAILED,
            1,
            stderr="Error: Individual quota reached. Resets in 1h.",
        )


class DirtyQuotaBackend(QuotaBackend):
    async def collect(self, run_id: str) -> AgentResult:
        request = self.started[-1]
        (Path(request.working_directory) / "provider-dirty.txt").write_text(
            "changed\n", encoding="utf-8"
        )
        return await super().collect(run_id)


def write_config(path: Path, repo: Path) -> None:
    execution = {
        "enabled": True,
        "owner_authorized": True,
        "allowed_next_actions": ["next_task"],
        "preferred_backends": ["agy", "dsh"],
        "backends": {"agy": {}, "dsh": {}},
        "bootstrap": {"request_id": "owner-p1", "task_id": "P1"},
    }
    path.write_text(
        json.dumps({"projects": [{
            "project_id": "p1", "repo_path": str(repo), "execution": execution,
        }]}),
        encoding="utf-8",
    )


def wait_terminal(executor: TransitionExecutor) -> dict:
    deadline = time.time() + 4
    while time.time() < deadline:
        record = executor.state()["executions"].get("owner-p1")
        if isinstance(record, dict) and record.get("state") in {"completed", "failed"}:
            thread = executor._threads.get("owner-p1")
            if thread is not None:
                thread.join(timeout=1.0)
            return record
        time.sleep(0.02)
    raise AssertionError("execution did not become terminal")


class RuntimeFallbackTests(unittest.TestCase):
    def test_quota_failure_falls_back_when_repository_is_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo, "P1"); config = base / "projects.json"; write_config(config, repo)
            agy = QuotaBackend("agy", terminal_state=AgentRunState.FAILED, exit_code=1)
            dsh = FakeBackend("dsh")
            executor = TransitionExecutor(
                runtime, backend_overrides={"agy": agy, "dsh": dsh}
            )
            launches = executor.advance(ready_summary(repo, "P1"), config)
            self.assertEqual(len(launches), 1)
            record = wait_terminal(executor)
            self.assertEqual(record["state"], "completed")
            self.assertEqual(record["backend_id"], "dsh")
            self.assertEqual(record["fallback_from_backend"], "agy")
            self.assertEqual(len(agy.started), 1)
            self.assertEqual(len(dsh.started), 1)

    def test_quota_failure_does_not_fallback_after_repository_change(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo, "P1"); config = base / "projects.json"; write_config(config, repo)
            agy = DirtyQuotaBackend("agy", terminal_state=AgentRunState.FAILED, exit_code=1)
            dsh = FakeBackend("dsh")
            executor = TransitionExecutor(
                runtime, backend_overrides={"agy": agy, "dsh": dsh}
            )
            executor.advance(ready_summary(repo, "P1"), config)
            record = wait_terminal(executor)
            self.assertEqual(record["state"], "failed")
            self.assertIn("repository changed", record.get("reason", ""))
            self.assertEqual(len(dsh.started), 0)


if __name__ == "__main__":
    unittest.main()
