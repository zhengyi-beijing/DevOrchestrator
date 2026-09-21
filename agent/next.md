# NEXT — Roadmap Handoff (P16 AI Capability Benchmark Project: OWNER_GATE Raised)

Status: **P16 OWNER_GATE RAISED (Pending Owner Containment Provisioning)**

Sequence:
P14.5 (closed) -> Watchdog recovery-epoch cleanup (closed) -> P14.6 Unattended Execution Stabilization Gate (closed) -> P15 Mobile Observability & Guarded Control (closed) -> P16 AI Capability Benchmark Project (OWNER_GATE raised)

## P16 AI Capability Benchmark Project state

Remediated and verified under `OWNER_GATE RAISED (Pending Owner Containment Provisioning)`:

Acceptance evidence:
1. **Isolated Benchmark Project & Zero Production Contamination**:
   - Resides strictly in `benchmark/` (`src/aibench/`, `pyproject.toml`, `scripts/`, `corpus/`, `evidence/`).
   - Production `src/dev_orchestrator/**` does not import `aibench` or expose a benchmark CLI command.
2. **AIBroker Boundary & Deterministic Resource Pinning**:
   - Uses `AIBrokerExecutionPort` boundary without provider CLI adapters or parsing provider credentials.
   - Deterministic resource pinning via exclusion (`AIRoleRequest.excluded_resource_ids`) validated against returned `resource_context.resource_id`.
   - Bounded reliability failovers chain up to 2 retries on resource failures.
3. **Paired A/B Evaluation & Ground-Truth Code-Only Scoring**:
   - Evaluates Planner, Reviewer, Worker, and Debugger roles across paired Track A (Native) and Track B (Retrieval) workspaces with byte-identical prompts and matching prompt hashes.
   - Ground-truth scoring verifies cited spans against corpus bytes, required/false findings, patch application in disposable workspaces, and unit test execution / regression rate. No model judge.
4. **Windows Containment Boundary**:
   - External scratch and queue roots outside DevO, AIResourceBroker, and production roots.
   - Dedicated non-admin SID verification via `whoami /user`.
   - Deny write/delete directory handle access denial audit against existing protected roots without creating files.
   - Program-specific Windows Firewall outbound-deny rule verified via regex.
   - Transactional, SDDL-backed provisioning and digest-checked uninstall scripts adhering to PowerShell 5.1 rules (zero `&&` or `||`).
5. **Total, Bounded 8-Gate Evaluation & Pipeline Self-Test Baseline**:
   - 24 trials executed and committed under `benchmark/evidence/p16_baseline_20260921/` with authentic `execution_source: "pipeline_self_test"` and mock provider provenance.
   - `capability_gate`: FAIL (zvec-grep not found on system or PATH).
   - Downstream retrieval gates marked `not_applicable_due_to_capability_failure`.
   - `evidence_gate`: FAIL (`lacks real broker correlation evidence (execution_source='pipeline_self_test', mock/simulated execution)`).
   - `fallback_gate`: PASS (12/12 native trials completed, 1.00 completion rate, correctness 1.0000).
   - Final Evaluated Decision: **`NO_PROMOTE`** (`reasons: ["capability_unsupported", "evidence_gate_failed"]`).
   - Artifacts committed under `benchmark/evidence/p16_baseline_20260921/` and canonical acceptance record in `docs/P16_BENCHMARK_ACCEPTANCE.md`.
6. **Explicit OWNER_GATE on Host Elevation**:
   - In accordance with the approved design ("Inability to provision the core three-resource acceptance set is an explicit OWNER_GATE"), OWNER_GATE is explicitly raised pending owner elevation to provision the dedicated Windows non-admin SID, ACL write-denials on production roots, and firewall rules. Stopped claiming live evaluation of 3 real broker resources in documentation and agent state.
7. **Acceptance Test Suite**:
   - 9 dedicated P16 test suites in `tests_py/test_p16_*.py` (61 passed in 4.32s).
8. **Roadmap Status**:
   - `agent/staged/roadmap.json` designates `successor: null`. P16 is the terminal milestone on the staged roadmap, currently paused at OWNER_GATE pending host containment provisioning.

## P15 Mobile Observability & Guarded Control closure

