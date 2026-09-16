# P12.5 Self-Host Operational Acceptance

Status: **READY_TO_RUN**

Owner authorization: **START P12.5 / 2026-09-15**

Goal: prove the canonical `main` deployment can safely operate DevOrchestrator as its own managed project, with P11 accounting and the P12 unified control surface observable from the running daemon.

Scope:
- Add a small read-only self-host smoke/acceptance utility under `ops/` that checks the local 8770 Control API and 8875 AIBroker surface without mutating lifecycle state.
- Verify the smoke result distinguishes daemon/control availability, AIBroker resource visibility, execution visibility, accounting availability, project identity, and warnings.
- Keep the utility machine-local friendly: loopback endpoints configurable, bounded timeouts, deterministic non-zero exit on failed required checks.
- Add focused automated tests for healthy and unavailable/malformed endpoint cases without requiring real providers.
- Document the minimal canonical self-host startup/verification commands and the fact that `C:\work\github\DevOrchestrator-dev` is currently the canonical `main` worktree.
- Preserve P11 accounting, P12 single-daemon authority, stale-state guards, audit recovery, owner-gate semantics, and existing 8770/8875 contracts.

Acceptance:
- The utility performs only GET/read-only health checks and cannot issue owner/lifecycle mutations.
- Healthy deterministic fixtures cover 8770 monitor/project/accounting plus 8875 resources/executions.
- Failure fixtures cover unreachable endpoint and malformed/partial response with clear diagnostics and non-zero status.
- Running the utility against the locally deployed daemon reports PASS for required self-host checks.
- Focused tests pass, then the relevant full regression passes, along with Python compilation and `git diff --check`.

## Approved executable design

Add a deterministic, read-only self-host acceptance utility that verifies the canonical main deployment through the 8770 unified Control API and the 8875 AIBroker diagnostic API, with fixture-based tests and concise operator documentation.

### Implementation steps
- Add a standard-library Python utility under ops/ with configurable loopback Control and AIBroker base URLs, project ID, expected repository path, expected branch, and a bounded request timeout; default to 127.0.0.1:8770, 127.0.0.1:8875, project devorchestrator, the repository containing the utility, and branch main.
- Restrict both configured endpoints to loopback HTTP targets, construct fixed allowlisted paths, and issue GET requests only; do not import or call lifecycle, owner-gate, command-submission, daemon-start, or persistence APIs.
- Read GET /api/v1/control/overview and produce separate required checks for daemon health, enabled/non-degraded control authority, fresh monitor state, exact project identity, P11 accounting availability, and broker resource/execution visibility through the unified surface. Preserve the overview warnings as a distinct non-mutating diagnostic category rather than hiding or inferring their meaning.
- Read GET /api/resources and GET /api/executions directly from 8875, require valid JSON objects with their respective list fields, and report visibility/counts without requiring an active execution or inferring provider health, quota, usage, or cost. Do not require exact temporal equality between direct broker results and the 8770 proxy snapshot.
- Emit one deterministic structured result containing overall PASS/FAIL, independently named check outcomes, diagnostics, and warnings. Return zero only when every required check passes; return a stable non-zero status for unavailable, non-2xx, malformed, partial, stale, degraded, or identity-mismatched required evidence, with secrets and response bodies excluded from diagnostics.
- Add focused tests using ephemeral loopback HTTP fixture servers and subprocess invocation of the utility. Cover a fully healthy overview/resources/executions fixture, exact GET-only request paths, warning preservation, unreachable Control and broker endpoints, malformed JSON, missing or incorrectly typed required fields, stale/degraded monitor or control state, accounting unavailability, and project path/branch/ID mismatch without starting providers.
- Update README.md with the canonical worktree C:\work\github\DevOrchestrator-dev on main, the minimal PowerShell commands to set PYTHONPATH, validate configuration, start or inspect the unified daemon, and run the new acceptance utility, plus the expected PASS/non-zero behavior and endpoint override examples.
- After implementation and tests, run graphify update . so the repository knowledge graph reflects the new utility, test, and documentation relationships.

