"""Durable store for review sessions and index."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

from dev_orchestrator.accounting.events import InterProcessFileLock
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json
from .models import ReviewSession


def _safe_session_id(session_id: str) -> str:
    cleaned = str(session_id).strip()
    if not cleaned or len(cleaned) > 128:
        raise ValueError(f"invalid session_id: {session_id!r}")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_:")
    if not all(c in allowed for c in cleaned):
        raise ValueError(f"invalid characters in session_id: {session_id!r}")
    return cleaned.replace(":", "-")


class ReviewSessionStore:
    """Thread- and process-safe storage for ReviewSession records."""

    def __init__(self, runtime_root: Path | str, *, read_only: bool = False) -> None:
        self.runtime_root = Path(runtime_root).resolve()
        self.review_root = self.runtime_root / "review"
        self.sessions_dir = self.review_root / "sessions"
        self.index_path = self.review_root / "index.json"
        self.lock_path = self.review_root / "review.lock"
        self.read_only = read_only
        if not read_only:
            self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def _session_file(self, session_id: str) -> Path:
        safe_id = _safe_session_id(session_id)
        return self.sessions_dir / f"{safe_id}.json"

    def save_session(self, session: ReviewSession) -> None:
        """Persist review session record and update index under lock."""
        if self.read_only:
            raise RuntimeError("cannot write to read-only ReviewSessionStore")
        file_p = self._session_file(session.session_id)
        with InterProcessFileLock(self.lock_path):
            write_json(file_p, session.to_dict(), indent=2)
            self._update_index_unlocked(session)

    def get_session(self, session_id: str) -> Optional[ReviewSession]:
        """Load review session by identifier."""
        file_p = self._session_file(session_id)
        if not file_p.is_file():
            return None
        data = read_json(file_p, None)
        if not isinstance(data, dict):
            return None
        try:
            return ReviewSession.from_dict(data)
        except Exception:
            return None

    def update_session(
        self,
        session_id: str,
        updater: Callable[[ReviewSession], None],
    ) -> ReviewSession:
        """Apply mutation under store lock and persist."""
        if self.read_only:
            raise RuntimeError("cannot write to read-only ReviewSessionStore")
        file_p = self._session_file(session_id)
        with InterProcessFileLock(self.lock_path):
            sess = self.get_session(session_id)
            if sess is None:
                raise ValueError(f"review session {session_id} not found")
            updater(sess)
            sess.timestamps["updated_at"] = utc_now_iso()
            write_json(file_p, sess.to_dict(), indent=2)
            self._update_index_unlocked(sess)
            return sess

    def list_sessions(self, project_id: Optional[str] = None) -> list[dict[str, Any]]:
        """List summary info for all review sessions."""
        if not self.index_path.is_file():
            return []
        data = read_json(self.index_path, {})
        sessions_map = data.get("sessions", {}) if isinstance(data, dict) else {}
        results: list[dict[str, Any]] = []
        for s in sessions_map.values():
            if isinstance(s, dict):
                if project_id is None or s.get("project_id") == project_id:
                    results.append(dict(s))
        results.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
        return results

    def _update_index_unlocked(self, session: ReviewSession) -> None:
        data = read_json(self.index_path, {"sessions": {}})
        if not isinstance(data, dict):
            data = {"sessions": {}}
        s_map = data.setdefault("sessions", {})
        s_map[session.session_id] = {
            "session_id": session.session_id,
            "project_id": session.request.project_id,
            "task_id": session.request.task_id,
            "source_request_id": session.request.source_request_id,
            "state": session.state,
            "job_id": session.job_id,
            "mode": session.request.mode,
            "created_at": session.timestamps.get("created_at"),
            "updated_at": session.timestamps.get("updated_at"),
            "completed_at": session.timestamps.get("completed_at"),
            "disposition": session.result.disposition if session.result else None,
            "findings_count": len(session.result.findings) if session.result else 0,
            "completeness": session.result.completeness if session.result else None,
        }
        data["updated_at"] = utc_now_iso()
        write_json(self.index_path, data, indent=2)
