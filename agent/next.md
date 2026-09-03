# Python v1 Reviewer remediation — Git porcelain activity parsing

STATUS: EXECUTABLE REMEDIATION
SCOPE: Python v1 P2 Compatibility Slice only

## Defect

Reviewer reproduced a real parity bug in `git_changed_activity_utc()`: a tracked worktree line such as ` M alpha.txt` is `.strip()`-ed before fixed-column parsing, so the two porcelain status columns shift and the path is truncated. The new frozen `tests_py/test_git_activity.py` fails because activity returns `None` for a modified tracked file.

## Required fix

Parse Git porcelain v1 paths while preserving the leading two status columns. Keep rename handling and quoted paths compatible; do not change Git mutation/read-only policy. Make the new regression GREEN without weakening it.

## Acceptance

- Full `tests_py` suite GREEN (now 7 tests).
- Existing PowerShell telemetry/web self-tests remain GREEN.
- Real LabDemo one-shot monitor remains read-only.
- `git diff --check` exit 0.
- No P3/PHASE_AUTO/AgentBackend implementation and no LabDemo phase/hardware action.
