import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.bridge.store import BrowserBridgeStore
from dev_orchestrator.core.dispatcher import DISPATCHER_STATE_FILE, dispatch_worker_done_events


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], check=True,
        text=True, capture_output=True,
    )
    return proc.stdout.strip()


def make_repo(root: Path) -> str:
    root.mkdir(parents=True)
    git(root, "init")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    (root / "seed.txt").write_text("one", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "seed")
    return git(root, "rev-parse", "HEAD")

def snap(project_id: str, repo: Path, binding_id: str | None, run_id: str, *, state="completed") -> dict:
    binding = None if binding_id is None else {
        "transport": "browser_bridge",
        "adapter": "chatgpt_web",
        "binding_id": binding_id,
    }
    return {
        "id": project_id,
        "project_id": project_id,
        "repo_path": str(repo),
        "conversation_binding": binding,
        "orchestration_ready": binding is not None,
        "state": "WAITING_REVIEW" if state == "completed" else "WORKER_RUNNING",
        "git": {"branch": "stale", "head": "stale"},
        "worker": {
            "kind": "task", "state": state, "exit_code": 0,
            "updated_at": "2026-09-04T00:00:00+00:00",
            "command": "dsh --profile headless next",
        },
        "telemetry": {"run_id": run_id, "task_id": "P4.3.4"},
        "next_title": "P4.3.4",
        "next_status": "awaiting review",
    }

class WorkerDoneDispatcherTests(unittest.TestCase):
    def test_completed_worker_enqueues_exactly_one_reviewer_request(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; make_repo(repo)
            runtime = base / "runtime"; store = BrowserBridgeStore(runtime / "bridge")
            summary = {"projects": [snap("alpha", repo, "conv-A", "run-1")]}

            first = dispatch_worker_done_events(summary, store, runtime)
            second = dispatch_worker_done_events(summary, store, runtime)

            self.assertEqual(len(first), 1)
            self.assertEqual(second, [])
            claim = store.claim("chatgpt_web", "conv-A")
            self.assertEqual(claim.project_id, "alpha")
            self.assertEqual(claim.event, "worker_done")
            self.assertEqual(claim.role, "reviewer")
            self.assertEqual(claim.task_id, "P4.3.4")
            self.assertIn("WORKER_DONE", claim.prompt)

    def test_restart_does_not_replay_same_worker_occurrence(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; make_repo(repo)
            runtime = base / "runtime"; summary = {"projects": [snap("alpha", repo, "conv-A", "run-1")]}
            first_store = BrowserBridgeStore(runtime / "bridge")
            first = dispatch_worker_done_events(summary, first_store, runtime)
            self.assertEqual(len(first), 1)
            claim = first_store.claim("chatgpt_web", "conv-A")
            first_store.respond("chatgpt_web", "conv-A", claim.request_id, claim.nonce, claim.claim_token, "done")

            reopened = BrowserBridgeStore(runtime / "bridge")
            again = dispatch_worker_done_events(summary, reopened, runtime)
            self.assertEqual(again, [])
            self.assertIsNone(reopened.claim("chatgpt_web", "conv-A"))

    def test_consumed_response_tombstone_prevents_replay_after_dispatch_and_queue_loss(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; make_repo(repo)
            runtime = base / "runtime"; summary = {"projects": [snap("alpha", repo, "conv-A", "run-1")]}
            first_store = BrowserBridgeStore(runtime / "bridge")
            first = dispatch_worker_done_events(summary, first_store, runtime)
            self.assertEqual(len(first), 1)
            request_id = first[0].request_id
            (runtime / "websol-decisions.json").write_text(json.dumps({
                "version": 1,
                "decisions": {
                    request_id: {
                        "project_id": "alpha",
                        "request_id": request_id,
                        "disposition": "apply",
                        "next_action": "next_task",
                    }
                },
            }), encoding="utf-8")
            (runtime / DISPATCHER_STATE_FILE).unlink()

            pruned_store = BrowserBridgeStore(runtime / "bridge-after-prune")
            again = dispatch_worker_done_events(summary, pruned_store, runtime)
            self.assertEqual(again, [])
            self.assertIsNone(pruned_store.claim("chatgpt_web", "conv-A"))

    def test_two_projects_route_to_distinct_bindings(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); runtime = base / "runtime"
            repo_a, repo_b = base / "a", base / "b"
            make_repo(repo_a); make_repo(repo_b)
            store = BrowserBridgeStore(runtime / "bridge")
            summary = {"projects": [
                snap("alpha", repo_a, "conv-A", "run-a"),
                snap("beta", repo_b, "conv-B", "run-b"),
            ]}
            dispatched = dispatch_worker_done_events(summary, store, runtime)
            self.assertEqual(len(dispatched), 2)
            self.assertEqual(store.claim("chatgpt_web", "conv-A").project_id, "alpha")
            self.assertEqual(store.claim("chatgpt_web", "conv-B").project_id, "beta")

    def test_missing_binding_and_noncompleted_worker_emit_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; make_repo(repo)
            runtime = base / "runtime"; store = BrowserBridgeStore(runtime / "bridge")
            summary = {"projects": [
                snap("no-binding", repo, None, "run-1"),
                snap("running", repo, "conv-R", "run-2", state="running"),
            ]}
            self.assertEqual(dispatch_worker_done_events(summary, store, runtime), [])
            self.assertIsNone(store.claim("chatgpt_web", "conv-R"))

    def test_request_uses_fresh_repository_truth_not_monitor_git(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; old_head = make_repo(repo)
            (repo / "seed.txt").write_text("two", encoding="utf-8")
            git(repo, "add", "."); git(repo, "commit", "-m", "new")
            fresh_head = git(repo, "rev-parse", "HEAD")
            self.assertNotEqual(old_head, fresh_head)
            runtime = base / "runtime"; store = BrowserBridgeStore(runtime / "bridge")
            summary = {"projects": [snap("alpha", repo, "conv-A", "run-fresh")]}
            dispatch_worker_done_events(summary, store, runtime)
            claim = store.claim("chatgpt_web", "conv-A")
            self.assertEqual(claim.head, fresh_head)
            self.assertEqual(claim.branch, git(repo, "rev-parse", "--abbrev-ref", "HEAD"))


if __name__ == "__main__":
    unittest.main()
