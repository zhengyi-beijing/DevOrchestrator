"""JobRecoveryCoordinator providing startup recovery, bounded per-tick sweep, and accounting."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.storage.json_store import utc_now_iso

from .config import JobsConfig
from .models import JobRecord
from .service import JobService


class JobRecoveryCoordinator:
    """Coordinates daemon-side job recovery, retry re-drive, and accounting emission."""

    def __init__(
        self,
        runtime_root: Path | str,
        service: Optional[JobService] = None,
        config: Optional[JobsConfig] = None,
        accounting: Any = None,
        per_tick_budget: int = 10,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.accounting = accounting
        self.service = service or JobService(self.runtime_root, config=config, accounting=accounting)
        self.per_tick_budget = max(1, int(per_tick_budget))

    def recover(self) -> dict[str, Any]:
        """Startup sweep: cleans corruption, re-drives stranded retry intent, reconciles active jobs, applies retention."""
        quarantined = self.service.store.repair_corruption()
        pruned = self.service.store.apply_retention(self.service.config.retention if self.service.config else None)
        reconciled: list[dict[str, Any]] = []
        redriven: list[str] = []

        all_jobs = self.service.store.list()
        for jdata in all_jobs:
            jid = jdata.get("job_id")
            if not jid:
                continue
            rec = self.service.store.get(jid)
            if rec is None:
                continue

            # Stranded retry intent: predecessor recorded successor_job_id but successor was never spawned
            succ_id = rec.retry.get("successor_job_id")
            succ_spawned = rec.retry.get("successor_spawned_at")
            if succ_id and not succ_spawned:
                succ_rec = self.service.store.get(succ_id)
                if succ_rec is None:
                    retry_req_id = rec.retry.get("retry_request_id")
                    if retry_req_id:
                        try:
                            self.service.retry(rec.job_id, retry_req_id)
                            redriven.append(succ_id)
                        except Exception:
                            pass

            # Reconcile non-terminal jobs
            if rec.state not in ("completed", "failed", "cancelled"):
                try:
                    updated = self.service.reconcile(rec.job_id)
                    reconciled.append({"job_id": rec.job_id, "state": updated.state})
                    self._record_accounting_if_terminal(updated)
                except Exception:
                    pass
            else:
                self._record_accounting_if_terminal(rec)

        return {
            "quarantined": [q.get("quarantined_path") for q in quarantined if isinstance(q, dict)],
            "pruned": pruned,
            "reconciled": reconciled,
            "redriven": redriven,
        }

    def advance(self) -> dict[str, Any]:
        """Bounded per-tick sweep over active jobs, stranded retries, and retention."""
        self.service.store.apply_retention(self.service.config.retention if self.service.config else None)
        all_jobs = self.service.store.list()
        reconciled: list[dict[str, Any]] = []
        redriven: list[str] = []
        count = 0

        for jdata in all_jobs:
            if count >= self.per_tick_budget:
                break
            jid = jdata.get("job_id")
            if not jid:
                continue
            state = jdata.get("state")
            succ_id = jdata.get("successor_job_id")

            if state not in ("completed", "failed", "cancelled") or succ_id:
                rec = self.service.store.get(jid)
                if rec is None:
                    continue

                if succ_id and not rec.retry.get("successor_spawned_at"):
                    if not self.service.store.get(succ_id):
                        retry_req_id = rec.retry.get("retry_request_id")
                        if retry_req_id:
                            try:
                                self.service.retry(rec.job_id, retry_req_id)
                                redriven.append(succ_id)
                                count += 1
                            except Exception:
                                pass

                if rec.state not in ("completed", "failed", "cancelled"):
                    try:
                        updated = self.service.reconcile(rec.job_id)
                        reconciled.append({"job_id": rec.job_id, "state": updated.state})
                        self._record_accounting_if_terminal(updated)
                        count += 1
                    except Exception:
                        pass
                else:
                    self._record_accounting_if_terminal(rec)

        return {"reconciled": reconciled, "redriven": redriven}

    def _record_accounting_if_terminal(self, record: JobRecord) -> None:
        """Record managed_validation accounting interval exactly once on terminal outcome."""
        if self.accounting is None:
            return
        if record.accounting_recorded_at is not None:
            return
        if record.state not in ("completed", "failed", "cancelled"):
            return

        started_at = record.timestamps.get("started_at") or record.timestamps.get("created_at") or utc_now_iso()
        finished_at = record.timestamps.get("finished_at") or utc_now_iso()
        outcome = "accepted" if record.state == "completed" else "failed"

        try:
            self.accounting.start_interval(
                phase="managed_validation",
                interval_id=record.job_id,
                occurred_at=started_at,
                project_id=record.project_id,
                task_id=record.task_id,
                stage_run_id=record.stage_run_id,
                role_run_id=record.role_run_id,
            )
            self.accounting.end_interval(
                phase="managed_validation",
                interval_id=record.job_id,
                occurred_at=finished_at,
                outcome=outcome,
                project_id=record.project_id,
                task_id=record.task_id,
                stage_run_id=record.stage_run_id,
                role_run_id=record.role_run_id,
            )
        except Exception:
            return

        def _mark(rec: JobRecord) -> None:
            rec.accounting_recorded_at = utc_now_iso()

        self.service.store.update(record.job_id, _mark)
