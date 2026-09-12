import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dev_orchestrator.bridge.server import make_bridge_server
from dev_orchestrator.bridge.store import BrowserBridgeStore
from dev_orchestrator.core.progress import (
    NORMAL_MILESTONES,
    PROGRESS_LEVEL_NORMAL,
    PROGRESS_LEVEL_QUIET,
    PROGRESS_LEVEL_VERBOSE,
    QUIET_MILESTONES,
    VERBOSE_MILESTONES,
    ProgressChannel,
    ProgressMilestone,
)


class ProgressChannelTests(unittest.TestCase):
    def test_milestone_definitions_and_sets(self):
        # All required normal milestones exist
        required_normal = {
            "PLAN_STARTED",
            "PLAN_ACCEPTED",
            "WORKER_STARTED",
            "WORKER_DONE",
            "TEST_FAILED",
            "REVIEW_STARTED",
            "REMEDIATE",
            "REVIEW_ACCEPTED",
            "OWNER_GATE",
            "BLOCKED",
            "TASK_COMPLETE",
            "NEXT_TASK",
        }
        for m in required_normal:
            self.assertIn(m, NORMAL_MILESTONES)
            self.assertTrue(hasattr(ProgressMilestone, m))

        # Quiet is a strict subset
        self.assertTrue(QUIET_MILESTONES.issubset(NORMAL_MILESTONES))
        self.assertIn("BLOCKED", QUIET_MILESTONES)
        self.assertIn("OWNER_GATE", QUIET_MILESTONES)
        self.assertNotIn("WORKER_STARTED", QUIET_MILESTONES)

        # Verbose contains all normal plus diagnostic events
        self.assertTrue(NORMAL_MILESTONES.issubset(VERBOSE_MILESTONES))
        self.assertIn("WORKER_FAILED", VERBOSE_MILESTONES)

    def test_default_normal_level_filtering(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            channel = ProgressChannel(runtime)
            project = {"project_id": "proj-1"}

            # Default is normal -> WORKER_STARTED admitted, WORKER_FAILED admitted in verbose only
            notif_started = channel.emit(project, "WORKER_STARTED", task_id="T1")
            self.assertIsNotNone(notif_started)
            self.assertEqual(notif_started.milestone, "WORKER_STARTED")
            self.assertEqual(notif_started.level, PROGRESS_LEVEL_NORMAL)

            # WORKER_FAILED is not in NORMAL_MILESTONES
            notif_failed = channel.emit(project, "WORKER_FAILED", task_id="T1")
            self.assertIsNone(notif_failed)

    def test_emit_tolerates_none_telemetry(self):
        with tempfile.TemporaryDirectory() as td:
            channel = ProgressChannel(Path(td))
            notif = channel.emit(
                {"project_id": "proj-none", "telemetry": None},
                "WORKER_STARTED",
            )
            self.assertIsNotNone(notif)
            self.assertEqual(notif.task_id, "")

    def test_quiet_and_verbose_configuration(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            channel = ProgressChannel(runtime)

            # Quiet project
            quiet_proj = {
                "project_id": "p-quiet",
                "progress_channel": {"enabled": True, "level": "quiet"},
            }
            # WORKER_STARTED rejected in quiet
            self.assertIsNone(channel.emit(quiet_proj, "WORKER_STARTED", task_id="T1"))
            # BLOCKED admitted in quiet
            blocked = channel.emit(quiet_proj, "BLOCKED", task_id="T1")
            self.assertIsNotNone(blocked)
            self.assertEqual(blocked.milestone, "BLOCKED")

            # Verbose project
            verbose_proj = {
                "project_id": "p-verbose",
                "progress_channel": {"enabled": True, "level": "verbose"},
            }
            worker_failed = channel.emit(verbose_proj, "WORKER_FAILED", task_id="T1")
            self.assertIsNotNone(worker_failed)
            self.assertEqual(worker_failed.milestone, "WORKER_FAILED")

    def test_deduplication_and_restart_idempotency(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            channel1 = ProgressChannel(runtime)
            project = {"project_id": "proj-1"}

            # First emission succeeds
            first = channel1.emit(project, "WORKER_STARTED", task_id="T1")
            self.assertIsNotNone(first)

            # Duplicate emission with same project, task, milestone, occurrence returns None
            dup = channel1.emit(project, "WORKER_STARTED", task_id="T1")
            self.assertIsNone(dup)

            # Different task or occurrence key succeeds
            diff_task = channel1.emit(project, "WORKER_STARTED", task_id="T2")
            self.assertIsNotNone(diff_task)

            diff_occ = channel1.emit(project, "WORKER_STARTED", task_id="T1", occurrence_key="retry-1")
            self.assertIsNotNone(diff_occ)

            # Simulate restart: create new ProgressChannel with same runtime
            channel2 = ProgressChannel(runtime)
            # Memory from channel1 was persisted to runtime/progress-channel.json
            restart_dup = channel2.emit(project, "WORKER_STARTED", task_id="T1")
            self.assertIsNone(restart_dup)

            # New milestone still succeeds
            done = channel2.emit(project, "WORKER_DONE", task_id="T1")
            self.assertIsNotNone(done)

    def test_rate_limiting_suppresses_rapid_non_critical_emissions(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            # Rate limit: 10 seconds
            channel = ProgressChannel(runtime, rate_limit_seconds=10.0)
            project = {"project_id": "proj-rate"}

            # First emission
            first = channel.emit(project, "WORKER_STARTED", task_id="T1")
            self.assertIsNotNone(first)

            # Immediate second emission (non-critical) on same target within 10s is rate-limited
            second = channel.emit(project, "WORKER_DONE", task_id="T1")
            self.assertIsNone(second)

            # Critical events bypass rate limiting: BLOCKED or OWNER_GATE
            critical = channel.emit(project, "BLOCKED", task_id="T1")
            self.assertIsNotNone(critical)
            self.assertEqual(critical.milestone, "BLOCKED")

    def test_transport_isolation_from_decision_queues(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            bridge_store = BrowserBridgeStore(runtime / "bridge", require_live_binding=False)
            channel = ProgressChannel(runtime, bridge_store=bridge_store)

            project = {
                "project_id": "p-bound",
                "conversation_binding": {
                    "transport": "browser",
                    "adapter": "chatgpt_web",
                    "binding_id": "conv-42",
                },
            }

            channel.emit(project, "WORKER_STARTED", task_id="T1", message="Worker started on T1")

            # Verify decision queues directory has NO items for conv-42
            queue_dir = runtime / "bridge" / "queues" / "chatgpt_web" / "conv-42"
            self.assertFalse(queue_dir.exists())

            # Progress store has the item
            claimed = bridge_store.claim_progress("chatgpt_web", "conv-42", limit=10)
            self.assertEqual(len(claimed), 1)
            self.assertEqual(claimed[0]["milestone"], "WORKER_STARTED")
            self.assertEqual(claimed[0]["project_id"], "p-bound")
            self.assertIn("Worker started", claimed[0]["message"])

            # Second claim is empty (messages consumed)
            claimed2 = bridge_store.claim_progress("chatgpt_web", "conv-42", limit=10)
            self.assertEqual(len(claimed2), 0)


class ProgressChannelBindingPropagationTests(unittest.TestCase):
    """Regression tests for P6 review gap: binding propagation and resolution in ProgressChannel."""

    def _binding(self, binding_id: str = "conv-abc") -> dict:
        return {"transport": "browser", "adapter": "chatgpt_web", "binding_id": binding_id}

    def test_register_project_caches_binding_and_emit_resolves_it(self):
        """register_project must persist binding so emit() without explicit binding still delivers."""
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            bridge_store = BrowserBridgeStore(runtime / "bridge", require_live_binding=False)
            channel = ProgressChannel(runtime, bridge_store=bridge_store)

            channel.register_project(
                {"project_id": "p-reg", "conversation_binding": self._binding("conv-reg")}
            )
            # Emit with minimal dict - no binding in call site
            notif = channel.emit({"project_id": "p-reg"}, "WORKER_STARTED", task_id="T1")
            self.assertIsNotNone(notif)

            claimed = bridge_store.claim_progress("chatgpt_web", "conv-reg", limit=5)
            self.assertEqual(len(claimed), 1)
            self.assertEqual(claimed[0]["milestone"], "WORKER_STARTED")

    def test_register_projects_bulk_caches_bindings(self):
        """register_projects must cache bindings for multiple projects in one call."""
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            bridge_store = BrowserBridgeStore(runtime / "bridge", require_live_binding=False)
            channel = ProgressChannel(runtime, bridge_store=bridge_store)

            channel.register_projects([
                {"project_id": "p1", "conversation_binding": self._binding("conv-p1")},
                {"project_id": "p2", "conversation_binding": self._binding("conv-p2")},
            ])

            channel.emit({"project_id": "p1"}, "WORKER_STARTED", task_id="T1")
            channel.emit({"project_id": "p2"}, "PLAN_STARTED", task_id="T2")

            self.assertEqual(len(bridge_store.claim_progress("chatgpt_web", "conv-p1", limit=5)), 1)
            self.assertEqual(len(bridge_store.claim_progress("chatgpt_web", "conv-p2", limit=5)), 1)

    def test_resolve_binding_returns_cached_entry(self):
        """resolve_binding must return the binding stored via register_project."""
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            channel = ProgressChannel(runtime)
            channel.register_project(
                {"project_id": "p-lookup", "conversation_binding": self._binding("conv-lu")}
            )
            resolved = channel.resolve_binding("p-lookup")
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved["binding_id"], "conv-lu")

    def test_resolve_binding_survives_restart(self):
        """Bindings cached via register_project must be persisted and reloaded after restart."""
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            ch1 = ProgressChannel(runtime)
            ch1.register_project(
                {"project_id": "p-persist", "conversation_binding": self._binding("conv-persist")}
            )
            # Simulate restart
            ch2 = ProgressChannel(runtime)
            resolved = ch2.resolve_binding("p-persist")
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved["binding_id"], "conv-persist")

    def test_emit_with_string_project_id(self):
        """emit() must accept a bare string project ID and resolve binding from cache."""
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            bridge_store = BrowserBridgeStore(runtime / "bridge", require_live_binding=False)
            channel = ProgressChannel(runtime, bridge_store=bridge_store)
            channel.register_project(
                {"project_id": "p-str", "conversation_binding": self._binding("conv-str")}
            )
            notif = channel.emit("p-str", "REVIEW_STARTED", task_id="T1")
            self.assertIsNotNone(notif)
            claimed = bridge_store.claim_progress("chatgpt_web", "conv-str", limit=5)
            self.assertEqual(len(claimed), 1)

    def test_emit_explicit_binding_overrides_and_updates_cache(self):
        """Passing binding explicitly in emit() must update the project cache for future calls."""
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            bridge_store = BrowserBridgeStore(runtime / "bridge", require_live_binding=False)
            channel = ProgressChannel(runtime, bridge_store=bridge_store)

            project = {
                "project_id": "p-explicit",
                "conversation_binding": self._binding("conv-new"),
            }
            channel.emit(project, "WORKER_STARTED", task_id="T1")

            # After emit, binding must be in cache; subsequent emit without binding resolves it
            channel.emit({"project_id": "p-explicit"}, "WORKER_DONE", task_id="T1")
            all_claimed = bridge_store.claim_progress("chatgpt_web", "conv-new", limit=10)
            self.assertEqual(len(all_claimed), 2)

    def test_resolve_binding_via_project_resolver(self):
        """set_project_resolver callback must be invoked for projects not in cache."""
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            binding = self._binding("conv-resolver")
            resolver_called = []

            def my_resolver(project_id: str):
                resolver_called.append(project_id)
                return {"conversation_binding": binding}

            channel = ProgressChannel(runtime, project_resolver=my_resolver)
            resolved = channel.resolve_binding("p-dynamic")
            self.assertEqual(len(resolver_called), 1)
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved["binding_id"], "conv-resolver")


class ProgressHttpBridgeTests(unittest.TestCase):
    def test_progress_get_url_decodes_binding_id(self):
        import threading
        import urllib.request
        from urllib.parse import urlencode

        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            bridge_store = BrowserBridgeStore(runtime / "bridge", require_live_binding=False)
            server = make_bridge_server("127.0.0.1", 0, bridge_store)
            port = server.server_address[1]
            th = threading.Thread(target=server.serve_forever, daemon=True)
            th.start()
            try:
                binding_id = "conv +/%"
                channel = ProgressChannel(runtime, bridge_store=bridge_store)
                channel.emit(
                    {
                        "project_id": "http-encoded",
                        "conversation_binding": {
                            "transport": "browser",
                            "adapter": "chatgpt_web",
                            "binding_id": binding_id,
                        },
                    },
                    "PLAN_STARTED",
                    task_id="Plan-encoded",
                )
                query = urlencode({"adapter": "chatgpt_web", "binding_id": binding_id})
                url = f"http://127.0.0.1:{port}/v1/progress?{query}"
                with urllib.request.urlopen(urllib.request.Request(url, method="GET")) as resp:
                    self.assertEqual(resp.status, 200)
                    body = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(body["count"], 1)
                    self.assertEqual(body["claimed"][0]["binding_id"], binding_id)
            finally:
                server.shutdown()
                server.server_close()
                th.join(timeout=2)

    def test_progress_http_endpoints(self):
        import urllib.request
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            bridge_store = BrowserBridgeStore(runtime / "bridge", require_live_binding=False)
            server = make_bridge_server("127.0.0.1", 0, bridge_store)
            port = server.server_address[1]

            import threading
            th = threading.Thread(target=server.serve_forever, daemon=True)
            th.start()
            try:
                channel = ProgressChannel(runtime, bridge_store=bridge_store, rate_limit_seconds=0.0)
                project = {
                    "project_id": "http-p",
                    "conversation_binding": {
                        "transport": "browser",
                        "adapter": "chatgpt_web",
                        "binding_id": "conv-99",
                    },
                }
                channel.emit(project, "PLAN_STARTED", task_id="Plan-1")

                # GET /v1/progress?adapter=chatgpt_web&binding_id=conv-99
                url_get = f"http://127.0.0.1:{port}/v1/progress?adapter=chatgpt_web&binding_id=conv-99"
                req_get = urllib.request.Request(url_get, method="GET")
                with urllib.request.urlopen(req_get) as resp:
                    self.assertEqual(resp.status, 200)
                    body = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(len(body.get("claimed", [])), 1)
                    self.assertEqual(body["claimed"][0]["milestone"], "PLAN_STARTED")

                # Emit another notification
                channel.emit(project, "PLAN_ACCEPTED", task_id="Plan-1")

                # POST /v1/progress with JSON body
                url_post = f"http://127.0.0.1:{port}/v1/progress"
                payload = json.dumps({"adapter": "chatgpt_web", "binding_id": "conv-99"}).encode("utf-8")
                req_post = urllib.request.Request(
                    url_post, data=payload, headers={"Content-Type": "application/json"}, method="POST"
                )
                with urllib.request.urlopen(req_post) as resp:
                    self.assertEqual(resp.status, 200)
                    body = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(len(body.get("claimed", [])), 1)
                    self.assertEqual(body["claimed"][0]["milestone"], "PLAN_ACCEPTED")

            finally:
                server.shutdown()
                server.server_close()
                th.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
