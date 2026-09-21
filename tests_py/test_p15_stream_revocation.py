import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dev_orchestrator.control.adapter import ControlAdapterClient
from dev_orchestrator.control.security import ControlSecurity
from dev_orchestrator.mobile.gateway import make_mobile_gateway
from dev_orchestrator.mobile.projection import MobileProjectionService
from dev_orchestrator.web.server import make_server


class TestP15StreamRevocation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.tmp.name)
        self.runtime = self.base / "runtime"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.web_root = self.base / "web"
        self.web_root.mkdir(parents=True, exist_ok=True)
        (self.web_root / "index.html").write_text("ok", encoding="utf-8")

        self.web_server = make_server(
            "127.0.0.1", 0, self.runtime, self.web_root,
            enable_control=True
        )
        self.web_port = self.web_server.server_address[1]
        self.web_thread = threading.Thread(target=self.web_server.serve_forever, daemon=True)
        self.web_thread.start()

        self.security = self.web_server.control_security
        pairing = self.security.create_mobile_pairing()
        self.device = self.security.redeem_mobile_pairing(pairing["pairing_id"], pairing["code"], "Pixel Test")
        self.device_token = self.device["token"]
        self.device_id = self.device["device_id"]

        self.control_client = ControlAdapterClient(
            base_url=f"http://127.0.0.1:{self.web_port}",
            token=self.security.token(),
            runtime_root=self.runtime,
        )
        self.projection_service = MobileProjectionService(
            runtime_root=self.runtime,
            config_provider={"projects": []},
            mobile_device_authorizer=self.security,
        )
        self.gateway = make_mobile_gateway(
            "127.0.0.1", 0, self.runtime,
            authorizer=self.security,
            projection_service=self.projection_service,
            control_client=self.control_client,
            verify_bind=False,
        )
        self.gateway_port = self.gateway.server_address[1]
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever, daemon=True)
        self.gateway_thread.start()

    def tearDown(self):
        self.gateway.close_all_streams()
        self.gateway.shutdown()
        self.gateway.server_close()
        self.gateway_thread.join(timeout=2)
        self.web_server.shutdown()
        self.web_server.server_close()
        self.web_thread.join(timeout=2)
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def test_revocation_terminates_active_sse_stream(self):
        url = f"http://127.0.0.1:{self.gateway_port}/api/v1/mobile/v1/events"
        headers = {"Authorization": f"Bearer {self.device_token}"}
        req = urllib.request.Request(url, headers=headers)

        stream_closed = threading.Event()
        frames_received = []

        def reader():
            try:
                with urllib.request.urlopen(req) as resp:
                    while True:
                        line = resp.readline()
                        if not line:
                            break
                        frames_received.append(line.decode("utf-8"))
            except Exception:
                pass
            finally:
                stream_closed.set()

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        # Wait for initial stream connection
        time.sleep(0.5)
        self.assertGreater(len(self.gateway._open_streams), 0)

        # Broadcast 1 event while valid
        self.gateway.broadcast_event("test_event_1", {"msg": "hello"})
        time.sleep(0.3)

        # Revoke device
        self.security.revoke_mobile_device(self.device_id)

        # Broadcast event after revocation: should NOT be received, stream must terminate
        self.gateway.broadcast_event("test_event_2", {"msg": "should_not_reach"})

        # Stream should close promptly
        stream_closed.wait(timeout=3.0)
        self.assertTrue(stream_closed.is_set())

        # Assert test_event_2 was never emitted to the revoked device
        all_text = "".join(frames_received)
        self.assertIn("test_event_1", all_text)
        self.assertNotIn("test_event_2", all_text)

        # Reconnect is denied
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 401)

    def test_revocation_terminates_long_poll_session(self):
        url = f"http://127.0.0.1:{self.gateway_port}/api/v1/mobile/v1/events/poll"
        headers = {"Authorization": f"Bearer {self.device_token}"}
        req = urllib.request.Request(url, headers=headers)

        poll_result = {}
        poll_done = threading.Event()

        def poller():
            try:
                with urllib.request.urlopen(req) as resp:
                    poll_result["status"] = resp.status
                    poll_result["body"] = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                poll_result["status"] = exc.code
                poll_result["body"] = json.loads(exc.read().decode("utf-8"))
            finally:
                poll_done.set()

        t = threading.Thread(target=poller, daemon=True)
        t.start()

        # Wait a bit while waiting in long-poll
        time.sleep(0.4)

        # Revoke device while long poll is waiting
        self.security.revoke_mobile_device(self.device_id)

        # Wake up wait by broadcasting
        self.gateway.broadcast_event("wake", {"wake": True})

        poll_done.wait(timeout=3.0)
        self.assertTrue(poll_done.is_set())
        # Must return 401 Unauthorized
        self.assertEqual(poll_result.get("status"), 401)


if __name__ == "__main__":
    unittest.main()
