import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dev_orchestrator.bridge.store import BrowserBridgeStore, BridgeConflictError
from dev_orchestrator.core.websol import WebSolEvent, WebSolRequest, WebSolRole


def req(project: str, request_id: str, nonce: str) -> WebSolRequest:
    return WebSolRequest(
        project_id=project, request_id=request_id, task_id="T1", stage_id="S1",
        branch="main", head="a" * 40, role=WebSolRole.REVIEWER,
        event=WebSolEvent.REVIEW_REQUIRED, nonce=nonce,
    )
class BrowserBridgeStoreTests(unittest.TestCase):
    def test_two_bindings_never_cross_route(self):
        with tempfile.TemporaryDirectory() as td:
            store = BrowserBridgeStore(Path(td), lease_seconds=30)
            store.submit("chatgpt_web", "conv-A", req("alpha", "r-a", "n-a"), "prompt A")
            store.submit("chatgpt_web", "conv-B", req("beta", "r-b", "n-b"), "prompt B")
            a = store.claim("chatgpt_web", "conv-A")
            b = store.claim("chatgpt_web", "conv-B")
            self.assertEqual(a.project_id, "alpha")
            self.assertEqual(a.prompt, "prompt A")
            self.assertEqual(b.project_id, "beta")
            self.assertEqual(b.prompt, "prompt B")

    def test_submit_is_idempotent_but_request_id_nonce_conflict_fails(self):
        with tempfile.TemporaryDirectory() as td:
            store = BrowserBridgeStore(Path(td))
            first = store.submit("chatgpt_web", "conv-A", req("alpha", "r1", "n1"), "p")
            again = store.submit("chatgpt_web", "conv-A", req("alpha", "r1", "n1"), "p")
            self.assertEqual(first.request_id, again.request_id)
            with self.assertRaises(BridgeConflictError):
                store.submit("chatgpt_web", "conv-A", req("alpha", "r1", "DIFFERENT"), "p")
    def test_expired_claim_is_reclaimable(self):
        with tempfile.TemporaryDirectory() as td:
            base = datetime(2026, 9, 3, tzinfo=timezone.utc)
            store = BrowserBridgeStore(Path(td), lease_seconds=10)
            store.submit("chatgpt_web", "conv-A", req("alpha", "r2", "n2"), "p", now=base)
            first = store.claim("chatgpt_web", "conv-A", now=base)
            self.assertIsNone(store.claim("chatgpt_web", "conv-A", now=base + timedelta(seconds=5)))
            second = store.claim("chatgpt_web", "conv-A", now=base + timedelta(seconds=11))
            self.assertEqual(second.request_id, first.request_id)
            self.assertNotEqual(second.claim_token, first.claim_token)

    def test_response_requires_exact_binding_nonce_and_claim_token(self):
        with tempfile.TemporaryDirectory() as td:
            store = BrowserBridgeStore(Path(td))
            store.submit("chatgpt_web", "conv-A", req("alpha", "r3", "n3"), "p")
            claim = store.claim("chatgpt_web", "conv-A")
            with self.assertRaises(BridgeConflictError):
                store.respond("chatgpt_web", "conv-B", "r3", "n3", claim.claim_token, "bad")
            with self.assertRaises(BridgeConflictError):
                store.respond("chatgpt_web", "conv-A", "r3", "WRONG", claim.claim_token, "bad")
            with self.assertRaises(BridgeConflictError):
                store.respond("chatgpt_web", "conv-A", "r3", "n3", "WRONG", "bad")
            stored = store.respond("chatgpt_web", "conv-A", "r3", "n3", claim.claim_token, "ok")
            self.assertEqual(stored.response_text, "ok")
            self.assertEqual(store.get_response("r3", "n3").response_text, "ok")

    def test_pending_request_survives_store_reopen(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = BrowserBridgeStore(root)
            first.submit("chatgpt_web", "conv-A", req("alpha", "r4", "n4"), "persisted")
            reopened = BrowserBridgeStore(root)
            claim = reopened.claim("chatgpt_web", "conv-A")
            self.assertEqual(claim.request_id, "r4")
            self.assertEqual(claim.prompt, "persisted")
