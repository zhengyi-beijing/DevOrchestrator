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


class TestP15MobileStream(unittest.TestCase):
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

    def _read_sse_events(self, url: str, headers: dict, count: int = 2, timeout: float = 3.0) -> list[dict]:
        events = []
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req) as resp:
            cur_event = {}
            start = time.time()
            while time.time() - start < timeout:
                line = resp.readline().decode("utf-8")
                if not line:
                    break
                line = line.strip()
                if not line:
                    if cur_event:
                        events.append(cur_event)
                        cur_event = {}
                        if len(events) >= count:
                            break
                    continue
                if line.startswith("event: "):
                    cur_event["event"] = line[7:]
                elif line.startswith("id: "):
                    cur_event["id"] = line[4:]
                elif line.startswith("data: "):
                    cur_event["data"] = json.loads(line[6:])
        return events

    def test_cursor_resume_without_gaps_or_duplicates(self):
        # Broadcast 3 events
        c1 = self.gateway.broadcast_event("item_1", {"num": 1})
        c2 = self.gateway.broadcast_event("item_2", {"num": 2})
        c3 = self.gateway.broadcast_event("item_3", {"num": 3})

        # Resume from c1 using ?cursor=
        url = f"http://127.0.0.1:{self.gateway_port}/api/v1/mobile/v1/events?cursor={c1}"
        headers = {"Authorization": f"Bearer {self.device_token}"}
        evs = self._read_sse_events(url, headers, count=2)

        self.assertEqual(len(evs), 2)
        self.assertEqual(evs[0]["event"], "item_2")
        self.assertEqual(evs[0]["id"], c2)
        self.assertEqual(evs[1]["event"], "item_3")
        self.assertEqual(evs[1]["id"], c3)

    def test_last_event_id_header_equivalence(self):
        c1 = self.gateway.broadcast_event("item_a", {"val": "a"})
        c2 = self.gateway.broadcast_event("item_b", {"val": "b"})

        # Resume using Last-Event-ID header instead of query param
        url = f"http://127.0.0.1:{self.gateway_port}/api/v1/mobile/v1/events"
        headers = {
            "Authorization": f"Bearer {self.device_token}",
            "Last-Event-ID": c1,
        }
        evs = self._read_sse_events(url, headers, count=1)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["event"], "item_b")
        self.assertEqual(evs[0]["id"], c2)

    def test_resync_required_when_cursor_outside_retention(self):
        # Broadcast more events than max retention (retention is 100)
        first_c = None
        for i in range(120):
            c = self.gateway.broadcast_event(f"ev_{i}", {"index": i})
            if first_c is None:
                first_c = c

        # Attempt to resume from first_c which was evicted from buffer
        url = f"http://127.0.0.1:{self.gateway_port}/api/v1/mobile/v1/events?cursor={first_c}"
        headers = {"Authorization": f"Bearer {self.device_token}"}
        evs = self._read_sse_events(url, headers, count=1)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["event"], "resync_required")
        self.assertIn("projects", evs[0]["data"])

    def test_long_poll_resume_and_resync(self):
        c1 = self.gateway.broadcast_event("lp_1", {"msg": "one"})
        c2 = self.gateway.broadcast_event("lp_2", {"msg": "two"})

        url = f"http://127.0.0.1:{self.gateway_port}/api/v1/mobile/v1/events/poll?cursor={c1}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.device_token}"})
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertFalse(data["resync_required"])
            self.assertEqual(len(data["events"]), 1)
            self.assertEqual(data["events"][0]["event"], "lp_2")


if __name__ == "__main__":
    unittest.main()
