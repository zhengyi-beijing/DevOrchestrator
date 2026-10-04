"""Provider-neutral ReviewerHarness protocol and default implementation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from dev_orchestrator.jobs.models import JobSpec
from dev_orchestrator.jobs.service import JobService
from dev_orchestrator.storage.json_store import read_json, utc_now_iso

from .models import (
    ReviewCoverage,
    ReviewFinding,
    ReviewManifest,
    ReviewRequest,
    ReviewResult,
    ReviewSession,
)
from .ocr_adapter import OpenCodeReviewAdapter
from .store import ReviewSessionStore


@runtime_checkable
class ReviewerHarness(Protocol):
    """Provider-neutral evidence-only review harness interface."""

    def submit(self, request: ReviewRequest) -> ReviewSession:
        """Submit a new review request; returns existing session on replay."""
        ...

    def status(self, session_id: str) -> ReviewSession:
        """Query current session progress and state."""
        ...

    def reconcile(self, session_id: str) -> ReviewSession:
        """Reconcile in-flight or interrupted session against durable evidence."""
        ...

    def result(self, session_id: str) -> ReviewResult:
        """Retrieve completed review findings, coverage and artifacts."""
        ...


class DefaultReviewerHarness:
    """Default harness coordinating OpenCodeReview preparation and P14 job execution."""

    def __init__(
        self,
        runtime_root: Path | str,
        *,
        job_service: Optional[JobService] = None,
        session_store: Optional[ReviewSessionStore] = None,
        ocr_adapter_factory: Optional[Callable[[Path], OpenCodeReviewAdapter]] = None,
        ocr_executable: Optional[str] = None,
        command_ref: str = "review-runner",
        execution_port: Any = None,
    ) -> None:
        self.runtime_root = Path(runtime_root).resolve()
        self.session_store = session_store or ReviewSessionStore(self.runtime_root)
        self.job_service = job_service or JobService(self.runtime_root)
        self.ocr_executable = ocr_executable
        self.command_ref = command_ref
        self._ocr_adapter_factory = ocr_adapter_factory
        self._execution_port = execution_port

    def submit(self, request: ReviewRequest) -> ReviewSession:
        """Prepare review manifest and submit as P14 durable job."""
        existing = self.session_store.get_session(request.request_id)
        if existing is not None:
            return existing

        # Resolve repo path from project configuration or metadata
        repo_path_str = request.metadata.get("repo_path")
        if not repo_path_str and self.job_service.config:
            proj_cfg = self.job_service.config.projects.get(request.project_id)
            if proj_cfg:
                repo_path_str = str(proj_cfg.repo_path)
        if not repo_path_str:
            repo_path_str = "."
        repo_path = Path(repo_path_str).resolve()

        # Step 1: OpenCodeReview preparation
        ocr_exec = request.metadata.get("ocr_executable") or self.ocr_executable
        if self._ocr_adapter_factory:
            adapter = self._ocr_adapter_factory(repo_path)
        else:
            adapter = OpenCodeReviewAdapter(repo_path, executable=ocr_exec)
        manifest = adapter.build_manifest(request)

        # Step 2: Content-addressed input payload
        input_payload = {
            "request": request.to_dict(),
            "manifest": manifest.to_dict(),
        }
        import hashlib
        payload_raw = json.dumps(input_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        input_digest = "sha256:" + hashlib.sha256(payload_raw).hexdigest()

        # Step 3: P14 Job submission
        command_ref = str(request.metadata.get("command_ref") or self.command_ref or "review-runner")
        idem_key = request.idempotency_key or request.request_id
        spec = JobSpec(
            project_id=request.project_id,
            command_ref=command_ref,
            idempotency_key=idem_key,
            kind="review",
            task_id=request.task_id,
            source_request_id=request.source_request_id,
            transport=request.transport,
            input_digest=input_digest,
            metadata=dict(request.metadata),
        )

        now = utc_now_iso()
        job_record = self.job_service.submit(spec, input_payload=input_payload)

        # Step 4: Initial session persistence
        session = ReviewSession(
            session_id=request.request_id,
            request=request,
            state="running" if job_record.state in ("queued", "running") else job_record.state,
            job_id=job_record.job_id,
            manifest=manifest,
            timestamps={
                "created_at": now,
                "started_at": now,
                "updated_at": now,
            },
        )
        self.session_store.save_session(session)

        # Step 5: Persist role harness session for timeout/cleanup tracking
        res_id = (
            request.metadata.get("resource_id")
            or (request.metadata.get("resource_context") or {}).get("resource_id")
        )
        child_pids = list(request.metadata.get("child_pids") or [])
        try:
            from dev_orchestrator.core.ai_planner import persist_harness_session
            persist_harness_session(
                self.runtime_root,
                session_id=session.session_id,
                role="reviewer",
                request_id=request.source_request_id or request.request_id,
                resource_id=res_id,
                started_at=now,
                child_pids=child_pids,
            )
        except Exception:
            pass

        return session

    def record_child_pid(self, session_id: str, pid: int, *, created: float) -> None:
        """Register a spawned child PID under the active harness session."""
        try:
            from dev_orchestrator.core.ai_planner import persist_harness_session
            persist_harness_session(
                self.runtime_root,
                session_id=session_id,
                role="reviewer",
                child_pids=[pid],
                child_create_times={str(pid): created},
            )
        except Exception:
            pass

    def status(self, session_id: str) -> ReviewSession:
        """Poll job status and update session state."""
        session = self.session_store.get_session(session_id)
        if session is None:
            raise ValueError(f"review session {session_id} not found")

        if session.state in ("completed", "failed", "cancelled") and session.result is not None:
            return session

        if not session.job_id:
            return session

        job_rec = self.job_service.status(session.job_id)
        if job_rec is None:
            return session

        heartbeat = job_rec.heartbeat or {}
        if (heartbeat.get("start_token") == job_rec.supervisor.get("start_token")
                and isinstance(heartbeat.get("start_token"), str) and heartbeat["start_token"]
                and heartbeat.get("pid") == job_rec.supervisor.get("pid")
                and isinstance(heartbeat.get("child_pid"), int)
                and isinstance(heartbeat.get("child_create_time"), (int, float))):
            self.record_child_pid(session_id, heartbeat["child_pid"], created=heartbeat["child_create_time"])

        try:
            artifact = self.job_service.get_artifact(session.job_id, "active-review-dispatch.json")
        except (ValueError, OSError):
            # The runner has not published a dispatch yet, or this is a legacy
            # job. Absence never supplies cleanup permission.
            artifact = None
        dispatch = artifact.get("content") if isinstance(artifact, dict) else None
        if (isinstance(dispatch, dict) and dispatch.get("session_id") == session_id
                and dispatch.get("role") == "reviewer" and isinstance(dispatch.get("request_id"), str)):
            request_id = dispatch["request_id"]
            registry = read_json(self.runtime_root / "harness-sessions.json", {})
            existing = (registry.get("sessions") or {}).get(request_id) or {}
            if not existing.get("resource_id"):
                if self._execution_port is None:
                    from dev_orchestrator.ai.runtime_config import load_aibroker_execution_port
                    self._execution_port = load_aibroker_execution_port(self.runtime_root)
                try:
                    fact = self._execution_port.status(request_id) if self._execution_port else None
                except (RuntimeError, ValueError, OSError) as exc:
                    session.failure_reason = f"reviewer resource identity not yet verifiable ({type(exc).__name__})"
                    fact = None
                if isinstance(fact, dict) and fact.get("request_id") == request_id and fact.get("resource_id"):
                    from dev_orchestrator.core.ai_planner import persist_harness_session
                    parent = (registry.get("sessions") or {}).get(session_id) or {}
                    persist_harness_session(self.runtime_root, request_id, role="reviewer",
                        request_id=request_id, resource_id=fact["resource_id"], parent_session_id=session_id,
                        started_at=session.timestamps.get("started_at"),
                        child_pids=parent.get("child_pids", []), child_create_times=parent.get("child_create_times", {}))

        if job_rec.state == "completed":
            # Load result and output artifacts
            try:
                res_art = self.job_service.get_artifact(session.job_id, "session.json")
                if res_art and isinstance(res_art.get("content"), dict):
                    saved_sess = ReviewSession.from_dict(res_art["content"])
                    if saved_sess.result:
                        session.result = saved_sess.result
                        session.state = "completed"
                        session.timestamps["completed_at"] = saved_sess.timestamps.get("completed_at") or utc_now_iso()
                        self.session_store.save_session(session)
                        return session
            except Exception:
                pass

            # Fallback: construct ReviewResult from findings and coverage artifacts
            try:
                f_art = self.job_service.get_artifact(session.job_id, "findings.json")
                c_art = self.job_service.get_artifact(session.job_id, "coverage.json")
                findings_raw = f_art.get("content", []) if isinstance(f_art, dict) else []
                cov_raw = c_art.get("content", {}) if isinstance(c_art, dict) else {}
                findings = [ReviewFinding.from_dict(f) for f in findings_raw if isinstance(f, dict)]
                coverage = ReviewCoverage.from_dict(cov_raw) if isinstance(cov_raw, dict) else ReviewCoverage()
                blocking = [f for f in findings if f.severity in session.request.blocking_severities]

                if coverage.completeness != "complete":
                    disp = "failed"
                    reason = f"Coverage {coverage.completeness}"
                elif blocking:
                    disp = "remediate"
                    reason = f"{len(blocking)} blocking findings"
                else:
                    disp = "next"
                    reason = "Clean review"

                session.result = ReviewResult(
                    session_id=session.session_id,
                    job_id=session.job_id,
                    disposition=disp,
                    completeness=coverage.completeness,
                    findings=findings,
                    coverage=coverage,
                    reason=reason,
                )
                session.state = "completed"
                session.timestamps["completed_at"] = utc_now_iso()
                self.session_store.save_session(session)
            except Exception as exc:
                session.state = "failed"
                session.failure_reason = f"artifact loading failed: {exc}"
                self.session_store.save_session(session)

        elif job_rec.state in ("failed", "cancelled"):
            session.state = job_rec.state
            session.failure_reason = job_rec.state_reason or job_rec.failure_kind
            self.session_store.save_session(session)

        elif job_rec.state == "unknown_recovery":
            session.state = "unknown_recovery"
            session.failure_reason = job_rec.state_reason or "transport/supervisor ambiguous interruption"
            self.session_store.save_session(session)

        return session

    def cleanup_timed_out(self, session_id: str, *, max_age_seconds: float) -> int:
        """Use only observed actual reviewer identities, never the prior Worker."""
        from dev_orchestrator.core.ai_planner import cleanup_stale_role_tool_children
        data = read_json(self.runtime_root / "harness-sessions.json", {})
        count = 0
        for row in (data.get("sessions") or {}).values():
            if row.get("parent_session_id") == session_id and row.get("request_id") and row.get("resource_id"):
                count += cleanup_stale_role_tool_children(self.runtime_root, role="reviewer", max_age_seconds=max_age_seconds,
                    request_id=row["request_id"], resource_id=row["resource_id"])
        return count

    def reconcile(self, session_id: str) -> ReviewSession:
        """Reconcile session and underlying P14 execution job."""
        session = self.session_store.get_session(session_id)
        if session is None:
            raise ValueError(f"review session {session_id} not found")

        if session.job_id:
            try:
                self.job_service.reconcile(session.job_id)
            except Exception:
                pass

        return self.status(session_id)

    def result(self, session_id: str) -> ReviewResult:
        """Get final ReviewResult; raises if review is not complete."""
        session = self.status(session_id)
        if session.result is not None:
            return session.result
        if session.state != "completed":
            raise ValueError(f"review session {session_id} is in state {session.state!r}, not completed")
        raise ValueError(f"review session {session_id} completed without ReviewResult")
