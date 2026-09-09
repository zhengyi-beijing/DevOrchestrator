import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dev_orchestrator.control.store import (
    ConversationControlStore,
    ControlConflictError,
)


class ConversationControlStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = datetime(2026, 9, 9, 1, 0, tzinfo=timezone.utc)
        self.store = ConversationControlStore(
            self.root, session_presence_seconds=300
        )

    def tearDown(self):
        self.tmp.cleanup()

    def heartbeat(self, binding_id="conv-A", tab="tab-1", now=None):
        return self.store.heartbeat(
            "chatgpt_web",
            binding_id,
            title="LabDemo development",
            url="https://chatgpt.com/c/" + binding_id,
            tab_instance_id=tab,
            now=now or self.base,
        )

    def test_heartbeat_discovers_live_then_stale_session(self):
        self.heartbeat()
        live = self.store.list_sessions(now=self.base + timedelta(seconds=10))
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["state"], "live")
        self.assertEqual(live[0]["active_tab_count"], 1)

        stale = self.store.list_sessions(now=self.base + timedelta(seconds=301))
        self.assertEqual(stale[0]["state"], "stale")
        self.assertEqual(stale[0]["active_tab_count"], 0)

    def test_binding_is_persistent_and_unique(self):
        self.heartbeat()
        bound = self.store.bind(
            "labdemo", "chatgpt_web", "conv-A", now=self.base
        )
        self.assertEqual(bound["project_id"], "labdemo")
        reopened = ConversationControlStore(self.root)
        self.assertEqual(
            reopened.binding_for_project("labdemo")["binding_id"], "conv-A"
        )
        with self.assertRaises(ControlConflictError):
            reopened.bind("xray-hw-platform", "chatgpt_web", "conv-A")

    def test_bind_is_idempotent_but_changed_route_requires_rebind(self):
        self.heartbeat()
        first = self.store.bind("labdemo", "chatgpt_web", "conv-A", now=self.base)
        second = self.store.bind("labdemo", "chatgpt_web", "conv-A", now=self.base)
        self.assertEqual(first, second)
        self.heartbeat("conv-B", tab="tab-2")
        with self.assertRaises(ControlConflictError):
            self.store.bind("labdemo", "chatgpt_web", "conv-B")

    def test_rebind_moves_project_atomically(self):
        self.heartbeat("conv-A")
        self.heartbeat("conv-B", tab="tab-2")
        self.store.bind("labdemo", "chatgpt_web", "conv-A", now=self.base)
        moved = self.store.rebind(
            "labdemo", "chatgpt_web", "conv-B", now=self.base + timedelta(seconds=1)
        )
        self.assertEqual(moved["binding_id"], "conv-B")
        self.assertIsNone(self.store.project_for_binding("chatgpt_web", "conv-A"))
        self.assertEqual(
            self.store.project_for_binding("chatgpt_web", "conv-B"), "labdemo"
        )

    def test_rebind_rejects_route_owned_by_another_project(self):
        self.heartbeat("conv-A")
        self.heartbeat("conv-B", tab="tab-2")
        self.store.bind("labdemo", "chatgpt_web", "conv-A")
        self.store.bind("xray-hw-platform", "chatgpt_web", "conv-B")
        with self.assertRaises(ControlConflictError):
            self.store.rebind("labdemo", "chatgpt_web", "conv-B")

    def test_unbind_is_idempotent(self):
        self.heartbeat()
        self.store.bind("labdemo", "chatgpt_web", "conv-A")
        removed = self.store.unbind("labdemo")
        self.assertEqual(removed["binding_id"], "conv-A")
        self.assertIsNone(self.store.unbind("labdemo"))
        self.assertIsNone(self.store.binding_for_project("labdemo"))


if __name__ == "__main__":
    unittest.main()
