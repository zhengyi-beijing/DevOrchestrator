import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.bridge.store import BrowserBridgeStore
from dev_orchestrator.core.dispatcher import DISPATCHER_STATE_FILE, dispatch_worker_done_events
from dev_orchestrator.storage.json_store import read_json, write_json
from tests_py.test_worker_done_dispatcher import git, make_repo, snap


def binding(binding_id: str, *, transport="browser_bridge", adapter="chatgpt_web") -> dict:
    return {"transport": transport, "adapter": adapter, "binding_id": binding_id}


class DispatcherSliceAcceptanceTests(unittest.TestCase):
    def test_fail_closed_for_unready_malformed_nonbrowser_and_invalid_repo(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; make_repo(repo)
            runtime = base / "runtime"; store = BrowserBridgeStore(runtime / "bridge")
            cases = [
                snap("unready", repo, "conv-U", "run-u"),
                snap("nonbrowser", repo, "conv-N", "run-n"),
                snap("missing", repo, None, "run-m"),
                snap("invalid-repo", base / "missing", "conv-I", "run-i"),
            ]
            cases[0]["orchestration_ready"] = None
            cases[1]["conversation_binding"]["transport"] = "other"
            self.assertEqual(dispatch_worker_done_events({"projects": cases}, store, runtime), [])

    def test_stage_only_identity_and_old_occurrence_never_replays(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; make_repo(repo)
            runtime = base / "runtime"; store = BrowserBridgeStore(runtime / "bridge")
            stage_only = snap("alpha", repo, "conv-A", "run-1")
            stage_only["telemetry"]["task_id"] = None
            stage_only["stage_id"] = "P4.3"
            first = dispatch_worker_done_events({"projects": [stage_only]}, store, runtime)
            self.assertEqual(len(first), 1)
            claim1 = store.claim("chatgpt_web", "conv-A")
            self.assertIsNone(claim1.task_id)
            self.assertEqual(claim1.stage_id, "P4.3")
            store.respond("chatgpt_web", "conv-A", claim1.request_id, claim1.nonce, claim1.claim_token, "reviewed")

            newer = snap("alpha", repo, "conv-A", "run-2")
            self.assertEqual(len(dispatch_worker_done_events({"projects": [newer]}, store, runtime)), 1)
            claim2 = store.claim("chatgpt_web", "conv-A")
            store.respond("chatgpt_web", "conv-A", claim2.request_id, claim2.nonce, claim2.claim_token, "reviewed")
            self.assertEqual(dispatch_worker_done_events({"projects": [stage_only]}, store, runtime), [])
            self.assertIsNone(store.claim("chatgpt_web", "conv-A"))

    def test_crash_window_reuses_prepared_identity_after_head_changes(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; original_head = make_repo(repo)
            runtime = base / "runtime"; store = BrowserBridgeStore(runtime / "bridge")
            summary = {"projects": [snap("alpha", repo, "conv-A", "run-crash")]}
            first = dispatch_worker_done_events(summary, store, runtime)
            self.assertEqual(len(first), 1)

            state_path = runtime / DISPATCHER_STATE_FILE
            state = read_json(state_path)
            occurrence = state["worker_done"]["alpha"]["occurrences"]["run-crash"]
            occurrence["state"] = "prepared"
            write_json(state_path, state, indent=2)

            (repo / "seed.txt").write_text("changed after crash", encoding="utf-8")
            git(repo, "add", "."); git(repo, "commit", "-m", "post-crash-head")
            self.assertNotEqual(original_head, git(repo, "rev-parse", "HEAD"))

            recovered = dispatch_worker_done_events(summary, BrowserBridgeStore(runtime / "bridge"), runtime)
            self.assertEqual(len(recovered), 1)
            claim = BrowserBridgeStore(runtime / "bridge").claim("chatgpt_web", "conv-A")
            self.assertEqual(claim.head, original_head)
            final = read_json(state_path)["worker_done"]["alpha"]["occurrences"]["run-crash"]
            self.assertEqual(final["state"], "submitted")


if __name__ == "__main__":
    unittest.main()
