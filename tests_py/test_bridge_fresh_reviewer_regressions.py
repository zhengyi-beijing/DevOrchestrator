import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dev_orchestrator.bridge.store import BrowserBridgeStore, BridgeConflictError
from dev_orchestrator.core.websol import WebSolEvent, WebSolRequest, WebSolRole


def request():
    return WebSolRequest(
        project_id="alpha", request_id="lease-expiry", task_id="T1", stage_id="S1",
        branch="main", head="e" * 40, role=WebSolRole.REVIEWER,
        event=WebSolEvent.REVIEW_REQUIRED, nonce="nonce-expiry",
    )


class BridgeFreshReviewerRegressions(unittest.TestCase):
    def test_expired_claim_cannot_submit_response(self):
        with tempfile.TemporaryDirectory() as td:
            base = datetime(2026, 9, 3, tzinfo=timezone.utc)
            store = BrowserBridgeStore(Path(td), lease_seconds=10)
            store.submit("chatgpt_web", "conv-A", request(), "prompt", now=base)
            claim = store.claim("chatgpt_web", "conv-A", now=base)
            with self.assertRaises(BridgeConflictError):
                store.respond(
                    "chatgpt_web", "conv-A", claim.request_id, claim.nonce,
                    claim.claim_token, "late response", now=base + timedelta(seconds=11),
                )

    def test_userscript_renew_failure_policy_is_fail_closed(self):
        source = (Path(__file__).resolve().parents[1] / "browser" / "chatgpt-web-adapter.user.js").read_text(encoding="utf-8")
        self.assertIn("classifyRenewResult", source)
        self.assertIn("lease_expires_at", source)
        self.assertIn("RENEW_RETRY_MS", source)
        self.assertIn("abandonClaim", source)