Closed after implementing and verifying all P15 Mobile Observability and Guarded Control capabilities:

Acceptance evidence:
1. **Durable Source-Locking & Replay Conflict Safety**:
   - `ControlCommandStore.submit` normalizes `source` before locking and rejects replays with mismatched sources via `ControlCommandConflictError`.
   - Replay of same command_id with matching source and body is an idempotent replay; same source with changed body conflicts.
2. **Device Identity, Pairing, & Single Revocation Authority**:
   - Single source of truth in `runtime/control/adapter-capabilities.json` managed under `InterProcessFileLock`.
   - Monotonic `mobile_revocation_generation` tracks revocation events.
   - `validate_mobile_bearer` returns non-secret `MobileDevicePrincipal` at gateway ingress; `lookup_mobile_device` re-validates device identity without bearer token. Zero bearer token propagation into command records, coordinators, or audit logs.
   - `POST /api/v1/mobile/v1/pair` is single-use, TTL-bounded, attempt-bounded, and returns device token once.
3. **Guarded Mobile Control & Mobile Owner-Gate Channel**:
   - Loopback `POST /api/v1/control/commands` admits `X-DevO-Control-Source: mobile_gateway:<device_id>` only under master bearer and successful tokenless `lookup_mobile_device`.
   - `approve_owner_gate` via `approval_channel='mobile_device'` skips conversation-binding requirement, preserves pending-gate and repository-truth checks, and durably records `approved_via` and `approving_device_id`.
   - Approval never launches a Worker; progress still requires explicit `continue`.
4. **Read-Only Mobile Projection & Bounded Controls**:
   - `MobileProjectionService` composes read-only views directly from `runtime_root` without coordinator invocation or lifecycle mutations.
   - Exposes strictly `MOBILE_CONTROL_ACTIONS` (`continue`, `pause`, `resume`, `stop`, `retry`, `reconcile`, `approve_owner_gate`).
   - Copies watchdog state and recovery epoch verbatim.
5. **Tailscale Bind Policy & Streaming Infrastructure**:
   - `verify_tailscale_bind_address` restricts gateway to verified local Tailscale `100.64.0.0/10` and `fd7a:115c:a1e0::/48` addresses, refusing wildcard, loopback, and RFC1918.
   - Reconnectable SSE and long-poll streams with 15-second mid-stream authorization rechecks; mid-stream revocation immediately terminates connections.
6. **Notification-Only Alert Boundary**:
   - Disjoint progress-family and transport-family alert classes. Stall alerts derive strictly from authoritative watchdog state.
7. **Client Implementations & Acceptance Suite**:
   - Headless Python `MobileContractClient` and Kotlin/Jetpack Compose Android application skeleton in `android/`.
   - 13 comprehensive P15 test suites in `tests_py/test_p15_*.py` (61 tests passed).
   - Full repository regression: 969 passed, 87 subtests passed in 356.33s.
   - `compileall`, `git diff --check`, and `graphify update .` all passed cleanly.

## P16 goal

Create a separate, reproducible benchmark project that measures the real capability, efficiency and reliability of the AI resources available to DevO/AIBroker, and determines when shared repository retrieval such as zvec-grep should become a standard Agent Harness capability.

Refer to `agent/staged/P16.md` for initial scope and acceptance criteria.

## Approved executable design

Create an isolated benchmark project under `benchmark/` that invokes every AI trial through the existing AIBroker execution boundary, freezes deterministic corpora and scoring, compares identical-prompt native and zvec-enabled workspaces, and emits a bounded PROMOTE or NO_PROMOTE decision. Exact-resource cells use AIBroker’s supported exclusion contract against a frozen resource snapshot, while separate bounded retry chains measure Broker decisions, executions, sessions, quota observations and failover. A dedicated non-admin Windows scheduled-task runner, external scratch root, transactional ACL provisioning and static zvec network denial provide an executable containment boundary. Unsupported zvec is a terminal capability-gate failure producing NO_PROMOTE rather than missing-evidence escalation.

