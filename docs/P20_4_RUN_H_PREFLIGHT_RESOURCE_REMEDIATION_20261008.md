# P20.4 Run H preflight resource remediation

Date: 2026-10-08

## Outcome

Run H (`p20-live-20261008-h`) never entered the measurement window. Its
isolated initial Planner received `You've hit your weekly limit` from
`claude/default/opus` three times. DevOrchestrator did not classify that real
provider response as quota exhaustion, so it retried the same resource instead
of applying its existing bounded resource-failover path.

The isolated primary pipeline therefore never became active. Canonical soak job
`job-b88542ae1a5828b4` exited with code 3 and
`Primary real-AI pipeline has not started normally.` No Run H run file,
`started_at`, measurement clock, task evidence, fault disposition, or
reliability run was created. The canonical lifecycle converged normally to
`SOAK_EVIDENCE_UNRESOLVED` with no active role.

## Remediation

Commit `1422042` adds `weekly limit` to both resource-failure normalization
paths:

- direct Broker result normalization; and
- reconciled dispatch failure classification after restart or response loss.

The direct path remains guarded by `status=failed`. Classified failures retain
the exact resource identity, add it to Planner exclusions, and use the existing
bounded failover budget without consuming or resetting semantic remediation
budgets.

The ignored machine-local AIBroker configuration now permits the already
enabled and successfully exercised `copilot/default/claude-sonnet-4.6`
resource to serve the `planner` role at priority 50. The previous configuration
is preserved at
`runtime/config-backups/resources-m2-before-copilot-planner-20261008.yaml`
with SHA-256
`8eb2ad0716d6c9b5238f937d26640d30d0d1228b0cb4fe01c8060e1cef7382e0`.
AIBroker was normally restarted and the Copilot resource reprobed as available
with a reusable session.

## Verification

- AIBroker execution-port and Planner tests: 66 passed;
- all P20.4 tests: 124 passed;
- compile, diff, and Graphify update checks: passed.

Independent read-only Technical Review request
`p20-run-h-weekly-limit-review-20261008-r1` ran on
`copilot/default/claude-sonnet-4.6` and returned **ACCEPT** with zero BLOCKING
and zero NON_BLOCKING findings. Its dispatch was
`99af8920-7caf-4535-85dc-98c21cea5c05` and its execution was
`c05837c2-04ca-49cb-a65e-98829536c2fb`.

Run H's failed pre-measurement job and isolated evidence remain preserved. The
next attempt must use fresh Run I run/project/permit/path/job/source-request
identities and must start its own new six-hour clock.
