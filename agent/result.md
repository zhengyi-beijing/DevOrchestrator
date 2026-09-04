# RESULT — ChatGPT Web duplicate/reclaim remediation

Verdict: **ACCEPTED**

Problem reproduced in the live POC: after a claim lease expired, the same Bridge request could be reclaimed and inserted into the ChatGPT conversation again.

Remediation:
1. Freeze the failure contract in `b07adb4`.
2. Add binding/request-scoped submitted evidence to `browser/chatgpt-web-adapter.user.js`.
3. Check storage and current user-message DOM before inserting a reclaimed prompt.
4. Resume waiting for the existing ChatGPT response under the new claim token.
5. Record submitted state only after a successful `insertAndSubmit()`.

Acceptance evidence:
- focused duplicate/Bridge tests: **11/11 PASS**;
- full `tests_py`: **63/63 PASS** on XLabServer normal environment;
- userscript Node syntax check: PASS;
- `git diff --check`: PASS;
- live dedicated binding `6a9a5c4a-b360-83ea-82cf-111fff98eed3`;
- first and reclaimed claims used distinct tokens, proving real reclaim;
- Bridge reached `responded` with the original response;
- UI Automation counted exactly one request message after excluding accessibility clones;
- fresh Reviewer: **ACCEPT**, no blocking defect.

Non-blocking carry-over: bound lost-send recovery, submitted-mark cleanup, tiny post-send/pre-mark crash window, stronger behavioral adapter test, and routine retention/pruning.
