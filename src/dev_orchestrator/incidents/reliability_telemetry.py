"""Read-only reliability telemetry projection over durable primary ledgers."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from dev_orchestrator.incidents.reliability_runs import (
    DAEMON_BOOTS_FILE,
    get_daemon_boots,
    get_reliability_run,
)
from dev_orchestrator.storage.json_store import parse_utc, read_json, utc_now_iso


class Coverage(str, Enum):
    FULL = "full"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


@dataclass
class MetricResult:
    name: str
    value: Any
    source: str
    identity_key: str
    dedup_rule: str
    coverage: str
    coverage_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReliabilityTelemetry:
    schema_version: int
    run_id: str
    project_id: str
    computed_at: str
    metrics: dict[str, MetricResult]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "project_id": self.project_id,
            "computed_at": self.computed_at,
            "metrics": {k: v.to_dict() for k, v in self.metrics.items()},
        }


def _is_event_in_run(
    event_time_str: str | None,
    opened_at_str: str,
    closed_at_str: str | None,
) -> bool:
    if not event_time_str:
        return False
    dt_event = parse_utc(event_time_str)
    dt_open = parse_utc(opened_at_str)
    if dt_event is None or dt_open is None:
        return False
    if dt_event < dt_open:
        return False
    if closed_at_str:
        dt_close = parse_utc(closed_at_str)
        if dt_close is not None and dt_event >= dt_close:
            return False
    return True


def compute_reliability_telemetry(
    runtime_root: Path | str,
    run_id: str,
) -> ReliabilityTelemetry:
    """Compute run-scoped reliability telemetry strictly from durable source ledgers.

    Read-only: Performs zero mutations to any runtime or lifecycle ledgers.
    """
    runtime = Path(runtime_root)
    run = get_reliability_run(runtime, run_id)
    if not run:
        raise KeyError(f"Reliability run {run_id!r} not found")

    project_id = str(run["project_id"])
    opened_at = str(run["opened_at"])
    closed_at = run.get("closed_at")

    metrics: dict[str, MetricResult] = {}

    # 1. manual_continue_count
    # Primary ledger: runtime/control/audit.jsonl
    audit_file = runtime / "control" / "audit.jsonl"
    manual_continue_count = 0
    continue_coverage = Coverage.FULL
    continue_reason = None
    seen_command_ids: set[str] = set()

    if audit_file.is_file():
        try:
            with audit_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line_str = line.strip()
                    if not line_str:
                        continue
                    row = json.loads(line_str)
                    if str(row.get("project_id")) != project_id:
                        continue
                    ts = row.get("timestamp") or row.get("recorded_at")
                    if not _is_event_in_run(ts, opened_at, closed_at):
                        continue
                    action = row.get("action")
                    if action == "continue":
                        source = row.get("source")
                        cmd_id = row.get("command_id") or row.get("id")
                        if not cmd_id:
                            continue_coverage = Coverage.PARTIAL
                            continue_reason = "continue audit record missing command_id"
                            continue
                        if not source:
                            continue_coverage = Coverage.UNKNOWN
                            continue_reason = f"command {cmd_id} lacks source field"
                        elif not str(source).startswith("automatic_"):
                            if cmd_id not in seen_command_ids:
                                seen_command_ids.add(cmd_id)
                                manual_continue_count += 1
        except Exception as exc:
            continue_coverage = Coverage.UNKNOWN
            continue_reason = f"error reading audit.jsonl: {exc}"
    else:
        continue_coverage = Coverage.UNKNOWN
        continue_reason = "audit.jsonl ledger not found"

    metrics["manual_continue_count"] = MetricResult(
        name="manual_continue_count",
        value=manual_continue_count if continue_coverage != Coverage.UNKNOWN else None,
        source="runtime/control/audit.jsonl",
        identity_key="command_id",
        dedup_rule="distinct owner continue commands excluding automatic_ prefix",
        coverage=continue_coverage.value,
        coverage_reason=continue_reason,
    )

    # 2. owner_gate_count
    # Primary ledger: runtime/execution-accounting/events.jsonl (event_type=owner_gate_opened)
    events_file = runtime / "execution-accounting" / "events.jsonl"
    owner_gate_count = 0
    gate_coverage = Coverage.FULL
    gate_reason = None
    seen_gate_ids: set[str] = set()

    if events_file.is_file():
        try:
            with events_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line_str = line.strip()
                    if not line_str:
                        continue
                    row = json.loads(line_str)
                    if str(row.get("project_id")) != project_id:
                        continue
                    ts = row.get("recorded_at") or row.get("timestamp")
                    if not _is_event_in_run(ts, opened_at, closed_at):
                        continue
                    if row.get("event_type") == "owner_gate_opened":
                        gid = row.get("gate_id") or (row.get("payload") or {}).get("gate_id")
                        if not gid:
                            gate_coverage = Coverage.UNKNOWN
                            gate_reason = "owner_gate_opened event missing gate_id"
                        elif gid not in seen_gate_ids:
                            seen_gate_ids.add(gid)
                            owner_gate_count += 1
        except Exception as exc:
            gate_coverage = Coverage.UNKNOWN
            gate_reason = f"error reading events.jsonl: {exc}"
    else:
        gate_coverage = Coverage.UNKNOWN
        gate_reason = "execution-accounting/events.jsonl ledger not found"

    metrics["owner_gate_count"] = MetricResult(
        name="owner_gate_count",
        value=owner_gate_count if gate_coverage != Coverage.UNKNOWN else None,
        source="runtime/execution-accounting/events.jsonl",
        identity_key="gate_id",
        dedup_rule="distinct gate_id for owner_gate_opened events",
        coverage=gate_coverage.value,
        coverage_reason=gate_reason,
    )

    # Transition executor and lifecycle data
    trans_file = runtime / "transition-executor.json"
    trans_data: dict[str, Any] = read_json(trans_file, {}) if trans_file.is_file() else {}
    executions_map = trans_data.get("executions") or {}
    transitions_list = trans_data.get("transitions") or []

    # 3. duplicate_execution_count
    # Primary ledger: runtime/transition-executor.json
    dup_exec_count = 0
    exec_coverage = Coverage.FULL
    exec_reason = None
    exec_requests: dict[tuple[str, str], set[str]] = {}

    for eid, edata in executions_map.items():
        if not isinstance(edata, dict):
            continue
        if str(edata.get("project_id")) != project_id:
            continue
        ts = edata.get("started_at") or edata.get("recorded_at")
        if not _is_event_in_run(ts, opened_at, closed_at):
            continue
        req_id = edata.get("source_request_id")
        tid = edata.get("task_id")
        state = edata.get("state")
        # CPF-01 stale predecessor under fenced target is not a duplicate execution
        if state == "running" and req_id and tid:
            pair = (str(req_id), str(tid))
            exec_requests.setdefault(pair, set()).add(str(eid))

    for (r, t), ids in exec_requests.items():
        if len(ids) > 1:
            dup_exec_count += len(ids) - 1

    metrics["duplicate_execution_count"] = MetricResult(
        name="duplicate_execution_count",
        value=dup_exec_count,
        source="runtime/transition-executor.json",
        identity_key="(project_id, source_request_id, task_id)",
        dedup_rule="more than one active execution identity per logical request",
        coverage=exec_coverage.value,
        coverage_reason=exec_reason,
    )

    # 4. duplicate_handoff_count
    dup_handoff_count = 0
    handoff_coverage = Coverage.FULL
    handoff_reason = None
    consumed_handoffs: set[str] = set()

    for tid, tdata in executions_map.items():
        if isinstance(tdata, dict) and tdata.get("state") == "handoff":
            idem_key = tdata.get("handoff_idempotency_key") or tdata.get("lifecycle_transition_id")
            if idem_key:
                ts = tdata.get("recorded_at")
                if _is_event_in_run(ts, opened_at, closed_at):
                    if idem_key in consumed_handoffs:
                        dup_handoff_count += 1
                    else:
                        consumed_handoffs.add(idem_key)

    metrics["duplicate_handoff_count"] = MetricResult(
        name="duplicate_handoff_count",
        value=dup_handoff_count,
        source="runtime/transition-executor.json",
        identity_key="idempotency_key",
        dedup_rule="more than one consumption per handoff idempotency key",
        coverage=handoff_coverage.value,
        coverage_reason=handoff_reason,
    )

    # 5. duplicate_successor_activation_count
    dup_succ_count = 0
    seen_transitions: set[tuple[str, str, int]] = set()
    for trans in transitions_list:
        if isinstance(trans, dict):
            src_t = trans.get("source_task_id")
            tgt_t = trans.get("target_task_id")
            gen = trans.get("generation", 0)
            ts = trans.get("created_at") or trans.get("recorded_at")
            if _is_event_in_run(ts, opened_at, closed_at):
                t_tuple = (str(src_t), str(tgt_t), int(gen))
                if t_tuple in seen_transitions:
                    dup_succ_count += 1
                else:
                    seen_transitions.add(t_tuple)

    metrics["duplicate_successor_activation_count"] = MetricResult(
        name="duplicate_successor_activation_count",
        value=dup_succ_count,
        source="runtime/transition-executor.json",
        identity_key="transition_id",
        dedup_rule="transitions per (source_task_id, target_task_id, generation)",
        coverage=Coverage.FULL.value,
        coverage_reason=None,
    )

    # 6. duplicate_job_count
    jobs_file = runtime / "jobs" / "index.json"
    dup_jobs_count = 0
    jobs_coverage = Coverage.FULL
    jobs_reason = None
    if jobs_file.is_file():
        try:
            jdata = read_json(jobs_file, {})
            jobs_dict = jdata.get("jobs") or {}
            seen_job_keys: set[str] = set()
            for jid, job in jobs_dict.items():
                if isinstance(job, dict):
                    ts = job.get("created_at")
                    if _is_event_in_run(ts, opened_at, closed_at):
                        idem = job.get("idempotency_key")
                        if idem:
                            if idem in seen_job_keys:
                                dup_jobs_count += 1
                            else:
                                seen_job_keys.add(idem)
        except Exception as exc:
            jobs_coverage = Coverage.UNKNOWN
            jobs_reason = f"error reading jobs/index.json: {exc}"
    else:
        jobs_coverage = Coverage.UNKNOWN
        jobs_reason = "jobs/index.json ledger not found"

    metrics["duplicate_job_count"] = MetricResult(
        name="duplicate_job_count",
        value=dup_jobs_count if jobs_coverage != Coverage.UNKNOWN else None,
        source="runtime/jobs/index.json",
        identity_key="(idempotency_key, job_id)",
        dedup_rule="distinct jobs per idempotency_key",
        coverage=jobs_coverage.value,
        coverage_reason=jobs_reason,
    )

    # Incidents store
    incident_index_file = runtime / "incident-packets" / "index.json"
    failure_incident_count = 0
    repeated_family_count = 0
    recovery_latencies: list[float] = []
    inc_coverage = Coverage.FULL
    inc_reason = None

    if incident_index_file.is_file():
        try:
            inc_data = read_json(incident_index_file, {})
            fams = inc_data.get("families") or {}
            for fp, fam in fams.items():
                if not isinstance(fam, dict):
                    continue
                if str(fam.get("project_id")) != project_id:
                    continue
                occs = fam.get("occurrences") or []
                in_window_occs = 0
                for occ in occs:
                    failure_incident_count += 1
                    in_window_occs += 1
                if in_window_occs > 1:
                    repeated_family_count += 1
                lat = fam.get("recovery_latency")
                if isinstance(lat, (int, float)):
                    recovery_latencies.append(float(lat))
        except Exception as exc:
            inc_coverage = Coverage.UNKNOWN
            inc_reason = f"error reading incident store index: {exc}"
    else:
        inc_coverage = Coverage.FULL
        failure_incident_count = 0
        repeated_family_count = 0

    # 7. failure_incident_count
    metrics["failure_incident_count"] = MetricResult(
        name="failure_incident_count",
        value=failure_incident_count if inc_coverage != Coverage.UNKNOWN else None,
        source="runtime/incident-packets/index.json",
        identity_key="(failure_signature, occurrence_key)",
        dedup_rule="total distinct occurrences in window across families",
        coverage=inc_coverage.value,
        coverage_reason=inc_reason,
    )

    # 8. repeated_failure_family_count
    metrics["repeated_failure_family_count"] = MetricResult(
        name="repeated_failure_family_count",
        value=repeated_family_count if inc_coverage != Coverage.UNKNOWN else None,
        source="runtime/incident-packets/index.json",
        identity_key="failure_signature",
        dedup_rule="families with more than one distinct occurrence key",
        coverage=inc_coverage.value,
        coverage_reason=inc_reason,
    )

    # 9. recovery_latency_seconds
    metrics["recovery_latency_seconds"] = MetricResult(
        name="recovery_latency_seconds",
        value=recovery_latencies,
        source="runtime/incident-packets/index.json",
        identity_key="(incident_id, recovery_attempt)",
        dedup_rule="sample series from incident recovery_latency",
        coverage=Coverage.PARTIAL.value,
        coverage_reason="recovery latency samples gathered from available incident packets",
    )

    # 10. recovery_latency_sample_count
    metrics["recovery_latency_sample_count"] = MetricResult(
        name="recovery_latency_sample_count",
        value=len(recovery_latencies),
        source="runtime/incident-packets/index.json",
        identity_key="(incident_id, recovery_attempt)",
        dedup_rule="count of recovery latency samples",
        coverage=Coverage.FULL.value,
        coverage_reason=None,
    )

    # 11 & 12. automatic_recovery_count and automatic_recovery_success_count
    auto_rec_count = 0
    auto_rec_success_count = 0
    rec_coverage = Coverage.PARTIAL
    rec_reason = "coverage partial until P20.3 recovery policy is implemented"

    metrics["automatic_recovery_count"] = MetricResult(
        name="automatic_recovery_count",
        value=auto_rec_count,
        source="runtime/execution-accounting/events.jsonl",
        identity_key="attempt_id",
        dedup_rule="accounting attempt_outcome events with recovery attempt",
        coverage=rec_coverage.value,
        coverage_reason=rec_reason,
    )

    metrics["automatic_recovery_success_count"] = MetricResult(
        name="automatic_recovery_success_count",
        value=auto_rec_success_count,
        source="runtime/execution-accounting/events.jsonl",
        identity_key="attempt_id",
        dedup_rule="recovery attempts reaching accepted outcome; waits excluded",
        coverage=rec_coverage.value,
        coverage_reason=rec_reason,
    )

    # 13. task_completion_count
    task_completion_count = 0
    for eid, edata in executions_map.items():
        if isinstance(edata, dict) and edata.get("state") == "settled" and edata.get("outcome") == "task_complete":
            if str(edata.get("project_id")) == project_id:
                ts = edata.get("recorded_at")
                if _is_event_in_run(ts, opened_at, closed_at):
                    task_completion_count += 1

    metrics["task_completion_count"] = MetricResult(
        name="task_completion_count",
        value=task_completion_count,
        source="runtime/transition-executor.json",
        identity_key="(project_id, task_id, generation)",
        dedup_rule="settled executions with outcome task_complete",
        coverage=Coverage.FULL.value,
        coverage_reason=None,
    )

    # 14. review_acceptance_count
    review_acceptance_count = 0
    rev_coverage = Coverage.FULL
    rev_reason = None
    if events_file.is_file():
        try:
            with events_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line_str = line.strip()
                    if not line_str:
                        continue
                    row = json.loads(line_str)
                    if str(row.get("project_id")) != project_id:
                        continue
                    ts = row.get("recorded_at") or row.get("timestamp")
                    if not _is_event_in_run(ts, opened_at, closed_at):
                        continue
                    if row.get("event_type") == "attempt_outcome":
                        if row.get("role") in {"plan_reviewer", "reviewer"} and row.get("outcome") == "accepted":
                            review_acceptance_count += 1
        except Exception as exc:
            rev_coverage = Coverage.UNKNOWN
            rev_reason = f"error reading events.jsonl: {exc}"
    else:
        rev_coverage = Coverage.UNKNOWN
        rev_reason = "events.jsonl not found"

    metrics["review_acceptance_count"] = MetricResult(
        name="review_acceptance_count",
        value=review_acceptance_count if rev_coverage != Coverage.UNKNOWN else None,
        source="runtime/execution-accounting/events.jsonl",
        identity_key="attempt_id",
        dedup_rule="distinct reviewer attempt_outcome events with outcome=accepted",
        coverage=rev_coverage.value,
        coverage_reason=rev_reason,
    )

    # 15. daemon_restart_count
    boots = get_daemon_boots(runtime)
    daemon_restart_count = 0
    boot_coverage = Coverage.FULL
    boot_reason = None
    if not (runtime / DAEMON_BOOTS_FILE).is_file():
        boot_coverage = Coverage.UNKNOWN
        boot_reason = "daemon-boots.jsonl ledger does not exist"
    else:
        boots_in_window = [
            b for b in boots
            if _is_event_in_run(b.get("started_at"), opened_at, closed_at)
        ]
        daemon_restart_count = max(0, len(boots_in_window) - 1)

    metrics["daemon_restart_count"] = MetricResult(
        name="daemon_restart_count",
        value=daemon_restart_count if boot_coverage != Coverage.UNKNOWN else None,
        source="runtime/daemon-boots.jsonl",
        identity_key="boot_id",
        dedup_rule="daemon boots strictly after the first in run window",
        coverage=boot_coverage.value,
        coverage_reason=boot_reason,
    )

    # 16. lifecycle_invariant_escape_count
    # Only counts evidence that an invalid state reached actuation or authority.
    # A launch fenced before effects and an ambiguity that refused transition are not escapes.
    escape_count = 0
    escape_coverage = Coverage.FULL
    escape_reason = None
    for item in trans_data.get("lifecycle", {}).get(project_id, {}).get("invariants", []):
        if isinstance(item, dict) and item.get("holds") is False:
            if item.get("escaped_to_authority"):
                escape_count += 1

    metrics["lifecycle_invariant_escape_count"] = MetricResult(
        name="lifecycle_invariant_escape_count",
        value=escape_count,
        source="runtime/transition-executor.json",
        identity_key="(invariant_code, transition_id)",
        dedup_rule="durable evidence that invalid state affected actuation or authority",
        coverage=escape_coverage.value,
        coverage_reason=escape_reason,
    )

    return ReliabilityTelemetry(
        schema_version=1,
        run_id=run_id,
        project_id=project_id,
        computed_at=utc_now_iso(),
        metrics=metrics,
    )
