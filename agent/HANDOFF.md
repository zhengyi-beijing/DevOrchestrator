# P16.13 Handoff — Successor Consistency & Zero-Touch Handoff Recovery

Last updated: 2026-09-26. Branch `main`, worktree clean at `af91981`.

Status: **REVIEW ACCEPTED / AWAITING DAEMON RESTART**. Implementation and
independent Technical Review are complete. One closure gate remains and it needs
owner action, not more implementation.

Repository state is authoritative. Verify everything below before acting on it.

## 1. What was done in this session

Continued from the interrupted Codex session. Five commits on top of `9e5909a`:

| Commit | What it does |
| --- | --- |
| `f51a01b` | Derives every tick consumer from the lifecycle authority; adds the missing projection regression |
| `b5d42f2` | Remediates the four blocking review findings (B1-B4) |
| `981a342` | Closes the non-blocking follow-ups (N-A, N-B, N-D) from the delta re-review |
| `e8eece7` | Adds `ops/p1613_lifecycle_smoke.py` as a durable runtime smoke |
| `af91981` | Records review/remediation evidence in `agent/CURRENT.md` |

The inherited dirty `src/dev_orchestrator/daemon.py` diff was classified **A
(valid unfinished P16.13 work)** and committed as part of `f51a01b`. It was valid
but had no test coverage at all: the suite passed identically with and without
it. Without it the Supervisor advanced on `raw_summary` (no authority), and the
dispatch view and published projection both reported the predecessor `P16.12`.
The live daemon corroborated the defect independently: the authority read
`READY_TO_RUN` while `runtime/summary.json` published `RECOVERY_REQUIRED`.

Independent Technical Review returned `REMEDIATE` with four blocking findings.
The bounded, delta-only re-review of `b5d42f2` returned `ACCEPT` with all four
closed. Both reviews were performed by independent reviewers that were not given
the implementer's conclusions.

## 2. Current validation state

- Full Python regression: **1,301 tests + 99 subtests** passing.
- Focused lifecycle suite: **109 tests + 25 subtests** passing.
- `python -m compileall -q src ops tests_py` clean; `git diff --check` clean.
- Runtime smoke `python ops/p1613_lifecycle_smoke.py`: **33 of 35 checks pass**.
  The two failures are the undeployed-fix symptom described in section 3.
- Live `devorchestrator` authority: `current_task_id=P16.13`,
  `lifecycle_state=READY_TO_RUN`, `matches_authority=true`, no owner gate, no
  surviving predecessor owner. **The original P16.12 -> P16.13 incident is
  resolved.**

## 3. THE ONE BLOCKING ITEM: the canonical daemon still runs pre-fix code

The running daemon is **PID 26264**, started `2026-09-26T00:47:00Z`, i.e. before
any of this session's commits. It therefore does not have the recovery fence
loaded. Consequence, observed live and still growing:

```
linescanviewer     lifecycle_recovery_attempts count=133 state=failed  gate_id=None
xray-hw-platform   lifecycle_recovery_attempts count=133 state=failed  gate_id=None
devorchestrator    lifecycle_recovery_attempts count=1   state=recovered
```

The counters climbed 63 -> 91 -> 121 -> 133 over the course of the session. This
is the B1 loop, still live, purely because the fix is committed but not deployed.

**Required action (owner).** Restart the daemon so it loads the fence. In-session
restart was attempted and **denied by the environment's permission classifier
(`Interfere With Workloads`)**; it was deliberately not routed around. Zero
active executions were verified before the attempt, so a restart interrupts no
Worker.

`ops/stop-monitor.ps1` reads `runtime/monitor.pid`, which is currently **empty**,
so it will probably not find the process. The daemon's actual invocation is:

```
python -m dev_orchestrator daemon \
  --config C:\work\github\DevOrchestrator-dev\config\projects.json \
  --runtime-root C:\work\github\DevOrchestrator-dev\runtime \
  --web-root C:\work\github\DevOrchestrator-dev\web \
  --listen 127.0.0.1 --port 8770 \
  --bridge-listen 127.0.0.1 --bridge-port 8765 --interval 60
```

**Expected post-restart behavior.** Each gated project performs **at most one
more** recovery attempt (133 -> 134) because the legacy gate records predate the
`recovery_key` field, then fences permanently with `state="gated"`,
`fenced=true`, and a stable `gate_id`. Then re-run the smoke:

```
python ops/p1613_lifecycle_smoke.py
```

It should report **35 of 35**. The smoke judges boundedness structurally (attempt
is `recovered`, `gated`, or a transient wait) rather than by the absolute
counter, because a ledger written before the fence keeps its historical count.

## 4. Known open issues, by severity

### 4.1 Needs owner disposition, not code (liveness, per-project)

`linescanviewer` and `xray-hw-platform` each hold an accepted
`apply`+`next`+`next_task` decision whose actuation is durably `blocked`, and
**neither repository has `agent/staged/roadmap.json`**. `resolve_successor`
returns `kind="absent"`, so no successor lineage can be proven and recovery
cannot converge. Post-restart they will sit in a durable owner gate, which is the
correct fail-closed outcome. To clear them, the owner must either add
`agent/staged/roadmap.json` to those repos or dispose of the pending decision.

`linescanviewer` additionally has an authority gate
`CURRENT_TASK_MATCHES_ACTIVE_EXECUTION` (`repository_task_id=P16`,
`authority_task_id=P15`) for the same root cause.

### 4.2 Deferred NON_BLOCKING findings (still open in code)

