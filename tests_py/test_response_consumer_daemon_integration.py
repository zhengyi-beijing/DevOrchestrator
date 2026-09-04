import http.client
import json
import tempfile
import time
import unittest
from pathlib import Path

from tests_py.test_daemon import ROOT, free_port, run_cli
from tests_py.test_worker_done_daemon_integration import claim, make_completed_repo


def post_response(port: int, claimed: dict, response_text: str):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    body = json.dumps({
        "adapter": "chatgpt_web",
        "binding_id": "conv-p1",
        "request_id": claimed["request_id"],
        "nonce": claimed["nonce"],
        "claim_token": claimed["claim_token"],
        "response_text": response_text,
    })
    conn.request("POST", "/v1/response", body=body, headers={"Content-Type": "application/json"})
    response = conn.getresponse(); raw = response.read(); conn.close()
    return response.status, (json.loads(raw) if raw else None)


def structured_response(claimed: dict) -> str:
    payload = {key: claimed[key] for key in (
        "project_id", "request_id", "task_id", "stage_id",
        "branch", "head", "role", "event", "nonce",
    )}
    payload["decision"] = "next"
    payload["next_action"] = "next_task"
    marker = "[DEVORCH_WEB_SOL_RESPONSE {0}]".format(claimed["request_id"])
    return marker + "\n" + json.dumps(payload, separators=(",", ":"))


class ResponseConsumerDaemonIntegrationTests(unittest.TestCase):
    def test_daemon_consumes_bridge_response_into_disposition_only(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); runtime = base / "runtime"; repo = base / "repo"
            make_completed_repo(repo)
            config = base / "projects.json"
            config.write_text(json.dumps({"projects": [{
                "project_id": "p1",
                "repo_path": str(repo),
                "worker_runtime": "tmp/worker",
                "conversation_binding": {
                    "transport": "browser_bridge",
                    "adapter": "chatgpt_web",
                    "binding_id": "conv-p1",
                },
            }]}), encoding="utf-8")
            web_port, bridge_port = free_port(), free_port()
            common = ["--runtime-root", str(runtime)]
            started = run_cli(
                "start-daemon", "--config", str(config),
                "--web-root", str(ROOT / "web"),
                "--listen", "127.0.0.1", "--port", str(web_port),
                "--bridge-listen", "127.0.0.1", "--bridge-port", str(bridge_port),
                "--interval", "5", *common,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            try:
                status0, claimed0 = claim(bridge_port, "conv-p1")
                self.assertEqual(status0, 204)
                self.assertIsNone(claimed0)

                claim_deadline = time.time() + 8
                status, claimed = 204, None
                while time.time() < claim_deadline and status == 204:
                    time.sleep(0.25)
                    status, claimed = claim(bridge_port, "conv-p1")
                self.assertEqual(status, 200)
                response_status, _ = post_response(
                    bridge_port, claimed, structured_response(claimed)
                )
                self.assertEqual(response_status, 200)

                deadline = time.time() + 12
                decisions_path = runtime / "websol-decisions.json"
                decisions = None
                while time.time() < deadline:
                    if decisions_path.exists():
                        decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
                        if claimed["request_id"] in decisions.get("decisions", {}):
                            break
                    time.sleep(0.1)
                self.assertIsNotNone(decisions)
                record = decisions["decisions"][claimed["request_id"]]
                self.assertEqual(record["disposition"], "apply")
                self.assertEqual(record["next_action"], "next_task")
                self.assertNotIn("worker_pid", record)
                self.assertNotIn("executed", record)
            finally:
                stopped = run_cli("stop-daemon", *common)
                self.assertEqual(stopped.returncode, 0, stopped.stderr)


if __name__ == "__main__":
    unittest.main()
