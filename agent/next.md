# P8 D1 Final Review UTF-8 Remediation

Status: **COMPLETED ? FINAL REVIEW ACCEPTED** (Ready for owner-gated promotion)
Timebox: **Completed within timebox**

Goal: remove the Windows GBK reviewer lifecycle blocker and obtain a clean final independent review for D1.

Scope:
- in `src/dev_orchestrator/ai/aibroker_subprocess.py`, force the Broker subprocess/reconciliation environment to UTF-8 (`PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8`) without changing provider selection;
- add a regression test proving Unicode reviewer output such as `✅` cannot fail due to the Windows locale code page;
- preserve the existing D1 Progress Channel and broker-native status behavior;
- run focused tests, full `python -m unittest discover -s tests_py`, `node --check browser/chatgpt-web-adapter.user.js`, and `git diff --check`;
- update agent files and commit locally; do not push.

Safety boundary:
- modify only `C:\work\github\DevOrchestrator-dev`;
- do not modify/restart/promote the stable controller;
- no physical hardware actions.

Acceptance:
- UTF-8 environment is applied to both dispatch and reconciliation Broker subprocess calls;
- Unicode regression is covered;
- all D1 regressions remain green;
- worktree clean after local commit;
- independent reviewer returns a valid terminal decision instead of a codec lifecycle error.

Final independent Opus review: **ACCEPT** on code commit `6b296d8`; 213 tests, Node syntax check, and `git diff --check` passed on the reviewed worktree.
