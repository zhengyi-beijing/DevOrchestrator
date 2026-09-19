"""Provider-neutral ReviewerHarness protocol and default implementation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from dev_orchestrator.jobs.models import JobSpec
from dev_orchestrator.jobs.service import JobService
from dev_orchestrator.storage.json_store import utc_now_iso

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
    ) -> None:
        self.runtime_root = Path(runtime_root).resolve()
        self.session_store = session_store or ReviewSessionStore(self.runtime_root)
        self.job_service = job_service or JobService(self.runtime_root)
        self.ocr_executable = ocr_executable
        self._ocr_factory = ocr_adapter_factory or (
            lambda repo_p: OpenCodeReviewAdapter(repo_p, executable=self.ocr_executable)
        )

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
        adapter = self._ocr_factory(repo_path)
        manifest = adapter.build_manifest(request)

        # Step 2: Content-addressed input payload
        input_payload = {
            "request": request.to_dict(),
            "manifest": manifest.to_dict(),
        }

        # Step 3: P14 Job submission
        command_ref = str(request.metadata.get("command_ref") or "review-runner")
        idem_key = request.idempotency_key or request.request_id
        spec = JobSpec(
            project_id=request.project_id,
            command_ref=command_ref,
            idempotency_key=idem_key,
            kind="review",
            task_id=request.task_id,
            source_request_id=request.source_request_id,
            transport=request.transport,
            input_digest=manifest.input_digest,
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
        return session

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
