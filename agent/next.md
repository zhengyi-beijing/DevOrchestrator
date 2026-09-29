# P19 — P19.1 Remote HTTP/HTTPS Gateway + Web UI

Status: **READY_TO_RUN**

Predecessor: P18
Successor: P19.2

## Objective

Add a production-quality, loopback web console for the bounded native machine
operations delivered by P18. The browser UI must reuse the existing Control API,
transport selector, durable job service, staged-write/CAS path, audit trail, and
lifecycle authority. It must not create a second controller or expose an
unrestricted shell or filesystem.

This staged task retains the lifecycle ID `P19` for compatibility with the
already-activated successor handoff from P18; conceptually it is **P19.1**.
P19.2 is the separately staged Remote MCP Interface and follows P19.1 without
expanding or delaying P19.1 except where preserving the shared service boundary
requires it.

## Architecture boundary

```text
same-origin browser UI
  -> existing short-lived browser session + CSRF authorization
  -> existing /api/v1/control/transport/* routes
  -> shared DevO control/service layer
  -> existing transport selector / ExecutionTransport
  -> LocalTransport or SSHTransport
```

- Never disclose or send `runtime/control/api-token` to browser JavaScript.
- Reuse the P18 route implementations; do not duplicate execution, job, file,
  CAS, policy, capability, lifecycle, or audit logic in the UI.
- Keep shared DevO operations below protocol adapters so the later P19.2 MCP
  adapter and this REST/Web adapter can call the same control/service layer.
- This task does not change lifecycle authority, transition execution, successor
  rules, owner gates, invariant evaluation, or watchdog recovery.
- RDC remains outside the normal request path and must never be selected
  implicitly.
- Real X-ray, conveyor, detector-board, serial-device, or other hardware actions
  remain unavailable without the existing explicit human safety authorization.

## Scope

1. Add an Operations destination to the existing dashboard navigation and an
   accessible, responsive native-operations workspace.
2. Establish/reuse the existing same-origin browser session and attach its CSRF
   token to every privileged transport request. Fail closed on expired or
   missing sessions and allow the operator to reconnect without reloading.
3. Provide bounded forms for:
   - `exec` with project, host, allowlisted command reference, JSON parameters,
     working directory, timeout, and idempotency key;
   - `spawn`, returning a stable durable job ID;
   - `poll` and `cancel`, scoped to the selected project and exact job ID;
   - `read_file` with allowed-root path, offset, and byte limit;
   - `stat`;
   - staged binary upload followed by create-only or expected-digest CAS write.
4. Present request ID, selected transport, status, exit code, job ID, hashes,
   bounded stdout/stderr/output, timestamps, and actionable errors without
   rendering untrusted output as HTML.
5. Require explicit confirmation for effectful spawn, cancel, and write commit.
   Do not store operation history, file contents, secrets, or tokens in browser
   persistent storage.
6. Show recent sanitized transport-operation evidence from the existing P18
   observability endpoint and provide an explicit refresh control.
7. Keep command entry at the `command_ref` level. Do not add arbitrary argv,
   shell, environment-variable, executable-path, or raw filesystem authority.
8. Keep all existing dashboard observation and guarded lifecycle controls
   functional and visually consistent.
9. Document browser usage, security boundaries, session recovery, supported
   operations, RDC verification, and rollback.

## Security and failure behavior

- The console is available only from the loopback-bound unified daemon.
- Same-origin, fetch-metadata, HttpOnly cookie, SameSite=Strict, and CSRF checks
  remain mandatory for browser authority.
- All request bodies, timeouts, output, reads, writes, paths, concurrency, and
  job identity remain bounded by the existing P18 server and transport policy.
- Mutating requests use caller-visible idempotency keys. Writes require staged
  content plus exactly one precondition: `if_absent=true` or
  `expected_sha256`.
- A browser refresh must not replay a mutation. The UI may retain only
  in-memory form/result state for the active page.
- Authentication, policy, stale capability, path, CAS, job ownership, timeout,
  and transport failures must be shown as failures; never fall back to RDC.
- Output and file previews use text nodes/textContent only. Binary readback must
  be represented safely and remain bounded.

## Verification

