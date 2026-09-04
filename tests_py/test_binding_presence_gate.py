import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dev_orchestrator.bridge.store import BrowserBridgeStore
from dev_orchestrator.core.dispatcher import DISPATCHER_STATE_FILE, dispatch_worker_done_events
from dev_orchestrator.storage.json_store import read_json
from tests_py.test_worker_done_dispatcher import make_repo, snap


class BindingPresenceGateTests(unittest.TestCase):
    def test_empty_claim_poll_registers_presence_that_expires(self):
        with tempfile.TemporaryDirectory() as td:
            base = datetime(2026, 9, 4, tzinfo=timezone.utc)
            store = BrowserBridgeStore(Path(td), require_live_binding=True,
                                       binding_presence_seconds=30)
            self.assertIsNone(store.claim("chatgpt_web", "conv-A", now=base))
            self.assertEqual(store.binding_status("chatgpt_web", "conv-A", now=base)["state"], "bound")
            self.assertEqual(store.binding_status("chatgpt_web", "conv-A", now=base + timedelta(seconds=31))["state"], "unbound")

    def test_unbound_occurrence_stays_prepared_then_resumes_same_identity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); repo = root / "repo"; make_repo(repo)
            runtime = root / "runtime"
            store = BrowserBridgeStore(runtime / "bridge", require_live_binding=True)
            summary = {"projects": [snap("labdemo", repo, "conv-L", "run-1")]}
            self.assertEqual(dispatch_worker_done_events(summary, store, runtime), [])
            occurrence = read_json(runtime / DISPATCHER_STATE_FILE)["worker_done"]["labdemo"]["occurrences"]["run-1"]
            frozen = {k: occurrence[k] for k in ("request_id", "nonce", "branch", "head", "prompt")}
            self.assertEqual(occurrence["state"], "prepared")
            self.assertEqual(occurrence["delivery_state"], "unbound")
            self.assertIsNone(store.claim("chatgpt_web", "conv-L"))
            sent = dispatch_worker_done_events(summary, store, runtime)
            self.assertEqual(len(sent), 1)
            claim = store.claim("chatgpt_web", "conv-L")
            self.assertIsNotNone(claim)
            for key, value in frozen.items():
                observed = claim.request_id if key == "request_id" else getattr(claim, key)
                self.assertEqual(observed, value)

    def test_missing_binding_is_blocked_and_can_rebind_without_new_request(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); repo = root / "repo"; make_repo(repo)
            runtime = root / "runtime"
            store = BrowserBridgeStore(runtime / "bridge", require_live_binding=True)
            blocked = snap("labdemo", repo, None, "run-2")
            self.assertEqual(dispatch_worker_done_events({"projects": [blocked]}, store, runtime), [])
            first = read_json(runtime / DISPATCHER_STATE_FILE)["worker_done"]["labdemo"]["occurrences"]["run-2"]
            self.assertEqual(first["delivery_state"], "unbound")
            request_id = first["request_id"]
            rebound = snap("labdemo", repo, "conv-new", "run-2")
            self.assertIsNone(store.claim("chatgpt_web", "conv-new"))
            sent = dispatch_worker_done_events({"projects": [rebound]}, store, runtime)
            self.assertEqual(len(sent), 1)
            claim = store.claim("chatgpt_web", "conv-new")
            self.assertEqual(claim.request_id, request_id)


if __name__ == "__main__":
    unittest.main()