### Implementation steps
- Create the standalone `benchmark/` Python project with `src/aibench/`, frozen configuration, README and CLI entry point; add only test import plumbing outside that tree. Production `src/dev_orchestrator/**` must not import aibench or gain a benchmark command.
- Build frozen corpus fixtures as small synthetic Git repositories with manifests, normalized file hashes, deterministic revisions, seeded defects and known answers covering Planner, Reviewer, Worker and Debugger tasks, including architecture ownership, cross-file flow, rationale, compatibility paths and test discovery. Never run benchmark prompts against production repositories.
- Define versioned task and rubric files before live execution. Each task has one immutable prompt byte sequence, role, timeout, response schema and ground-truth reference. The common prompt tells both tracks to use `.aibench/tools/zvec-grep` for initial discovery when present, otherwise use native tools, and always verify citations through exact source reads; ground truth is never included.
- Implement deterministic workspace materialization beneath the configured external scratch root. Track A and Track B use separate fresh copies of the same tracked corpus revision; the only treatment difference is untracked `.aibench/` retrieval material in Track B. Worker changes remain confined to its disposable copy.
- Implement a versioned zvec adapter whose injected command has one benchmark-owned interface for capability, index, query and stats operations. It wraps a configured real zvec-grep installation, validates JSON hits containing path and line spans, rejects paths outside the corpus, and records the executable version and hash. It must not emulate semantic results when the installation cannot satisfy the contract.
- Implement the common retrieval treatment: Track B receives the verified adapter plus an index built from the pristine revision; Track A receives neither. Record whether the agent invoked zvec and independently verify every returned or cited span. Native exact search, reads and tests remain available in both tracks, so the comparison measures adding retrieval rather than forbidding provider-native tools.
- Implement `BrokerBenchmarkClient` on the existing `AIBrokerExecutionPort`. Start an ordinary AIBroker loopback service inside the contained worker with the configured Broker Python/repository/resource registry, a scratch-local telemetry database and token file; discover resources through its supported resource API and dispatch all provider work through the port. Do not implement provider CLI adapters or parse provider credentials in aibench.
- Freeze the full Broker resource snapshot and registry digest into each run plan. For an exact-resource trial, pass every other snapshot resource through `AIRoleRequest.excluded_resource_ids`; require the returned `resource_context.resource_id` to equal the target and abort that cell on registry drift or mismatch. This uses Broker selection, invocation, credentials, probes and telemetry while deterministically pinning the A/B resource.
- Record every `AIRoleResult` and correlated Broker status/usage evidence, including request, dispatch, decision, execution and session IDs; provider/account/model identity; timestamps; usage and source; quota/rate-limit observations; and failure classification. Unknown Broker facts remain explicitly unknown and are never inferred from text or the registry.
- Add a separate bounded reliability schedule using normal Broker selection. On an explicit resource failure only, issue at most two new AIBroker requests excluding previously failed resource IDs; preserve every attempt and selected resource as one retry/failover chain. Never substitute a different resource inside an exact-resource A/B pair or retry semantic/task failures as provider failures.
- Implement a frozen `TrialPlan` with corpus/rubric/config hashes, selected resources, task cells, two tracks, repeats, randomized within-pair order, timeout, thresholds, seed and a maximum dispatch count. Require at least three usable resources and all four role classes for acceptance; the runner never extends the budget dynamically.
- For every A/B pair, assert identical prompt hash, task, tracked revision, target resource, role, timeout, rubric and Broker configuration before dispatch. Failed, timed-out and unavailable halves remain outcomes in the predetermined sample rather than being silently replaced, preventing survivorship bias.
- Capture zvec wrapper calls and same-policy shell-command observations from workspace-scoped shims, plus correlated AIBroker harness tool events where the backend reports them. Publish `observed_shell_tool_calls`, `zvec_calls` and `provider_reported_tool_calls` separately; expose an aggregate tool-call metric only when its source declares complete coverage, so native/internal reads are never presented as fully observed.
- Implement code-only scoring for grounded answers and patches: verify cited paths/spans against frozen bytes, score required and false findings, apply Worker output only in its disposable workspace, and execute the frozen relevant-test subset for patch validity and regression rate. No model judge or undocumented subjective ranking is permitted.
- Implement staleness and operating-cost probes by adding, renaming and deleting known symbols after indexing without refresh, then measuring false stale hits, misses, retrieval latency, index time/size and availability. Run the adapter under the preprovisioned outbound-deny rule; only successful index/query probes under that verified rule may set `local_only=true`.
- Implement the Windows containment protocol. A fixed scheduled task named in machine-local configuration runs `python -m aibench.scheduled_worker` under one dedicated non-admin SID with stored Scheduler credentials, least privilege and a fixed external queue. The coordinator validates the exported task definition, submits a hashed plan request, starts it with `schtasks.exe`, and accepts output only after the worker records the expected SID from `whoami /user`.
- Place scratch and queue roots outside DevO, AIResourceBroker and every configured production project root. The scheduled worker validates deny-write/delete ACLs for its SID on all protected roots, opens directory handles requesting write/delete access and requires access denial without creating files, then performs create/write/rename/delete tests in scratch. It aborts before starting Broker if identity, ACL, scratch or registry checks fail.
- Provide elevated install/uninstall scripts for the static containment boundary. The installer validates resolved roots, stores original SDDL and firewall state in `%ProgramData%\DevOrchestrator\P16`, journals each mutation before applying the dedicated-SID deny ACEs and unique program-specific outbound-deny rule, and rolls back an incomplete transaction on the next invocation. Uninstall restores only state whose installed digest still matches, otherwise stops for owner review. Live runs never change ACLs or firewall rules.
- Implement resumable append-only run records, deterministic aggregation and reports. Versioned thresholds require correctness/evidence non-inferiority, bounded false findings, at least one statistically supported wall-time, complete-tool-call or reported-token benefit, and passing capability, privacy, staleness, latency, availability, index-cost and native-fallback gates.
- Make the final decision total and bounded: all promotion and evidence gates pass yields PROMOTE; every other completed run yields NO_PROMOTE with enumerated failed gates. A missing or incompatible zvec installation yields `NO_PROMOTE` with `capability_unsupported`; downstream zvec metrics become `not_applicable_due_to_capability_failure`, not `not_measured`. If zvec is supported, missing required measurements or coverage fails the evidence gate and also yields NO_PROMOTE after the frozen budget.
- Execute and commit sanitized acceptance evidence under `benchmark/evidence/<run_id>/` plus `docs/P16_BENCHMARK_ACCEPTANCE.md`. A supported adapter must include applied native-versus-zvec pairs; an unsupported adapter must include its capability proof, a native-fallback pair with `retrieval_applied=false`, and NO_PROMOTE. Raise OWNER_GATE only when the dedicated boundary or three real Broker resources cannot be provisioned, not merely because zvec is unsupported.
- Add focused unit, integration, containment-fixture and evidence-schema tests, run the full Python regression and compile checks, scan new PowerShell/code for unsafe shell usage, run `git diff --check`, and finish with `graphify update .` as required by repository policy.

