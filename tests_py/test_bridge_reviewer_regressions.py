import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dev_orchestrator.bridge.prompt import render_websol_prompt
from dev_orchestrator.bridge.store import BrowserBridgeStore, BridgeConflictError
from dev_orchestrator.core.websol import WebSolEvent, WebSolRequest, WebSolRole


def make_request(project="alpha", request_id="same-id", nonce="same-nonce"):
    return WebSolRequest(
        project_id=project, request_id=request_id, task_id="T1", stage_id="S1",
        branch="main", head="d" * 40, role=WebSolRole.REVIEWER,
        event=WebSolEvent.REVIEW_REQUIRED, nonce=nonce,
    )


class BridgeReviewerRegressions(unittest.TestCase):
    def test_one_request_id_cannot_exist_in_two_bindings(self):
        with tempfile.TemporaryDirectory() as td:
            store = BrowserBridgeStore(Path(td))
            store.submit("chatgpt_web", "conv-A", make_request("alpha"), "prompt A")
            with self.assertRaises(BridgeConflictError):
                store.submit("chatgpt_web", "conv-B", make_request("beta"), "prompt B")
    def test_claim_can_be_renewed_while_chatgpt_is_still_answering(self):
        with tempfile.TemporaryDirectory() as td:
            base = datetime(2026, 9, 3, tzinfo=timezone.utc)
            store = BrowserBridgeStore(Path(td), lease_seconds=30)
            store.submit("chatgpt_web", "conv-A", make_request(), "prompt", now=base)
            claim = store.claim("chatgpt_web", "conv-A", now=base)
            store.renew("chatgpt_web", "conv-A", claim.request_id, claim.nonce,
                        claim.claim_token, now=base + timedelta(seconds=20))
            self.assertIsNone(store.claim("chatgpt_web", "conv-A", now=base + timedelta(seconds=35)))
            reclaimed = store.claim("chatgpt_web", "conv-A", now=base + timedelta(seconds=51))
            self.assertEqual(reclaimed.request_id, claim.request_id)
            self.assertNotEqual(reclaimed.claim_token, claim.claim_token)

    def test_prompt_requires_the_full_structured_response_schema(self):
        text = render_websol_prompt(make_request(), "Review evidence")
        for field in ("project_id", "request_id", "task_id", "stage_id", "branch", "head",
                      "role", "event", "nonce", "decision", "next_action"):
            self.assertIn('"' + field + '"', text)
        for value in ("next", "remediate", "retry", "owner_gate", "stop",
                      "continue_current_stage", "next_task", "next_stage"):
            self.assertIn(value, text)

    def test_userscript_renews_claim_during_long_response_wait(self):
        source = (Path(__file__).resolve().parents[1] / "browser" / "chatgpt-web-adapter.user.js").read_text(encoding="utf-8")
        self.assertIn("/v1/renew", source)
        self.assertGreaterEqual(source.count("renewClaim("), 2)


if __name__ == "__main__":
    unittest.main()
