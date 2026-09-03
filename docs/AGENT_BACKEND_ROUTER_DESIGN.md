# Unified Agent Backend + Router Foundation

Status: DESIGN FROZEN / TEST-FIRST
Baseline: `81120db` on `feature/agent-router-foundation`

## Goal

Introduce one provider-neutral execution contract, one real `dsh` backend, and a deterministic fail-closed router. This layer is infrastructure only: no PHASE_AUTO state machine consumes it in this slice.

## Architecture

```text
Workflow (future)
  -> AgentRequest(role + capabilities + constraints)
  -> AgentRouter
  -> BackendRegistry
       -> DshBackend (real)
       -> Codex/Claude/Gemini/Local (future adapters)
```

Provider names stay below the workflow boundary. Workflow requests a role/capability set, not a concrete vendor/model.## Frozen domain model

- Roles: `designer`, `worker`, `reviewer`, `supervisor`, `mechanical`.
- Quota states: `HEALTHY`, `CONSERVE`, `LOW`, `EXHAUSTED`, `UNKNOWN`.
- Run states: `starting`, `running`, `completed`, `failed`, `cancelled`.
- `CapabilitySet`: supported roles, string capability tags, provider/independence domain, cancellation support.
- `BackendStatus`: available flag, diagnostic reason, quota state, model/profile label, optional latency.
- `AgentRequest`: project id, role, prompt, working directory, required capability tags, preferred/excluded backend ids, metadata.
- `RoutingDecision`: selected backend id or `None`, ordered candidate evaluations, explicit reason.

`UNKNOWN` quota is eligible; `EXHAUSTED` is not. An unavailable backend, unsupported role, missing required capability, or explicitly excluded backend is ineligible.## Backend contract

```python
class AgentBackend(ABC):
    @property
    def backend_id(self) -> str: ...
    def capabilities(self) -> CapabilitySet: ...
    def probe(self) -> BackendStatus: ...
    async def start(self, request: AgentRequest) -> AgentRun: ...
    async def status(self, run_id: str) -> AgentRun: ...
    async def cancel(self, run_id: str) -> AgentRun: ...
    async def collect(self, run_id: str) -> AgentResult: ...
```

`start()` is explicit execution authority. Probe/routing never starts a model. Unknown run ids fail closed with a typed backend error. This slice requires only same-process run tracking; crash/restart run recovery is deferred.## DshBackend

- Default command prefix: `dsh` (resolved with `shutil.which`); tests may inject a command prefix.
- `probe()` executes only `--version`; it never sends a prompt.
- Execution command: `<prefix> --profile headless <prompt>` with `shell=False` and explicit argv.
- Working directory must exist and comes from `AgentRequest`.
- stdout/stderr are captured per run under DevOrchestrator-owned `runtime/agent-runs/<run_id>/`; no files are written into the observed project by the backend itself.
- `cancel()` targets only the process belonging to that backend run.
- Capability identity: backend id `dsh`, provider/independence domain `dsh`, profile label `headless`.

Real acceptance uses a fake injected dsh process for start/cancel/collect semantics plus a real XLabServer `dsh --version` probe. It must not issue a real AI prompt during Reviewer acceptance.## Router policy

Routing is deterministic and side-effect free. For every registered backend the router records eligibility/rejection evidence.

Mandatory filters, in order: explicit exclusion; probe availability; quota not `EXHAUSTED`; requested role supported; all required capability tags present.

Eligible scoring:
- quota: HEALTHY 40, UNKNOWN 30, CONSERVE 20, LOW 10;
- preferred backend list: first +100, second +90, then descending by 10 to a floor of +10;
- otherwise no provider-specific hidden preference.

Highest score wins; ties resolve lexicographically by backend id. If none is eligible, selected backend is `None` and the decision contains all rejection reasons. The router never silently falls back around an explicit exclusion or required capability.## Acceptance

Designer-owned tests are `tests_py/test_agent_router.py` and `tests_py/test_dsh_backend.py`; they must not be weakened.

Required evidence:
1. Full `tests_py` GREEN, including existing P2 tests.
2. Fake DSH lifecycle verifies probe/start/status/collect/cancel and nonzero failure semantics without modifying the observed fixture working directory.
3. Router tests verify preference, quota, availability, role/capability filters, explicit exclusion, deterministic ties, and duplicate registry rejection.
4. Real XLabServer DSH probe succeeds with `probe()` only; no real AI prompt is issued for acceptance.
5. Existing PowerShell P2 self-tests remain GREEN.
6. Real LabDemo monitor remains read-only; no phase/hardware action.
7. `git diff --check` is clean.

## Non-goals

No PHASE_AUTO, no workflow transition engine, no automatic Designer/Worker/Reviewer invocation, no HTTP prompt endpoint, no Codex/Claude/Gemini/Local implementation, no quota scraping, no provider credentials, no cross-process AgentRun recovery, no remote-node protocol, no LabDemo P4.3.4, and no hardware action.