### Interfaces / contracts
- Operational CLI: ops/self_host_acceptance.py with loopback-only Control URL, broker URL, project identity, expected repository/branch, and bounded-timeout options; defaults target the canonical local self-host deployment.
- Read dependencies: GET http://127.0.0.1:8770/api/v1/control/overview, GET http://127.0.0.1:8875/api/resources, and GET http://127.0.0.1:8875/api/executions. Existing 8770 and 8875 response contracts remain unchanged.
- Result contract: deterministic JSON with an overall status, discrete daemon/control, project identity, accounting, broker resources, broker executions, and warnings sections; required-check failures produce clear diagnostics and a non-zero process exit.
- No write interface is introduced: the utility cannot submit /api/v1/control/commands, owner-gate approvals, lifecycle actions, pairings, heartbeats, or any non-GET request.

### Validation plan
- Run the focused suite with python -m pytest tests_py/test_self_host_acceptance.py -q and verify healthy fixtures exit zero while unreachable, malformed, partial, stale/degraded, and identity-mismatch fixtures exit non-zero with the expected categorized diagnostics.
- Against the running canonical deployment, execute the utility from C:\work\github\DevOrchestrator-dev and confirm overall PASS for devorchestrator on main, fresh daemon/control state, available accounting, and visible 8875 resources/executions while retaining any reported warnings.
- Run the relevant full regression with python -m pytest tests_py -q.
- Compile Python sources with python -m compileall -q src ops tests_py.
- Run git diff --check and inspect the final diff for accidental lifecycle, owner-gate, accounting, audit, stale-state, daemon-authority, or generated-secret changes.
- Run graphify update . after code changes and confirm it completes successfully.
- Execute every validation command as a separate PowerShell statement with explicit exit-code handling; do not use && or || under Windows PowerShell 5.1, per VERIFIED_FAILURE_MEMORY provenance seed:p11b:rdc-powershell-5.1.

### Risks / failure modes
- An overly permissive parser could report PASS for partial or semantically unavailable responses; required fields, types, availability, monitor freshness, and control degradation must therefore fail closed with category-specific diagnostics.
- Windows path casing and separator differences could create false project-identity failures; compare resolved normalized paths while still requiring the configured project ID and main branch exactly.
- The 8770 proxy and direct 8875 reads occur at different instants, so strict payload equality would be flaky; validate independent visibility and provenance instead of snapshot equality.
- Treating every warning as failure could reject an otherwise observable deployment, while ignoring degraded required sources could mask unsafe operation; preserve warnings separately and fail only when a required check is unavailable, stale, degraded, or malformed.
- Allowing arbitrary remote URLs would expand the trusted boundary and could expose local operational probing; reject non-loopback endpoints.
- PowerShell 5.1 rejects && and || pipeline chains; validation and documented commands must use PowerShell-safe sequencing, preserving VERIFIED_FAILURE_MEMORY provenance seed:p11b:rdc-powershell-5.1.

### Out of scope
- Changing any existing 8770 or 8875 API schema, route, authentication rule, command envelope, or lifecycle behavior.
- Starting, stopping, restarting, configuring, or mutating the DevOrchestrator daemon, AIBroker, providers, projects, owner gates, audits, accounting ledgers, or conversation bindings.
- Adding remote-access support, TLS, credentials, provider health inference, quota/cost inference, or a general monitoring service.
- Reworking P11 accounting, P12 command authority, stale-state guards, audit recovery, dashboard UI, broker routing, or provider selection.
- Attaching additional managed projects, resuming hardware projects, or validating real provider execution; automated coverage remains deterministic and provider-free.

### Independent plan review
- Approved: All acceptance criteria map to concrete plan steps: the read-only ops/self_host_acceptance.py utility with loopback-only enforcement is specified in steps 1-2; the three required GET endpoints (GET /api/v1/control/overview, GET /api/resources, GET /api/executions) are confirmed to exist in server.py and their response shapes are known; warning-preservation, stale-monitor, degraded-control, and accounting-unavailability fail-closed behaviors are explicitly called out in steps 3-5; fixture-based tests with ephemeral loopback servers and subprocess invocation cover the full healthy+failure matrix in step 6; the VERIFIED_FAILURE_MEMORY PowerShell 5.1 constraint is carried into both the risks section and validation step 7 with explicit sequencing guidance; out-of-scope exclusions correctly bound the utility away from lifecycle, owner-gate, and mutation surfaces; and validation includes both the focused test suite and a live run against the canonical deployment. No BLOCKING gaps were identified: remaining details such as exact field names in the overview JSON, local exception wording, and test fixture helper naming are NON_BLOCKING implementation choices the Worker can resolve with reference to the existing server.py contracts.
