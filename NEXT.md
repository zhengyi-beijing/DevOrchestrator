# DevOrchestrator current handoff

Canonical live handoff files are under `agent/`:

- `agent/CURRENT.md` — accepted current state and evidence;
- `agent/result.md` — result of the most recent bounded slice;
- `agent/next.md` — next authorized/candidate bounded work.

Current baseline: **P16 AI Capability Benchmark Project is closed with a
documented `NO_PROMOTE` decision (`capability_unsupported`,
`evidence_gate_failed`); the last benchmark commit is `78aeda5`. Next
authorized work is P16.7 Self-Healing Project Activation & Readiness, then
P16.8 and P16.9.**

Task lineage to date: P12.6 -> P12.7 -> P13 -> P13.5 -> P14 -> P14.5 -> P14.6
-> P15 -> P16 (closed).

Staged successor chain in `agent/staged/roadmap.json`:
P16 -> P16.7 -> P16.8 -> P16.9 -> end of roadmap. P16.9 was linked into the
chain at `f909d74`; before that the chain terminated at P16.8 and the watchdog
work would never have been handed off.

P16.7, P16.8 and P16.9 share one objective: a single owner Start/Continue must
survive transient and recoverable failures without another owner message.
P16.7 covers activation/readiness self-healing and structured blockers, P16.8
the golden-path lifecycle plus durable ExecutionContext/ExecutionIntent, P16.9
watchdog execution-loss invariants. All three exist as staged specs only — none
of their core contracts (`execution_state`, `ExecutionIntent`,
`ExecutionContext`, `ORPHANED_PROJECT_STATE`, `WORKER_VANISHED_*`,
`explain-block`) appear in `src/` yet.

## Blocked state as of 2026-09-23

P16.7 is not progressing, and the projected lifecycle does not show why.

- The P16.7 Planner succeeded (`ai_plan:969c6860`, HEAD `f093038`, 2026-09-22
  23:23 UTC, `claude/default/opus`) and produced a complete plan.
- Plan review then failed on a transient broker timeout: `broker service
  invocation failed for http://127.0.0.1:8875/api/dispatch: timed out`. One
  attempt, no failover. The plan has been `state=failed` since 2026-09-22
  23:39 UTC.
- Ports 8770 and 8875 are listening again, so the timeout was transient and the
  review is retryable.

Two projection defects hide the above and show a resolved gate instead:

- `.devorch/status.json` reports `OWNER_GATE / WAITING_REVIEW` on task P16. The
  gate originates from review `ai_review:...auto-58add345...:execute` (task
  P16, HEAD `795819c`, 2026-09-21), which asked the owner either to provision a
  dedicated Windows account/ACL/firewall boundary or to accept P16 closure on
  `NO_PROMOTE`. The owner resolved this on 2026-09-22 — see
  `docs/P16_BENCHMARK_ACCEPTANCE.md` item 9 and commit `78aeda5` — but the
  review record was never closed, so it still projects.
- `core/lifecycle_projection.py:8` `_latest()` selects role records by
  `project_id` only, never by `task_id` or `head`, so a P16-era review governs
  the P16.7 projection.
- `core/lifecycle_projection.py:77` projects `PLAN_FAILED` only when
  `lifecycle == base`. Line 70 has already moved `lifecycle` to `OWNER_GATE`,
  so a P16.7 plan failure can never surface.

This is the case P16.8 already names: terminal-state closure, and "DONE treats
stale review descendants as audit history only".

## Operational notes

- The daemon is live (pid 45612, started 2026-09-23 07:12 local), tracks HEAD
  `f909d74` and sees a clean worktree.
- The monitor process (pid 13676) has been dead since 2026-09-20 00:58 UTC.
  `.devorch/projects/devorchestrator.json` is consequently frozen at P14.5 with
  empty git fields and is not current truth; `.devorch/status.json` is.
- DevOrchestrator is **not** owner-paused (resumed 2026-09-21 06:07 UTC).
  `suppress_static_starts` remains true.
- Watchdog reports `state=ok, attempts=0, last_diagnosis=null` while the failed
  P16.7 plan sits stalled, because an `agent/next.md` mtime touch counts as
  activity evidence. This is the blind spot P16.9 targets.
- Review IDs are accumulating prefixes
  (`ai_review:ai_review:ai_review:auto-...:execute`); id derivation re-prefixes
  somewhere.
- Local commits are ahead of any remote. Nothing is pushed automatically.
- `reviewer_harness` is demonstrated but not enabled in `config/projects.json`;
  enabling it is a deliberate future decision.

The historical P2-only dashboard milestone is superseded by the current
multi-project daemon + Browser Bridge + Response Consumer/Decision Guard
implementation. For the authoritative current state and evidence see
`agent/CURRENT.md`; for the roadmap sequence see `docs/backlog.md`.
