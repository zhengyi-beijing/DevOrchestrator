import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dev_orchestrator.control.command_store import (
    ControlCommandConflictError,
    ControlCommandStore,
)


def make_command_payload(command_id: str, action: str = "pause", target: dict = None):
    expected = {
        "revision": 1,
        "project_id": "demo",
        "repo_path": "C:/work/demo",
        "branch": "main",
        "head": "abc",
        "dirty": False,
        "status_hash": "sha256:111",
        "task_id": "task-1",
        "lifecycle_state": "idle",
        "gate_id": None,
        "paused": False,
        "binding_state": "none",
        "binding_id": None,
        "binding_adapter": None,
    }
    return {
        "schema_version": 1,
        "command_id": command_id,
        "project_id": "demo",
        "action": action,
        "expected": expected,
        "target": target or {},
    }


class TestP15CommandSourceIdempotency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.tmp.name)
        self.store = ControlCommandStore(self.runtime_root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_source_same_body_returns_original(self):
        payload = make_command_payload("cmd-001", "pause")
        res1 = self.store.submit(payload, source="control_api")
        self.assertEqual(res1["command_id"], "cmd-001")
        self.assertEqual(res1["source"], "control_api")

        # Replay with exact same source and body
        res2 = self.store.submit(payload, source="control_api")
        self.assertEqual(res2["command_id"], "cmd-001")
        self.assertEqual(res2["source"], "control_api")

    def test_same_source_changed_body_raises_conflict(self):
        payload1 = make_command_payload("cmd-002", "pause")
        payload2 = make_command_payload("cmd-002", "resume")
        self.store.submit(payload1, source="control_api")
        with self.assertRaises(ControlCommandConflictError):
            self.store.submit(payload2, source="control_api")

    def test_mobile_then_control_api_replay_conflicts(self):
        payload = make_command_payload("cmd-003", "continue", target={"gate_id": "gate-1"})
        self.store.submit(payload, source="mobile_gateway:dev-alpha")
        with self.assertRaises(ControlCommandConflictError) as ctx:
            self.store.submit(payload, source="control_api")
        self.assertIn("already belongs to different source", str(ctx.exception))

    def test_control_api_then_mobile_replay_conflicts(self):
        payload = make_command_payload("cmd-004", "continue", target={"gate_id": "gate-1"})
        self.store.submit(payload, source="control_api")
        with self.assertRaises(ControlCommandConflictError) as ctx:
            self.store.submit(payload, source="mobile_gateway:dev-alpha")
        self.assertIn("already belongs to different source", str(ctx.exception))

    def test_device_a_and_device_b_replays_conflict_both_ways(self):
        payload = make_command_payload("cmd-005", "approve_owner_gate", target={"gate_id": "gate-1"})
        # A then B
        self.store.submit(payload, source="mobile_gateway:dev-a")
        with self.assertRaises(ControlCommandConflictError):
            self.store.submit(payload, source="mobile_gateway:dev-b")

        # B then A with different command id
        payload2 = make_command_payload("cmd-006", "approve_owner_gate", target={"gate_id": "gate-1"})
        self.store.submit(payload2, source="mobile_gateway:dev-b")
        with self.assertRaises(ControlCommandConflictError):
            self.store.submit(payload2, source="mobile_gateway:dev-a")

    def test_mismatched_replay_never_inherits_authorization_source(self):
        payload = make_command_payload("cmd-007", "approve_owner_gate", target={"gate_id": "gate-1"})
        self.store.submit(payload, source="mobile_gateway:authorized-dev")
        # Attempting replay with unauthenticated / different source fails
        with self.assertRaises(ControlCommandConflictError):
            self.store.submit(payload, source="control_api")
        # Original record source remains unchanged
        stored = self.store.get("cmd-007")
        self.assertIsNotNone(stored)
        self.assertEqual(stored["source"], "mobile_gateway:authorized-dev")


if __name__ == "__main__":
    unittest.main()
