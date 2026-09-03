# Bridge lease authority remediation

Status: AUTHORIZED — bounded remediation only
Authority: docs/BROWSER_BRIDGE_CHATGPT_BINDING_DESIGN.md + tests_py/test_bridge_fresh_reviewer_regressions.py

Fix exactly the fresh Reviewer lease-authority defects:
1. BrowserBridgeStore.respond() must reject an expired claim even if state/token/nonce still match. Expired response must not mutate queue state.
2. Expose lease_expires_at in claim/renew transport metadata so the Userscript can reason only about transport authority.
3. Userscript renewal must inspect the HTTP result. 200 continues; 409/authoritative rejection abandons the stale claim immediately; transient network/5xx may retry only inside the current lease safety window, otherwise abandon.
4. Add a small pure helper such as classifyRenewResult plus an explicit abandonClaim path so the failure policy is testable. Do not parse or apply workflow decisions.

Acceptance:
- frozen fresh Reviewer regressions GREEN;
- all existing bridge tests and full tests_py GREEN;
- PowerShell telemetry 10/10 and Web selftest PASS;
- same-PID bridge daemon PASS;
- Userscript node --check + adapter tests PASS;
- real LabDemo monitor remains read-only;
- git diff --check PASS.

Non-goals: PHASE_AUTO, event dispatcher, real ChatGPT send, project-specific code, Worker start, hardware actions, restoring Codex WIP. Stop after remediation evidence for a fresh Reviewer.
