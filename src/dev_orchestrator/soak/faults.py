"""P20.4 Fault Registry, Injection Protocol and Sanitization."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from dev_orchestrator.incidents.sanitize import sanitize_incident_payload
from dev_orchestrator.soak.permit import SoakPermit
from dev_orchestrator.storage.json_store import parse_utc, read_json, utc_now_iso, write_json


class TargetEscapesDisposableBoundaryError(ValueError):
    """Raised when an injection target escapes the allowed disposable paths."""


@dataclass(frozen=True)
class SoakFault:
    fault_id: str
    owner_index: int
    kind: str
    target_kind: str
    expected_disposition: str
    unsafe: bool
    allowed_modes: tuple[str, ...]
    cpf_scenario_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


SOAK_FAULTS: tuple[SoakFault, ...] = (
    SoakFault(
        fault_id="F01_MALFORMED_PLANNER",
        owner_index=1,
        kind="malformed_planner_output",
        target_kind="planner",
        expected_disposition="automatic_recovery",
        unsafe=False,
        allowed_modes=("deterministic_test", "live_injection"),
        cpf_scenario_ids=("CPF-08",),
    ),
    SoakFault(
        fault_id="F02_PLANNER_TIMEOUT",
        owner_index=2,
        kind="planner_timeout",
        target_kind="planner",
        expected_disposition="automatic_recovery",
        unsafe=False,
        allowed_modes=("deterministic_test", "live_injection"),
        cpf_scenario_ids=("CPF-08",),
    ),
    SoakFault(
        fault_id="F03_WORKER_KILLED",
        owner_index=3,
        kind="worker_killed_once",
        target_kind="worker",
        expected_disposition="automatic_recovery",
        unsafe=False,
        allowed_modes=("deterministic_test", "live_injection"),
        cpf_scenario_ids=("CPF-01",),
    ),
    SoakFault(
        fault_id="F04_REVIEWER_REJECT",
        owner_index=4,
        kind="reviewer_reject",
        target_kind="reviewer",
        expected_disposition="automatic_recovery",
        unsafe=False,
        allowed_modes=("deterministic_test", "live_injection"),
        cpf_scenario_ids=(),
    ),
    SoakFault(
        fault_id="F05_QUOTA_EXHAUSTED",
        owner_index=5,
        kind="quota_exhausted",
        target_kind="provider",
        expected_disposition="owner_gate",
        unsafe=True,
        allowed_modes=("deterministic_test", "live_injection"),
        cpf_scenario_ids=(),
    ),
    SoakFault(
        fault_id="F06_PROVIDER_UNAVAILABLE",
        owner_index=6,
        kind="provider_unavailable",
        target_kind="provider",
        expected_disposition="automatic_recovery",
        unsafe=False,
        allowed_modes=("deterministic_test", "live_injection"),
        cpf_scenario_ids=(),
    ),
    SoakFault(
        fault_id="F07_BROKER_UNAVAILABLE",
        owner_index=7,
        kind="broker_unavailable",
        target_kind="broker",
        expected_disposition="automatic_recovery",
        unsafe=False,
        allowed_modes=("deterministic_test", "live_injection"),
        cpf_scenario_ids=(),
    ),
    SoakFault(
        fault_id="F08_DAEMON_RESTART_ACTIVE",
        owner_index=8,
        kind="daemon_restart_active",
        target_kind="daemon",
        expected_disposition="automatic_recovery",
        unsafe=False,
        allowed_modes=("deterministic_test", "live_injection"),
        cpf_scenario_ids=("CPF-01",),
    ),
    SoakFault(
        fault_id="F09_DAEMON_RESTART_HANDOFF",
        owner_index=9,
        kind="daemon_restart_handoff",
        target_kind="daemon",
        expected_disposition="automatic_recovery",
        unsafe=False,
        allowed_modes=("deterministic_test", "live_injection"),
        cpf_scenario_ids=("CPF-03",),
    ),
    SoakFault(
        fault_id="F10_STALE_HEAD",
        owner_index=10,
        kind="stale_head",
        target_kind="git",
        expected_disposition="owner_gate",
        unsafe=True,
        allowed_modes=("deterministic_test", "live_injection"),
        cpf_scenario_ids=("CPF-09",),
    ),
    SoakFault(
        fault_id="F11_UNEXPECTED_DIRT",
        owner_index=11,
        kind="unexpected_dirt",
        target_kind="git",
        expected_disposition="fail_closed",
        unsafe=True,
        allowed_modes=("deterministic_test", "live_injection"),
        cpf_scenario_ids=("CPF-05",),
    ),
    SoakFault(
        fault_id="F12_MISSING_PROJECTION",
        owner_index=12,
        kind="missing_execution_projection",
        target_kind="projection",
        expected_disposition="owner_gate",
        unsafe=True,
        allowed_modes=("deterministic_test", "live_injection"),
        cpf_scenario_ids=(),
    ),
    SoakFault(
        fault_id="F13_LOST_HANDOFF",
        owner_index=13,
        kind="lost_handoff",
        target_kind="handoff",
        expected_disposition="automatic_recovery",
        unsafe=False,
        allowed_modes=("deterministic_test", "live_injection"),
        cpf_scenario_ids=("CPF-02",),
    ),
    SoakFault(
        fault_id="F14_DUPLICATE_RETRY",
        owner_index=14,
        kind="duplicate_retry",
        target_kind="retry",
        expected_disposition="automatic_recovery",
        unsafe=False,
        allowed_modes=("deterministic_test", "live_injection"),
        cpf_scenario_ids=("CPF-10",),
    ),
    SoakFault(
        fault_id="F15_REPEATED_SIGNATURE",
        owner_index=15,
        kind="repeated_same_signature",
        target_kind="signature",
        expected_disposition="owner_gate",
        unsafe=True,
        allowed_modes=("deterministic_test", "live_injection"),
        cpf_scenario_ids=(),
    ),
)


CPF_COVERAGE_MAP: dict[str, dict[str, str]] = {
    "CPF-01": {"fault_id": "F08_DAEMON_RESTART_ACTIVE", "boundary": "authority_reconciliation", "expectation": "fail_closed_gate"},
    "CPF-02": {"fault_id": "F13_LOST_HANDOFF", "boundary": "watchdog_recovery", "expectation": "converge_single_owner"},
    "CPF-03": {"fault_id": "F09_DAEMON_RESTART_HANDOFF", "boundary": "successor_handoff", "expectation": "replay_journal"},
    "CPF-04": {"fault_id": "F13_LOST_HANDOFF", "boundary": "successor_handoff", "expectation": "fail_closed_gate"},
    "CPF-05": {"fault_id": "F11_UNEXPECTED_DIRT", "boundary": "successor_handoff", "expectation": "wait_without_mutation"},
    "CPF-06": {"fault_id": "F14_DUPLICATE_RETRY", "boundary": "authority_reconciliation", "expectation": "byte_stable_idempotent"},
    "CPF-07": {"fault_id": "F08_DAEMON_RESTART_ACTIVE", "boundary": "handoff_publication", "expectation": "converge_single_owner"},
    "CPF-08": {"fault_id": "F01_MALFORMED_PLANNER", "boundary": "worker_launch", "expectation": "fail_closed_gate"},
    "CPF-09": {"fault_id": "F10_STALE_HEAD", "boundary": "special_gate_reconciliation", "expectation": "converge_single_owner"},
    "CPF-10": {"fault_id": "F14_DUPLICATE_RETRY", "boundary": "worker_launch", "expectation": "byte_stable_idempotent"},
}


def get_soak_fault(fault_id: str) -> SoakFault | None:
    for fault in SOAK_FAULTS:
        if fault.fault_id == fault_id:
            return fault
    return None


def disposition_allowed(fault_id: str, disposition: str) -> bool:
    """Return whether a disposition preserves the registered fault contract.

    Recovery-capable cases may safely converge either automatically or at an
    owner gate. A fail-closed case is stricter because substituting a gate could
    hide mutation of the condition that must remain untouched.
    """
    fault = get_soak_fault(fault_id)
    if fault is None or disposition not in {'automatic_recovery', 'owner_gate', 'fail_closed'}:
        return False
    if disposition == 'fail_closed' or fault.expected_disposition == 'fail_closed':
        return disposition == fault.expected_disposition
    return disposition in {'automatic_recovery', 'owner_gate'}


def assert_target_disposable(permit: SoakPermit, target: Any) -> None:
    """Verify that target stays strictly within the disposable boundaries."""
    if permit is None:
        raise TargetEscapesDisposableBoundaryError("permit is absent")

    disp_repo = Path(permit.disposable_repo_path).resolve()
    disp_rt = Path(permit.disposable_runtime_root).resolve()
    can_repo = Path(permit.canonical_repo_path).resolve()
    can_rt = Path(permit.canonical_runtime_root).resolve()

    def _check_path(p: Path) -> None:
        resolved = p.resolve()
        # Fail if contained within canonical repo or runtime
        try:
            if resolved.is_relative_to(can_repo) or resolved.is_relative_to(can_rt):
                raise TargetEscapesDisposableBoundaryError(f"Target {resolved} escapes into canonical path")
        except AttributeError:
            if str(resolved).startswith(str(can_repo)) or str(resolved).startswith(str(can_rt)):
                raise TargetEscapesDisposableBoundaryError(f"Target {resolved} escapes into canonical path")

        # Must be relative to disposable repo or disposable runtime
        try:
            if not (resolved.is_relative_to(disp_repo) or resolved.is_relative_to(disp_rt)):
                raise TargetEscapesDisposableBoundaryError(f"Target {resolved} is outside disposable tree")
        except AttributeError:
            if not (str(resolved).startswith(str(disp_repo)) or str(resolved).startswith(str(disp_rt))):
                raise TargetEscapesDisposableBoundaryError(f"Target {resolved} is outside disposable tree")

    if isinstance(target, (str, Path)):
        _check_path(Path(target))
    elif isinstance(target, dict):
        if "path" in target:
            _check_path(Path(target["path"]))


def plan_fault_schedule(run_state: dict[str, Any]) -> list[dict[str, Any]]:
    """Generate ordered schedule of fault injections for the soak run.

    Safe faults run during tasks; unsafe faults run in child probes or at terminal phase.
    """
    schedule: list[dict[str, Any]] = []
    # Interleave safe faults first
    safe_faults = [f for f in SOAK_FAULTS if not f.unsafe]
    unsafe_faults = [f for f in SOAK_FAULTS if f.unsafe]

    order_idx = 1
    for f in safe_faults:
        schedule.append({
            "order": order_idx,
            "fault_id": f.fault_id,
            "mode": "deterministic_test",
            "unsafe": False,
            "target_kind": f.target_kind,
            "expected_disposition": f.expected_disposition,
        })
        order_idx += 1

    for f in unsafe_faults:
        schedule.append({
            "order": order_idx,
            "fault_id": f.fault_id,
            "mode": "deterministic_test",
            "unsafe": True,
            "target_kind": f.target_kind,
            "expected_disposition": f.expected_disposition,
        })
        order_idx += 1

    return schedule


def reserve_fault_injection(
    runtime_root: Path | str,
    run_id: str,
    fault_id: str,
    mode: str,
    target: Any,
    pre_state: dict[str, Any],
) -> dict[str, Any]:
    """Reserve and durably flush an intent record before any injection effect."""
    fault = get_soak_fault(fault_id)
    cpf_ids = fault.cpf_scenario_ids if fault else ()
    injection_id = f"inj-{fault_id.lower()}-{uuid4().hex[:8]}"

    sanitized_target = sanitize_incident_payload(target)
    sanitized_pre = sanitize_incident_payload(pre_state)

    record = {
        "injection_id": injection_id,
        "run_id": run_id,
        "fault_id": fault_id,
        "mode": mode,
        "target": sanitized_target,
        "cpf_scenario_ids": list(cpf_ids),
        "pre_state": sanitized_pre,
        "state": "reserved",
        "reserved_at": utc_now_iso(),
        "disposition": None,
        "incident_id": None,
    }

    p = Path(runtime_root) / "soak-faults" / f"{injection_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    write_json(p, record, indent=2)
    return record


def record_fault_disposition(
    runtime_root: Path | str,
    injection_id: str,
    incident_id: str,
    failure_family: str,
    recovery_strategy: str,
    recovery_result: str,
    recovery_latency: float,
    owner_gate_id: str | None,
    disposition: str,
) -> dict[str, Any]:
    """Record outcome after injection."""
    p = Path(runtime_root) / "soak-faults" / f"{injection_id}.json"
    record = read_json(p, {})
    if not isinstance(record, dict) or not record:
        raise ValueError(f"Unknown fault injection {injection_id}")

    record.update({
        "state": "completed",
        "completed_at": utc_now_iso(),
        "incident_id": incident_id,
        "failure_family": failure_family,
        "recovery_strategy": recovery_strategy,
        "recovery_result": recovery_result,
        "recovery_latency": float(recovery_latency),
        "owner_gate_id": owner_gate_id,
        "disposition": disposition,
    })
    write_json(p, record, indent=2)
    return record


def load_fault_injection(runtime_root: Path | str, injection_id: str) -> dict[str, Any] | None:
    p = Path(runtime_root) / "soak-faults" / f"{injection_id}.json"
    if not p.is_file():
        return None
    return read_json(p, None)
