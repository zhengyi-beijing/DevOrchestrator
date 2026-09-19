"""Atomic, idempotent execution job store with retry-intent fencing and index rebuild."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

from dev_orchestrator.accounting.events import InterProcessFileLock
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json

from .models import (
    JOB_SCHEMA_VERSION,
    JobConflictError,
    JobCorruptionError,
    JobRecord,
    JobSpec,
    job_id_for,
    retry_successor_id,
    spec_hash,
)


def _safe_job_id(job_id: str) -> str:
    cleaned = job_id.strip()
    if not cleaned or len(cleaned) > 128:
        raise ValueError(f"invalid job_id: {job_id!r}")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    if not all(c in allowed for c in cleaned):
        raise ValueError(f"invalid characters in job_id: {job_id!r}")
    return cleaned


class ExecutionJobStore:
    """Serialize job submission, retry claims, and status updates with durable fsync."""

    def __init__(self, runtime_root: Path | str, *, read_only: bool = False) -> None:
        self.runtime_root = Path(runtime_root)
        self.jobs_root = self.runtime_root / "jobs"
        self.index_path = self.jobs_root / "index.json"
        self.health_path = self.jobs_root / "health.json"
        self.quarantine_dir = self.jobs_root / "quarantine"
        self.lock_path = self.jobs_root / "jobs.lock"
        self.read_only = read_only
        if not read_only:
            self.jobs_root.mkdir(parents=True, exist_ok=True)

    def _job_dir(self, job_id: str) -> Path:
        safe_id = _safe_job_id(job_id)
        return self.jobs_root / safe_id

    def _read_record_unlocked(self, job_id: str) -> JobRecord | None:
        jdir = self._job_dir(job_id)
        jfile = jdir / "job.json"
        if not jfile.is_file():
            return None
        data = read_json(jfile, None)
        if not isinstance(data, dict):
            raise JobCorruptionError(f"corrupt job record at {jfile}")
        try:
            return JobRecord.from_dict(data)
        except Exception as exc:
            raise JobCorruptionError(f"invalid job record schema at {jfile}: {exc}") from exc

    def _update_index_entry_unlocked(self, record: JobRecord) -> None:
        index_data = read_json(self.index_path, {"schema_version": JOB_SCHEMA_VERSION, "jobs": {}})
        if not isinstance(index_data, dict):
            index_data = {"schema_version": JOB_SCHEMA_VERSION, "jobs": {}}
        jobs_map = index_data.setdefault("jobs", {})
        jobs_map[record.job_id] = {
            "job_id": record.job_id,
            "project_id": record.project_id,
            "command_ref": record.command_ref,
            "state": record.state,
            "state_reason": record.state_reason,
            "failure_kind": record.failure_kind,
            "transport": record.transport,
            "host_identity": record.host_identity,
            "created_at": record.timestamps.get("created_at"),
            "started_at": record.timestamps.get("started_at"),
            "finished_at": record.timestamps.get("finished_at"),
            "updated_at": record.timestamps.get("updated_at"),
            "successor_job_id": record.retry.get("successor_job_id"),
            "retry_of": record.retry.get("retry_of"),
        }
        index_data["updated_at"] = utc_now_iso()
        write_json(self.index_path, index_data, indent=2)

    def claim_or_get(
        self,
        spec: JobSpec,
        record_factory: Callable[[str, str], JobRecord],
        target_job_id: Optional[str] = None,
    ) -> tuple[JobRecord, bool]:
        """Claim a new job or return existing record if already submitted.
        
        Raises JobConflictError if job_id exists with different spec_hash.
        Returns (record, is_new).
        """
        expected_spec_hash = spec_hash(spec)
        actual_job_id = target_job_id or job_id_for(spec)

        with InterProcessFileLock(self.lock_path):
            existing = self._read_record_unlocked(actual_job_id)
            if existing is not None:
                if existing.spec_hash != expected_spec_hash:
                    raise JobConflictError(
                        f"job_id {actual_job_id} already exists with conflicting spec_hash "
                        f"({existing.spec_hash} != {expected_spec_hash})"
                    )
                return existing, False

            # Create new job
            record = record_factory(actual_job_id, expected_spec_hash)
            jdir = self._job_dir(actual_job_id)
            jdir.mkdir(parents=True, exist_ok=True)
            write_json(jdir / "job.json", record.to_dict(), indent=2)
            self._update_index_entry_unlocked(record)
            return record, True

    def claim_retry(
        self,
        predecessor_job_id: str,
        retry_request_id: str,
    ) -> tuple[str, bool]:
        """Record write-once retry intent under predecessor lock.
        
        Returns (successor_job_id, is_new).
        Raises JobConflictError if different retry_request_id is supplied once successor exists.
        """
        safe_req_id = retry_request_id.strip()
        if not safe_req_id:
            raise ValueError("retry_request_id must be nonblank")

        req_hash = "sha256:" + hashlib.sha256(safe_req_id.encode("utf-8")).hexdigest()

        with InterProcessFileLock(self.lock_path):
            pred = self._read_record_unlocked(predecessor_job_id)
            if pred is None:
                raise ValueError(f"predecessor job {predecessor_job_id} does not exist")

            # Check existing retry intent
            existing_succ = pred.retry.get("successor_job_id")
            if existing_succ:
                existing_req = pred.retry.get("retry_request_id")
                if existing_req == safe_req_id:
                    # Idempotent replay of same retry_request_id
                    return existing_succ, False
                raise JobConflictError(
                    f"predecessor {predecessor_job_id} already has successor {existing_succ} "
                    f"for different retry_request_id ({existing_req!r} != {safe_req_id!r})"
                )

            # Refuse retry if predecessor is non-terminal and not recovery-safe, or in unknown_recovery
            is_terminal = pred.state in ("completed", "failed", "cancelled")
            is_recovery_safe = bool(pred.recovery.get("recovery_safe_retry", False))
            if pred.state == "unknown_recovery" or (not is_terminal and not is_recovery_safe):
                raise ValueError(
                    f"predecessor job {predecessor_job_id} is in state {pred.state!r} "
                    "which is non-terminal and not marked recovery_safe_retry"
                )

            # Derive deterministic successor id
            successor_id = retry_successor_id(predecessor_job_id, safe_req_id)

            pred.retry["retry_request_id"] = safe_req_id
            pred.retry["retry_request_hash"] = req_hash
            pred.retry["successor_job_id"] = successor_id
            pred.timestamps["updated_at"] = utc_now_iso()

            jdir = self._job_dir(predecessor_job_id)
            write_json(jdir / "job.json", pred.to_dict(), indent=2)
            self._update_index_entry_unlocked(pred)

            return successor_id, True

    def get(self, job_id: str) -> JobRecord | None:
        """Get job record; raises JobCorruptionError if unreadable."""
        if not self.jobs_root.is_dir():
            return None
        with InterProcessFileLock(self.lock_path):
            return self._read_record_unlocked(job_id)

    def save(self, record: JobRecord) -> None:
        """Atomically persist job record and update index."""
        with InterProcessFileLock(self.lock_path):
            jdir = self._job_dir(record.job_id)
            jdir.mkdir(parents=True, exist_ok=True)
            record.timestamps["updated_at"] = utc_now_iso()
            write_json(jdir / "job.json", record.to_dict(), indent=2)
            self._update_index_entry_unlocked(record)

    def update(self, job_id: str, updater: Callable[[JobRecord], None]) -> JobRecord:
        """Atomically mutate a job record under lock."""
        with InterProcessFileLock(self.lock_path):
            record = self._read_record_unlocked(job_id)
            if record is None:
                raise ValueError(f"job {job_id} does not exist")
            updater(record)
            record.timestamps["updated_at"] = utc_now_iso()
            jdir = self._job_dir(job_id)
            write_json(jdir / "job.json", record.to_dict(), indent=2)
            self._update_index_entry_unlocked(record)
            return record

    def save_heartbeat(self, job_id: str, heartbeat_data: dict[str, Any]) -> None:
        """Write heartbeat.json and update job.json heartbeat fields."""
        with InterProcessFileLock(self.lock_path):
            jdir = self._job_dir(job_id)
            jdir.mkdir(parents=True, exist_ok=True)
            write_json(jdir / "heartbeat.json", heartbeat_data, indent=2)
            record = self._read_record_unlocked(job_id)
            if record is not None:
                record.heartbeat.update(heartbeat_data)
                seq = heartbeat_data.get("heartbeat_sequence") or heartbeat_data.get("sequence")
                if seq is not None:
                    record.heartbeat["sequence"] = int(seq)
                    record.heartbeat["heartbeat_sequence"] = int(seq)
                record.timestamps["updated_at"] = utc_now_iso()
                write_json(jdir / "job.json", record.to_dict(), indent=2)
                self._update_index_entry_unlocked(record)

    def get_heartbeat(self, job_id: str) -> dict[str, Any] | None:
        """Read heartbeat.json if present."""
        jdir = self._job_dir(job_id)
        hb_file = jdir / "heartbeat.json"
        if not hb_file.is_file():
            return None
        return read_json(hb_file, None)

    def save_result(self, job_id: str, result_data: dict[str, Any]) -> None:
        """Write result.json write-once and transition job record to terminal."""
        with InterProcessFileLock(self.lock_path):
            jdir = self._job_dir(job_id)
            jdir.mkdir(parents=True, exist_ok=True)
            res_file = jdir / "result.json"
            if res_file.is_file():
                # Write-once: don't overwrite existing terminal result
                return
            write_json(res_file, result_data, indent=2)

    def get_result(self, job_id: str) -> dict[str, Any] | None:
        """Read result.json if present."""
        jdir = self._job_dir(job_id)
        res_file = jdir / "result.json"
        if not res_file.is_file():
            return None
        return read_json(res_file, None)

    def list(self, project_id: Optional[str] = None) -> list[dict[str, Any]]:
        """List jobs from index.json (rebuilding if index missing)."""
        if not self.jobs_root.is_dir():
            return []
        with InterProcessFileLock(self.lock_path):
            if not self.index_path.is_file():
                self._rebuild_index_unlocked()
            try:
                with open(self.index_path, "r", encoding="utf-8-sig") as handle:
                    data = json.load(handle)
            except Exception as exc:
                raise JobCorruptionError(f"corrupt jobs index at {self.index_path}: {exc}") from exc
            if not isinstance(data, dict):
                raise JobCorruptionError(f"invalid jobs index schema at {self.index_path}")
            jobs_map = data.get("jobs", {})
            results: list[dict[str, Any]] = []
            for jdata in jobs_map.values():
                if isinstance(jdata, dict):
                    if project_id is None or jdata.get("project_id") == project_id:
                        results.append(jdata)
            results.sort(key=lambda item: str(item.get("created_at") or ""))
            return results

    def quarantine(self, job_id: str, reason: str) -> dict[str, Any]:
        """Move a corrupt or invalid job directory to quarantine."""
        with InterProcessFileLock(self.lock_path):
            return self._quarantine_unlocked(job_id, reason)

    def _quarantine_unlocked(self, job_id: str, reason: str) -> dict[str, Any]:
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        jdir = self._job_dir(job_id)
        dest = self.quarantine_dir / f"{job_id}.corrupt-{uuid4().hex}"
        try:
            if jdir.exists():
                shutil.move(str(jdir), str(dest))
        except OSError as exc:
            reason = f"{reason}; quarantine move failed: {exc}"
            dest = jdir

        # Mark health degraded
        health_data = read_json(self.health_path, {"schema_version": JOB_SCHEMA_VERSION, "degraded": False, "events": []})
        if not isinstance(health_data, dict):
            health_data = {"schema_version": JOB_SCHEMA_VERSION, "degraded": False, "events": []}
        events = list(health_data.get("events") or [])[-49:]
        events.append({
            "occurred_at": utc_now_iso(),
            "job_id": job_id,
            "reason": reason,
            "quarantined_path": dest.name,
        })
        health_data["degraded"] = True
        health_data["events"] = events
        write_json(self.health_path, health_data, indent=2)

        self._rebuild_index_unlocked()
        return {
            "job_id": job_id,
            "quarantined": True,
            "reason": reason,
            "quarantined_path": dest.name,
        }

    def repair_corruption(self) -> list[dict[str, Any]]:
        """Quarantine unreadable job records and rebuild index."""
        with InterProcessFileLock(self.lock_path):
            quarantined: list[dict[str, Any]] = []
            if self.jobs_root.is_dir():
                for entry in sorted(self.jobs_root.iterdir()):
                    if entry.is_dir() and entry.name != "quarantine":
                        jfile = entry / "job.json"
                        if not jfile.is_file():
                            quarantined.append(self._quarantine_unlocked(entry.name, "missing job.json"))
                            continue
                        data = read_json(jfile, None)
                        if not isinstance(data, dict):
                            quarantined.append(self._quarantine_unlocked(entry.name, "unreadable job.json"))
                            continue
                        try:
                            JobRecord.from_dict(data)
                        except Exception as exc:
                            quarantined.append(self._quarantine_unlocked(entry.name, f"invalid job schema: {exc}"))
            self._rebuild_index_unlocked()
            return quarantined

    def _rebuild_index_unlocked(self) -> dict[str, Any]:
        index_data: dict[str, Any] = {
            "schema_version": JOB_SCHEMA_VERSION,
            "updated_at": utc_now_iso(),
            "jobs": {},
        }
        if self.jobs_root.is_dir():
            for entry in sorted(self.jobs_root.iterdir()):
                if entry.is_dir() and entry.name != "quarantine":
                    jfile = entry / "job.json"
                    if jfile.is_file():
                        data = read_json(jfile, None)
                        if isinstance(data, dict):
                            try:
                                rec = JobRecord.from_dict(data)
                                index_data["jobs"][rec.job_id] = {
                                    "job_id": rec.job_id,
                                    "project_id": rec.project_id,
                                    "command_ref": rec.command_ref,
                                    "state": rec.state,
                                    "state_reason": rec.state_reason,
                                    "failure_kind": rec.failure_kind,
                                    "transport": rec.transport,
                                    "host_identity": rec.host_identity,
                                    "created_at": rec.timestamps.get("created_at"),
                                    "started_at": rec.timestamps.get("started_at"),
                                    "finished_at": rec.timestamps.get("finished_at"),
                                    "updated_at": rec.timestamps.get("updated_at"),
                                    "successor_job_id": rec.retry.get("successor_job_id"),
                                    "retry_of": rec.retry.get("retry_of"),
                                }
                            except Exception:
                                pass
        write_json(self.index_path, index_data, indent=2)
        return index_data

    def rebuild_index(self) -> dict[str, Any]:
        with InterProcessFileLock(self.lock_path):
            return self._rebuild_index_unlocked()

    def health(self) -> dict[str, Any]:
        """Return store health status including degraded flag and detected corruptions."""
        with InterProcessFileLock(self.lock_path):
            data = read_json(self.health_path, {})
            result = dict(data) if isinstance(data, dict) else {}
            corrupt: list[str] = []
            if self.jobs_root.is_dir():
                for entry in self.jobs_root.iterdir():
                    if entry.is_dir() and entry.name != "quarantine":
                        jfile = entry / "job.json"
                        if not jfile.is_file() or not isinstance(read_json(jfile, None), dict):
                            corrupt.append(entry.name)
            result.setdefault("schema_version", JOB_SCHEMA_VERSION)
            result["degraded"] = bool(result.get("degraded") or corrupt)
            result["detected_corruption"] = corrupt
            quarantined = []
            if self.quarantine_dir.is_dir():
                quarantined = sorted(p.name for p in self.quarantine_dir.iterdir() if p.is_dir() or p.is_file())
            result["quarantined"] = quarantined
            return result

    def apply_retention(self, retention: dict[str, Any] | None = None) -> list[str]:
        """Prune terminal jobs exceeding max_jobs or max_age_days.

        Active and unknown_recovery jobs are never pruned.
        Predecessors with unspawned retry intent are preserved.
        Returns list of pruned job_ids.
        """
        from datetime import datetime, timezone
        from .config import DEFAULT_MAX_AGE_DAYS, DEFAULT_MAX_JOBS_RETENTION

        ret_dict = retention or {}
        max_jobs = int(ret_dict.get("max_jobs", DEFAULT_MAX_JOBS_RETENTION))
        max_age_days = int(ret_dict.get("max_age_days", DEFAULT_MAX_AGE_DAYS))

        if not self.jobs_root.is_dir():
            return []

        with InterProcessFileLock(self.lock_path):
            if not self.index_path.is_file():
                self._rebuild_index_unlocked()
            data = read_json(self.index_path, {})
            jobs_map = data.get("jobs", {}) if isinstance(data, dict) else {}
            if not isinstance(jobs_map, dict):
                return []

            # Identify candidates: only terminal jobs (completed, failed, cancelled)
            terminal_jobs: list[dict[str, Any]] = []
            for jid, jinfo in jobs_map.items():
                if isinstance(jinfo, dict):
                    state = jinfo.get("state")
                    if state in ("completed", "failed", "cancelled"):
                        # If it has an unspawned successor, preserve it
                        succ_id = jinfo.get("successor_job_id")
                        if succ_id and succ_id not in jobs_map:
                            continue
                        terminal_jobs.append(dict(jinfo))

            now = datetime.now(timezone.utc)
            to_prune: set[str] = set()

            # 1. Prune jobs older than max_age_days
            for tj in terminal_jobs:
                fin = tj.get("finished_at") or tj.get("created_at")
                if fin:
                    try:
                        fin_dt = datetime.fromisoformat(str(fin).replace("Z", "+00:00"))
                        age_days = (now - fin_dt).total_seconds() / 86400.0
                        if age_days > max_age_days:
                            to_prune.add(tj["job_id"])
                    except Exception:
                        pass

            # 2. Prune excess jobs over max_jobs (oldest first)
            remaining_terminal = [tj for tj in terminal_jobs if tj["job_id"] not in to_prune]
            if len(remaining_terminal) > max_jobs:
                remaining_terminal.sort(key=lambda x: str(x.get("finished_at") or x.get("created_at") or ""))
                excess_count = len(remaining_terminal) - max_jobs
                for tj in remaining_terminal[:excess_count]:
                    to_prune.add(tj["job_id"])

            pruned_ids: list[str] = []
            for pid in sorted(to_prune):
                pdir = self._job_dir(pid)
                if pdir.is_dir():
                    try:
                        shutil.rmtree(pdir, ignore_errors=True)
                    except OSError:
                        pass
                jobs_map.pop(pid, None)
                pruned_ids.append(pid)

            if pruned_ids:
                data["updated_at"] = utc_now_iso()
                data["jobs"] = jobs_map
                write_json(self.index_path, data, indent=2)

            return pruned_ids