- **N1 — Watchdog fails open on executor-state loss.**
  `src/dev_orchestrator/core/watchdog.py:1605-1610` swallows an `executor.state()`
  exception and sets `executor_state=None`; line `1681` then passes `{}`. With an
  empty ledger five of the six invariants evaluate `holds=True`, so the tick
  publishes a clean bill of health derived from absent evidence, and nothing is
  written to `prow["last_error"]`. This is the same class as B2 (which was fixed
  for `decisions_state`) but for `executor_state`. Fix: mark the invariant block
  `evidence_unavailable` and record the error instead of publishing "all hold".
- **N2 — `resolve_successor` ignores staged evidence when `roadmap.json` is
  absent.** `src/dev_orchestrator/core/successor_consistency.py:156` treats a
  staged `Predecessor:` claim as `inconsistent` only for
  `roadmap.kind in {"end_of_roadmap", "unlisted"}`; an absent file yields
  `kind="absent"`, so `transition_executor.py` takes the `else` branch and
  `_record_settled(outcome="task_complete")` durably claims the project complete
  despite unambiguous staged evidence. This contradicts `agent/staged/P16.13.md`
  scope line 13 ("fail closed on NEXT/next_task when roadmap advertises no
  successor but staged evidence indicates one"). **Coupled hole — fix both
  together:** `reconcile_roadmap_successor`'s rollback at
  `successor_consistency.py:232` only restores when `original` is non-empty,
  so a newly *created* `roadmap.json` whose validate/commit fails is left behind
  uncommitted, dirtying the tree and permanently blocking the clean-tree recovery
  precondition.
- **N-C — accepted fail-closed risk.** A legacy ledger with pending obligations on
  two *different* tasks and only an unconsumed cross-task barrier will now
  surface both, tripping `SINGLE_ACTIVE_LIFECYCLE_OWNER` and gating. Verified
  **not reachable** in any live project today (each has at most one surviving
  obligation on one task). Also, the `319b5e2` legacy bootstrap migration at
  `transition_executor.py:740` requires `not owners`, so a resurfaced obligation
  permanently blocks it.
- **Pre-existing, unchanged by this work:** `_prune_state`
  (`watchdog.py:1157`) does not prune `lifecycle_recovery_attempts`, so entries
  accumulate one per git HEAD; and the attempt counter is an unbounded int for
  transient reasons (by design, since a transient wait must keep retrying).

### 4.3 Weakest part of the evidence: unmapped acceptance criteria

This is the most important remaining gap for anyone claiming the recurring
lifecycle-consistency failure class is fully controlled.

- **Acceptance 4** ("successor Planner/Worker starts automatically with no owner
  continue") — the mechanism exists (`control_commands.py:246-260`,
  `automatic_review_handoff` -> `planner.start_deferred` ->
  `mark_handoff_consumed`) but **no test exercises it**.
  `test_I_and_J` calls `executor.mark_handoff_consumed(...)` by hand, which is
  precisely the step the criterion requires to be automatic. No live project
  completed an autonomous successor launch during the session either.
- **Acceptance 6** ("stale execution-loss state converges to resolved after
  authoritative handoff") — no test asserts an execution-loss finding
  transitioning to resolved as a consequence of an authoritative handoff;
  `EXECUTION_LOSS_RESOLVED` / `execution_loss_slots` are untouched by the P16.13
  suite.
- **Acceptance 8** ("end-to-end with zero manual intervention") — **no test
  crosses the Watchdog -> executor -> Planner boundary.** Every A-J test calls
  executor/resolver methods directly. `BoundedRecoveryActuationTests` does now
  drive the real `WatchdogCoordinator.advance` (this was the first coverage of
  that path at all), but it stops at the executor.
- **Acceptance 2** — the detect-and-do-not-settle logic exists and was read, but
  no test asserts the "does not terminal-settle as project complete" half, and
  the `absent`-roadmap variant actively settles (that is N2).
- Review finding **N5**, deferred: assertion-weak spots in the A-J matrix.
  `test_A_and_H` never involves the Watchdog, so it cannot catch a Watchdog-side
  relabel. `test_G` covers the descendant-review exhaustion path but not the
  replay path at `ai_reviewer.py:869`, which is the half of acceptance 10 stating
  "repeated ticks never grant duplicate extensions". `test_D` asserts
  `reconcile_roadmap_successor` directly and never exercises or excludes
  `_record_settled`.

## 5. Recommended order for the next session

1. Restart the daemon (section 3) and re-run the smoke; confirm 35/35 and that
   the counters stop at 134 with `state="gated"` and a non-null `gate_id`.
2. Close **acceptance 4/6/8** with one end-to-end test that crosses
   Watchdog -> executor -> Planner and asserts a successor Planner starts with no
   owner continue. This is the highest-value remaining work.
3. Fix **N1** (a two-part change mirroring B2) and **N2 plus its coupled rollback
   hole** (must be fixed together).
4. Strengthen the **N5** assertion-weak tests.
5. Only then consider P16.13 closed and advance the roadmap.

Do not restart full planning for any of the above: all are localized, testable
remediation items inside the approved P16.13 direction, per
`docs/development-workflow.md`.

## 6. Reference

- Task spec: `agent/staged/P16.13.md`
- Lifecycle contract: `docs/P16_13_SUCCESSOR_CONSISTENCY_CONTRACT.md`
- Workflow/review policy: `docs/development-workflow.md`
- Invariant evaluator: `src/dev_orchestrator/core/lifecycle_authority.py`
- Authority/journal: `src/dev_orchestrator/core/transition_executor.py`
- Fault matrix and regressions: `tests_py/test_p1613_successor_consistency.py`
- Runtime smoke: `ops/p1613_lifecycle_smoke.py`
- Session evidence: `agent/CURRENT.md`
