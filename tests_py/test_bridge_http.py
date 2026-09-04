import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from dev_orchestrator.bridge.server import make_bridge_server
from dev_orchestrator.bridge.store import BrowserBridgeStore
from dev_orchestrator.core.websol import WebSolEvent, WebSolRequest, WebSolRole


def request_obj():
    return WebSolRequest(
        project_id="alpha", request_id="req-http", task_id="T1", stage_id=None,
        branch="main", head="b" * 40, role=WebSolRole.REVIEWER,
        event=WebSolEvent.REVIEW_REQUIRED, nonce="nonce-http",
    )


def post(port: int, path: str, payload: dict):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    body = json.dumps(payload).encode("utf-8")
    conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, (json.loads(data) if data else None)
class BridgeHttpTests(unittest.TestCase):
    def test_claim_and_response_are_binding_scoped(self):
        with tempfile.TemporaryDirectory() as td:
            store = BrowserBridgeStore(Path(td))
            store.submit("chatgpt_web", "conv-A", request_obj(), "rendered prompt")
            server = make_bridge_server("127.0.0.1", 0, store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            try:
                status, empty = post(port, "/v1/claim", {"adapter":"chatgpt_web","binding_id":"conv-B"})
                self.assertEqual(status, 204)
                self.assertIsNone(empty)
                status, claim = post(port, "/v1/claim", {"adapter":"chatgpt_web","binding_id":"conv-A"})
                self.assertEqual(status, 200)
                self.assertEqual(claim["project_id"], "alpha")
                self.assertEqual(claim["prompt"], "rendered prompt")
                status, ack = post(port, "/v1/response", {
                    "adapter":"chatgpt_web", "binding_id":"conv-A",
                    "request_id":"req-http", "nonce":"nonce-http",
                    "claim_token":claim["claim_token"],
                    "response_text":"[DEVORCH_WEB_SOL_RESPONSE req-http] ok",
                })
                self.assertEqual(status, 200)
                self.assertEqual(ack["state"], "responded")
                status2, ack2 = post(port, "/v1/response", {
                    "adapter":"chatgpt_web", "binding_id":"conv-A",
                    "request_id":"req-http", "nonce":"nonce-http",
                    "claim_token":claim["claim_token"],
                    "response_text":"[DEVORCH_WEB_SOL_RESPONSE req-http] ok",
                })
                self.assertEqual(status2, 200)
                self.assertEqual(ack2, ack)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_no_generic_prompt_or_workflow_endpoint_exists(self):
        with tempfile.TemporaryDirectory() as td:
            server = make_bridge_server("127.0.0.1", 0, BrowserBridgeStore(Path(td)))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start(); port = server.server_address[1]
            try:
                for route in ("/v1/prompt", "/v1/worker", "/v1/next-action"):
                    status, _ = post(port, route, {})
                    self.assertEqual(status, 404, route)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

# Reviewer regression: lease renewal is transport-only and must be exposed by HTTP.
def _test_http_renew(testcase):
    with tempfile.TemporaryDirectory() as td:
        store = BrowserBridgeStore(Path(td), lease_seconds=30)
        store.submit("chatgpt_web", "conv-renew", request_obj(), "prompt")
        server = make_bridge_server("127.0.0.1", 0, store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start(); port = server.server_address[1]
        try:
            status, claim = post(port, "/v1/claim", {"adapter":"chatgpt_web","binding_id":"conv-renew"})
            testcase.assertEqual(status, 200)
            status, renewed = post(port, "/v1/renew", {
                "adapter":"chatgpt_web", "binding_id":"conv-renew", "request_id":"req-http",
                "nonce":"nonce-http", "claim_token":claim["claim_token"]})
            testcase.assertEqual(status, 200)
            testcase.assertEqual(renewed["state"], "claimed")
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

BridgeHttpTests.test_claim_lease_can_be_renewed = lambda self: _test_http_renew(self)
