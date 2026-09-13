# P11x Explicit Staged Task Materializer

Status: **READY_TO_RUN**

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

Fixed design constraints from the first independent review:
- Missing manifest is NOT end-of-roadmap. Missing, malformed, or inconsistent staged definitions must block. End-of-roadmap is valid only when a valid manifest explicitly contains the completed task with `successor: null`.
- Exactly-once must use a durable two-phase ledger state, not an in-memory/file rollback heuristic.
- Before mutating `agent/next.md`, persist a `materializing` intent keyed by the accepted review `source_request_id`, including reviewed branch/HEAD/status hash, completed task id, successor id, staged spec path/digest, and original next.md digest.
- After the intent is durable, atomically replace `agent/next.md`, commit only that file locally, then finalize the same ledger record to existing `handoff/planning_required` with the materialized commit HEAD.
- Recovery of `materializing` must be deterministic: if repo is still at reviewed HEAD with original next.md, resume materialization; if repo is exactly the one expected materialization commit descendant and next.md/spec digest match, finalize handoff without another commit; any unrelated HEAD/content change blocks.
- Never try to 'undo' a successful git commit by only restoring file bytes. Post-commit recovery must reconcile from the durable intent.
- Keep all planner JSON entries concise enough to satisfy the existing bounded-string schema; do not restate the whole task in one list item.

Owner resolution after bounded review gate:
- Successor spec status is a closed interface: require exactly one line whose trimmed text is exactly `Status: **PENDING DESIGN**`. Missing, duplicate, negated, lowercase/alternate wording, extra prose on the Status line, or any noncanonical form blocks. Reuse the exact sentinel expected by `AIPlannerCoordinator`; do not broaden planner parsing.
- Add tests for canonical status, missing status, duplicate status, `NOT PENDING DESIGN`, lowercase/noncanonical variants, and extra text after the canonical marker.
- Pre-commit cleanup/restore is not terminal unless repository truth is proven restored. If temp cleanup, index reset, or original `agent/next.md` restoration fails, keep the durable row in `materializing`, record a bounded materialization failure/degraded reason, and let reconciliation own recovery on the next tick.
- Transition `materializing -> blocked` only after fresh truth proves HEAD is still the reviewed HEAD, index/worktree are clean, original `next.md` digest matches, and no materialization temp artifact remains. If those invariants cannot be proved, remain `materializing` and surface degraded status.
- Add deterministic tests where restore, reset/index cleanup, and temp-file cleanup each fail. Assert no terminal blocked row is written until repository invariants are restored; repeated ticks reconcile or remain owner-visible degraded without creating a second commit/handoff.
- Keep planner implementation_steps/interfaces/validation list entries concise and below the existing bounded-string limits.

## Approved executable design

Add an explicit staged-roadmap materializer to TransitionExecutor. When a reviewer accepts 'next' on a COMPLETE task and nothing else is advertised, it reads a validated manifest, writes the successor spec into agent/next.md and commits only that file, guarded by a durable two-phase ledger row. Round 2 fix: every digest (spec, original, worktree, index, HEAD blob) is SHA-256 over raw bytes from one helper. Index bytes come from 'git show :agent/next.md', with explicit states for absent and unmerged entries. Git object IDs are never compared with these digests. Recovery uses a fixed case table (R1-R6): resume, continue the commit, finalize, restore or degrade. It never makes a second commit and blocks only once repository truth is proven restored. The existing _resume_decision_handoffs then starts the Planner.

