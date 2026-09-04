# NEXT — Response Consumer + Decision Guard

Status: **AUTHORIZED — design + TDD + implementation**

Goal: consume the raw Browser Bridge response and produce a validated Web Sol disposition without executing any Worker action.

Bounded chain:
`Bridge response → parse exact DEVORCH_WEB_SOL_RESPONSE marker/JSON → construct WebSolResponse → validate echoed identity → fresh repository truth → Decision Guard → disposition`.

Required behavior:
1. Match the exact request marker/request_id and parse one JSON object only.
2. Require exact echo of project_id/request_id/task_id/stage_id/branch/head/role/event/nonce.
3. Re-read fresh repository branch/HEAD/dirty state before accepting a decision.
4. Apply the existing decision/next_action compatibility rules fail-closed.
5. Return only a disposition/intention such as APPLY/IGNORE/STALE/STOP/REVIEW_REQUIRED/OWNER_GATE.
6. Preserve project/binding isolation and deterministic request identity.
7. Add fixture-only tests for valid, malformed, stale, dirty, mismatched, and owner-gated responses.

Strict non-goals:
- do not start/restart a Worker;
- do not invoke Agent Router;
- do not auto-execute NEXT/REMEDIATE/RETRY;
- do not implement PHASE_AUTO;
- do not touch LabDemo or hardware.

Carry-over hardening from live POC may be addressed only if it stays bounded and does not expand into actuation.