### Interfaces / contracts
- `benchmark/src/aibench/` and `python -m aibench {corpus-verify,containment-audit,zvec-probe,plan-freeze,submit,run,report,decide}`; live execution is delegated to `python -m aibench.scheduled_worker`.
- Existing `dev_orchestrator.ai.execution_port.AIExecutionPort` and `AIBrokerExecutionPort.execute(AIRoleRequest) -> AIRoleResult`; no new provider-facing adapter or production execution contract is introduced.
- `BrokerBenchmarkClient.snapshot_resources() -> ResourceSnapshot`, `execute_exact(target_resource_id, request) -> BrokerAttempt`, and `execute_reliability_chain(request, max_attempts=3) -> tuple[BrokerAttempt,...]`; exact dispatch is implemented by excluding all other frozen Broker resources.
- `ResourceSnapshot` records sanitized resource_id/provider/account/model/capabilities plus Broker registry digest; raw registry credentials, environment values and service tokens are never copied into evidence.
- `BenchmarkTask` and `FrozenPrompt` carry task_id, role, prompt bytes/hash, timeout, response schema, rubric reference and retrieval-sensitive flag; the same prompt object is used for both tracks.
- `ZvecAdapter.probe`, `build_index`, `query`, `stats` and `verify_network_denial`; `RetrievalHit(path,start_line,end_line,score,mode)` is accepted only after exact workspace validation.
- `ContainmentConfig` in ignored `runtime/benchmark.json` contains absolute external scratch/queue roots, dedicated SID, scheduled-task name, Broker runtime paths, zvec path and expected firewall-rule identity; it contains no Scheduler password, provider token or service token.
- Scheduled-task queue protocol: atomically written request JSON with plan path/hash and nonce, worker-owned result JSON with nonce, SID, timestamps and exit classification, and coordinator timeout/cancellation. The fixed task action accepts no user-supplied command line.
- Containment state under `%ProgramData%\DevOrchestrator\P16` stores the transaction journal, original and installed ACL digests, protected absolute paths, zvec executable hash and firewall-rule ownership needed for recovery and uninstall.
- `TrialPlan`, `TrialRecord`, `BrokerAttempt`, `TrialScore`, `MetricCoverage`, `RunSummary` and `PromotionDecision`; records are schema-versioned and retain raw correlation IDs and metric provenance.
- Artifacts: frozen corpus manifests and rubrics, `promotion-thresholds.json`, `trial_plan.json`, append-only `results.jsonl`, `summary.json`, `report.md`, `promotion_decision.json` and sanitized acceptance evidence.
- Decision enum is exactly `PROMOTE` or `NO_PROMOTE`; reason codes include `capability_unsupported`, `evidence_gate_failed`, `quality_gate_failed`, `benefit_gate_failed`, `privacy_gate_failed`, `staleness_gate_failed`, `cost_gate_failed` and `fallback_gate_failed`.

