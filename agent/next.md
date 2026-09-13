# P11x Explicit Staged Task Materializer

Status: **PENDING DESIGN**

Owner authorization: **START P11x / 2026-09-13**

Goal: eliminate the cross-task stop after a reviewed task completes. When an explicitly approved staged roadmap declares a next task, DevOrchestrator must materialize that next task and start its planner automatically without a new `project-continue`.

Scope:
- Add only the minimum control-plane support required for explicit staged-task handoff. Do not add arbitrary backlog selection or heuristic task generation.
- The source of truth must be an explicit project-local staged roadmap/manifest, not free-form `docs/backlog.md` parsing.
- A staged entry must identify a machine-distinct task id (for example `P11b`) and a concrete task specification that can become `agent/next.md`.
- On accepted technical review with decision `next`, when the reviewed task is COMPLETE and no different next task is currently advertised, consult the explicit staged roadmap.
- If a declared successor exists and all repository/owner guards pass, atomically materialize its task specification into `agent/next.md`, record the handoff, and enter the existing planner lifecycle automatically.
- Reuse existing `NEXT_TASK` / handoff / planner mechanisms after materialization; do not create a second execution path.
- Preserve exact task identity and require the successor id to differ from the completed task id.
- End-of-roadmap must settle normally as TASK_COMPLETE/IDLE, not loop or invent work.
- Invalid or missing staged definitions, task-id mismatch, dirty repository, HEAD mismatch, unauthorized next action, or write failure must fail closed with an owner-visible reason.
- Materialization must be idempotent across repeated daemon ticks: one reviewed completion may create at most one successor handoff.
- Preserve existing review-remediation behavior and project isolation; do not resume the paused `xray-hw-platform` project.

Acceptance:
- Deterministic integration test: fixture task A completes and receives reviewer `next`; the staged roadmap declares fixture task B; without any new control command B is materialized as `PENDING DESIGN`, the handoff is recorded, and the existing Planner starts.
- Prove exactly-once behavior when the same accepted review decision is observed repeatedly.
- Prove a final staged task with no successor settles once as TASK_COMPLETE/IDLE.
- Prove missing, malformed, cyclic, duplicate, or mismatched staged entries do not modify `agent/next.md` and produce a bounded blocked result.
- Prove an already advertised different successor keeps using the existing handoff path and is not overwritten.
- Use the preserved `agent/staged/P11b.md` as the first real successor fixture so promotion can immediately exercise P11x -> P11b automatically.
- Run focused transition-executor, control, daemon, and planner tests, then full `python -m pytest tests_py -q` and `git diff --check`.
- Commit locally on `feature/self-hosted-dev`; do not push during Worker implementation.

Out of scope:
- Arbitrary AI selection of backlog items or free-form backlog parsing.
- Reordering the staged roadmap without an owner-approved manifest change.
- P11b accounting implementation itself, provider routing changes, or resuming paused projects.
