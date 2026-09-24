"""Direct AIBroker reviewer path, independent of browser conversations."""
from __future__ import annotations

import copy
import hashlib
import datetime as dt
import json
import threading
import time
from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.ai.contracts import (
    AIRoleRequest,
    ResourceContext,
    ROLE_RESOURCE_FAILURES,
    missing_independence_fields,
)
from dev_orchestrator.ai.execution_port import AIExecutionPort
from dev_orchestrator.ai.structured_output import (
    StructuredOutputError,
    extract_unique_json_object,
    protocol_repair_prompt,
    require_exact_keys,
)
from dev_orchestrator.accounting import ExecutionRecorder, FailureMemory, environment_for_project
from dev_orchestrator.config import load_projects_config
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.core.workflow_policy import workflow_policy_prompt
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json

REVIEWER_STATE_FILE = "ai-reviewer.json"
REVIEW_DECISIONS_FILE = "review-decisions.json"
_STATE_VERSION = 1
_DECISION_VERSION = 1
_ACTIVE_STATES = frozenset({"launching", "running"})
_TERMINAL_STATES = frozenset({"completed", "failed", "recovery_required"})
_DEFAULT_MAX_REMEDIATION_ROUNDS = 2
_MAX_REMEDIATION_ROUNDS = 5
#: States a daemon restart must resolve. ``recovery_required`` is no longer
#: produced by this coordinator, but records written by an earlier daemon are
#: picked up here so legacy state is migrated to a retry-eligible outcome
#: instead of remaining permanently unactionable.
_RECOVERABLE_STATES = frozenset(_ACTIVE_STATES | {"recovery_required"})
_ALLOWED_DECISIONS = {
    ("next", "next_task"),
    ("remediate", "continue_current_stage"),
    ("owner_gate", "stop"),
    ("stop", "stop"),
}
_DECISION_DISPOSITIONS = {
    ("next", "next_task"): "apply",
    ("remediate", "continue_current_stage"): "apply",
    ("owner_gate", "stop"): "owner_gate",
    ("stop", "stop"): "stop",
}

#: Decision-ledger fields that define a decision's identity. `consumed_at` is
#: deliberately excluded: it is regenerated on every write, so including it
#: would make an idempotent replay indistinguishable from a real conflict.
_DECISION_IDENTITY_FIELDS = (
    "project_id", "request_id", "disposition", "next_action", "decision",
    "reason", "review_status_hash", "task_id", "stage_id", "branch", "head",
    "role", "event", "source",
)


def _terminal_milestone(state_name: str, decision: Optional[str]) -> str:
    """Map a terminal review state to the lifecycle milestone it owes, if any."""
    if state_name == "completed":
        if decision == "next":
            return "REVIEW_ACCEPTED"
        if decision == "remediate":
            return "REMEDIATE"
        if decision == "owner_gate":
            return "OWNER_GATE"
        return ""
    if state_name == "failed":
        return "REVIEW_FAILED"
    return ""


def _decision_identity(record: Any) -> tuple:
    """Return the conflict-relevant identity of one decision-ledger record."""
    if not isinstance(record, dict):
        return ()
    return tuple(record.get(name) for name in _DECISION_IDENTITY_FIELDS)


