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

    # 2. owner_gate_count & shared events.jsonl pass
    # Primary ledger: runtime/execution-accounting/events.jsonl
    events_file = runtime / "execution-accounting" / "events.jsonl"
    owner_gate_count = 0
    gate_coverage = Coverage.FULL
    gate_reason = None
    seen_gate_ids: set[str] = set()

    review_acceptance_count = 0
    rev_coverage = Coverage.FULL
    rev_reason = None
    seen_review_attempt_ids: set[str] = set()

    auto_rec_count = 0
    auto_rec_success_count = 0
    seen_recovery_attempt_ids: set[str] = set()
    seen_recovery_success_ids: set[str] = set()
    rec_source = "runtime/incident-packets/index.json"
    ledger_latencies: list[float] = []

    ledger_attempts_read = False
    ledger_has_malformed = False
    ledger_error = None
    try:
        from dev_orchestrator.incidents.recovery_ledger import load_recovery_attempts
        ledger_attempts = load_recovery_attempts(runtime, project_id)
        ledger_attempts_read = True
        if ledger_attempts:
            for att in ledger_attempts:
                ts = att.get("reserved_at") or att.get("settled_at") or att.get("actuated_at")
                if _is_event_in_run(ts, opened_at, closed_at):
                    aid = str(att.get("attempt_id") or "")
                    if aid:
                        if aid not in seen_recovery_attempt_ids:
                            seen_recovery_attempt_ids.add(aid)
                            auto_rec_count += 1
                            if att.get("status") in {"succeeded", "reconciled"}:
                                seen_recovery_success_ids.add(aid)
                                auto_rec_success_count += 1
                    else:
                        ledger_has_malformed = True
                    lat = att.get("recovery_latency")
                    if isinstance(lat, (int, float)):
                        ledger_latencies.append(float(lat))
    except Exception as exc:
        ledger_error = str(exc)

    if ledger_attempts_read:
        if ledger_has_malformed:
            rec_coverage = Coverage.PARTIAL
            rec_reason = "recovery attempt record missing attempt_id"
        else:
            rec_coverage = Coverage.FULL
            rec_reason = None
        rec_source = "runtime/incident-packets/index.json"
    else:
        rec_coverage = Coverage.UNKNOWN
        rec_reason = f"recovery ledger unavailable: {ledger_error or 'not loaded'}"

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
                    ts = row.get("recorded_at") or row.get("timestamp") or row.get("occurred_at")
                    if not _is_event_in_run(ts, opened_at, closed_at):
                        continue

                    ev_type = row.get("event_type")
                    if ev_type == "owner_gate_opened":
                        gid = row.get("gate_id") or (row.get("payload") or {}).get("gate_id")
                        if not gid:
                            gate_coverage = Coverage.UNKNOWN
                            gate_reason = "owner_gate_opened event missing gate_id"
                        elif gid not in seen_gate_ids:
                            seen_gate_ids.add(gid)
                            owner_gate_count += 1
                    elif ev_type == "attempt_outcome":
                        att_id = row.get("attempt_id")
                        role = row.get("role")
                        outcome = str(row.get("outcome", "")).lower()

                        if role in {"plan_reviewer", "reviewer"} and outcome == "accepted":
                            if not att_id:
                                rev_coverage = Coverage.PARTIAL
                                rev_reason = "attempt_outcome event missing attempt_id"
                            elif att_id not in seen_review_attempt_ids:
                                seen_review_attempt_ids.add(att_id)
                                review_acceptance_count += 1

                        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
                        meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
                        is_recovery = (
                            row.get("recovery_attempt") is not None
                            or payload.get("recovery_attempt") is not None
                            or meta.get("recovery_attempt") is not None
                            or row.get("phase") in {"retry", "managed_validation"}
                            or bool(row.get("is_recovery") or payload.get("is_recovery") or meta.get("is_recovery"))
                            or bool(row.get("recovery_strategy") or payload.get("recovery_strategy") or meta.get("recovery_strategy"))
                        )
                        if is_recovery:
                            rec_id = att_id or (row.get("invocation_id") or row.get("event_id"))
                            if rec_id:
                                if rec_id not in seen_recovery_attempt_ids:
                                    seen_recovery_attempt_ids.add(rec_id)
                                    auto_rec_count += 1

                                is_wait_deferral = (
                                    outcome in {"wait", "deferral", "deferred", "owner_wait"}
                                    or row.get("phase") == "owner_wait"
                                    or bool(payload.get("deferred") or meta.get("deferred") or payload.get("wait") or meta.get("wait"))
                                )
                                if outcome == "accepted" and not is_wait_deferral:
                                    if rec_id not in seen_recovery_success_ids:
                                        seen_recovery_success_ids.add(rec_id)
                                        auto_rec_success_count += 1
                            else:
                                if not ledger_attempts_read:
                                    rec_coverage = Coverage.PARTIAL
                                    rec_reason = "attempt_outcome recovery row missing attempt_id"
            if not ledger_attempts_read and rec_coverage == Coverage.UNKNOWN:
                rec_coverage = Coverage.PARTIAL
                rec_reason = "recovery counts sourced from fallback events.jsonl; recovery ledger unpopulated or absent"
                rec_source = "runtime/execution-accounting/events.jsonl"
        except Exception as exc:
            gate_coverage = Coverage.UNKNOWN
            gate_reason = f"error reading events.jsonl: {exc}"
            rev_coverage = Coverage.UNKNOWN
            rev_reason = f"error reading events.jsonl: {exc}"
            if not ledger_attempts_read:
                rec_coverage = Coverage.UNKNOWN
                rec_reason = f"error reading events.jsonl: {exc}"
    else:
        gate_coverage = Coverage.UNKNOWN
        gate_reason = "execution-accounting/events.jsonl ledger not found"
        rev_coverage = Coverage.UNKNOWN
        rev_reason = "events.jsonl not found"
        if not ledger_attempts_read:
            rec_coverage = Coverage.UNKNOWN
            rec_reason = "execution-accounting/events.jsonl ledger not found and recovery ledger unavailable"

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
    inc_coverage = Coverage.FULL
    inc_reason = None

    if incident_index_file.is_file():
        try:
            inc_data = read_json(incident_index_file, {})
            fams = inc_data.get("families") or {}
            missing_timestamps = False
            for fp, fam in fams.items():
                if not isinstance(fam, dict):
                    continue
                if str(fam.get("project_id")) != project_id:
                    continue

                occs = fam.get("occurrences") or []
                ev_refs = fam.get("evidence_references") or []
                ref_timestamps: dict[str, str] = {}
                for ref in ev_refs:
                    if isinstance(ref, dict):
                        k = ref.get("occurrence_key")
                        ts = ref.get("recorded_at")
                        if k and ts:
                            ref_timestamps[k] = ts

                in_window_occ_keys: set[str] = set()
                for occ in occs:
                    if isinstance(occ, dict):
                        occ_k = occ.get("occurrence_key")
                        occ_ts = occ.get("recorded_at") or (ref_timestamps.get(occ_k) if occ_k else None)
                    elif isinstance(occ, str):
                        occ_k = occ
                        occ_ts = ref_timestamps.get(occ_k)
                    else:
                        continue

                    if not occ_k:
                        continue

                    if occ_ts:
                        if _is_event_in_run(occ_ts, opened_at, closed_at):
                            in_window_occ_keys.add(occ_k)
                    else:
                        missing_timestamps = True

                failure_incident_count += len(in_window_occ_keys)
                if len(in_window_occ_keys) > 1:
                    repeated_family_count += 1

            if missing_timestamps:
                inc_coverage = Coverage.PARTIAL
                inc_reason = "per-occurrence timestamps unavailable for some compacted occurrences"
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

    # Recovery latency samples
    recovery_latencies: list[float] = []
    seen_latency_keys: set[tuple[str, int]] = set()

    packets_dir = runtime / "incident-packets" / "packets"
    if packets_dir.is_dir():
        for pkt_file in packets_dir.glob("*.json"):
            pkt = read_json(pkt_file, None)
            if isinstance(pkt, dict) and str(pkt.get("project_id")) == project_id:
                pkt_ts = pkt.get("captured_at") or pkt.get("first_seen_at")
                if _is_event_in_run(pkt_ts, opened_at, closed_at):
                    lat = pkt.get("recovery_latency")
                    if isinstance(lat, (int, float)):
                        inc_id = str(pkt.get("packet_id") or pkt.get("incident_id") or pkt_file.stem)
                        rec_att = int(pkt.get("recovery_attempt") or 1)
                        lat_key = (inc_id, rec_att)
                        if lat_key not in seen_latency_keys:
                            seen_latency_keys.add(lat_key)
                            recovery_latencies.append(float(lat))

    if incident_index_file.is_file():
        try:
            inc_data = read_json(incident_index_file, {})
            fams = inc_data.get("families") or {}
            for fam in fams.values():
                if isinstance(fam, dict) and str(fam.get("project_id")) == project_id:
                    lat = fam.get("recovery_latency")
                    if isinstance(lat, (int, float)):
                        fam_ts = fam.get("last_seen_at") or fam.get("first_seen_at")
                        if _is_event_in_run(fam_ts, opened_at, closed_at):
                            fam_id = str(fam.get("packet_id") or fam.get("family_id") or "fam")
                            rec_att = int(fam.get("recovery_attempt") or 1)
                            lat_key = (fam_id, rec_att)
                            if lat_key not in seen_latency_keys:
                                seen_latency_keys.add(lat_key)
                                recovery_latencies.append(float(lat))
        except Exception:
            pass

    if ledger_latencies:
        recovery_latencies.extend(ledger_latencies)

    # 9. recovery_latency_seconds & 10. recovery_latency_sample_count
    if not recovery_latencies:
        sample_count_cov = Coverage.UNKNOWN
        sample_count_reason = "no incident in window carries measured recovery latency"
        sample_count_val = None
        lat_cov = Coverage.UNKNOWN
        lat_reason = "no incident in window carries measured recovery latency"
    else:
        sample_count_cov = Coverage.FULL
        sample_count_reason = None
        sample_count_val = len(recovery_latencies)
        lat_cov = Coverage.FULL if ledger_latencies else Coverage.PARTIAL
        lat_reason = None if ledger_latencies else "recovery latency samples gathered from available incident packets"

    metrics["recovery_latency_seconds"] = MetricResult(
        name="recovery_latency_seconds",
        value=recovery_latencies,
        source=rec_source if ledger_latencies else "runtime/incident-packets/index.json",
        identity_key="attempt_id" if ledger_latencies else "(incident_id, recovery_attempt)",
        dedup_rule="sample series from incident recovery_latency",
        coverage=lat_cov.value,
        coverage_reason=lat_reason,
    )

    metrics["recovery_latency_sample_count"] = MetricResult(
        name="recovery_latency_sample_count",
        value=sample_count_val,
        source=rec_source if ledger_latencies else "runtime/incident-packets/index.json",
        identity_key="attempt_id" if ledger_latencies else "(incident_id, recovery_attempt)",
        dedup_rule="count of recovery latency samples",
        coverage=sample_count_cov.value,
        coverage_reason=sample_count_reason,
    )

    # 11 & 12. automatic_recovery_count and automatic_recovery_success_count
    metrics["automatic_recovery_count"] = MetricResult(
        name="automatic_recovery_count",
        value=auto_rec_count if rec_coverage != Coverage.UNKNOWN else None,
        source=rec_source,
        identity_key="attempt_id",
        dedup_rule="recovery attempts recorded in recovery ledger or accounting events",
        coverage=rec_coverage.value,
        coverage_reason=rec_reason,
    )

    metrics["automatic_recovery_success_count"] = MetricResult(
        name="automatic_recovery_success_count",
        value=auto_rec_success_count if rec_coverage != Coverage.UNKNOWN else None,
        source=rec_source,
        identity_key="attempt_id",
        dedup_rule="recovery attempts reaching succeeded/reconciled outcome; waits excluded",
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
        boot_id_at_open = run.get("boot_id_at_open")
        if boot_id_at_open:
            daemon_restart_count = sum(1 for b in boots_in_window if b.get("boot_id") != boot_id_at_open)
        else:
            boots_prior = [
                b for b in boots
                if not _is_event_in_run(b.get("started_at"), opened_at, closed_at)
                and b.get("started_at") and b.get("started_at") < opened_at
            ]
            if not boots_prior and boots_in_window:
                daemon_restart_count = max(0, len(boots_in_window) - 1)
            else:
                boot_coverage = Coverage.UNKNOWN
                boot_reason = "boot_id_at_open is null and initial boot cannot be identified"

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
    seen_escape_keys: set[tuple[str, str]] = set()
    has_escape_evidence_field = False

    inv_items = trans_data.get("lifecycle", {}).get(project_id, {}).get("invariants", [])
    if isinstance(inv_items, list):
        for item in inv_items:
            if isinstance(item, dict):
                if "escaped_to_authority" in item:
                    has_escape_evidence_field = True
                    if item.get("holds") is False and item.get("escaped_to_authority"):
                        inv_code = str(item.get("invariant_code") or item.get("code") or item.get("invariant") or "unknown")
                        trans_id = str(item.get("transition_id") or item.get("execution_id") or "none")
                        key = (inv_code, trans_id)
                        if key not in seen_escape_keys:
                            seen_escape_keys.add(key)
                            escape_count += 1

    if not has_escape_evidence_field:
        escape_coverage = Coverage.UNKNOWN
        escape_reason = "no escape evidence field produced"

    metrics["lifecycle_invariant_escape_count"] = MetricResult(
        name="lifecycle_invariant_escape_count",
        value=escape_count if escape_coverage != Coverage.UNKNOWN else None,
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