### Implementation steps
- Add src/dev_orchestrator/core/staged_roadmap.py. load_roadmap(repo) reads agent/staged/roadmap.json {version:1, entries:[{task_id, spec, successor}]}. Block on a missing or malformed file, duplicate ids, unknown or self successor, cycles, a spec outside agent/staged, or a spec title id that differs from the entry id.
- Add PENDING_DESIGN_SENTINEL='Status: **PENDING DESIGN**' and validate_spec_status(text). Exactly one line must trim to the sentinel; anything else blocks. ai_planner._render_next reuses the constant, and planner parsing is unchanged.
- Add agent/staged/roadmap.json with P11x->P11b and P11b->null. Leave agent/staged/P11b.md unchanged.
- Add a digest helper sha256_bytes(b)->hex. It is the only hash used for spec, original, worktree, index and HEAD-blob comparisons. Never compare against git object ids from rev-parse or ls-files.
- Add read_worktree_bytes(repo, path)->bytes|None. It reads raw binary and returns None if the file is missing.
- Add read_index_entry(repo, path)->('absent'|'unmerged'|'present', bytes|None). Run git ls-files -s -z -- path: no entry means absent, and stage != 0 means unmerged. Otherwise run git show :path, captured as bytes with no text decoding.
- Add read_commit_bytes(repo, rev, path)->bytes|None using git show <rev>:path, captured in binary. It returns None when the path is missing in rev, and blocks or degrades on other git errors.
- In _advance_decisions, the COMPLETE branch with no other task advertised resolves the successor. A valid manifest with successor null settles task_complete. A missing or invalid manifest calls _record_blocked with next.md untouched. The advertised-handoff and READY_TO_RUN branches stay unchanged.
- In _materialize_successor, run the pre-intent guards: policy allows next_task, branch/HEAD/status hash match the review, the tree is clean, successor != task, spec status is valid, and next.md is at most 256 KB. On any failure, call _record_blocked with no row.
- Byte-identity guard before intent: sha256(worktree next.md) == sha256(HEAD:next.md) == sha256(index next.md). Also sha256(spec file) == sha256(HEAD:spec) and the spec has no CR bytes. Any mismatch blocks, which catches autocrlf and filters.
- Persist a materializing row with _save_ledger before any mutation, keyed by source_request_id. It stores the reviewed branch/HEAD/status hash, task and successor ids, spec_path, spec_sha256, original_next_sha256, original_next_b64, temp_path, attempts=0 and a phase hint.
- Fixed temp path: agent/.next.md.materializing-<sha8(source_request_id)>.tmp. Write the spec bytes, fsync and os.replace onto next.md. Then run git add -- agent/next.md and git commit -m 'stage(<id>): materialize staged task' -- agent/next.md.
- After the commit, finalize the same row to handoff: planning_required, next_task_id=successor, materialized_head=HEAD. Emit NEXT_TASK once, guarded by that state transition.
- _reconcile_materializing(row) reads fresh truth: HEAD, HEAD^, W=sha256(worktree) or None, the index state plus I=sha256(index bytes), dirty paths excluding temp_path, and whether the temp file exists. Apply the table in order.
- R1: HEAD == reviewed, the only dirty path is next.md, and W == spec. Either I == spec (crash after add) or I == original (crash after replace). Delete the temp, git add, commit once, then finalize.
- R2: HEAD == reviewed, the tree is clean, W == I == original and the index is present. Delete the temp. If attempts < 3, increment attempts and resume the write/commit; otherwise go to R6.
- R3: HEAD^ == reviewed, the HEAD diff names only agent/next.md, sha256(HEAD:next.md) == spec, W == I == spec and the tree is clean apart from the temp. Delete the temp and finalize with materialized_head=HEAD, with no new commit.
- R4: HEAD == reviewed and the only dirty path is next.md, but the digests match no case above (for example an absent index, I == spec with W == original, or unknown W). Run git reset -q -- agent/next.md, restore original_next_b64 via temp+os.replace, delete the temp, then re-read truth for R2/R6.
- R5 (everything else: unrelated HEAD, foreign dirty paths, unmerged index, index.lock, a failed temp unlink, or R4 not converging): never mutate. Keep the row materializing with a bounded degraded_reason and emit a BLOCKED detail with state=materializing.
- R6 writes blocked only when fresh truth proves: HEAD == reviewed, the index is present, W == I == original_next_sha256, the tree is clean, and the temp is absent. Failures after intent (replace/add/commit) go through R4 and then R6.
- Reconcile all materializing rows at the start of _advance_decisions, before the existing-row skip. Rows in handoff or blocked are never reconciled again.
- Truncate git stderr and error text to 300 chars before persisting. ControlCommandConsumer._resume_decision_handoffs and mark_handoff_* stay unchanged.

### Interfaces / contracts
- agent/staged/roadmap.json: {version:1, entries:[{task_id, spec:'agent/staged/<id>.md', successor:<id>|null}]}
- staged_roadmap.load_roadmap(repo)->(Roadmap|None, error|None); resolve_successor(roadmap, task_id)->(entry|None, error|None)
- staged_roadmap.PENDING_DESIGN_SENTINEL='Status: **PENDING DESIGN**'; validate_spec_status(text)->error|None
- sha256_bytes(bytes)->hex: the single digest for spec_sha256, original_next_sha256, W, I and HEAD-blob digests
- read_worktree_bytes(repo,path)->bytes|None; read_index_entry(repo,path)->(absent|unmerged|present, bytes|None) via ls-files -s -z plus git show :path
- read_commit_bytes(repo, rev, path)->bytes|None via git show <rev>:path, binary capture
- Materializing row: source_request_id, task_id, successor_task_id, reviewed_branch/head/status_hash, spec_path, spec_sha256, original_next_sha256, original_next_b64, temp_path, attempts, phase, degraded_reason?
- Temp contract: agent/.next.md.materializing-<sha8(source_request_id)>.tmp. It is excluded from foreign-dirty checks and deleted before any commit or terminal decision.
- Reconcile outcomes: R1 commit->handoff; R2 resume|R6; R3 finalize->handoff; R4 restore->R2/R6|R5; R5 degraded (no mutation); R6 blocked only when invariants are proven.
- Transitions: materializing->handoff(planning_required, next_task_id, materialized_head) or materializing->blocked (R6 only). The handoff row shape is unchanged.
- Events reuse NEXT_TASK, TASK_COMPLETE and BLOCKED. Degraded state is a bounded BLOCKED detail with state=materializing.

