import asyncio
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from dev_orchestrator.agents.backends.dsh import DshBackend
from dev_orchestrator.agents.base import AgentBackend
from dev_orchestrator.agents.models import AgentRequest, AgentRole, BackendStatus, CapabilitySet, QuotaState, AgentRunState
from dev_orchestrator.agents.registry import BackendRegistry
from dev_orchestrator.agents.router import AgentRouter


class ProbeBackend(AgentBackend):
    def __init__(self, backend_id, explode=False): self._id, self.explode = backend_id, explode
    @property
    def backend_id(self): return self._id
    def capabilities(self): return CapabilitySet(frozenset({AgentRole.WORKER}), frozenset({"code"}), self._id, self._id)
    def probe(self):
        if self.explode: raise RuntimeError("probe boom")
        return BackendStatus(self._id, True, "", QuotaState.UNKNOWN)
    async def start(self, request): raise NotImplementedError
    async def status(self, run_id): raise NotImplementedError
    async def cancel(self, run_id): raise NotImplementedError
    async def collect(self, run_id): raise NotImplementedError

class ReviewerRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_router_contains_probe_exception_and_uses_healthy_fallback(self):
        registry = BackendRegistry()
        registry.register(ProbeBackend("broken", explode=True))
        registry.register(ProbeBackend("healthy"))
        request = AgentRequest("p", AgentRole.WORKER, "x", Path("."), frozenset({"code"}))
        decision = AgentRouter(registry).route(request)
        self.assertEqual(decision.selected_backend_id, "healthy")
        broken = next(c for c in decision.candidates if c.backend_id == "broken")
        self.assertFalse(broken.eligible)
        self.assertIn("probe", " ".join(broken.reasons).lower())

    async def test_cancel_after_natural_completion_does_not_relabel_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake = root / "fake.py"
            fake.write_text("import sys; print('done')\n", encoding="utf-8")
            backend = DshBackend(root / "rt", (sys.executable, str(fake)))
            request = AgentRequest("p", AgentRole.WORKER, "HELLO", root, frozenset({"code"}))
            run = await backend.start(request)
            for _ in range(100):
                snap = await backend.status(run.run_id)
                if snap.state in (AgentRunState.COMPLETED, AgentRunState.FAILED):
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(snap.state, AgentRunState.COMPLETED)
            cancelled = await backend.cancel(run.run_id)
            self.assertEqual(cancelled.state, AgentRunState.COMPLETED)


if __name__ == "__main__": unittest.main()