### Validation plan
- Unit tests prove both A/B halves share the exact prompt hash, tracked revision, target resource, role, timeout and rubric, while only Track B receives untracked retrieval material; any divergent constant prevents dispatch or scoring.
- Broker integration tests use a fake Broker service to verify resource discovery, exclusion of every non-target resource, preservation of all Broker correlation/session/usage/quota fields, rejection of a returned resource mismatch and fail-closed behavior on registry drift.
- A live acceptance run contains at least three distinct AIBroker-returned resource identities, Planner, Reviewer, Worker and Debugger tasks, predetermined repeats, non-null Broker dispatch/decision/execution evidence when supplied, and no directly invoked provider process.
- Reliability tests prove only explicit resource failures trigger the bounded next dispatch, each retry excludes prior failed resources, all attempts retain Broker decisions, and no exact-resource A/B pair silently fails over to another resource.
- Prompt-policy tests verify both tracks receive identical bytes, the conditional zvec instruction and exact-source requirement are present in that common prompt, no ground-truth text leaks into prompts, and Track B invocation is measured rather than assumed.
- zvec tests cover supported and unsupported installations, malformed JSON, escaping paths, invalid spans, deterministic index/query fixtures and network-denial verification. Unsupported capability maps directly to NO_PROMOTE and marks dependent metrics not applicable.
- Containment tests validate resolved scratch is outside every protected root, scheduled-task principal/action/run level match configuration, the worker SID proof matches, protected directory write/delete handle requests are denied, scratch mutation succeeds and Broker never starts after a failed preflight.
- Provisioning tests operate on temporary ACL targets and mocked firewall commands to prove journal-before-mutation ordering, idempotent install, rollback after interruption, conflict-safe uninstall and refusal to restore over externally changed ACL/rule state.
- Scoring golden tests cover every task class, cited-span verification, false and missed findings, patch application, frozen tests and regression rate without model grading.
- Metric tests ensure shell observations, zvec calls and provider-reported tool events remain separate; incomplete coverage cannot satisfy the tool-call benefit gate or be rendered as a complete tool-call count.
- Promotion tests exercise every threshold and assert the total mapping: all gates pass means PROMOTE; unsupported zvec, insufficient coverage, unavailable required metrics or any failed gate means NO_PROMOTE, never an unbounded evidence-extension loop.
- Acceptance evidence validation requires either supported applied A/B pairs or verified capability-unsupported evidence plus a native-fallback pair and NO_PROMOTE; both paths require the three-resource role-capability benchmark and machine-readable/human-readable outputs.
- Repeated aggregation of the same ledger produces byte-stable decision and summary data apart from explicitly excluded timestamps, and replay verifies all committed corpus, prompt, rubric, plan and registry hashes.
- Run `pytest tests_py`, focused benchmark tests, `python -m compileall src benchmark/src`, `git diff --check`, and `graphify update .`; compare against the existing regression baseline.
- Scan new Python, PowerShell and documentation to reject `shell=True` and direct PowerShell 5.1 `&&` or `||`; use list arguments, PowerShell-safe sequencing and explicit exit-code checks per failure-memory provenance `seed:p11b:rdc-powershell-5.1`.