def _nonblank(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _is_utc_timestamp(value: Any) -> bool:
    """Return whether ``value`` is an aware ISO-8601 timestamp in UTC."""
    text = _nonblank(value)
    if text is None:
        return False
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == dt.timedelta(0)


def _review_policy(project: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    execution = project.get("execution")
    if not isinstance(execution, dict) or execution.get("engine") != "aibroker":
        return None, "direct reviewer requires execution.engine=aibroker"
    roles = project.get("ai_roles")
    if not isinstance(roles, dict):
        return None, "ai_roles missing"
    raw = roles.get("reviewer")
    if not isinstance(raw, dict) or raw.get("enabled") is not True:
        return None, "reviewer role disabled"
    quality = raw.get("quality", "high")
    independence = raw.get("independence", "resource")
    if quality not in ("economy", "balanced", "high"):
        return None, "reviewer quality invalid"
    if independence not in ("resource", "account", "provider"):
        return None, "reviewer independence invalid"
    timeout = raw.get("timeout_seconds", 600)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        return None, "reviewer timeout_seconds must be positive"
    max_rounds = raw.get("max_remediation_rounds", _DEFAULT_MAX_REMEDIATION_ROUNDS)
    if (
        isinstance(max_rounds, bool)
        or not isinstance(max_rounds, int)
        or not 1 <= max_rounds <= _MAX_REMEDIATION_ROUNDS
    ):
        return None, "reviewer max_remediation_rounds must be an integer from 1 to 5"
    return {
        "quality": quality,
        "independence": independence,
        "timeout_seconds": float(timeout),
        "max_remediation_rounds": max_rounds,
    }, ""


def _parse_review_output(text: str | None) -> tuple[str, str, str]:
    payload = extract_unique_json_object(text, label="reviewer")
    require_exact_keys(payload, {"decision", "next_action", "reason"}, label="reviewer")
    decision = _nonblank(payload.get("decision"))
    next_action = _nonblank(payload.get("next_action"))
    reason = _nonblank(payload.get("reason"))
    if (decision, next_action) not in _ALLOWED_DECISIONS or reason is None:
        raise StructuredOutputError("semantic", "invalid reviewer decision")
    return decision, next_action, reason


class AIReviewerCoordinator:
    """Schedule one independent AIBroker reviewer per completed broker Worker."""

    def __init__(
        self, runtime_root: Path | str, port: AIExecutionPort | None,
        progress_channel: Optional[Any] = None,
        accounting: ExecutionRecorder | None = None,
        failure_memory: FailureMemory | None = None,
        failure_memory_max_chars: int = 2000,
        harness: Optional[Any] = None,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.port = port
        self.progress_channel = progress_channel
        self.accounting = accounting
        self.failure_memory = failure_memory
        self.failure_memory_max_chars = failure_memory_max_chars
        self.harness = harness
        self.state_path = self.runtime_root / REVIEWER_STATE_FILE
        self.decisions_path = self.runtime_root / REVIEW_DECISIONS_FILE
        self.transition_path = self.runtime_root / "transition-executor.json"
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        self._project_bindings: dict[str, dict[str, Any]] = {}
        self._recover_interrupted()

    def _get_harness(self) -> Any:
        if self.harness is not None:
            return self.harness
        from dev_orchestrator.review.harness import DefaultReviewerHarness
        self.harness = DefaultReviewerHarness(self.runtime_root)
        return self.harness

    def _load_state(self) -> dict[str, Any]:
        raw = read_json(self.state_path, None)
        reviews = raw.get("reviews") if isinstance(raw, dict) else None
        if not isinstance(reviews, dict):
            reviews = {}
        return {"version": _STATE_VERSION, "reviews": {
            key: value for key, value in reviews.items()
            if isinstance(key, str) and isinstance(value, dict)
        }}

    def _save_state(self, state: dict[str, Any]) -> None:
        write_json(self.state_path, state, indent=2)

    def _recover_interrupted(self) -> None:
        """Reconcile reviews interrupted by a daemon restart.

        Every interrupted review must end in a state the lifecycle can act on.
        A reconciled harness session that already finished is dispositioned
        through the normal deterministic path so its durable evidence yields a
        real decision; a session still in flight is resumed; anything else is
        marked ``failed``. Records with authoritative source evidence remain
        retry candidates; records without it receive an explicit terminal
        recovery classification. No interrupted review is left active.

        Terminal transitions are never written inline here. Every one of them
        goes through :meth:`_finish_harness_terminal`, which persists the
        terminal state together with its ``lifecycle_event_pending`` marker
        before emitting, so a crash or transport failure at any point still
        leaves the owed event replayable. Records whose terminal write has not
        happened yet simply stay active and are rescanned on the next restart.
        """
        pending_finalize: list[tuple[str, dict[str, Any], Any]] = []
        resume: list[tuple[str, dict[str, Any], str]] = []
        failed: list[tuple[str, dict[str, Any], str, str | None]] = []

        with self._lock:
            state = self._load_state()
            transitions = self._transition_records()
            changed = False
            blocked: dict[str, tuple[str, str]] = {}

            # Normalize durable identities before touching a harness or emitting
            # any lifecycle event. The map key is the canonical review id.
            for review_id, record in state["reviews"].items():
                if record.get("state") not in _RECOVERABLE_STATES:
                    continue
                embedded_id = _nonblank(record.get("review_id"))
                prior_error = _nonblank(record.get("recovery_identity_error"))
                if prior_error is not None:
                    record["review_id"] = review_id
                    blocked[review_id] = (prior_error, "terminal_identity_conflict")
                    changed = True
                    continue
                if embedded_id is not None and embedded_id != review_id:
                    reason = (
                        "recovered review identity mismatch: embedded review_id "
                        f"{embedded_id!r} disagrees with canonical key {review_id!r}"
                    )
                    record["recovery_identity_error"] = reason
                    record["recovery_embedded_review_id"] = embedded_id
                    record["review_id"] = review_id
                    blocked[review_id] = (reason, "terminal_identity_conflict")
                    changed = True
                    continue
                if embedded_id is None:
                    record["review_id"] = review_id
                    changed = True

                source_request_id = _nonblank(record.get("source_request_id"))
                if source_request_id is None:
                    source_request_id = self._recover_source_request_id(
                        review_id, record, transitions,
                    )
                    if source_request_id is None:
                        reason = (
                            "recovered review lacks authoritative source_request_id evidence; "
                            "automatic retry is unavailable"
                        )
                        record["recovery_source_error"] = reason
                        blocked[review_id] = (reason, "terminal_source_unrecoverable")
                        changed = True
                        continue
                    record["source_request_id"] = source_request_id
                    record.pop("recovery_source_error", None)
                    changed = True

            if changed:
                self._save_state(state)

            changed = False
            for review_id, record in state["reviews"].items():
                if record.get("state") not in _RECOVERABLE_STATES:
                    continue
                if review_id in blocked:
                    reason, classification = blocked[review_id]
                    record["recovered_at"] = utc_now_iso()
                    changed = True
                    failed.append((review_id, copy.deepcopy(record), reason, classification))
                    continue
                session_id = _nonblank(record.get("session_id")) or review_id
                if record.get("harness") and session_id:
                    try:
                        harness = self._get_harness()
                        session = harness.reconcile(session_id)
                        record["session_id"] = session.session_id
                        record["job_id"] = session.job_id
                        record["recovered_at"] = utc_now_iso()
                        changed = True
                        if session.state == "completed":
                            # Keep the record active until the disposition runs,
                            # so a crash here is retried rather than silently
                            # settled as complete-without-decision.
                            record["state"] = "running"
                            pending_finalize.append((review_id, copy.deepcopy(record), session))
                        elif session.state in ("queued", "running", "launching"):
                            record["state"] = "running"
                            resume.append((review_id, copy.deepcopy(record), session.session_id))
                        else:
                            reason = session.failure_reason or (
                                "daemon restarted during reviewer execution; "
                                f"job state {session.state}"
                            )
                            failed.append((review_id, copy.deepcopy(record), reason, None))
                        continue
                    except Exception as exc:
                        reason = (
                            "daemon restarted during reviewer execution; "
                            f"harness reconcile failed: {exc}"
                        )
                        record["recovered_at"] = utc_now_iso()
                        changed = True
                        failed.append((review_id, copy.deepcopy(record), reason, None))
                        continue
                reason = "daemon restarted during reviewer execution; automatic replay forbidden"
                record["recovered_at"] = utc_now_iso()
                changed = True
                failed.append((review_id, copy.deepcopy(record), reason, None))
            if changed:
                self._save_state(state)

        # Re-emit any lifecycle event owed from a previous process before
        # handling this scan's own side effects.
        self._replay_pending_lifecycle_events()

        # Side effects run outside the state write so a slow disposition never
        # holds the interrupted-review scan open.
        for review_id, record, reason, classification in failed:
            self._fail_recovered_review(
                self._recovery_context(review_id, record), reason,
                classification=classification,
            )
        for review_id, record, session in pending_finalize:
            self._finalize_recovered_harness_review(review_id, record, session)
        for review_id, record, session_id in resume:
            self._resume_harness_polling(review_id, record, session_id)

    def _durable_decision(self, review_id: str) -> Optional[dict[str, Any]]:
        """Return the durable decision-ledger record for ``review_id``, if any."""
        raw = read_json(self.decisions_path, None)
        decisions = raw.get("decisions") if isinstance(raw, dict) else None
        if not isinstance(decisions, dict):
            return None
        record = decisions.get(review_id)
        return record if isinstance(record, dict) else None

    def _validate_durable_decision(
        self, review_id: str, durable: dict[str, Any], rec: dict[str, Any]
    ) -> str:
        """Return "" when ``durable`` may be projected, else the rejection reason.

        Atomic writes protect the ledger from torn bytes, not from structurally
        invalid records, stale schemas, or a competing writer's entry landing
        under this review id. A decision is only projected onto terminal
        reviewer state when it is complete, internally consistent, and agrees
        with the reviewer record's own identity and reviewed anchor.
        """
        missing = [
            name for name in _DECISION_IDENTITY_FIELDS
            if _nonblank(durable.get(name)) is None
        ]
        if missing:
            return f"incomplete decision record; missing {', '.join(sorted(missing))}"
        if not _is_utc_timestamp(durable.get("consumed_at")):
            return "decision consumed_at is not a timezone-aware ISO-8601 UTC timestamp"
        if _nonblank(durable.get("request_id")) != review_id:
            return "decision request_id does not match review id"
        if durable.get("role") != "reviewer" or durable.get("event") != "worker_done":
            return "decision role/event identity is not a reviewer worker_done record"
        if durable.get("stage_id") != "review":
            return "decision stage identity is not review"
        if durable.get("source") != "aibroker":
            return "decision source is not the authoritative aibroker writer"
        pair = (durable.get("decision"), durable.get("next_action"))
        if pair not in _ALLOWED_DECISIONS:
            return "decision/next_action pair is not allowed"
        expected_disposition = _DECISION_DISPOSITIONS.get(pair)
        if durable.get("disposition") != expected_disposition:
            return "decision disposition does not match decision/next_action"
        # The reviewer record is the local identity authority; a ledger entry
        # that disagrees with it belongs to a different review or a stale
        # schema and must not settle this one. The record must actually carry
        # the identity to compare against: treating an absent field as "no
        # constraint" would let a ledger entry naming a foreign branch/HEAD
        # settle an anchorless legacy record, which is the same fail-open this
        # validation exists to prevent.
        for name in (
            "project_id", "task_id", "branch", "head", "review_status_hash",
        ):
            expected = _nonblank(rec.get(name))
            if expected is None:
                return f"reviewer record lacks {name}; cannot verify decision identity"
            if _nonblank(durable.get(name)) != expected:
                return f"decision {name} does not match the reviewer record"
        return ""

    def _quarantine_decision(self, review_id: str, reason: str) -> None:
        """Move an unusable ledger entry out of the decided set.

        Leaving a malformed entry in ``decisions`` would both invite a later
        replay and keep the review inside ``resolve_retry_candidate``'s decided
        set, leaving it with no repair path. Quarantining preserves the record
        for forensics while restoring retry eligibility.
        """
        with self._lock:
            raw = read_json(self.decisions_path, None)
            payload = raw if isinstance(raw, dict) else {}
            decisions = payload.get("decisions")
            if not isinstance(decisions, dict) or review_id not in decisions:
                return
            quarantined = payload.get("quarantined_decisions")
            if not isinstance(quarantined, dict):
                quarantined = {}
            quarantined[review_id] = {
                "record": decisions.pop(review_id),
                "quarantined_at": utc_now_iso(),
                "reason": reason,
            }
            write_json(
                self.decisions_path,
                {
                    "version": _DECISION_VERSION,
                    "decisions": decisions,
                    "quarantined_decisions": quarantined,
                },
                indent=2,
            )

    def _project_durable_decision(
        self,
        review_id: str,
        durable: dict[str, Any],
        binding: Optional[dict[str, Any]],
    ) -> None:
        """Project an already-durable decision onto reviewer state and lifecycle.

        Used when a crash left the decision ledger written but the reviewer
        record unfinished. Converges the two stores without re-deriving or
        re-persisting the decision. The lifecycle event is re-emitted with the
        same occurrence key, which the progress channel deduplicates, so a
        replay never produces a second event.
        """
        decision = str(durable.get("decision") or "")
        next_action = str(durable.get("next_action") or "")
        reason = str(durable.get("reason") or "")
        self._finish_harness_terminal(
            review_id,
            str(durable.get("project_id") or ""),
            str(durable.get("task_id") or ""),
            "",
            "completed",
            reason,
            decision=decision or None,
            next_action=next_action or None,
            binding=binding,
            extra={"disposition": durable.get("disposition"), "recovered_decision": True},
        )

    def _recovery_repo_path(self, record: dict[str, Any], source: dict[str, Any]) -> Optional[str]:
        """Resolve a trusted repository path for a recovered review record.

        Records written before ``repo_path`` was persisted carry no path of
        their own. Such a record may only borrow the path from its exact source
        transition record, and only when that record agrees on project, task,
        source request and the reviewed branch/HEAD. Anything else returns
        ``None`` so the caller fails closed: an unresolved path would otherwise
        be passed to ``read_repository_truth`` as ``""``, which resolves to the
        daemon's working directory and could verify the wrong repository.
        """
        own = _nonblank(record.get("repo_path"))
        if own is not None:
            return own
        if not isinstance(source, dict):
            return None
        candidate = _nonblank(source.get("repo_path"))
        if candidate is None:
            return None
        for field in ("project_id", "task_id", "branch", "head"):
            expected = _nonblank(record.get(field))
            actual = _nonblank(source.get(field))
            if expected is None or actual is None or expected != actual:
                return None
        if _nonblank(source.get("source_request_id")) != _nonblank(record.get("source_request_id")):
            return None
        return candidate

    @staticmethod
    def _transition_matches_recovery_record(
        source_request_id: str,
        source: dict[str, Any],
        record: dict[str, Any],
    ) -> bool:
        """Return whether one transition is authoritative for ``record``."""
        if (
            _nonblank(source.get("source_request_id")) != source_request_id
            or source.get("engine") != "aibroker"
            or source.get("state") != "completed"
        ):
            return False
        for field in ("project_id", "task_id", "branch", "head"):
            expected = _nonblank(record.get(field))
            if expected is None or _nonblank(source.get(field)) != expected:
                return False
        repo_path = _nonblank(record.get("repo_path"))
        if repo_path is not None and _nonblank(source.get("repo_path")) != repo_path:
            return False
        return True

    @classmethod
    def _transition_supports_recovery_retry(
        cls,
        source_request_id: str,
        source: dict[str, Any],
        record: dict[str, Any],
    ) -> bool:
        """Return whether the source carries the evidence retry resolution needs."""
        if not cls._transition_matches_recovery_record(
            source_request_id, source, record,
        ):
            return False
        if source.get("source_kind") not in {
            "owner_start", "control", "decision", "remediation",
        }:
            return False
        if any(
            _nonblank(source.get(field)) is None
            for field in ("dispatch_id", "decision_id", "execution_id")
        ):
            return False
        resource = source.get("resource_context")
        return isinstance(resource, dict) and all(
            _nonblank(resource.get(field)) is not None
            for field in ("resource_id", "provider", "account", "model")
        )

    def _recover_source_request_id(
        self,
        review_id: str,
        record: dict[str, Any],
        transitions: dict[str, dict[str, Any]],
    ) -> str | None:
        """Recover a missing source id only from matching transition evidence."""
        candidates = [
            source_request_id
            for source_request_id, source in transitions.items()
            if self._transition_matches_recovery_record(source_request_id, source, record)
        ]
        direct = review_id.removeprefix("ai_review:")
        if review_id.startswith("ai_review:") and direct in candidates:
            return direct
        return candidates[0] if len(candidates) == 1 else None

    def _recovery_context(self, review_id: str, record: dict[str, Any]) -> dict[str, Any]:
        """Rebuild the disposition inputs for a recovered review record.

        ``repo_path`` is ``None`` when no trusted path could be established;
        every caller must fail closed on that rather than substituting a
        default.
        """
        source_request_id = _nonblank(record.get("source_request_id"))
        source = self._transition_records().get(source_request_id) if source_request_id else None
        if not isinstance(source, dict):
            source = {}
        failure_classification = None
        if source_request_id is not None and not self._transition_supports_recovery_retry(
            source_request_id, source, record,
        ):
            failure_classification = "terminal_source_unreachable"
        worker = record.get("worker")
        if not isinstance(worker, dict) or not worker:
            worker = source
        harness_cfg = record.get("harness_cfg")
        if not isinstance(harness_cfg, dict):
            harness_cfg = {}
        binding = record.get("conversation_binding")
        return {
            "review_id": review_id,
            "project_id": record.get("project_id") or "",
            "task_id": record.get("task_id") or "",
            "source_request_id": source_request_id,
            "repo_path": self._recovery_repo_path(record, source),
            "harness_cfg": harness_cfg,
            "worker": worker,
            "binding": binding if isinstance(binding, dict) and binding else None,
            "recovery_failure_classification": failure_classification,
        }

    def _fail_recovered_review(
        self,
        ctx: dict[str, Any],
        reason: str,
        *,
        classification: str | None = None,
    ) -> None:
        """Settle a recovered review as failed, with a classification if terminal."""
        classification = classification or ctx.get("recovery_failure_classification")
        self._finish_harness_terminal(
            ctx["review_id"], ctx["project_id"], ctx["task_id"],
            ctx["source_request_id"], "failed", reason, binding=ctx["binding"],
            extra={"recovery_classification": classification} if classification else None,
        )

    def _finalize_recovered_harness_review(
        self, review_id: str, record: dict[str, Any], session: Any,
    ) -> None:
        """Disposition a harness session that completed while the daemon was down."""
        ctx = self._recovery_context(review_id, record)
        if ctx["repo_path"] is None:
            self._fail_recovered_review(
                ctx, "recovered review lacks trusted repository path evidence",
            )
            return
        try:
            harness = self._get_harness()
            review_result = harness.result(session.session_id)
        except Exception as exc:
            self._fail_recovered_review(ctx, f"recovered harness result unavailable: {exc}")
            return
        self._finalize_harness_review(review_result=review_result, **ctx)

    def _resume_harness_polling(
        self, review_id: str, record: dict[str, Any], session_id: str,
    ) -> None:
        """Resume polling a harness session still in flight after a restart.

        Refuses to resume without trusted repository evidence, so a session can
        never be polled to completion and then verified against an unrelated
        directory.
        """
        ctx = self._recovery_context(review_id, record)
        if ctx["repo_path"] is None:
            self._fail_recovered_review(
                ctx, "recovered review lacks trusted repository path evidence",
            )
            return
        thread = threading.Thread(
            target=self._run_resumed_harness_poll,
            args=(review_id, copy.deepcopy(record), session_id),
            name=f"devorch-review-resume-{review_id}",
            daemon=True,
        )
        with self._lock:
            self._threads[review_id] = thread
        thread.start()

    def _run_resumed_harness_poll(
        self, review_id: str, record: dict[str, Any], session_id: str,
    ) -> None:
        ctx = self._recovery_context(review_id, record)
        if ctx["repo_path"] is None:
            self._fail_recovered_review(
                ctx, "recovered review lacks trusted repository path evidence",
            )
            return
        harness_cfg = ctx["harness_cfg"]
        policy = record.get("policy") if isinstance(record.get("policy"), dict) else {}
        timeout = float(
            harness_cfg.get("timeout_seconds")
            or harness_cfg.get("timeout")
            or policy.get("timeout_seconds")
            or 600.0
        )
        poll_interval = float(
            harness_cfg.get("poll_interval_seconds")
            or harness_cfg.get("poll_interval")
            or 0.2
        )
        try:
            harness = self._get_harness()
            session = harness.status(session_id)
            start_poll = time.monotonic()
            while time.monotonic() - start_poll < timeout:
                if session.state in ("completed", "failed", "cancelled"):
                    break
                time.sleep(poll_interval)
                session = harness.status(session_id)
            if session.state != "completed":
                reason = session.failure_reason or (
                    f"resumed review session {session.state}"
                    if session.state in ("failed", "cancelled")
                    else "resumed review session timed out waiting for completion"
                )
                self._finish_harness_terminal(
                    ctx["review_id"], ctx["project_id"], ctx["task_id"],
                    ctx["source_request_id"], "failed", reason, binding=ctx["binding"],
                )
                return
            review_result = harness.result(session_id)
        except Exception as exc:
            self._finish_harness_terminal(
                ctx["review_id"], ctx["project_id"], ctx["task_id"],
                ctx["source_request_id"], "failed",
                f"resumed reviewer harness error: {exc}", binding=ctx["binding"],
            )
            return
        self._finalize_harness_review(review_result=review_result, **ctx)

    def state(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._load_state())

    def enabled_project_ids(self, config_path: Path | str) -> frozenset[str]:
        config = load_projects_config(config_path)
        result = set()
        for project in config.get("projects") or []:
            h_cfg = project.get("reviewer_harness")
            if isinstance(h_cfg, dict) and h_cfg.get("enabled") is True:
                result.add(str(project["project_id"]))
                continue
            policy, _ = _review_policy(project)
            if policy is not None:
                result.add(str(project["project_id"]))
        return frozenset(result)

    def _transition_records(self) -> dict[str, dict[str, Any]]:
        raw = read_json(self.transition_path, None)
        executions = raw.get("executions") if isinstance(raw, dict) else None
        if not isinstance(executions, dict):
            return {}
        return {
            key: value for key, value in executions.items()
            if isinstance(key, str) and isinstance(value, dict)
        }

    def _technical_remediation_depth(
        self, review_id: str, project_id: str, task_id: str,
    ) -> tuple[int, str | None]:
        """Return completed remediation rounds in this review's exact lineage.

        A review of the original Worker has depth 0.  A review of remediation
        round N has depth N.  Missing or cyclic lineage fails closed so a
        corrupted history can never reopen an unbounded remediation loop.
        """
        transitions = self._transition_records()
        with self._lock:
            reviews = self._load_state()["reviews"]
        current = reviews.get(review_id)
        if not isinstance(current, dict):
            return 0, "current technical review record is unavailable"
        source_id = _nonblank(current.get("source_request_id"))
        if source_id is None:
            return 0, "current technical review lacks source execution identity"
        rounds = 0
        seen: set[str] = set()
        while source_id is not None:
            if source_id in seen:
                return rounds, "technical review remediation lineage contains a cycle"
            seen.add(source_id)
            worker = transitions.get(source_id)
            if not isinstance(worker, dict):
                # Older/recovered root reviews may retain reviewer state while
                # their original Worker transition ledger is unavailable.  A
                # source id that is not itself a known review is therefore a
                # safe root at depth 0.  If it names a review record, however,
                # it is a remediation descendant and the missing execution
                # evidence must fail closed.
                if isinstance(reviews.get(source_id), dict):
                    return rounds, f"source remediation execution {source_id!r} is missing from the durable ledger"
                return rounds, None
            if worker.get("project_id") != project_id or worker.get("task_id") != task_id:
                return rounds, "technical review remediation lineage changed project/task identity"
            if worker.get("source_kind") != "remediation":
                return rounds, None
            rounds += 1
            parent_review_id = _nonblank(worker.get("review_decision_id"))
            if parent_review_id is None:
                # Legacy remediation records predate explicit review lineage.
                # Treat such a record as the root remediation round.  New
                # remediation launches persist review_decision_id, so any
                # subsequent round becomes fully bounded and auditable.
                return rounds, None
            parent = reviews.get(parent_review_id)
            if not isinstance(parent, dict):
                return rounds, f"parent technical review {parent_review_id!r} is missing"
            if parent.get("project_id") != project_id or parent.get("task_id") != task_id:
                return rounds, "parent technical review changed project/task identity"
            source_id = _nonblank(parent.get("source_request_id"))
            if source_id is None:
                return rounds, "parent technical review lacks source execution identity"
        return rounds, None

    def _apply_remediation_budget(
        self, review_id: str, project_id: str, task_id: str,
        decision: str, next_action: str, reason: str, max_rounds: int,
    ) -> tuple[str, str, str, int]:
        """Fail closed to owner judgment when technical remediation is exhausted."""
        depth, lineage_error = self._technical_remediation_depth(review_id, project_id, task_id)
        if decision != "remediate":
            return decision, next_action, reason, depth
        if lineage_error is not None:
            return (
                "owner_gate", "stop",
                "technical review remediation lineage is not safely recoverable: "
                + lineage_error + "; unresolved reviewer finding: " + reason,
                depth,
            )
        if depth >= max_rounds:
            return (
                "owner_gate", "stop",
                f"technical review remediation budget exhausted ({depth} >= {max_rounds}); "
                "unresolved blocking finding requires owner disposition: " + reason,
                depth,
            )
        return decision, next_action, reason, depth

    @staticmethod
    def _project_map(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            str(project["project_id"]): project
            for project in config.get("projects") or []
            if isinstance(project, dict) and project.get("project_id")
        }

    def advance(self, config_path: Path | str) -> list[str]:
        config = load_projects_config(config_path)
        projects = self._project_map(config)
        with self._lock:
            for p_id, p in projects.items():
                b = p.get("conversation_binding")
                if isinstance(b, dict) and b.get("adapter") and b.get("binding_id"):
                    self._project_bindings[p_id] = copy.deepcopy(b)
        if self.progress_channel is not None and hasattr(self.progress_channel, "register_projects"):
            self.progress_channel.register_projects(projects.values())
        transition_records = self._transition_records()
        latest: dict[str, tuple[str, dict[str, Any], dict[str, Any]]] = {}
        for source_request_id, worker in transition_records.items():
            if worker.get("engine") != "aibroker" or worker.get("state") != "completed":
                continue
            project_id = _nonblank(worker.get("project_id"))
            if project_id is None or project_id not in projects:
                continue
            proj_dict = projects[project_id]
            harness_cfg = proj_dict.get("reviewer_harness")
            if isinstance(harness_cfg, dict) and harness_cfg.get("enabled") is True:
                base_policy, _ = _review_policy(proj_dict)
                policy = dict(base_policy) if base_policy else {
                    "quality": "high",
                    "independence": "resource",
                    "timeout_seconds": 600.0,
                    "max_remediation_rounds": _DEFAULT_MAX_REMEDIATION_ROUNDS,
                }
                policy["harness"] = True
                if harness_cfg.get("timeout_seconds"):
                    policy["timeout_seconds"] = float(harness_cfg["timeout_seconds"])
            else:
                if self.port is None:
                    continue
                policy, _ = _review_policy(proj_dict)
                if policy is None:
                    continue
            candidate_key = (str(worker.get("completed_at") or worker.get("started_at") or ""), source_request_id)
            previous = latest.get(project_id)
            previous_key = ((str(previous[1].get("completed_at") or previous[1].get("started_at") or ""), previous[0]) if previous else None)
            if previous_key is None or candidate_key > previous_key:
                latest[project_id] = (source_request_id, worker, policy)

        launched: list[str] = []
        for project_id, (source_request_id, worker, policy) in latest.items():
            review_id = "ai_review:" + source_request_id
            with self._lock:
                state = self._load_state()
                if review_id in state["reviews"]:
                    continue
            repo_path = _nonblank(worker.get("repo_path"))
            task_id = _nonblank(worker.get("task_id"))
            if repo_path is None or task_id is None:
                self._record_terminal(review_id, project_id, source_request_id, "failed", "worker identity incomplete")
                continue
            truth = read_repository_truth(repo_path)
            if not truth.valid:
                self._record_terminal(review_id, project_id, source_request_id, "failed", "repository truth unavailable")
                continue
            proj_dict = projects.get(project_id, {})
            binding = proj_dict.get("conversation_binding") or self._project_bindings.get(project_id)
            if binding is None and isinstance(worker.get("conversation_binding"), dict):
                binding = worker.get("conversation_binding")

            resource = worker.get("resource_context")
            if not isinstance(resource, dict):
                self._record_terminal(review_id, project_id, source_request_id, "failed", "worker resource context missing")
                continue
            # Reviewer independence is a core contract: degenerate worker
            # evidence ({} or blank/None identity fields) must fail closed here
            # rather than silently degrading independence downstream.
            missing = missing_independence_fields(policy.get("independence", "resource"), resource)
            if missing:
                self._record_terminal(
                    review_id, project_id, source_request_id, "failed",
                    "worker resource context incomplete for independence "
                    f"{policy.get('independence', 'resource')!r}: missing {', '.join(missing)}",
                )
                continue

            harness_cfg = proj_dict.get("reviewer_harness")
            if isinstance(harness_cfg, dict) and harness_cfg.get("enabled") is True:
                self._launch_harness_review(
                    review_id,
                    source_request_id,
                    project_id,
                    task_id,
                    repo_path,
                    truth,
                    harness_cfg,
                    worker=worker,
                    binding=binding,
                    policy=policy,
                )
                launched.append(review_id)
                continue

            previous = ResourceContext(
                resource.get("resource_id"), resource.get("provider"),
                resource.get("account"), resource.get("model"),
            )
            from dev_orchestrator.core.project_context import context_prompt_block
            context_block, resolution = context_prompt_block(proj_dict, "reviewer")
            ctx_decl = proj_dict.get("project_context") or {}
            if ctx_decl.get("enabled") and ctx_decl.get("require_valid", True) and resolution.state == "invalid":
                self._record_terminal(
                    review_id, project_id, source_request_id, "failed",
                    "durable project context is invalid: {0}".format(resolution.reason),
                )
                continue
            binding = proj_dict.get("conversation_binding") or self._project_bindings.get(project_id)
            if binding is None and isinstance(worker.get("conversation_binding"), dict):
                binding = worker.get("conversation_binding")
            failure_memory_block = ""
            if self.failure_memory is not None:
                failure_memory_block = self.failure_memory.prompt_block(
                    environment_for_project(proj_dict), max_chars=self.failure_memory_max_chars
                )
            prompt = self._review_prompt(
                project_id,
                task_id,
                source_request_id,
                truth,
                context_block=context_block,
                failure_memory_block=failure_memory_block,
            )
            request = AIRoleRequest(
                project_id=project_id, task_run_id=task_id, stage_run_id="review",
                role_run_id="reviewer-" + source_request_id.replace(":", "-"),
                request_id=review_id, role="reviewer", prompt=prompt,
                working_directory=Path(repo_path), quality=policy["quality"],
                independence=policy["independence"], previous_resource_context=previous,
                timeout_seconds=policy["timeout_seconds"],
                metadata={
                    "worker_source_request_id": source_request_id,
                    "conversation_binding": copy.deepcopy(binding) if isinstance(binding, dict) else None,
                    "failure_environment": environment_for_project(proj_dict),
                    "max_remediation_rounds": policy["max_remediation_rounds"],
                },
            )
            self._launch_review(
                review_id, source_request_id, request, truth,
                conversation_binding=binding, resolution=resolution,
            )
            launched.append(review_id)
        return launched

    def reconcile(
        self, project: dict[str, Any], snapshot: dict[str, Any],
        candidate: dict[str, Any], command_id: str,
    ) -> tuple[str | None, str]:
        """Launch one explicit, current-HEAD re-review without launching a Worker.

        The control coordinator has already resolved the target, but this
        adapter resolves it again immediately before persisting anything.  That
        closes the projection-to-launch race without modifying the stale review,
        its decision, or its original Worker execution.
        """
        from dev_orchestrator.control.reconcile import resolve_reconcile_candidate
        target, reason = resolve_reconcile_candidate(snapshot, self.runtime_root, project)
        if target is None:
            return None, reason
        if target.get("target_id") != candidate.get("target_id"):
            return None, "reconcile target changed before reviewer launch"
        policy, policy_reason = _review_policy(project)
        if policy is None:
            return None, policy_reason
        if self.port is None:
            return None, "AIBroker reviewer port unavailable"
        review_id = "ai_review:reconcile:" + command_id
        with self._lock:
            existing = self._load_state()["reviews"].get(review_id)
            if isinstance(existing, dict):
                if (
                    existing.get("reconcile_of") == target["target_id"]
                    and existing.get("project_id") == project.get("project_id")
                    and existing.get("source_request_id") == target.get("source_request_id")
                    and existing.get("task_id") == target.get("task_id")
                ):
                    return review_id, "reconcile review already launched for command_id"
                return None, "conflicting reconcile review replay"
        repo_path = _nonblank(project.get("repo_path"))
        source_id = _nonblank(target.get("source_request_id"))
        task_id = _nonblank(target.get("task_id"))
        resource = target.get("resource_context")
        if repo_path is None or source_id is None or task_id is None or not isinstance(resource, dict):
            return None, "reconcile candidate source identity is incomplete"
        truth = read_repository_truth(repo_path)
        if not truth.valid or truth.dirty or truth.branch != target.get("branch") or truth.head != target.get("current_head"):
            return None, "current repository changed before reconcile reviewer launch"
        previous = ResourceContext(
            resource.get("resource_id"), resource.get("provider"),
            resource.get("account"), resource.get("model"),
        )
        from dev_orchestrator.core.project_context import context_prompt_block
        context_block, resolution = context_prompt_block(project, "reviewer")
        ctx_decl = project.get("project_context") or {}
        if ctx_decl.get("enabled") and ctx_decl.get("require_valid", True) and resolution.state == "invalid":
            self._record_terminal(
                review_id, str(project["project_id"]), source_id, "failed",
                "durable project context is invalid: {0}".format(resolution.reason),
            )
            return None, "durable project context is invalid: {0}".format(resolution.reason)
        binding = project.get("conversation_binding") or self._project_bindings.get(str(project["project_id"]))
        failure_memory_block = ""
        if self.failure_memory is not None:
            failure_memory_block = self.failure_memory.prompt_block(
                environment_for_project(project), max_chars=self.failure_memory_max_chars
            )
        prior_reason = str(target.get("prior_reason") or "(no prior reviewer reason recorded)").strip()[:2000]
        reanchor_context = (
            "[STALE_REVIEW_REANCHOR]\n"
            "A prior technical review at HEAD {0} requested REMEDIATE: {1}\n"
            "That review anchor is stale. Independently review the CURRENT clean HEAD; "
            "do not assume the prior verdict remains correct.\n"
            "[/STALE_REVIEW_REANCHOR]"
        ).format(target.get("reviewed_head"), prior_reason)
        request = AIRoleRequest(
            project_id=str(project["project_id"]), task_run_id=task_id, stage_run_id="review",
            role_run_id="reviewer-reconcile-" + command_id,
            request_id=review_id, role="reviewer",
            prompt=self._review_prompt(
                str(project["project_id"]), task_id, source_id, truth,
                context_block=context_block, failure_memory_block=failure_memory_block,
                reanchor_context=reanchor_context,
            ),
            working_directory=Path(repo_path), quality=policy["quality"],
            independence=policy["independence"], previous_resource_context=previous,
            timeout_seconds=policy["timeout_seconds"],
            metadata={
                "worker_source_request_id": source_id,
                "conversation_binding": copy.deepcopy(binding) if isinstance(binding, dict) else None,
                "failure_environment": environment_for_project(project),
                "max_remediation_rounds": policy["max_remediation_rounds"],
                "reconcile_of": target["target_id"],
            },
        )
        self._launch_review(
            review_id, source_id, request, truth,
            conversation_binding=binding, resolution=resolution,
        )
        return review_id, "stale technical review re-anchored at current clean HEAD"


    def rereview_descendant(self, project, snapshot, candidate, command_id):
        """Re-review current clean descendant HEAD while preserving the stale review lineage."""
        from dev_orchestrator.control.reconcile import resolve_rereview_candidate
        target, reason = resolve_rereview_candidate(snapshot, self.runtime_root, project)
        if target is None or target.get("target_id") != candidate.get("target_id"):
            return None, reason or "re-review target changed before launch"
        policy, policy_reason = _review_policy(project)
        if policy is None or self.port is None:
            return None, policy_reason or "AIBroker reviewer port unavailable"
        review_id = "ai_review:rereview:" + command_id
        with self._lock:
            existing = self._load_state()["reviews"].get(review_id)
            if isinstance(existing, dict):
                return (review_id, "descendant re-review already launched for command_id") if existing.get("rereview_of") == target["target_id"] else (None, "conflicting descendant re-review replay")
        repo_path = _nonblank(project.get("repo_path")); source_id = _nonblank(target.get("source_request_id")); task_id = _nonblank(target.get("task_id"))
        resource = target.get("resource_context"); truth = read_repository_truth(repo_path or "")
        if not repo_path or not source_id or not task_id or not isinstance(resource, dict) or not truth.valid or truth.dirty or truth.head != target.get("current_head"):
            return None, "current repository or source identity changed before descendant re-review"
        previous = ResourceContext(resource.get("resource_id"), resource.get("provider"), resource.get("account"), resource.get("model"))
        from dev_orchestrator.core.project_context import context_prompt_block
        context_block, resolution = context_prompt_block(project, "reviewer")
        binding = project.get("conversation_binding") or self._project_bindings.get(str(project["project_id"]))
        if target.get("kind") == "next":
            reanchor = ("[ACCEPTED_NEXT_DESCENDANT_REREVIEW]\n"
                        "A prior technical review accepted NEXT (next_task) at HEAD {0}: {1}\n"
                        "That acceptance anchor is now stale because the current clean HEAD is a descendant. "
                        "Independently review the CURRENT HEAD from repository evidence; do not inherit or import the prior accepted verdict.\n"
                        "[/ACCEPTED_NEXT_DESCENDANT_REREVIEW]").format(target.get("reviewed_head"), target.get("prior_reason"))
        elif target.get("kind") == "remediate":
            reanchor = ("[REMEDIATED_DESCENDANT_REREVIEW]\n"
                        "A prior technical review requested REMEDIATE at HEAD {0}: {1}\n"
                        "The current clean HEAD is a descendant containing subsequent bounded remediation. "
                        "Independently review the CURRENT HEAD from repository evidence; do not assume the remediation succeeded and do not inherit the prior verdict.\n"
                        "[/REMEDIATED_DESCENDANT_REREVIEW]").format(target.get("reviewed_head"), target.get("prior_reason"))
        else:
            reanchor = ("[FAILED_REVIEW_DESCENDANT_REREVIEW]\nPrior reviewer infrastructure failed at HEAD {0}: {1}\n"
                        "The current clean HEAD is a descendant containing bounded recovery fixes. Independently review CURRENT HEAD; do not inherit a verdict.\n"
                        "[/FAILED_REVIEW_DESCENDANT_REREVIEW]").format(target.get("reviewed_head"), target.get("prior_reason"))
        request = AIRoleRequest(project_id=str(project["project_id"]), task_run_id=task_id, stage_run_id="review",
            role_run_id="reviewer-rereview-" + command_id, request_id=review_id, role="reviewer",
            prompt=self._review_prompt(str(project["project_id"]), task_id, source_id, truth, context_block=context_block, reanchor_context=reanchor),
            working_directory=Path(repo_path), quality=policy["quality"], independence=policy["independence"], previous_resource_context=previous,
            timeout_seconds=policy["timeout_seconds"], metadata={"worker_source_request_id": source_id, "conversation_binding": copy.deepcopy(binding) if isinstance(binding, dict) else None,
            "failure_environment": environment_for_project(project), "max_remediation_rounds": policy["max_remediation_rounds"],
            "rereview_of": target["target_id"]})
        self._launch_review(review_id, source_id, request, truth, conversation_binding=binding, resolution=resolution)
        if target.get("kind") == "next":
            return review_id, "stale accepted NEXT re-reviewed at current clean descendant HEAD"
        if target.get("kind") == "remediate":
            return review_id, "stale REMEDIATE re-reviewed at current clean descendant HEAD"
        return review_id, "failed reviewer lineage re-anchored at current clean descendant HEAD"

    def retry_failed(
        self, project: dict[str, Any], snapshot: dict[str, Any],
        candidate: dict[str, Any], command_id: str,
    ) -> tuple[str | None, str]:
        """Retry one exact failed technical review at the unchanged clean HEAD."""
        from dev_orchestrator.control.reconcile import resolve_retry_candidate
        target, reason = resolve_retry_candidate(snapshot, self.runtime_root, project)
        if target is None:
            return None, reason
        if target.get("target_id") != candidate.get("target_id"):
            return None, "retry target changed before reviewer launch"
        policy, policy_reason = _review_policy(project)
        if policy is None:
            return None, policy_reason
        if self.port is None:
            return None, "AIBroker reviewer port unavailable"
        review_id = "ai_review:retry:" + command_id
        with self._lock:
            existing = self._load_state()["reviews"].get(review_id)
            if isinstance(existing, dict):
                if (
                    existing.get("retry_of") == target["target_id"]
                    and existing.get("project_id") == project.get("project_id")
                    and existing.get("source_request_id") == target.get("source_request_id")
                    and existing.get("task_id") == target.get("task_id")
                ):
                    return review_id, "failed review retry already launched for command_id"
                return None, "conflicting failed-review retry replay"
        repo_path = _nonblank(project.get("repo_path"))
        source_id = _nonblank(target.get("source_request_id"))
        task_id = _nonblank(target.get("task_id"))
        resource = target.get("resource_context")
        if repo_path is None or source_id is None or task_id is None or not isinstance(resource, dict):
            return None, "retry candidate source identity is incomplete"
        truth = read_repository_truth(repo_path)
        if not truth.valid or truth.dirty or truth.branch != target.get("branch") or truth.head != target.get("current_head"):
            return None, "current repository changed before retry reviewer launch"
        previous = ResourceContext(
            resource.get("resource_id"), resource.get("provider"),
            resource.get("account"), resource.get("model"),
        )
        from dev_orchestrator.core.project_context import context_prompt_block
        context_block, resolution = context_prompt_block(project, "reviewer")
        ctx_decl = project.get("project_context") or {}
        if ctx_decl.get("enabled") and ctx_decl.get("require_valid", True) and resolution.state == "invalid":
            self._record_terminal(
                review_id, str(project["project_id"]), source_id, "failed",
                "durable project context is invalid: {0}".format(resolution.reason),
            )
            return None, "durable project context is invalid: {0}".format(resolution.reason)
        binding = project.get("conversation_binding") or self._project_bindings.get(str(project["project_id"]))
        failure_memory_block = ""
        if self.failure_memory is not None:
            failure_memory_block = self.failure_memory.prompt_block(
                environment_for_project(project), max_chars=self.failure_memory_max_chars
            )
        retry_context = (
            "[FAILED_REVIEW_RETRY]\n"
            "A prior technical review of this SAME clean HEAD failed before producing a durable decision: {0}\n"
            "Independently review the current HEAD from repository evidence. Do not infer a verdict from the failed attempt.\n"
            "[/FAILED_REVIEW_RETRY]"
        ).format(str(target.get("prior_reason") or "review infrastructure failure")[:2000])
        request = AIRoleRequest(
            project_id=str(project["project_id"]), task_run_id=task_id, stage_run_id="review",
            role_run_id="reviewer-retry-" + command_id,
            request_id=review_id, role="reviewer",
            prompt=self._review_prompt(
                str(project["project_id"]), task_id, source_id, truth,
                context_block=context_block, failure_memory_block=failure_memory_block,
                reanchor_context=retry_context,
            ),
            working_directory=Path(repo_path), quality=policy["quality"],
            independence=policy["independence"], previous_resource_context=previous,
            timeout_seconds=policy["timeout_seconds"],
            metadata={
                "worker_source_request_id": source_id,
                "conversation_binding": copy.deepcopy(binding) if isinstance(binding, dict) else None,
                "failure_environment": environment_for_project(project),
                "max_remediation_rounds": policy["max_remediation_rounds"],
                "retry_of": target["target_id"],
            },
        )
        self._launch_review(
            review_id, source_id, request, truth,
            conversation_binding=binding, resolution=resolution,
        )
        return review_id, "failed technical review retried at the same clean HEAD"

    @staticmethod
    def _review_prompt(
        project_id: str, task_id: str, source_request_id: str, truth: Any,
        context_block: str = "",
        failure_memory_block: str = "",
        reanchor_context: str = "",
    ) -> str:
        prompt = (
            "You are the independent reviewer for a completed software-development Worker. "
            "Review only; do not modify files, commit, push, or start another Worker. "
            "Inspect the repository, current diff/status, task evidence, tests, and acceptance criteria. "
            "Return exactly one JSON object and no markdown or extra text, with exactly these keys: "
            '{"decision":"next|remediate|owner_gate|stop","next_action":"next_task|continue_current_stage|stop","reason":"..."}. '
            "Allowed pairs are next/next_task, remediate/continue_current_stage, owner_gate/stop, stop/stop. "
            "Use next only when the reviewed task is actually complete and repository evidence supports advancing. "
            "Use remediate for bounded fixable gaps in this reviewed task; owner_gate only when owner input is genuinely required.\n\n"
            f"{workflow_policy_prompt('technical_reviewer')}\n\n"
            f"Project: {project_id}\nReviewed task: {task_id}\nWorker source: {source_request_id}\n"
            f"Review branch: {truth.branch}\nReview HEAD: {truth.head}\nReview dirty: {truth.dirty}\n"
        )
        if context_block:
            prompt += f"\n{context_block}\n"
        if failure_memory_block:
            prompt += f"\n{failure_memory_block}\n"
        if reanchor_context:
            prompt += f"\n{reanchor_context}\n"
        return prompt

    def _launch_harness_review(
        self,
        review_id: str,
        source_request_id: str,
        project_id: str,
        task_id: str,
        repo_path: str,
        truth: Any,
        harness_cfg: dict[str, Any],
        *,
        worker: dict[str, Any],
        binding: Optional[dict[str, Any]] = None,
        policy: dict[str, Any],
    ) -> None:
        with self._lock:
            state = self._load_state()
            if review_id in state["reviews"]:
                return
            state["reviews"][review_id] = {
                "review_id": review_id,
                "project_id": project_id,
                "source_request_id": source_request_id,
                "task_id": task_id,
                "branch": truth.branch,
                "head": truth.head,
                "review_status_hash": truth.status_hash,
                "review_dirty": bool(truth.dirty),
                "state": "launching",
                "started_at": utc_now_iso(),
                "role_run_id": "reviewer-" + source_request_id.replace(":", "-"),
                "conversation_binding": copy.deepcopy(binding) if isinstance(binding, dict) else None,
                "harness": True,
                # Recovery context: a daemon restart must be able to run the
                # full deterministic disposition without the in-memory launch
                # arguments, so the inputs it needs are durable on the record.
                "repo_path": repo_path,
                "harness_cfg": copy.deepcopy(harness_cfg) if isinstance(harness_cfg, dict) else {},
                "worker": copy.deepcopy(worker) if isinstance(worker, dict) else {},
                "policy": copy.deepcopy(policy) if isinstance(policy, dict) else {},
            }
            if binding and isinstance(binding, dict):
                self._project_bindings[project_id] = copy.deepcopy(binding)
            self._save_state(state)

        thread = threading.Thread(
            target=self._run_harness_review,
            args=(review_id, source_request_id, project_id, task_id, repo_path, truth, harness_cfg, worker, binding, policy),
            name=f"devorch-review-harness-{project_id}",
            daemon=True,
        )
        with self._lock:
            self._threads[review_id] = thread
        thread.start()

        if self.progress_channel is not None:
            payload = {"project_id": project_id}
            if binding:
                payload["conversation_binding"] = binding
            self.progress_channel.emit(
                payload, "REVIEW_STARTED",
                task_id=task_id, occurrence_key=review_id,
                details={"review_id": review_id, "worker_source_request_id": source_request_id},
            )

    def _run_harness_review(
        self,
        review_id: str,
        source_request_id: str,
        project_id: str,
        task_id: str,
        repo_path: str,
        truth: Any,
        harness_cfg: dict[str, Any],
        worker: dict[str, Any],
        binding: Optional[dict[str, Any]],
        policy: dict[str, Any],
    ) -> None:
        with self._lock:
            state = self._load_state()
            record = state["reviews"].get(review_id)
            if not isinstance(record, dict):
                return
            record["state"] = "running"
            self._save_state(state)

        if self.accounting is not None:
            self.accounting.start_interval(
                "technical_review",
                review_id,
                project_id=project_id,
                task_id=task_id,
                role="reviewer",
                request_id=review_id,
                stage_run_id="review",
                role_run_id=record.get("role_run_id") or f"reviewer-{review_id}",
                source_request_id=source_request_id or None,
                attempt_id=source_request_id or None,
            )

        def _fail_review(err_msg: str) -> None:
            if self.accounting is not None:
                self.accounting.end_interval(
                    "technical_review",
                    review_id,
                    outcome="failed",
                    project_id=project_id,
                    task_id=task_id,
                    role="reviewer",
                    request_id=review_id,
                    stage_run_id="review",
                    role_run_id=record.get("role_run_id") or f"reviewer-{review_id}",
                    source_request_id=source_request_id or None,
                    attempt_id=source_request_id or None,
                )
                self.accounting.record_attempt_outcome(
                    source_request_id,
                    "rejected",
                    project_id=project_id,
                    task_id=task_id,
                    role="reviewer",
                    request_id=review_id,
                    source_request_id=source_request_id,
                    metadata={"review_kind": "technical_harness", "decision": "failed", "reason": err_msg},
                )
            self._finish_harness_terminal(
                review_id, project_id, task_id, source_request_id, "failed", err_msg,
                binding=binding,
            )

        try:
            worker_resource = worker.get("resource_context")
            if not isinstance(worker_resource, dict):
                _fail_review("worker resource context missing")
                return
            independence = policy.get("independence", "resource")
            missing = missing_independence_fields(independence, worker_resource)
            if missing:
                _fail_review(
                    f"worker resource context incomplete for independence {independence!r}: "
                    f"missing {', '.join(missing)}"
                )
                return

            from dev_orchestrator.review.models import ReviewRequest
            harness = self._get_harness()

            raw_limits = harness_cfg.get("file_limits") or {}
            file_limits = {
                "max_files": int(raw_limits.get("max_files", harness_cfg.get("max_files", 100))),
                "max_total_bytes": int(raw_limits.get("max_total_bytes", raw_limits.get("max_bytes", harness_cfg.get("max_bytes", harness_cfg.get("max_total_bytes", 1024 * 1024))))),
                "max_packet_files": int(raw_limits.get("max_packet_files", raw_limits.get("packet_size", harness_cfg.get("packet_size", harness_cfg.get("max_packet_files", 10))))),
                "max_packet_bytes": int(raw_limits.get("max_packet_bytes", harness_cfg.get("max_packet_bytes", 200 * 1024))),
            }
            file_limits["max_bytes"] = file_limits["max_total_bytes"]

            diff_refs = {"head": truth.head, "commit": truth.head, "base": f"{truth.head}~1"}

            metadata = {
                "repo_path": repo_path,
                "worker_source_request_id": source_request_id,
                "conversation_binding": copy.deepcopy(binding) if isinstance(binding, dict) else None,
                "command_ref": harness_cfg.get("command_ref", "review-runner"),
                "ocr_executable": harness_cfg.get("ocr_executable"),
                "independence": policy.get("independence", "resource"),
                "quality": policy.get("quality", "high"),
                "timeout_seconds": float(policy.get("timeout_seconds", 600.0)),
                "previous_resource_context": copy.deepcopy(worker_resource),
                "worker_resource_context": copy.deepcopy(worker_resource),
            }

            req = ReviewRequest(
                request_id=review_id,
                project_id=project_id,
                task_id=task_id,
                source_request_id=source_request_id,
                mode=harness_cfg.get("mode", "diff"),
                diff_mode=harness_cfg.get("diff_mode", "workspace"),
                diff_refs=diff_refs,
                scan_roots=list(harness_cfg.get("scan_roots") or []),
                rule_pack_path=harness_cfg.get("rule_pack"),
                blocking_severities=list(harness_cfg.get("blocking_severities") or ["blocking"]),
                independent_gates=list(harness_cfg.get("independent_gates") or []),
                branch=truth.branch,
                head=truth.head,
                status_hash=truth.status_hash,
                transport=harness_cfg.get("transport", "local"),
                file_limits=file_limits,
                metadata=metadata,
            )

            session = harness.submit(req)

            with self._lock:
                state = self._load_state()
                rec = state["reviews"].get(review_id)
                if rec:
                    rec["job_id"] = session.job_id
                    rec["session_id"] = session.session_id
                    self._save_state(state)

            timeout = float(harness_cfg.get("timeout_seconds", harness_cfg.get("timeout", policy.get("timeout_seconds", 600.0))))
            poll_interval = float(harness_cfg.get("poll_interval_seconds", harness_cfg.get("poll_interval", 0.2)))
            start_poll = time.monotonic()

            while time.monotonic() - start_poll < timeout:
                session = harness.status(session.session_id)
                if session.state in ("completed", "failed", "cancelled"):
                    break
                time.sleep(poll_interval)

            if session.state not in ("completed", "failed", "cancelled"):
                raise TimeoutError("review session timed out waiting for completion")

            if session.state != "completed":
                err_msg = session.failure_reason or f"review session {session.state}"
                _fail_review(err_msg)
                return

            review_result = harness.result(session.session_id)

        except Exception as exc:
            _fail_review(f"reviewer harness error: {exc}")
            return

        self._finalize_harness_review(
            review_id=review_id,
            project_id=project_id,
            task_id=task_id,
            source_request_id=source_request_id,
            repo_path=repo_path,
            harness_cfg=harness_cfg,
            worker=worker,
            binding=binding,
            review_result=review_result,
        )

    def _finalize_harness_review(
        self,
        *,
        review_id: str,
        project_id: str,
        task_id: str,
        source_request_id: str,
        repo_path: Optional[str],
        harness_cfg: dict[str, Any],
        worker: dict[str, Any],
        binding: Optional[dict[str, Any]],
        review_result: Any,
        recovery_failure_classification: str | None = None,
    ) -> None:
        """Run the deterministic post-harness disposition for one review.

        This is the single authoritative path from harness evidence to a DevO
        lifecycle decision: repository-truth recheck, independent gates,
        coverage completeness, findings classification, durable decision
        persistence and exactly one terminal lifecycle event. It is shared by
        normal completion and by daemon-restart recovery so a recovered review
        can never settle without a decision.

        Idempotent: a review that already holds a durable decision is left
        untouched and no duplicate event is emitted.
        """
        with self._lock:
            rec = self._load_state()["reviews"].get(review_id) or {}
            role_run_id = rec.get("role_run_id") or f"reviewer-{review_id}"
            durable = self._durable_decision(review_id)

        def _fail(err_msg: str) -> None:
            if self.accounting is not None:
                self.accounting.end_interval(
                    "technical_review",
                    review_id,
                    outcome="failed",
                    project_id=project_id,
                    task_id=task_id,
                    role="reviewer",
                    request_id=review_id,
                    stage_run_id="review",
                    role_run_id=role_run_id,
                    source_request_id=source_request_id or None,
                    attempt_id=source_request_id or None,
                )
                self.accounting.record_attempt_outcome(
                    source_request_id,
                    "rejected",
                    project_id=project_id,
                    task_id=task_id,
                    role="reviewer",
                    request_id=review_id,
                    source_request_id=source_request_id,
                    metadata={"review_kind": "technical_harness", "decision": "failed", "reason": err_msg},
                )
            self._finish_harness_terminal(
                review_id, project_id, task_id, source_request_id, "failed", err_msg,
                binding=binding,
                extra={"recovery_classification": recovery_failure_classification}
                if recovery_failure_classification else None,
            )

        if durable is not None:
            # The decision ledger is the source of truth, and it is written
            # before the reviewer-state terminal write. A replay that lands
            # between those two writes must converge on the durable decision
            # rather than re-deriving one: re-deriving would raise a spurious
            # replay conflict and settle the review as failed while a real
            # decision already exists. Re-emitting the terminal state is safe
            # because the progress channel deduplicates on occurrence key.
            #
            # The ledger is trusted only after validation: an entry that is
            # incomplete, internally inconsistent, or belongs to another review
            # must never settle this one as completed.
            invalid = self._validate_durable_decision(review_id, durable, rec)
            if invalid:
                self._quarantine_decision(review_id, invalid)
                _fail(f"durable decision rejected: {invalid}")
                return
            if rec.get("decision") and rec.get("state") in _TERMINAL_STATES:
                return
            self._project_durable_decision(review_id, durable, binding)
            return

        # 1. Repository truth check. The reviewed anchor and the repository it
        #    refers to must both be present and must still match; missing
        #    evidence fails closed rather than being accepted. The repository
        #    path is re-checked here as well as at each caller, because a blank
        #    path would otherwise resolve to the daemon's working directory and
        #    silently verify the wrong repository.
        if _nonblank(repo_path) is None:
            _fail("review repository path evidence missing; cannot verify repository truth")
            return
        branch = _nonblank(rec.get("branch"))
        head = _nonblank(rec.get("head"))
        status_hash = _nonblank(rec.get("review_status_hash"))
        if branch is None or head is None or status_hash is None:
            _fail("review anchor evidence missing; cannot verify repository truth")
            return
        current_truth = read_repository_truth(repo_path)
        if (
            not current_truth.valid
            or current_truth.branch != branch
            or current_truth.head != head
            or current_truth.status_hash != status_hash
        ):
            _fail("repository changed during review")
            return

        # 2. Independent gates check
        required_gates = harness_cfg.get("independent_gates") or []
        gates_ok, gate_err = self._check_independent_gates(worker, required_gates)
        if not gates_ok:
            _fail(f"independent gate failed: {gate_err}")
            return

        # 3. Coverage completeness check
        if review_result.completeness != "complete":
            _fail(f"review coverage incomplete: {review_result.completeness} ({review_result.reason})")
            return

        # 4. Findings classification
        blocking_severities = set(harness_cfg.get("blocking_severities") or ["blocking"])
        blocking_findings = [f for f in review_result.findings if f.severity in blocking_severities]

        if blocking_findings:
            decision = "remediate"
            next_action = "continue_current_stage"
            reason = f"{len(blocking_findings)} blocking finding(s) detected: " + "; ".join(
                f"{f.rule_id} at {f.file}:{f.start_line}" for f in blocking_findings[:3]
            )
        else:
            decision = "next"
            next_action = "next_task"
            reason = f"Review accepted clean ({len(review_result.findings)} non-blocking finding(s))"

        policy = rec.get("policy") if isinstance(rec.get("policy"), dict) else {}
        max_rounds = int(policy.get("max_remediation_rounds", _DEFAULT_MAX_REMEDIATION_ROUNDS))
        decision, next_action, reason, remediation_round = self._apply_remediation_budget(
            review_id, project_id, task_id, decision, next_action, reason, max_rounds
        )
        disposition = _DECISION_DISPOSITIONS[(decision, next_action)]

        try:
            self._write_decision(
                review_id=review_id,
                project_id=project_id,
                task_id=task_id,
                branch=branch,
                head=head,
                status_hash=status_hash,
                decision=decision,
                next_action=next_action,
                disposition=disposition,
                reason=reason,
            )
        except Exception as exc:
            _fail(f"decision persistence failed: {exc}")
            return

        if self.accounting is not None:
            self.accounting.end_interval(
                "technical_review",
                review_id,
                outcome="accepted" if decision == "next" else "failed",
                project_id=project_id,
                task_id=task_id,
                role="reviewer",
                request_id=review_id,
                stage_run_id="review",
                role_run_id=role_run_id,
                source_request_id=source_request_id or None,
                attempt_id=source_request_id or None,
            )
            self.accounting.record_attempt_outcome(
                source_request_id,
                "accepted" if decision == "next" else "rejected",
                project_id=project_id,
                task_id=task_id,
                role="reviewer",
                request_id=review_id,
                source_request_id=source_request_id,
                metadata={"review_kind": "technical_harness", "decision": decision, "reason": reason},
            )

        if self.accounting is not None and decision == "owner_gate":
            self.accounting.open_owner_gate(
                review_id,
                project_id=project_id,
                task_id=task_id,
                role="owner",
                request_id=review_id,
                source_request_id=source_request_id or None,
            )

        self._finish_harness_terminal(
            review_id, project_id, task_id, source_request_id, "completed", reason,
            decision=decision, next_action=next_action, binding=binding,
            extra={
                "disposition": disposition,
                "completeness": review_result.completeness,
                "findings_count": len(review_result.findings),
                "blocking_count": len(blocking_findings),
                "remediation_round": remediation_round,
                "max_remediation_rounds": max_rounds,
            }
        )

    @staticmethod
    def _check_independent_gates(worker: dict[str, Any], required_gates: list[str]) -> tuple[bool, str]:
        if not required_gates:
            return True, ""
        gates_dict = worker.get("independent_gates") or worker.get("gates") or {}
        if not isinstance(gates_dict, dict):
            gates_dict = {}
        for gate in required_gates:
            val = gates_dict.get(gate) if gate in gates_dict else worker.get(gate)
            if isinstance(val, dict):
                st = val.get("status")
            elif isinstance(val, bool):
                st = "passed" if val else "failed"
            elif isinstance(val, str):
                st = val
            else:
                st = None
            if st not in ("passed", "success", "ok"):
                return False, f"gate {gate!r} status is {st!r}, expected 'passed'"
        return True, ""

    def _finish_harness_terminal(
        self,
        review_id: str,
        project_id: str,
        task_id: str,
        source_request_id: str | None,
        state_name: str,
        reason: str,
        *,
        decision: Optional[str] = None,
        next_action: Optional[str] = None,
        binding: Optional[dict[str, Any]] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        milestone = _terminal_milestone(state_name, decision)
        with self._lock:
            state = self._load_state()
            record = state["reviews"].get(review_id)
            if isinstance(record, dict):
                record.update({
                    "state": state_name,
                    "reason": reason,
                    "completed_at": utc_now_iso(),
                })
                if decision:
                    record["decision"] = decision
                if next_action:
                    record["next_action"] = next_action
                if extra:
                    record.update(extra)
                # Mark the lifecycle event as owed before emitting it. A crash
                # between this write and the emit below would otherwise lose the
                # event permanently, because terminal records are not replayed
                # by interrupted-review recovery.
                if milestone:
                    record["lifecycle_event_pending"] = milestone
                else:
                    record.pop("lifecycle_event_pending", None)
                self._save_state(state)

        if milestone:
            self._emit_terminal_milestone(
                review_id, project_id, task_id, milestone, reason,
                decision=decision, binding=binding,
            )

    def _emit_terminal_milestone(
        self,
        review_id: str,
        project_id: str,
        task_id: str,
        milestone: str,
        reason: str,
        *,
        decision: Optional[str] = None,
        binding: Optional[dict[str, Any]] = None,
    ) -> None:
        """Emit one terminal lifecycle event and clear its pending marker.

        The progress channel deduplicates on occurrence key, so replaying an
        already-delivered event is a no-op.
        """
        if self.progress_channel is not None:
            proj_payload = {"project_id": project_id}
            if binding:
                proj_payload["conversation_binding"] = binding
            details = {"reason": reason}
            if decision:
                details["decision"] = decision
            try:
                self.progress_channel.emit(
                    proj_payload, milestone,
                    task_id=task_id, occurrence_key=review_id,
                    details=details,
                )
            except Exception:
                # Delivery failed: leave `lifecycle_event_pending` set so the
                # next recovery replays it. A transport fault must never
                # propagate out of recovery (and thus out of coordinator
                # construction) or abort daemon startup.
                return
        with self._lock:
            state = self._load_state()
            record = state["reviews"].get(review_id)
            if isinstance(record, dict) and record.pop("lifecycle_event_pending", None) is not None:
                self._save_state(state)

    def _replay_pending_lifecycle_events(self) -> None:
        """Re-emit terminal lifecycle events owed from a previous process.

        Covers a crash between the terminal state write and the event emit.
        Deduplication makes an unnecessary replay harmless.
        """
        with self._lock:
            state = self._load_state()
            owed = [
                (rid, copy.deepcopy(rec))
                for rid, rec in state["reviews"].items()
                if isinstance(rec, dict) and _nonblank(rec.get("lifecycle_event_pending"))
            ]
        for review_id, rec in owed:
            binding = rec.get("conversation_binding")
            self._emit_terminal_milestone(
                review_id,
                str(rec.get("project_id") or ""),
                str(rec.get("task_id") or ""),
                str(rec.get("lifecycle_event_pending")),
                str(rec.get("reason") or ""),
                decision=rec.get("decision"),
                binding=binding if isinstance(binding, dict) and binding else None,
            )

    def reconcile_session(self, session_id: str) -> Any:
        """Reconcile a review session via the harness."""
        harness = self._get_harness()
        return harness.reconcile(session_id)

    def get_review_session(self, session_id: str) -> Any:
        """Query a review session status via the harness."""
        harness = self._get_harness()
        return harness.status(session_id)

    def _launch_review(
        self, review_id: str, source_request_id: str, request: AIRoleRequest, truth: Any,
        conversation_binding: Optional[dict[str, Any]] = None,
        resolution: Optional[Any] = None,
    ) -> None:
        if conversation_binding is None and isinstance(request.metadata, dict):
            conversation_binding = request.metadata.get("conversation_binding")
        if conversation_binding is None:
            conversation_binding = self._project_bindings.get(request.project_id)
        with self._lock:
            state = self._load_state()
            if review_id in state["reviews"]:
                return
            state["reviews"][review_id] = {
                "review_id": review_id,
                "project_id": request.project_id,
                "source_request_id": source_request_id,
                "task_id": request.task_run_id,
                "branch": truth.branch,
                "head": truth.head,
                "review_status_hash": truth.status_hash,
                "review_dirty": bool(truth.dirty),
                "state": "launching",
                "started_at": utc_now_iso(),
                "role_run_id": request.role_run_id,
                "conversation_binding": copy.deepcopy(conversation_binding) if isinstance(conversation_binding, dict) else None,
                "context_state": resolution.state if resolution else None,
                "context_digest": resolution.document.digest if resolution and resolution.document else None,
                "reconcile_of": request.metadata.get("reconcile_of") if isinstance(request.metadata, dict) else None,
                "retry_of": request.metadata.get("retry_of") if isinstance(request.metadata, dict) else None,
                "rereview_of": request.metadata.get("rereview_of") if isinstance(request.metadata, dict) else None,
            }
            if conversation_binding and isinstance(conversation_binding, dict):
                self._project_bindings[request.project_id] = copy.deepcopy(conversation_binding)
            self._save_state(state)
        try:
            from dev_orchestrator.core.execution_context import update_context
            update_context(
                self.runtime_root,
                request.project_id,
                task_id=request.task_run_id,
                git_anchor=truth.head,
                active_role="reviewer",
                disposition="hold",
                next_action="technical_review",
                branch=truth.branch,
            )
        except Exception:
            pass
        thread = threading.Thread(
            target=self._run_review,
            args=(review_id, request),
            name="devorch-review-" + request.project_id,
            daemon=True,
        )
        with self._lock:
            self._threads[review_id] = thread
        thread.start()
        if self.progress_channel is not None:
            payload = {"project_id": request.project_id}
            if conversation_binding:
                payload["conversation_binding"] = conversation_binding
            self.progress_channel.emit(
                payload, "REVIEW_STARTED",
                task_id=request.task_run_id, occurrence_key=review_id,
                details={"review_id": review_id, "worker_source_request_id": source_request_id},
            )

    def _record_terminal(
        self, review_id: str, project_id: str, source_request_id: str,
        state_name: str, reason: str, **kwargs: Any,
    ) -> None:
        with self._lock:
            state = self._load_state()
            state["reviews"].setdefault(review_id, {
                "review_id": review_id, "project_id": project_id,
                "source_request_id": source_request_id, "started_at": utc_now_iso(),
            })
            state["reviews"][review_id].update({
                "state": state_name, "reason": reason, "completed_at": utc_now_iso(),
                **kwargs,
            })
            self._save_state(state)
        try:
            from dev_orchestrator.core.execution_context import record_role_completion, update_context
            record_role_completion(
                self.runtime_root,
                project_id,
                "reviewer",
                {
                    "state": state_name,
                    "reason": reason,
                },
            )
            if state_name == "failed":
                update_context(self.runtime_root, project_id, disposition="terminal_failure", active_role=None)
        except Exception:
            pass

    def _capture_reviewer_output(
        self,
        review_id: str,
        request: AIRoleRequest,
        result: Any,
        *,
        protocol_stage: str | None = None,
        repair_index: int = 0,
    ) -> None:
        raw = getattr(result, "output", None)
        raw_text = raw if isinstance(raw, str) else ""
        with self._lock:
            state = self._load_state()
            record = state["reviews"].get(review_id)
            if not isinstance(record, dict):
                return
            captures = record.setdefault("reviewer_raw_outputs", [])
            if not isinstance(captures, list):
                captures = []
                record["reviewer_raw_outputs"] = captures
            resource = getattr(result, "resource_context", None)
            captures.append({
                "request_id": request.request_id,
                "resource": {
                    "resource_id": getattr(resource, "resource_id", None),
                    "provider": getattr(resource, "provider", None),
                    "account": getattr(resource, "account", None),
                    "model": getattr(resource, "model", None),
                } if resource is not None else None,
                "repair_index": repair_index,
                "protocol_stage": protocol_stage,
                "sha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
                "output": raw_text,
                "captured_at": utc_now_iso(),
            })
            if len(captures) > 16:
                del captures[:-16]
            self._save_state(state)

    @staticmethod
    def _technical_review_protocol_repair_prompt(raw_output: str, failure: str) -> str:
        return protocol_repair_prompt(
            raw_output=raw_output,
            failure=failure,
            schema_example='{"decision":"next|remediate|owner_gate|stop","next_action":"next_task|continue_current_stage|stop","reason":"..."}',
            label="technical reviewer",
        )

    def _run_review(self, review_id: str, request: AIRoleRequest) -> None:
        with self._lock:
            state = self._load_state()
            record = state["reviews"].get(review_id)
            if not isinstance(record, dict):
                return
            record["state"] = "running"
            self._save_state(state)
        source_request_id = str(request.metadata.get("worker_source_request_id") or "")
        review_started = time.monotonic()

        failed_resource_ids: set[str] = set(request.excluded_resource_ids)
        max_resource_failovers = 2
        if isinstance(request.metadata, dict):
            max_failovers_meta = request.metadata.get("max_resource_failovers")
            if isinstance(max_failovers_meta, int) and 0 <= max_failovers_meta <= 5:
                max_resource_failovers = max_failovers_meta
        max_attempts = 1 + max_resource_failovers

        result = None
        parsed_review: tuple[str, str, str] | None = None
        current_request = request
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                meta = dict(request.metadata) if isinstance(request.metadata, dict) else {}
                meta["reviewer_attempt"] = attempt
                meta["reviewer_failover_from_resource_ids"] = sorted(failed_resource_ids)
                current_request = AIRoleRequest(
                    project_id=request.project_id,
                    task_run_id=request.task_run_id,
                    stage_run_id=request.stage_run_id,
                    role_run_id=request.role_run_id,
                    request_id=f"{review_id}:failover-{attempt - 1}",
                    role=request.role,
                    prompt=request.prompt,
                    working_directory=request.working_directory,
                    quality=request.quality,
                    independence=request.independence,
                    previous_resource_context=request.previous_resource_context,
                    excluded_resource_ids=tuple(sorted(failed_resource_ids)),
                    timeout_seconds=request.timeout_seconds,
                    metadata=meta,
                )
            if self.accounting is not None:
                self.accounting.start_interval(
                    "technical_review",
                    current_request.request_id,
                    project_id=request.project_id,
                    task_id=request.task_run_id,
                    role="reviewer",
                    request_id=current_request.request_id,
                    stage_run_id=request.stage_run_id,
                    role_run_id=request.role_run_id,
                    source_request_id=source_request_id or None,
                    attempt_id=source_request_id or None,
                )
            result = None
            try:
                result = self.port.execute(current_request) if self.port is not None else None
                if result is None:
                    raise RuntimeError("AIBroker reviewer port unavailable")
            except Exception as exc:
                if self.accounting is not None:
                    self.accounting.end_interval(
                        "technical_review",
                        current_request.request_id,
                        outcome="failed",
                        project_id=request.project_id,
                        task_id=request.task_run_id,
                        role="reviewer",
                        request_id=current_request.request_id,
                        stage_run_id=request.stage_run_id,
                        role_run_id=request.role_run_id,
                        source_request_id=source_request_id or None,
                        attempt_id=source_request_id or None,
                    )
                reconciled = None
                if self.port is not None:
                    try:
                        reconciled = self.port.status(current_request.request_id)
                    except Exception:
                        reconciled = None
                classification = None
                resource = None
                if isinstance(reconciled, dict) and str(reconciled.get("status") or "") == "failed":
                    from dev_orchestrator.core.ai_planner import _classify_reconciled_dispatch_failure
                    terminal_error = _nonblank(reconciled.get("execution_error")) or str(exc)
                    classification = _classify_reconciled_dispatch_failure(terminal_error)
                    resource_id = _nonblank(reconciled.get("resource_id"))
                    if resource_id is not None:
                        resource = ResourceContext(
                            resource_id,
                            _nonblank(reconciled.get("provider")),
                            _nonblank(reconciled.get("account")),
                            _nonblank(reconciled.get("model")),
                        )
                can_failover = (
                    classification in ROLE_RESOURCE_FAILURES
                    and resource is not None
                    and resource.resource_id is not None
                )
                if can_failover and attempt < max_attempts:
                    failed_resource_ids.add(resource.resource_id)
                    continue
                if self.failure_memory is not None:
                    self.failure_memory.record_matching_recurrences(
                        request.metadata.get("failure_environment", {}),
                        str(exc),
                        time.monotonic() - review_started,
                        project_id=request.project_id,
                        task_id=request.task_run_id,
                        role="reviewer",
                        request_id=current_request.request_id,
                        source_request_id=source_request_id or None,
                    )
                extra_terminal: dict[str, Any] = {}
                if failed_resource_ids:
                    extra_terminal["failover_from_resource_ids"] = sorted(failed_resource_ids)
                self._record_terminal(
                    review_id, request.project_id, source_request_id, "failed",
                    f"reviewer lifecycle error: {exc}",
                    **extra_terminal,
                )
                return

            if self.accounting is not None:
                resource = getattr(result, "resource_context", None) if result else None
                self.accounting.end_interval(
                    "technical_review",
                    current_request.request_id,
                    outcome="accepted" if result and result.status == "succeeded" else "failed",
                    project_id=request.project_id,
                    task_id=request.task_run_id,
                    role="reviewer",
                    request_id=current_request.request_id,
                    stage_run_id=request.stage_run_id,
                    role_run_id=request.role_run_id,
                    source_request_id=source_request_id or None,
                    attempt_id=source_request_id or None,
                    dispatch_id=result.dispatch_id if result else None,
                    decision_id=result.decision_id if result else None,
                    execution_id=result.execution_id if result else None,
                    session_id=result.session_id if result else None,
                    resource_id=resource.resource_id if resource else None,
                    provider=resource.provider if resource else None,
                    account=resource.account if resource else None,
                    model=resource.model if resource else None,
                )

            if result.status != "succeeded":
                classification = getattr(result, "failure_classification", None)
                resource = getattr(result, "resource_context", None)
                can_failover = (
                    result.status == "failed"
                    and classification in ROLE_RESOURCE_FAILURES
                    and resource is not None
                    and resource.resource_id is not None
                )
                if can_failover and attempt < max_attempts:
                    failed_resource_ids.add(resource.resource_id)
                    continue
                if self.failure_memory is not None:
                    self.failure_memory.record_matching_recurrences(
                        request.metadata.get("failure_environment", {}),
                        str(result.error or ""),
                        time.monotonic() - review_started,
                        project_id=request.project_id,
                        task_id=request.task_run_id,
                        role="reviewer",
                        request_id=current_request.request_id,
                        source_request_id=source_request_id or None,
                    )
                fail_reason = (
                    f"reviewer resource failover limit reached: {result.error}"
                    if can_failover
                    else (result.error or f"reviewer_{result.status}")
                )
                extra_failed: dict[str, Any] = {}
                if failed_resource_ids:
                    extra_failed["failover_from_resource_ids"] = sorted(failed_resource_ids)
                self._finish_result(review_id, result, "failed", fail_reason, **extra_failed)
                return

            raw_output = result.output if isinstance(result.output, str) else ""
            self._capture_reviewer_output(review_id, current_request, result)
            try:
                parsed_review = _parse_review_output(raw_output)
            except StructuredOutputError as protocol_exc:
                if protocol_exc.stage not in {"extract", "schema"}:
                    extra_semantic: dict[str, Any] = {}
                    if failed_resource_ids:
                        extra_semantic["failover_from_resource_ids"] = sorted(failed_resource_ids)
                    self._finish_result(review_id, result, "failed", str(protocol_exc), **extra_semantic)
                    return
                repair_request = AIRoleRequest(
                    project_id=current_request.project_id,
                    task_run_id=current_request.task_run_id,
                    stage_run_id=current_request.stage_run_id,
                    role_run_id=current_request.role_run_id + "-protocol-repair",
                    request_id=current_request.request_id + ":protocol-repair-1",
                    role=current_request.role,
                    prompt=self._technical_review_protocol_repair_prompt(raw_output, str(protocol_exc)),
                    working_directory=current_request.working_directory,
                    quality=current_request.quality,
                    independence=current_request.independence,
                    previous_resource_context=current_request.previous_resource_context,
                    excluded_resource_ids=current_request.excluded_resource_ids,
                    timeout_seconds=current_request.timeout_seconds,
                    metadata={
                        **dict(current_request.metadata),
                        "protocol_repair": True,
                        "protocol_repair_index": 1,
                        "protocol_failure_stage": protocol_exc.stage,
                    },
                )
                repair_result = self.port.execute(repair_request) if self.port is not None else None
                if repair_result is not None:
                    self._capture_reviewer_output(
                        review_id, repair_request, repair_result,
                        protocol_stage=protocol_exc.stage, repair_index=1,
                    )
                if repair_result is not None and repair_result.status == "succeeded":
                    try:
                        parsed_review = _parse_review_output(repair_result.output)
                        result = repair_result
                    except StructuredOutputError:
                        parsed_review = None
                if parsed_review is None:
                    resource = repair_result.resource_context if repair_result is not None else result.resource_context
                    if attempt < max_attempts and resource is not None and resource.resource_id is not None:
                        failed_resource_ids.add(resource.resource_id)
                        continue
                    extra_protocol: dict[str, Any] = {}
                    if failed_resource_ids:
                        extra_protocol["failover_from_resource_ids"] = sorted(failed_resource_ids)
                    self._finish_result(
                        review_id, repair_result or result, "failed",
                        "reviewer protocol repair failed or remained invalid",
                        **extra_protocol,
                    )
                    return
            break

        extra: dict[str, Any] = {}
        if failed_resource_ids:
            extra["failover_from_resource_ids"] = sorted(failed_resource_ids)
        if parsed_review is None:
            self._finish_result(review_id, result, "failed", "reviewer structured output unavailable", **extra)
            return
        decision, next_action, reason = parsed_review
        with self._lock:
            record = self._load_state()["reviews"].get(review_id, {})
        repo = read_repository_truth(request.working_directory)
        if (
            not repo.valid
            or repo.branch != record.get("branch")
            or repo.head != record.get("head")
            or repo.status_hash != record.get("review_status_hash")
        ):
            self._finish_result(review_id, result, "failed", "repository changed during review")
            return
        max_rounds = request.metadata.get("max_remediation_rounds", _DEFAULT_MAX_REMEDIATION_ROUNDS)
        if isinstance(max_rounds, bool) or not isinstance(max_rounds, int):
            max_rounds = _DEFAULT_MAX_REMEDIATION_ROUNDS
        decision, next_action, reason, remediation_round = self._apply_remediation_budget(
            review_id, request.project_id, request.task_run_id,
            decision, next_action, reason, max_rounds,
        )
        disposition = _DECISION_DISPOSITIONS[(decision, next_action)]
        extra["remediation_round"] = remediation_round
        extra["max_remediation_rounds"] = max_rounds
        try:
            self._write_decision(
                review_id=review_id,
                project_id=request.project_id,
                task_id=request.task_run_id,
                branch=str(record.get("branch") or ""),
                head=str(record.get("head") or ""),
                status_hash=str(record.get("review_status_hash") or ""),
                decision=decision,
                next_action=next_action,
                disposition=disposition,
                reason=reason,
            )
        except Exception as exc:
            self._finish_result(review_id, result, "failed", f"decision persistence failed: {exc}")
            return
        if self.accounting is not None and decision in {"next", "remediate", "stop"}:
            self.accounting.record_attempt_outcome(
                source_request_id,
                "accepted" if decision == "next" else "rejected",
                project_id=request.project_id,
                task_id=request.task_run_id,
                role="reviewer",
                request_id=review_id,
                source_request_id=source_request_id,
                dispatch_id=result.dispatch_id,
                decision_id=result.decision_id,
                execution_id=result.execution_id,
                session_id=result.session_id,
                metadata={"review_kind": "technical", "decision": decision, "reason": reason},
            )
        if self.accounting is not None and decision == "owner_gate":
            self.accounting.open_owner_gate(
                review_id,
                project_id=request.project_id,
                task_id=request.task_run_id,
                role="owner",
                request_id=review_id,
                source_request_id=source_request_id or None,
            )
        self._finish_result(review_id, result, "completed", reason, decision=decision, next_action=next_action, **extra)

    def _finish_result(self, review_id: str, result: Any, state_name: str, reason: str, **extra: Any) -> None:
        resource = result.resource_context
        resource_payload = None
        if resource is not None:
            resource_payload = {
                "resource_id": resource.resource_id, "provider": resource.provider,
                "account": resource.account, "model": resource.model,
            }
        with self._lock:
            state = self._load_state()
            record = state["reviews"].get(review_id)
            if not isinstance(record, dict):
                return
            task_id = str(record.get("task_id") or "")
            proj_id = str(record.get("project_id") or "")
            binding = record.get("conversation_binding")

        decision = extra.get("decision")
        if self.progress_channel is not None:
            if not binding:
                binding = self._project_bindings.get(proj_id)
            project_payload = {"project_id": proj_id}
            if binding:
                project_payload["conversation_binding"] = binding

            if state_name == "completed":
                if decision == "next":
                    self.progress_channel.emit(
                        project_payload, "REVIEW_ACCEPTED",
                        task_id=task_id, occurrence_key=review_id,
                        details={"decision": decision, "reason": reason},
                    )
                elif decision == "remediate":
                    self.progress_channel.emit(
                        project_payload, "REMEDIATE",
                        task_id=task_id, occurrence_key=review_id,
                        details={"decision": decision, "reason": reason},
                    )
                elif decision == "owner_gate":
                    self.progress_channel.emit(
                        project_payload, "OWNER_GATE",
                        task_id=task_id, occurrence_key=review_id,
                        details={"decision": decision, "reason": reason},
                    )
            elif state_name == "failed":
                self.progress_channel.emit(
                    project_payload, "REVIEW_FAILED",
                    task_id=task_id, occurrence_key=review_id,
                    details={"reason": reason},
                )
        try:
            from dev_orchestrator.core.execution_context import (
                record_role_completion,
                set_next_action,
                update_context,
            )
            record_role_completion(
                self.runtime_root,
                proj_id,
                "reviewer",
                {
                    "task_id": task_id,
                    "state": state_name,
                    "decision": decision,
                    "reason": reason,
                },
            )
            if state_name == "completed":
                if decision == "next":
                    set_next_action(self.runtime_root, proj_id, "none", disposition="terminal_success")
                elif decision == "remediate":
                    set_next_action(self.runtime_root, proj_id, "remediate", disposition="remediate")
                elif decision == "owner_gate":
                    update_context(self.runtime_root, proj_id, disposition="owner_gate", active_role=None)
            elif state_name == "failed":
                update_context(self.runtime_root, proj_id, disposition="terminal_failure", active_role=None)
        except Exception:
            pass

        with self._lock:
            state = self._load_state()
            record = state["reviews"].get(review_id)
            if not isinstance(record, dict):
                return
            record.update({
                "state": state_name, "reason": reason, "completed_at": utc_now_iso(),
                "dispatch_id": result.dispatch_id, "decision_id": result.decision_id,
                "execution_id": result.execution_id, "session_id": result.session_id,
                "resource_context": resource_payload, "usage_source": result.usage_source,
                **extra,
            })
            self._save_state(state)

    def _write_decision(
        self,
        *,
        review_id: str,
        project_id: str,
        task_id: str,
        branch: str,
        head: str,
        status_hash: str,
        decision: str,
        next_action: str,
        disposition: str,
        reason: str,
    ) -> None:
        with self._lock:
            raw = read_json(self.decisions_path, None)
            payload = raw if isinstance(raw, dict) else {}
            decisions = payload.get("decisions")
            if not isinstance(decisions, dict):
                decisions = {}
            quarantined = payload.get("quarantined_decisions")
            if not isinstance(quarantined, dict):
                quarantined = {}
            existing = decisions.get(review_id)
            record = {
                "project_id": project_id,
                "request_id": review_id,
                "disposition": disposition,
                "next_action": next_action,
                "decision": decision,
                "reason": reason,
                "review_status_hash": status_hash,
                "task_id": task_id,
                "stage_id": "review",
                "branch": branch,
                "head": head,
                "role": "reviewer",
                "event": "worker_done",
                "source": "aibroker",
                "consumed_at": utc_now_iso(),
            }
            if existing is not None:
                # A replay after a crash between decision persistence and the
                # reviewer-state write must converge, not conflict. Compare on
                # decision semantics only: `consumed_at` is regenerated on every
                # call, so including it would make every replay look like a
                # conflict and turn an already-durable decision into a spurious
                # failure. The original `consumed_at` is preserved so downstream
                # consumers keep a stable ordering key.
                if _decision_identity(existing) != _decision_identity(record):
                    raise RuntimeError("conflicting direct reviewer decision replay")
                record["consumed_at"] = existing.get("consumed_at") or record["consumed_at"]
                if existing == record:
                    return
            decisions[review_id] = record
            write_json(
                self.decisions_path,
                {
                    "version": _DECISION_VERSION,
                    "decisions": decisions,
                    "quarantined_decisions": quarantined,
                },
                indent=2,
            )