- Focused server tests prove browser-session/CSRF acceptance and rejection for
  every operation family while bearer and scoped-capability behavior remains
  unchanged.
- UI contract tests cover required controls, no inline script/style regression,
  no master-token exposure, safe text rendering, confirmation gates, request ID
  display, and staged/CAS write sequencing.
- Browser-level acceptance exercises status/exec/spawn/poll/cancel/read/stat and
  staged/CAS write against the live loopback daemon from the rendered page.
- Acceptance evidence records request IDs, selected transports, job IDs, exit
  codes, hashes, and RDC call count. All machine operations must select Local or
  SSH; RDC call count must be zero.
- Run P19 focused tests plus P18 external-control/transport, P13/P14 job,
  dashboard, successor/lifecycle, transition-executor, watchdog, project-status,
  and control-plane invariant regressions; run `python -m compileall -q src
  tests_py ops`, JavaScript syntax checking, `git diff --check`, and
  `graphify update .`.

## Acceptance

- An authorized local operator can perform every requested native operation
  from the web page without learning the master bearer token.
- An unauthenticated, cross-origin, missing-CSRF, expired-session, out-of-scope,
  or replayed request fails closed.
- Exec/spawn accept only configured command references; read/stat/write remain
  inside configured roots; existing output/read/write/concurrency limits hold.
- Poll/cancel cannot cross project or job identity boundaries.
- Existing staged-write and CAS conflict semantics are preserved, including an
  arbitrary-binary round trip.
- Every operation exposes a request ID and durable sanitized audit evidence.
- No normal browser operation selects RDC, mutates a lifecycle ledger, bypasses
  an owner gate, fabricates a successor, or authorizes real hardware.
- The existing dashboard and P18 MCP/API clients remain compatible.
- P19.1 begins only through the normal P18 successor handoff, can complete
  without implementing P19.2, and hands off to the separately staged P19.2
  specification through the normal successor mechanism.
- P19.1 completes with a clean, committed, unpushed worktree.

## Approved executable design