### Validation plan
- tests_py/test_staged_roadmap.py: a valid manifest passes; missing, malformed, duplicate, cyclic, self, unknown successor, id-mismatch and outside-staged specs all block.
- Sentinel tests: canonical passes; missing, duplicate, 'NOT PENDING DESIGN', lowercase/alternate wording and trailing text all block.
- Digest helper tests: read_index_entry returns present bytes whose sha256 equals the file-bytes sha256, not the rev-parse oid. It returns absent after git rm --cached, and unmerged for a conflict fixture.
- Byte-identity guard test: a CRLF worktree or spec (or an autocrlf mismatch) blocks before intent, leaving no row and next.md unchanged.
- Happy path in a git fixture: A COMPLETE plus next, with manifest A->B. next.md equals the B spec, there is one new commit touching only next.md, the handoff row has materialized_head, and no temp remains.
- Exactly-once: repeated _advance_decisions gives one commit, one handoff row and one NEXT_TASK.
- End of roadmap: B->null settles once as task_complete. A missing manifest blocks, and next.md bytes and HEAD are unchanged.
- Guard blocks: invalid manifest, dirty repo, HEAD mismatch and next_task not allowed each leave next.md and HEAD unchanged, with a bounded reason and no row.
- Advertised different PENDING DESIGN task C uses the existing handoff branch; the roadmap is not read.
- Crash after intent, before replace: the next tick matches R2, resumes, and ends with one commit and a handoff.
- Crash after replace (W == spec, I == original): R1 runs, with one commit and a handoff. Crash after add (W == I == spec): R1 runs, with one commit; a further tick adds nothing.
- Crash after commit, before finalize: R3 finalizes via the sha256 of the HEAD:next.md bytes. The commit count is unchanged.
- Index absent (git rm --cached next.md): R4 resets the index, W == I == original, and then R2/R6. Unmerged index: R5, no mutation.
- Mixed state (I == spec, W == original): R4 restores the original bytes and a clean index, then resume or block by attempts. A foreign dirty file or unrelated HEAD gives R5 with a persistent degraded reason.
- Fault injection: commit, restore, reset and temp unlink each fail. No blocked row is written while invariants are unproven. After the fault clears, the result is R6 blocked or a handoff, never a second commit.
- Leftover temp in each crash case is deleted before any commit or block, and the final tree is clean.
- Daemon/control integration: across ticks, the handoff drives _resume_decision_handoffs into planner.start with no new control command.
- Real fixture: copy agent/staged/P11b.md and roadmap.json into a temp repo; P11x->P11b materializes as PENDING DESIGN.
- Focused tests: test_transition_executor, test_control_commands, test_daemon_transition_integration, test_daemon, test_ai_planner, test_staged_roadmap.
- Run python -m pytest tests_py -q and git diff --check, then commit locally on feature/self-hosted-dev with no push.

### Risks / failure modes
- Git clean/smudge filters or core.autocrlf can make index bytes differ from worktree bytes. The pre-intent byte-identity guard blocks such repos instead of stranding recovery.
- subprocess text mode would translate newlines on Windows. All git show and file reads must capture raw bytes.
- git show :path fails when the entry is absent or unmerged. ls-files -s must classify the entry first, so errors are not mistaken for digests.
- A crash between replace and add leaves I != W. R1 must accept both I == spec and I == original.
- A commit can land before the ledger is finalized. R3 must detect the one-commit child and never restore bytes over it.
- The existing-row skip would hide materializing rows, so reconcile must run first.
- Windows locks or git index.lock can make replace/unlink/add fail. These stay degraded (R5); the lock is never deleted automatically.
- The monitor snapshot is stale within the same tick, so the Planner starts on the next tick and tests must model that.
- The watchdog or activity checks could flag the materialization commit. Verify the existing watchdog tests.

### Out of scope
- Free-form docs/backlog.md parsing or AI selection of backlog items.
- Reordering or editing the roadmap manifest without owner approval.
- Implementing P11b accounting, or changing provider routing or AIBroker policy.
- Resuming the paused xray-hw-platform project or changing cross-project isolation.
- Pushing to a remote, auto-deleting git index.lock, supporting repos with filters or autocrlf on next.md, or broadening planner status parsing.

### Independent plan review
- Approved: The plan is repository-aligned, bounded to explicit staged handoff, preserves the existing handoff/planner path, and specifies the manifest contract, canonical status validation, durable intent fields, byte-level Git guards, deterministic recovery cases, terminal invariants, failure visibility, and comprehensive integration/fault-injection validation sufficiently for Worker execution without material design guessing.
