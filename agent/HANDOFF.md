# P16.13 Handoff — Successor Consistency & Zero-Touch Handoff Recovery

Last updated: 2026-09-26. Branch `main`, worktree clean at `8565773`.

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
| `b39d2a7` | This handoff |
| `60ef19b` | End-to-end zero-touch test: closes acceptance 4, 6, 8 |
| `9629c44` | Closes the deferred N1 and N2 findings |
| `8565773` | Closes the N5 assertion-weak spots and acceptance 2 |

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

- Full Python regression: **1,308 tests + 99 subtests** passing.
- P16.13 suite: **36 tests + 7 subtests** passing.
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

### 4.2 Deferred NON_BLOCKING findings

- **N1 — CLOSED in `9629c44`.** The Watchdog no longer answers the invariant
  block from an empty ledger; it skips it and records
  `lifecycle_evidence_unavailable`.
- **N2 — CLOSED in `9629c44`.** An unambiguous staged `Predecessor:` claim now
  wins over an absent `roadmap.json`, so the caller no longer terminal-settles the
  project as complete. Fixed together with its coupled rollback hole: a roadmap
  created by a failed reconcile is now removed instead of being left behind to
  dirty the tree and block every later recovery.
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

### 4.3 Acceptance criteria evidence

**Closed in `60ef19b`.** `ZeroTouchSuccessorHandoffEndToEndTests` drives the real
`WatchdogCoordinator`, `TransitionExecutor`, `ControlCommandCoordinator` and
`AIPlannerCoordinator` in daemon tick order from the matrix-B fault. Observed: the
Watchdog rebuilds exactly one handoff with `P1 -> P2` lineage, the control plane
starts the successor Planner automatically, the real planner and its independent
plan review both run, `agent/next.md` is rewritten to `P2 READY_TO_RUN` with the
frozen plan, the authority advances to `P2` keeping `P1` as source, every
invariant converges, and **no owner command reaches the control plane**. Both legs
were verified load-bearing by removal: without the Watchdog leg there are 0
handoffs and 0 plans; without the control-plane leg the handoff exists but no
Planner starts and the authority stays at `P1`. This covers acceptance **4, 5, 6,
7 and 8**.

**Acceptance 2 and review finding N5 closed in `8565773`** (tests only, no source
change; each new assertion was verified to fail when the behavior it guards is
broken):

- **Acceptance 2** is now proven through the real decision actuation: no
  execution row carries `outcome="task_complete"` for an inconsistent successor,
  exactly one `P1 -> P2` handoff is produced instead, and the repair leaves a
  clean tree. Verified by disabling the inconsistent-successor reconcile branch,
  which produces exactly the forbidden settled/`task_complete` row.
- **N5 / `test_A_and_H`** now drives the real `WatchdogCoordinator` over the
  fenced state, so a Watchdog-side relabel would be caught, plus a vacuity guard
  asserting all six invariants were actually evaluated.
- **N5 / `test_G`** now covers the replay half of acceptance 10: a replayed tick
  reuses the grant with an unchanged `remediation_extension_granted_at`, and the
  next descendant review still gates. Verified by removing the replay
  short-circuit at `ai_reviewer.py:868-870`.
- **N5 / `test_D`** keeps its resolver assertions and is complemented by the
  acceptance-2 test above.

No known test-strength gaps remain in the A-J matrix.

## 5. Recommended order for the next session

1. Restart the daemon (section 3) and re-run the smoke; confirm 35/35 and that
   the counters stop at one past their current value with `state="gated"` and a
   non-null `gate_id`. **This is the only remaining closure gate, and it is
   operational, not implementation.**
2. Then P16.13 can be closed and the roadmap advanced.

Everything in section 4.2 that was open in code is now closed. The only recorded
residual is **N-C**, an accepted fail-closed risk that is not reachable in any
live project. Do not restart full planning for any of the above.

## 6. Reference

- Task spec: `agent/staged/P16.13.md`
- Lifecycle contract: `docs/P16_13_SUCCESSOR_CONSISTENCY_CONTRACT.md`
- Workflow/review policy: `docs/development-workflow.md`
- Invariant evaluator: `src/dev_orchestrator/core/lifecycle_authority.py`
- Authority/journal: `src/dev_orchestrator/core/transition_executor.py`
- Fault matrix and regressions: `tests_py/test_p1613_successor_consistency.py`
- Runtime smoke: `ops/p1613_lifecycle_smoke.py`
- Session evidence: `agent/CURRENT.md`