P19.1 adds a loopback-only Operations console to the existing dashboard (web/index.html, web/app.js) that drives the already-implemented P18 /api/v1/control/transport/* routes through the existing same-origin browser session + CSRF authority, never the master bearer token. Server work stays minimal and fail-closed: (a) a method-scoped read exception in src/dev_orchestrator/web/server.py so same-origin browser GET/HEAD reads (read, stat, operations, hosts, capabilities) succeed even though browsers omit Origin on same-origin GET, while mutations keep a mandatory valid Origin; (b) one owner-only, read-only command-catalog route so the operator can pick an allowlisted command_ref without ever seeing argv and without letting a scoped capability enumerate the allowlist. All execution, job, CAS, policy, capability, audit, and lifecycle logic is reused unchanged; the UI is a presentation adapter. Lifecycle advancement to P19.2 is left entirely to the normal reviewed successor mechanism. Deliverables: focused server auth tests, node UI contract tests, a live-daemon browser-shaped acceptance script emitting zero-RDC evidence, and docs.

### Implementation steps
- Method-scoped browser read exception in src/dev_orchestrator/web/server.py. Browsers omit Origin on same-origin GET, so _owner_authorized() currently rejects every browser GET read. Add a strict helper (e.g. _browser_read_context_ok) that returns True only when self.command in ('GET','HEAD') AND the Origin header is absent AND Sec-Fetch-Site is literally 'same-origin'. In the browser branch of _owner_authorized, accept cookie devorch_control + X-DevOrch-CSRF when _same_origin() holds OR that read-only helper holds. Mutating methods keep mandatory valid_origin(): an Origin-less POST must remain rejected even with a valid cookie, valid CSRF, and Sec-Fetch-Site: same-origin. Do not relax _fetch_metadata_ok (it returns True when the header is missing) and do not modify _validate_transport_auth, scoped transport capabilities, or nonce replay.
- Add GET /api/v1/control/transport/commands?project_id=&host_id= in _route, gated by _owner_authorized() only — deliberately not _authorized_control_read() — so a scoped transport capability, which carries its own allowed_commands restriction, can never use discovery to enumerate the full allowlist. Register it in _EXTERNAL_OPERATION_PATHS as 'transport_commands' so it is request-ID'd and audited. Build the payload field by field from dev_orchestrator.jobs.config.load_jobs_config(runtime/'execution-jobs.json'): command_ref, effect_class, max_runtime_seconds, declared parameter names/types from JobCommandConfig.parameters, and selectable=false for effect_class 'hardware'. Never emit argv, cwd, env, or executable paths. For a non-local host_id return an empty host-local list rather than probing SSH.
- web/index.html: append a seventh sidebar link 'Operations' with href '#operations-section' after '#system-section', and add <section id="operations-section" class="view-section" aria-labelledby="operations-heading" hidden> with panels for Session state, Exec, Spawn/Poll/Cancel, Read/Stat, Staged upload + CAS commit, and Transport evidence. Reuse existing panel/section-heading/kv-grid/timeline/btn classes and label every control. No inline <script>, no style=" attributes, no onclick attributes (CSP is default-src 'self' and test_p135_sidebar_nav asserts this).
- web/app.js: add 'operations-section' to VIEW_IDS so resolveViewId/isKnownViewHash/activateView and deep links work; leave LEGACY_HASH_ALIASES unchanged.
- web/app.js session hardening: replace the permanently cached controlCsrf with ensureControlSession({force}) plus resetControlSession(), and add controlFetch(path, {method, body, kind}) that always sends credentials:'same-origin', X-DevOrch-CSRF, Sec-Fetch-Site:'same-origin', and a caller-visible X-DevOrch-Request-ID matching ^[A-Za-z0-9._:-]{1,128}$. On 401/403 it invalidates the cached CSRF and retries exactly once; a second failure surfaces a visible 'Reconnect' control that re-establishes the session without a page reload. Server sessions are in-memory with a 15-minute TTL and die on daemon restart, so this path is routine.
- web/app.js pure builders: buildTransportRequest(kind, form) for exec|spawn|poll|cancel|read|stat|stage_write|write returning {method,url,body} with the exact P18 field names (project_id, host_id, command_ref, parameters, expected_working_directory, timeout_seconds, idempotency_key, operation_id, reason; path/offset_bytes/max_bytes as query params; content_base64/decoded_size_bytes/content_sha256; target_path/content_ref/if_absent/expected_sha256). Validate locally: parameters must parse as a JSON object, offsets/limits are integers, max_bytes clamps to 10 MiB, write requires exactly one of if_absent=true or expected_sha256, idempotency_key defaults to `ui-<kind>-<ts>-<rand>`. Export for node tests.
- web/app.js command entry: populate a datalist/select from the catalog route when it is available, but keep command_ref a plain typed field so an unavailable catalog degrades to server-side validation (unknown_command_ref) instead of blocking the console. Never render or accept argv, shell text, env vars, or executable paths.
- web/app.js result rendering: render every response with textContent only (reuse text()/kv()); show request_id, selected_transport, status, exit_code, job/operation id, content_sha256, decoded_size_bytes, timestamps, and the server message on failure. Clamp stdout/stderr previews in the UI on top of the existing server-side log caps. Never use innerHTML or insertAdjacentHTML for any server- or file-derived value; represent binary read-back as size + sha256 + a bounded safe preview.
- web/app.js confirmation gates following the existing CONFIRMED_ACTIONS/window.confirm pattern used by sendControl: require explicit confirmation for spawn, cancel, and the write commit, naming project_id, host_id, command_ref or target_path, and the chosen precondition (if_absent vs expected_sha256). exec, read, stat, catalog, and evidence refresh stay unconfirmed. Keep the prompt text in a pure function so node tests can assert the gate without a dialog.
- web/app.js staged write flow: File input -> ArrayBuffer -> chunked base64 -> crypto.subtle.digest('SHA-256') for content_sha256 -> POST /transport/stage-write -> hold the returned content_ref in a module-level variable only -> commit via POST /transport/write with if_absent or expected_sha256. Reject files whose encoded body would exceed the server's 16 MiB _MAX_STAGE_WRITE_BODY before sending. Clear the staged content_ref after a successful or failed commit so a second click cannot silently reuse it.
- web/app.js no-persistence rule: keep all operations state (CSRF, staged content_ref, last results, form values) in module-level in-memory variables. Do not touch localStorage, sessionStorage, indexedDB, or document.cookie, and do not auto-run any mutation on load, on the existing 10s refresh() interval, or on hash change — a browser reload must start clean and replay nothing.
- web/app.js evidence panel: GET /api/v1/control/transport/operations?limit=50 through controlFetch behind an explicit Refresh button, rendering sanitized rows (operation, request_id, selected_transport, status, exit_code, project_id, error) as text in a timeline list. Do not add it to the periodic auto-refresh.
- Update tests_py/test_p135_sidebar_nav.py to seven sidebar links and seven view sections in exact order with '#operations-section' appended, keeping the one-to-one nav/section mapping, no-inline-script, and no-inline-style assertions. Sweep for any other test that hardcodes the view count or VIEW_IDS membership and update it.
- New tests_py/test_p19_web_console.py (server-side, modeled on tests_py/test_p18_external_control.py setUp: make_server on 127.0.0.1:0 with a temp runtime). For every operation family — external/status, transport/commands, exec, spawn, poll, cancel, read, stat, stage-write, write, operations — assert a real browser session (POST /api/v1/control/browser-sessions with Origin + Sec-Fetch-Site, then cookie + X-DevOrch-CSRF) is accepted, and assert rejection for missing cookie, wrong/missing CSRF, expired session, cross-origin Origin, Sec-Fetch-Site: cross-site, and Origin absent with no Sec-Fetch-Site header.
- Add the two boundary test groups the review requires. (1) Origin-less mutation rejection: with a valid cookie + CSRF and Sec-Fetch-Site: same-origin but no Origin header, POST to /api/v1/control/commands, /api/v1/control/browser-sessions, /api/v1/control/adapter-pairings, and every /api/v1/control/transport/{exec,spawn,poll,cancel,stage-write,write} must fail closed, while the same headers on GET/HEAD reads succeed. (2) Capability scope: a valid scoped transport capability token (including one whose allowed_commands covers the project) must be rejected by GET /transport/commands, while its existing read/stat/operations access is unchanged; also assert bearer behavior is unchanged and poll/cancel across project or unknown job still return 403/404.
- Assert the catalog response body contains no argv, cwd, env, or executable path for any command, that effect_class 'hardware' rows are marked non-selectable, and that a non-local host_id yields an empty host-local list.
- New tests_py/test_p19_web_console_ui.py (node contract tests using the existing run_node/require(web/app.js) pattern): required element ids exist in index.html; 'operations-section' is in VIEW_IDS and resolveViewId maps its hash; buildTransportRequest emits the correct method/url/body and rejects both-preconditions, no-precondition, non-object JSON parameters, and oversized uploads; confirmation is required for spawn/cancel/write and not for exec/read/stat; a payload like <img src=x onerror=alert(1)> in stdout, error, or a path is rendered via textContent; app.js references no master api-token and performs no localStorage/sessionStorage/indexedDB/document.cookie write; stage-write must precede write; controlFetch re-authenticates at most once per call and never after a 5xx or timeout.
- New ops/p19_web_console_acceptance.py (modeled on ops/p18_external_control_acceptance.py but browser-shaped: urllib with an explicit cookie jar, Origin/Host/Sec-Fetch-Site headers, no bearer token). Establish a browser session, then exercise status, transport/commands, exec (git_status), spawn+poll to completion, spawn+cancel (external_control_sleep), read_file, stat, and a binary stage-write + if_absent CAS write followed by an expected_sha256 overwrite and a conflicting-precondition failure, each with a distinct X-DevOrch-Request-ID. Emit docs/evidence/P19_WEB_CONSOLE_ACCEPTANCE.json and docs/evidence/P19_WEB_CONSOLE_OPERATIONS.ndjson with request IDs, selected_transport, job IDs, exit codes, hashes, and rdc_call_count: 0; exit non-zero if any row selects anything other than local or ssh. Write only to a scratch path under the configured file root.
- Docs: add docs/P19_WEB_CONSOLE_DESIGN.md covering the same-origin browser-authority boundary, the method-scoped Origin-less read exception and why mutations still require a valid Origin, why command discovery is owner-only, session recovery, the supported operation set, confirmation and idempotency rules, staged/CAS semantics, RDC verification via docs/evidence, and rollback (revert the commit; no runtime or ledger migration). Amend docs/P18_EXTERNAL_CONTROL_API.md with the browser-session authorization path and the new owner-only transport/commands route, and point README/dashboard docs at the Operations destination.
- Close out without touching lifecycle authority. docs/P16_13_SUCCESSOR_CONSISTENCY_CONTRACT.md makes runtime/transition-executor.json the authoritative lifecycle/transition source and Markdown only a projection, so the Worker must not edit agent/next.md, agent/HANDOFF.md, agent/staged/* (including roadmap.json), or any runtime ledger, and must not publish a P19->P19.2 handoff by hand. Advancement to the staged P19.2 spec is left to the normal reviewed successor mechanism. Deliver a clean, committed, unpushed worktree on main.

### Interfaces / contracts
- New: GET /api/v1/control/transport/commands?project_id=<id>&host_id=<id> -> control envelope {host_id, project_id, host_local: true, commands: [{command_ref, effect_class, max_runtime_seconds, parameters: {name: type}, selectable}]}. Owner-only (master bearer or same-origin browser session); a scoped transport capability is rejected. Loopback-only, read-only, audited as external operation 'transport_commands'. Never exposes argv, cwd, env, or executable paths.
- Changed (narrowly, still fail-closed): src/dev_orchestrator/web/server.py::_owner_authorized browser branch accepts cookie + X-DevOrch-CSRF on GET/HEAD when Origin is absent and Sec-Fetch-Site is exactly 'same-origin', in addition to the existing valid_origin() case. For any mutating method a valid same-origin Origin remains mandatory. Missing Origin with no Sec-Fetch-Site header, or any same-site/cross-site value, remains unauthorized.
- Unchanged contracts: every /api/v1/control/transport/* request and response body, selected_transport reporting, X-DevOrch-Request-ID handling, master bearer authorization, scoped transport capabilities plus X-DevOrch-Nonce replay protection, _validate_transport_auth semantics, the MCP tool surface, and all lifecycle/control command routes.
- Browser request contract: HttpOnly cookie devorch_control (SameSite=Strict, Path=/api/v1/control) from POST /api/v1/control/browser-sessions, plus X-DevOrch-CSRF, Sec-Fetch-Site: same-origin, credentials: 'same-origin', a browser-supplied Origin on mutations, and a per-operation X-DevOrch-Request-ID. 15-minute in-memory server TTL; the UI re-establishes the session on demand.
- New web/app.js exports for node contract tests: buildTransportRequest, OPERATION_KINDS, confirmOperationPrompt, renderOperationResult, renderTransportEvidence, controlFetch, ensureControlSession, resetControlSession, plus VIEW_IDS extended with 'operations-section'.
- DOM id contract for web/index.html (stable names asserted by tests): opsSessionState, opsReconnect, opsProject, opsHost, opsCommandRef, opsCommandCatalog, opsParameters, opsWorkingDirectory, opsTimeout, opsIdempotencyKey, opsExec, opsSpawn, opsJobId, opsPoll, opsCancel, opsPath, opsOffset, opsMaxBytes, opsRead, opsStat, opsFile, opsStageWrite, opsTargetPath, opsIfAbsent, opsExpectedSha256, opsCommitWrite, opsResult, opsEvidence, opsRefreshEvidence.
- New evidence artifacts: docs/evidence/P19_WEB_CONSOLE_ACCEPTANCE.json and docs/evidence/P19_WEB_CONSOLE_OPERATIONS.ndjson, produced by ops/p19_web_console_acceptance.py with flags --base-url, --runtime-root, --project-id, --output, --operations-output.
- Lifecycle interface untouched: no route, file, or script in this task writes runtime/transition-executor.json, agent/next.md, agent/staged/*, owner gates, or invariant state.
- Service-boundary note for P19.2: all shared behavior stays in the existing control/transport/job service layer; the console adds no execution, job, or CAS logic a later MCP adapter would have to duplicate.

### Validation plan
- Focused: python -m pytest tests_py/test_p19_web_console.py tests_py/test_p19_web_console_ui.py tests_py/test_p135_sidebar_nav.py
- Security matrix must pass explicitly: Origin-less POST rejected on /api/v1/control/commands, /browser-sessions, /adapter-pairings, and all six /transport mutations; the same headers accepted on GET/HEAD reads; scoped transport capability rejected by /transport/commands while its read/stat/operations access is unchanged.
- P18 regressions: python -m pytest tests_py/test_p18_external_control.py tests_py/test_p18_selector_and_security.py tests_py/test_p18_transports.py tests_py/test_p18_binary_staging_and_write.py tests_py/test_p18_config_and_parameters.py tests_py/test_p18_canonical_identity.py
- Job/transport/dashboard regressions: python -m pytest tests_py/test_p13_execution_transport.py tests_py/test_p13_control_logs.py tests_py/test_p14_durable_jobs.py tests_py/test_p14_job_transport.py tests_py/test_p14_job_recovery.py tests_py/test_web.py tests_py/test_web_ui_refresh.py tests_py/test_unbound_web_ui.py tests_py/test_p12_control_actions.py
- Lifecycle/control-plane regressions: python -m pytest tests_py/test_staged_handoff.py tests_py/test_staged_roadmap.py tests_py/test_transition_executor.py tests_py/test_project_status.py tests_py/test_p17_invariants.py tests_py/test_p17_control_plane_declaration.py tests_py/test_watchdog.py tests_py/test_watchdog_recovery.py
- Whole-suite sanity: python -m pytest tests_py
- Static gates: python -m compileall -q src tests_py ops ; node --check web/app.js ; git diff --check ; graphify update .
- Diff hygiene check: git status and git diff confirm no change to agent/next.md, agent/HANDOFF.md, agent/staged/*, or runtime/transition-executor.json.
- Live acceptance: start the unified daemon via ops\start_devo_control_api.ps1, run python ops/p19_web_console_acceptance.py, and assert the evidence has a request ID per operation, selected_transport in {local, ssh} on every row, rdc_call_count == 0, matching hashes for the binary round trip, and a recorded CAS conflict failure.
- Manual browser pass at http://127.0.0.1:8770/#operations-section in Chrome or Edge: run status, catalog, exec, spawn, poll, cancel, read, stat, and staged+CAS write; confirm the master token never appears in DevTools network or storage, a forced session reset reconnects without reload, a page reload replays no mutation, and no persistent storage entries are created.
- Cross-check acceptance request IDs against runtime/control/audit.jsonl external_operation rows and runtime/logs/transport-operations.ndjson.
- PowerShell 5.1 safety (seed:p11b:rdc-powershell-5.1): never place && or || in a Windows PowerShell 5.1 command line; sequence steps and check $LASTEXITCODE explicitly, or invoke cmd.exe when cmd syntax is required.

### Risks / failure modes
- The Origin-less read exception is the one real security-surface edit, and _owner_authorized() gates mutating POST paths too. Mitigation: gate the exception on self.command in ('GET','HEAD') inside the helper itself so it cannot leak to mutations, keep valid_origin() mandatory for POST, and cover Origin-less POST rejection on every mutating route with tests.
- A loopback non-browser client holding a stolen cookie + CSRF could read files via the Origin-less GET path. Mitigation: require the literal Sec-Fetch-Site: same-origin header (never accept a missing header, so _fetch_metadata_ok must not be reused), keep the console loopback-only, and keep the 15-minute session TTL.
- Authorizing discovery through _authorized_control_read would let a scoped capability enumerate command refs outside its allowed_commands. Mitigation: owner-only authorization for /transport/commands plus an explicit restricted-capability rejection test; any future capability-scoped discovery must filter by allowed_commands and is out of scope here.
- Manually editing agent/next.md or publishing a successor handoff would contradict docs/P16_13_SUCCESSOR_CONSISTENCY_CONTRACT.md, where runtime/transition-executor.json is authoritative and Markdown is a projection. Mitigation: the Worker touches no lifecycle projection or ledger, and a diff-hygiene check enforces it.
- Browser sessions are per-process, in-memory, and expire in 15 minutes, so a daemon restart or an idle tab silently drops authority. The retry must not double-execute a mutation: re-authenticate and retry only on a 401/403 rejected-before-execution response, never on 5xx or timeout.
- The catalog could leak argv, cwd, or absolute executable paths if JobCommandConfig is serialized directly. Mitigation: build the response field by field and assert absence of argv/cwd/env in tests.
- Client-side base64 of a large file can freeze the tab and can exceed _MAX_STAGE_WRITE_BODY once encoded (~1.37x). Mitigation: enforce a pre-send raw-size limit derived from the server cap and encode in chunks.
- Any innerHTML use in the new renderers turns transport stdout, error strings, or file paths into stored XSS on the control plane. Mitigation: textContent-only renderers plus an explicit malicious-payload test.
- tests_py/test_p135_sidebar_nav.py asserts exactly six sidebar links and a one-to-one nav/section mapping, so adding Operations is a deliberate contract update; other tests may hardcode view counts and must be swept.
- window.confirm blocks headless node tests. Keep the confirmation decision in a pure, injectable function mirroring the existing describeGuardedAction/CONFIRMED_ACTIONS split.
- Live acceptance depends on the real allowlist (git_status, git_log, transport_compileall, external_control_sleep) and file root C:\work\github\DevOrchestrator-dev; a careless target_path could overwrite tracked source. Mitigation: restrict acceptance writes to a scratch subpath and assert the path before commit.
- crypto.subtle needs a secure context; http://127.0.0.1 qualifies in Chrome/Edge/Firefox, but if unavailable the staged upload must fail visibly instead of sending an unverified digest.
- No runtime/transport-hosts.json exists today, so the host selector will usually offer only 'local'. The UI must handle an empty or local-only host map, must never synthesize a host, and must never fall back to RDC.

### Out of scope
- P19.2 remote MCP endpoint, MCP SDK adoption, or any new MCP tool.
- Any manual lifecycle advancement: editing agent/next.md, agent/HANDOFF.md, agent/staged/* or roadmap.json, writing runtime/transition-executor.json, or publishing the P19->P19.2 successor handoff.
- TLS/HTTPS termination, non-loopback binding, reverse proxying, or remote authentication — the console stays loopback-only.
- Any change to lifecycle authority: ledger writes, transition execution, successor derivation, owner gates, invariant evaluation, or watchdog recovery.
- Capability-scoped command discovery or any change to scoped transport capability issuance, allowed_commands semantics, expiry, revocation, or nonce replay behavior.
- Relaxing Origin requirements for mutating requests, or any change to bearer authorization or the CSRF/cookie scheme itself.
- Enabling, exposing, or falling back to RDC for any browser operation.
- Arbitrary argv, shell text, environment variables, executable paths, directory listing, or raw filesystem browsing in the UI.
- New transport hosts or SSH provisioning.
- Any real hardware actuation (X-ray, conveyor/VFD, detector board, serial device) or relaxation of the hardware effect-class rejection.
- A second job scheduler, job state machine, retry policy, or duplicated CAS/staging implementation.
- Broad dashboard redesign or theming beyond adding the Operations destination; changes to the P15 mobile surface, the ChatGPT userscript, or existing bearer/MCP client contracts.
- Persistent browser storage of operation history, file contents, tokens, or form state.
- Introducing npm, a bundler, or a front-end framework; the UI stays dependency-free static HTML/CSS/JS with node only as a test runner.

### Independent plan review
- Approved: No BLOCKING findings. The revised plan resolves the security and source-of-truth blockers: Origin-less authorization is limited to GET/HEAD, mutations retain valid-Origin enforcement with explicit boundary tests, command discovery is owner-only, and successor publication remains with the authoritative lifecycle mechanism. Interfaces, sequencing, scope, regression coverage, zero-RDC evidence, and PowerShell 5.1 provenance are executable without core-design guessing. NON_BLOCKING acceptance notes: preserve _fetch_metadata_ok on the normal Origin-present browser branch, and ensure the Operations page exposes or invokes external/status because the manual browser acceptance names status without a dedicated DOM control.
