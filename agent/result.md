# RESULT — Browser Bridge lease remediation

Verdict: **ACCEPTED**

The 2026-09-03 interrupted lease-authority remediation has been resumed and verified.

Blocking defects fixed:
1. `BrowserBridgeStore.respond()` rejects expired claims even when state/token/nonce still match.
2. Claim/renew expose `lease_expires_at`; the ChatGPT Web adapter inspects renew results and fails closed: 200 continues, status 0/5xx retries only inside the current lease window, authoritative rejection such as 409 abandons immediately.

Acceptance evidence on XLabServer:
- frozen fresh Reviewer regressions 2/2 PASS;
- original Reviewer regressions 4/4 PASS;
- ChatGPT adapter tests 3/3 PASS;
- full `tests_py` 52/52 PASS;
- telemetry selftest 10/10 PASS;
- Web selftest PASS;
- unified same-PID bridge daemon test PASS;
- real LabDemo monitor read-only evidence PASS;
- `node --check` PASS;
- `git diff --check` PASS.

Independent fresh Reviewer found no blocking defect in the remediation content. Its sandbox could not execute the full runtime gates, but those gates were executed separately on the real XLabServer and passed.

Non-blocking follow-ups: strengthen Userscript renew-policy behavioral tests; assert `lease_expires_at` in HTTP tests; live DOM duplicate-message handling remains a separate deployment gate; long-term response retention may need pruning.

GitHub push/authentication is now operational via the ts-pc-zy HTTPS CONNECT proxy; the current branch is present on origin.

Next phase is bounded to `WORKER_DONE → Event Dispatcher → WebSolRequest → Bridge`. No response application or PHASE_AUTO is accepted yet.