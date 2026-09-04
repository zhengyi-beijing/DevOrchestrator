# RESULT — Portable Integration Contract V1

Verdict: **ACCEPTED BY AUTOMATED CONTRACT**

Implemented against baseline `8d8fe98`.

Delivered:
1. Config-relative `repo_path` semantics for relocatable deployment layouts.
2. `validate-config --config <path>` as a read-only onboarding gate.
3. Fail-closed adapter/repository validation without requiring a browser binding.
4. A generic project example with no tracked LabDemo/XLabServer path requirement.
5. A public standalone-service integration contract and current README.
6. New integration tests using arbitrary temporary external Git repositories.

Key evidence:
- config-only external project monitor: PASS and observed repo stays unchanged;
- relative project path independent of process CWD: PASS;
- valid monitor-only project validation: PASS;
- missing repository / unknown adapter fail closed: PASS;
- full Python regression: **87/87 PASS**;
- Userscript syntax + Git whitespace checks: PASS.

Authority unchanged:
- no Worker start/restart;
- no `next_action` execution;
- no PHASE_AUTO;
- no stage crossing or hardware action.
