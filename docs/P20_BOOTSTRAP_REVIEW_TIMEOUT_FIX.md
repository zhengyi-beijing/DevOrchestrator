# P20 bootstrap: read-only Plan Review terminal timeout

P20.1 is not implemented or accepted yet. These startup repairs are outside the
future isolated live soak and do not satisfy its zero-intervention requirement.

## Observed failure and bounded repair

The first remediated independent Plan Review lost its HTTP response at
2026-10-03T21:11:09Z while Broker still reported running; Broker later recorded
failed / `Codex turn timed out` at 21:11:30Z. No approval was inferred. Per-dispatch
full resource probing had consumed about 80 seconds before the provider's
900-second budget, exceeding the existing 60-second HTTP margin. Machine-local
configuration now skips that repeated full probe after startup health probing;
role deadlines and retry budgets are unchanged and the original config is backed
up outside Git. The next review returned the genuine terminal provider failure
at 21:33:22Z, proving the transport wait no longer hid the role's outcome.

The provider's exact terminal failed error was not classified on the normal
result path. A local exact-error mapping in `_run_plan_review` now identifies
`Codex turn timed out` as temporary resource unavailability, using the existing
finite failover budget, exact resource exclusion, preserved candidate plan,
planner context and provider-independent review. Non-failed statuses, absent resource
identity and unrelated semantic timeout do not retry. Worker/transport failure
classification is unchanged; no potentially live Worker is re-dispatched.

A reviewer-created Graphify query child had consumed 2311 CPU seconds since
04:44 local time. Its exact two process identities and command were verified,
then only those read-only children were terminated. Daemon and user processes
were preserved. This is bootstrap evidence, not an automatic recovery claim.

## Verification

Eight contract tests reproduce terminal timeout recovery, existing three-attempt
resource budget, zero failover budget, valid cancelled/no_candidate statuses, missing resource identity,
semantic timeout and conservative Worker classification. RED on canonical
source: two assertions failed and the zero-budget test passed. Prospective
isolated module GREEN: seven passed. A cross-directory prototype run re-imported
canonical source through tests_py/conftest.py; those two expected RED failures
are retained and not treated as repaired-source regression. Final verification
must run against the installed canonical patch.

Full pre-patch tests_py baseline: 1712 tests and 130 subtests passed in 716.78s.
Independent technical review targets the immutable prospective patch plus source
before/after hashes in runtime/p20-drafts/patch-manifest.json; its verdict and
canonical regression evidence must be recorded before commit/resume.

## Remaining P20 phase inputs

- Resource-failed review continuation preserves the candidate but resets semantic
  remediation round/rejection history. P20.3 must carry the total semantic budget
  across recovery/restart and use read-only unresolved-blocker delta adjudication
  after at most two remediation rounds; it must not restart a full-plan loop.
- A HTTP failure while Broker still runs needs bounded reconciliation or an exact
  durable actionable gate. PENDING_DESIGN with an unhandled failed plan is a
  silent-stall defect, not an accepted recovery outcome.
- Tool children and timed-out read-only harness sessions need bounded cleanup and
  durable correlation. Unknown Worker liveness must remain conservative.
- Two terminal Codex timeouts are manually captured in the existing incident
  family fam-689a96fe6ea9, recurrence 2, with no synthetic candidate. Other boot
  incidents and their runtime references are retained. They are not automatic
  failure-harvesting or live soak acceptance evidence.
- One bootstrap capture mistakenly passed flat policy keys to the legacy nested
  incidents configuration API. It generated a quarantined runtime-only synthetic
  candidate cand-7dd84495b777. It is explicitly ineligible for P20 acceptance and
  must never be promoted; preserve its historical evidence. P20.2 must reject
  vacuous synthetic oracles and verify concrete replay corpus evidence instead.

## Control-Plane Impact
- Invariants: CURRENT_TASK_MATCHES_ACTIVE_EXECUTION, TERMINAL_TASK_HAS_NO_RUNNING_EXECUTION, PENDING_DESIGN_NOT_EXECUTING, SUCCESSOR_HANDOFF_LINEAGE_VALID, NEXT_TASK_WITHOUT_HANDOFF, SINGLE_ACTIVE_LIFECYCLE_OWNER
- Transition boundaries: authority_reconciliation, worker_launch, owner_gate_transition
- Fault scenarios: CPF-01, CPF-02, CPF-03, CPF-04, CPF-05, CPF-06, CPF-07, CPF-08, CPF-09, CPF-10
- Convergence evidence: Only a known terminal read-only reviewer failure uses existing bounded resource failover; unknown/semantic/missing-identity failures preserve fences; no Worker retry path changes.

## Technical Review remediation evidence

Round 1 returned CHANGES_REQUIRED (one blocking test-validity gap): AIRoleResult
`unknown` was invalid and the negative test accidentally used the generic port
exception path. The test now uses valid cancelled/no_candidate statuses and
asserts unchanged failure classification. Those assertions reproduced two real
RED failures (2 failed, 6 passed). Adding the explicit `status == failed` guard
then passed 98 canonical regression tests in 32.27s. Source and prototype now
have identical LF byte hashes, recorded in the manifest. Round 2 must accept
this repaired evidence before commit/resume. No failed run is discarded.

Repeated Codex timeout resource is temporarily disabled through the existing
Broker resource-control API only after all dispatches became terminal. The
previous null override and exact restore API/body are retained in untracked
runtime/p20-codex-resource-quarantine-evidence.json. Startup health probes run
once after the registry reload; per-dispatch full probing stays disabled. This
forces an available eligible different provider for the next independent Plan
Review; author context, quality, deadlines and independence remain unchanged.
Re-enable only after bounded read-only harness/tool cleanup has been verified.

## Accepted bootstrap closeout

Independent Anthropic round 2 ACCEPT: no blocking findings. Four NON_BLOCKING
notes are retained in docs/evidence/P20_READONLY_TIMEOUT_TECHNICAL_REVIEW_R2.json
(planner timeout handling as a P20 phase input, retained historical draft,
self-describing regression invocation, CWD-dependent test import/style).
Canonical regression: 98 passed in 32.27s; eight final contract tests included.
Exact selection/command and raw hashes are in the review evidence. Compileall,
diff check and Graphify update passed. The old draft is preserved as historical
pre-remediation evidence; only the tracked eight-test source and its recorded
hash are authoritative. This ACCEPT does not accept P20.1 or any live soak.