### Risks / failure modes
- A dedicated account may lack provider credentials or access to at least three usable resources. Preflight probes through AIBroker under the exact scheduled-task SID expose this before trials; inability to provision the core three-resource acceptance set is an explicit OWNER_GATE.
- Using exclusions to pin a resource depends on a stable Broker snapshot. Registry hashes before and after the batch plus returned-resource validation prevent silent mispairing; drift aborts affected cells.
- Provider-native/internal file reads are not uniformly observable. Reports separate authoritative provider events from shell observations, and incomplete tool-call coverage cannot drive promotion.
- The common conditional tool instruction may lead Track A to test for a missing helper or Track B not to use it. Invocation is recorded, non-use remains an experimental outcome, and the immutable common prompt avoids introducing different task guidance.
- Permanent SID-specific ACLs and a program-specific firewall rule change host security configuration. Transaction journals, original-state backups, unique ownership markers, digest-checked uninstall and no per-run security mutations bound recovery risk.
- Windows Scheduler credentials and provider profiles are machine-owned secrets. The benchmark stores only the scheduled-task name and SID, never exports credentials, and sanitizes committed Broker/resource evidence.
- A zvec wrapper whose actual engine or output contract changes could invalidate results. Executable hashes, capability probes and strict output validation fail closed and produce NO_PROMOTE rather than fabricated metrics.
- Failed provider trials and quota exhaustion can reduce completed pairs. Predetermined failures remain availability outcomes; the fixed dispatch ceiling prevents indefinite repeats, while inability to complete the basic three-resource benchmark is surfaced rather than hidden.
- Small or synthetic corpora may overstate retrieval benefit. Promotion requires frozen representative task classes, repeated paired evidence and explicit quality/maintenance gates; raw outputs remain available for later re-scoring.
- Worker tasks intentionally modify disposable workspaces. Fresh per-trial materialization, tracked-revision verification and protected-root ACLs prevent contamination of later trials or production repositories.
- A static firewall rule can prove only that the configured zvec executable succeeds without outbound access; subprocesses outside that program path could escape the claim. The adapter records the exact executable/process boundary and fails the privacy gate unless the verified rule covers the complete configured runtime.

### Out of scope
- Promoting or integrating zvec-grep into the production Agent Harness; P16 produces evidence and a decision only.
- Changing AIBroker provider adapters, credential-slot logic, model invocation argv, session allocation, resource selection implementation or telemetry ownership.
- Adding exact-resource selection fields to production `AIRoleRequest`; benchmark pinning uses the existing supported exclusion contract and validates the returned identity.
- Changing DevOrchestrator lifecycle, Planner, Reviewer, Adjudicator, Worker, remediation, daemon, watchdog, control, mobile, web or transition behavior.
- Benchmarking against DevO, AIResourceBroker, LabDemo, xray-hw-platform or any other production source tree.
- Installing, vendoring or reimplementing zvec itself; the benchmark validates and wraps an owner-provided installation, and unsupported capability safely yields NO_PROMOTE.
- Claiming provider-internal file operations are observed when the Broker/harness does not report them.
- Model-graded scoring, undocumented subjective rankings or reputation-based resource ordering.
- Automatic creation of Windows user accounts or storage of account passwords; the owner provisions the account and Scheduler credential, while supplied scripts manage only the explicit ACL/firewall boundary.
- Dynamic or temporary per-trial ACL and firewall changes; the security boundary is statically provisioned and read-only audited during runs.
- CI scheduling, dashboards, remote publication, cloud storage or external result uploads.
- Unbounded sample extension, automatic budget requests or treating unsupported zvec as an unresolved completion state.
- Editing `docs/development-workflow.md`, staged roadmap state or prior phase acceptance artifacts.

