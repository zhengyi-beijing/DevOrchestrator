# P11x Deferred Staged Handoff (Planner-Owned Materialization)

Status: **PENDING DESIGN**

Owner authorization: **START P11x / 2026-09-13**

Goal: after an accepted technical review completes one task, automatically start the explicitly approved successor task without a new `project-continue`, while keeping repository mutation exclusively inside the existing Planner apply boundary.

Owner-decided architecture — freeze this direction:
- TransitionExecutor/control plane MUST NOT modify `agent/next.md`, staged specs, or create Git commits for task handoff.
- The explicit roadmap only selects a successor and its staged task specification.
- A handoff record carries successor task id, spec path/digest, reviewed branch/HEAD and source_request_id.
- ControlCommandCoordinator resumes that handoff by starting the Planner with an explicit task/spec override even though the repository still advertises the completed predecessor.
- Before plan approval the repository HEAD and `agent/next.md` must remain unchanged.
- On approval, AIPlannerCoordinator alone materializes the successor: validate the same clean HEAD, validate predecessor `agent/next.md` is unchanged, validate staged spec digest is unchanged, render the successor spec to READY_TO_RUN with the approved design, and use existing `_commit_plan()` for exactly one commit.
- Reuse existing handoff idempotency (`source_request_id`, deterministic continuation id, `handoff_consumed`, plan-id reuse). Do not add a second Git transaction/recovery state machine.
- Do not introduce `materializing`, commit trailers, sidecar recovery files, Git-reset recovery protocols, or pre-approval repo mutation.

Required implementation shape:
- Add a small pure staged-roadmap reader for `agent/staged/roadmap.json`; no free-form backlog parsing.
- If no roadmap file exists, keep current legacy settle behavior. If the file exists but is invalid or the completed task is missing, block fail-closed.
- A valid `successor: null` means end-of-roadmap and settles normally.
- Extend the existing handoff ledger row with staged successor metadata; do not add a new materialization ledger state.
- Extend Planner start with a deferred-handoff entry that accepts successor task id and staged spec while still pinning the current clean repository HEAD and current predecessor `agent/next.md` bytes.
- Refactor normal and deferred Planner start through one common lifecycle so plan/review/remediation behavior stays identical.
- Deferred `_apply_plan()` renders from the staged successor spec, not from the predecessor text, but verifies predecessor text and staged spec digest have not changed before writing.
- The resulting commit is the normal Planner plan commit and is the first repository mutation for the successor.

Acceptance:
- Fixture A COMPLETE + reviewer `next` + roadmap A->B automatically starts B Planner with no new control command and with unchanged HEAD/`agent/next.md` before approval.
- B plan approval produces exactly one new commit; that commit changes `agent/next.md` directly from completed A to B `READY_TO_RUN` plus Approved executable design.
- Repeated daemon ticks/restart do not start a duplicate Planner and do not create a duplicate commit.
- Invalid roadmap/spec blocks without repository mutation; `successor:null` settles normally.
- An already-advertised different `PENDING DESIGN` task continues to use the existing handoff path unchanged.
- Add real roadmap entries `P11x -> P11b -> P11c -> P11d -> null`, with machine-distinct ids and staged specs.
- Full `python -m pytest tests_py -q` and `git diff --check` must pass; commit locally only, no push.
