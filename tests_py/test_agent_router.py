import unittest
from pathlib import Path

from dev_orchestrator.agents.base import AgentBackend
from dev_orchestrator.agents.models import (
    AgentRequest, AgentRole, BackendStatus, CapabilitySet, QuotaState,
)
from dev_orchestrator.agents.registry import BackendRegistry
from dev_orchestrator.agents.router import AgentRouter


class FakeBackend(AgentBackend):
    def __init__(self, backend_id, *, available=True, quota=QuotaState.UNKNOWN,
                 roles=None, tags=None):
        self._id = backend_id
        self._status = BackendStatus(backend_id, available, "fixture", quota)
        self._caps = CapabilitySet(
            roles=frozenset(roles or {AgentRole.WORKER}),
            tags=frozenset(tags or {"code", "repository"}),
            provider=backend_id,
            independence_domain=backend_id,
        )

    @property
    def backend_id(self): return self._id
    def capabilities(self): return self._caps
    def probe(self): return self._status
    async def start(self, request): raise NotImplementedError
    async def status(self, run_id): raise NotImplementedError
    async def cancel(self, run_id): raise NotImplementedError
    async def collect(self, run_id): raise NotImplementedError

class AgentRouterTests(unittest.TestCase):
    def request(self, **kwargs):
        values = dict(
            project_id="labdemo",
            role=AgentRole.WORKER,
            prompt="bounded fixture",
            working_directory=Path("."),
            required_capabilities=frozenset({"code"}),
        )
        values.update(kwargs)
        return AgentRequest(**values)

    def router(self, *backends):
        registry = BackendRegistry()
        for backend in backends:
            registry.register(backend)
        return AgentRouter(registry)

    def test_preferred_backend_wins_when_eligible(self):
        router = self.router(FakeBackend("alpha"), FakeBackend("beta"))
        decision = router.route(self.request(preferred_backends=("beta", "alpha")))
        self.assertEqual(decision.selected_backend_id, "beta")
        self.assertTrue(next(c for c in decision.candidates if c.backend_id == "beta").eligible)

    def test_exhausted_unavailable_and_capability_mismatch_are_rejected(self):
        router = self.router(
            FakeBackend("down", available=False),
            FakeBackend("spent", quota=QuotaState.EXHAUSTED),
            FakeBackend("thin", tags={"text"}),
        )
        decision = router.route(self.request())
        self.assertIsNone(decision.selected_backend_id)
        reasons = {c.backend_id: " ".join(c.reasons) for c in decision.candidates}
        self.assertIn("unavailable", reasons["down"])
        self.assertIn("quota", reasons["spent"])
        self.assertIn("capability", reasons["thin"])
    def test_explicit_exclusion_is_fail_closed(self):
        router = self.router(FakeBackend("dsh"))
        decision = router.route(self.request(excluded_backends=frozenset({"dsh"})))
        self.assertIsNone(decision.selected_backend_id)
        self.assertIn("excluded", " ".join(decision.candidates[0].reasons))

    def test_unknown_quota_is_eligible_and_ties_are_deterministic(self):
        router = self.router(FakeBackend("zeta"), FakeBackend("alpha"))
        decision = router.route(self.request())
        self.assertEqual(decision.selected_backend_id, "alpha")

    def test_role_is_mandatory(self):
        router = self.router(FakeBackend("worker-only", roles={AgentRole.WORKER}))
        decision = router.route(self.request(role=AgentRole.REVIEWER))
        self.assertIsNone(decision.selected_backend_id)
        self.assertIn("role", " ".join(decision.candidates[0].reasons))

    def test_registry_rejects_duplicate_backend_ids(self):
        registry = BackendRegistry()
        registry.register(FakeBackend("same"))
        with self.assertRaises(ValueError):
            registry.register(FakeBackend("same"))


if __name__ == "__main__":
    unittest.main()