### Independent plan review
- Approved: Verified against the repository: the core contract the plan depends on exists and is executable today. `AIRoleRequest.excluded_resource_ids` is a real, validated field (src/dev_orchestrator/ai/contracts.py:112-141) and is honored by both transports (src/dev_orchestrator/ai/aibroker_subprocess.py:438, src/dev_orchestrator/ai/execution_transport.py:263), so exact-resource pinning by exclusion plus returned-identity validation is a supported use of the existing boundary rather than a new production interface. `AIRoleResult` already carries every evidence field the plan records (dispatch/decision/execution/session IDs, resource_context, usage/usage_source, quota and rate-limit observations, failure_classification, timestamps) (contracts.py:145-191), and `AIBrokerExecutionPort.execute` exists (aibroker_subprocess.py:193, 215). The configured broker registry at runtime/aibroker-m1/resources-m2.yaml lists ~12 resources across agy/claude/copilot/codex/chatgpt/dsh, so the staged three-distinct-resource, four-role acceptance is provisionable, and runtime/ is gitignored so the machine-local ContainmentConfig lands correctly. The plan holds DevO ownership intact (no production import of aibench, no benchmark command, no AIRoleRequest field additions, no lifecycle/harness change), so a bad benchmark result cannot cause an architectural reversal in production. Traceability to agent/staged/P16.md is sound: frozen corpus/rubric/repeat policy before comparative runs, role-based evaluation, identical-prompt A/B with zvec as a discovery layer verified against exact source reads, separated capability-vs-retrieval metrics, machine-readable plus human-readable output, and a documented PROMOTE/NO_PROMOTE decision with native fallback preserved. Verification path is concrete and falsifiable: fake-Broker integration tests for discovery/exclusion/mismatch/drift, prompt-hash identity assertions, golden code-only scoring, containment-fixture tests, evidence-schema and byte-stable aggregation replay, plus pytest tests_py, compileall, git diff --check and graphify update . against the existing regression baseline. The PowerShell 5.1 failure memory (seed:p11b:rdc-powershell-5.1) is applied explicitly in validation. I weighed three candidate blockers and rejected all three. (a) Host security mutation (dedicated-SID deny ACEs on production roots, program-specific outbound-deny firewall rule, %ProgramData% state) is heavyweight but is the executable form of the staged requirements to never mutate production trees and to substantiate a local-only privacy claim; it is journaled before mutation, backed by original SDDL/firewall state, digest-checked on uninstall, static across runs, owner-provisioned and elevated, and its absence raises an explicit OWNER_GATE — so it is bounded and reversible, not an unguarded security-boundary failure. (b) Exclusion-based pinning depends on a stable snapshot; the plan fails closed on registry drift or returned-resource mismatch and aborts only the affected cell, which is the correct direction. (c) Terminal `capability_unsupported` -> NO_PROMOTE without escalation still requires the committed capability proof, a native-fallback pair with retrieval_applied=false, and the full three-resource role benchmark, so the capability half of staged acceptance is still delivered and the decision remains total and bounded; whether the owner accepts that as P16 closure is an acceptance-time judgment, not a design gap that forces the Worker to guess. NON_BLOCKING, carry into acceptance notes and Technical Review rather than remediation: (1) interface path drift — `AIBrokerExecutionPort` lives in dev_orchestrator.ai.aibroker_subprocess, not dev_orchestrator.ai.execution_port (which defines only the AIExecutionPort Protocol); the Worker should import from the actual module. (2) `snapshot_resources` has no verified DevO-side listing endpoint — the service transport exposes only /api/dispatch, /api/dispatches/{id} and /interrupt, so the Worker should source the snapshot and registry digest from the configured resources YAML (or a genuine Broker resource endpoint if one exists) and record which source it used. (3) The scratch-local benchmark Broker instance must bind a loopback port distinct from the live 127.0.0.1:8875 service and use its own telemetry DB and token so benchmark dispatches never merge into production telemetry. (4) Real-quota consumption by benchmark trials is shared with production usage; the fixed dispatch ceiling bounds it, but the run report should surface consumed quota explicitly. None of these require design decisions; each is resolvable in code and verifiable by the planned tests. A Worker can begin safely without guessing the core contract.
