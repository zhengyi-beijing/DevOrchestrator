"""Canonical structured blocker diagnostics for DevOrchestrator.

Provides structured explain-block diagnostics exposing failed predicates,
expected values, observed evidence, evidence sources, failure classifications,
and bounded remediations across CLI, Web Control API, mobile projection,
and watchdog.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from dev_orchestrator.core.activation import detect_orphan_state
from dev_orchestrator.core.diagnostics import classify_failure_class
from dev_orchestrator.core.readiness import resolve_readiness
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.storage.json_store import read_json


@dataclass(frozen=True)
class Blocker:
    """Structured blocker diagnostic item."""

    code: str
    predicate: str
    expected: Any
    observed: Any
    evidence_source: str
    remediation: str
    failure_class: str
    owner_gate_required: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "predicate": self.predicate,
            "expected": self.expected,
            "observed": self.observed,
            "evidence_source": self.evidence_source,
            "remediation": self.remediation,
            "failure_class": self.failure_class,
            "owner_gate_required": self.owner_gate_required,
        }


def blocker_payload(blockers: Sequence[Blocker]) -> list[dict[str, Any]]:
    """Convert a sequence of Blocker objects into serialized JSON dicts."""
    return [b.to_dict() for b in blockers]


def _read_config_safe(config_path: Optional[Path | str]) -> Optional[dict[str, Any]]:
    if not config_path:
        return None
    p = Path(config_path)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def explain_block(
    *,
    project_id: Optional[str] = None,
    project_config: Optional[dict[str, Any]] = None,
    snapshot: Optional[dict[str, Any]] = None,
    repo_path: Optional[Path | str] = None,
    runtime_root: Path | str,
    config_path: Optional[Path | str] = None,
    action: str = "continue",
) -> list[Blocker]:
    """Derive canonical blocker list, ordered most-blocking first.

    Usable for registered projects (via project_config/snapshot) and unregistered
    repositories (via bare repo_path).
    """
    runtime = Path(runtime_root)
    cfg_path = Path(config_path) if config_path else (runtime.parent / "config" / "projects.json")

    config_data = _read_config_safe(cfg_path)
    resolved_repo: Optional[Path] = None

    if project_config is not None:
        if not project_id:
            project_id = str(project_config.get("project_id") or project_config.get("id") or "").strip()
        p_root = project_config.get("repo_path") or project_config.get("root")
        if p_root:
            resolved_repo = Path(p_root)

    if snapshot is not None and not resolved_repo:
        p_root = snapshot.get("repo_path") or snapshot.get("root")
        if p_root:
            resolved_repo = Path(p_root)
        if not project_id:
            project_id = str(snapshot.get("project_id") or snapshot.get("id") or "").strip()

    if repo_path is not None:
        resolved_repo = Path(repo_path)
        if not project_id:
            project_id = resolved_repo.name

    blockers: list[Blocker] = []

    # 0. Check snapshot/inspection errors & transient infrastructure
    if snapshot is not None:
        snap_error = snapshot.get("error") or snapshot.get("inspection_error")
        snap_state = str(snapshot.get("state") or "")
        broker_err = ""
        if isinstance(snapshot.get("broker"), dict):
            broker_err = str(snapshot["broker"].get("error") or "")
        diag_reason = ""
        diag_code = ""
        if isinstance(snapshot.get("diagnosis"), dict):
            diag_reason = str(snapshot["diagnosis"].get("reason") or "")
            diag_code = str(snapshot["diagnosis"].get("code") or "")

        err_text = str(snap_error or broker_err or diag_reason or "")
        code_text = snap_state or diag_code or ("TRANSIENT_INSPECTION_FAILURE" if err_text else "")

        if err_text or snap_state in ("MONITOR_ERROR", "ERROR"):
            fc = classify_failure_class(code_text, err_text)
            if fc == "transient_infrastructure":
                blockers.append(
                    Blocker(
                        code="TRANSIENT_INSPECTION_FAILURE",
                        predicate="inspection_healthy",
                        expected="successful inspection snapshot",
                        observed=err_text or snap_state,
                        evidence_source=str(snapshot.get("evidence_source") or "snapshot"),
                        remediation="wait for transient backoff or retry inspection",
                        failure_class="transient_infrastructure",
                        owner_gate_required=False,
                    )
                )
            elif snap_state in ("MONITOR_ERROR", "ERROR") or snap_error:
                blockers.append(
                    Blocker(
                        code="INSPECTION_FAILED",
                        predicate="inspection_healthy",
                        expected="successful inspection snapshot",
                        observed=err_text or snap_state,
                        evidence_source=str(snapshot.get("evidence_source") or "snapshot"),
                        remediation="resolve inspection error or check monitor diagnostics",
                        failure_class=fc,
                        owner_gate_required=(fc in {"owner_gate", "terminal"}),
                    )
                )

    # 1. Registration & Orphan state. A caller-supplied project_config comes
    # from the effective daemon registry and is authoritative; do not re-label
    # it unregistered merely because config_path is omitted or non-default.
    is_registered = False
    if isinstance(project_config, dict):
        supplied_id = str(project_config.get("project_id") or project_config.get("id") or "").strip()
        supplied_root = str(project_config.get("repo_path") or project_config.get("root") or "").strip()
        if project_id and supplied_id == project_id:
            is_registered = True
        elif resolved_repo is not None and supplied_root:
            is_registered = (
                Path(supplied_root).resolve(strict=False) == resolved_repo.resolve(strict=False)
            )
    if not is_registered and config_data and isinstance(config_data.get("projects"), list):
        for p in config_data["projects"]:
            if isinstance(p, dict):
                p_id = str(p.get("project_id") or p.get("id") or "").strip()
                p_root = str(p.get("repo_path") or p.get("root") or "").strip()
                if (project_id and p_id == project_id) or (
                    resolved_repo and p_root and Path(p_root).resolve(strict=False) == resolved_repo.resolve(strict=False)
                ):
                    is_registered = True
                    if not project_config:
                        project_config = p
                    break

    if not is_registered and project_id:
        orphan = detect_orphan_state(project_id, resolved_repo, config_data, runtime)
        if orphan is not None:
            blockers.append(
                Blocker(
                    code="ORPHANED_PROJECT_STATE",
                    predicate="project_authoritative_state",
                    expected="registered project in effective daemon configuration",
                    observed=orphan,
                    evidence_source=orphan.get("evidence_source") or str(runtime),
                    remediation="reconcile project registration from activation request or run project-activate",
                    failure_class="recoverable_orchestration",
                    owner_gate_required=False,
                )
            )
        blockers.append(
            Blocker(
                code="PROJECT_NOT_REGISTERED",
                predicate="project_in_registry",
                expected=True,
                observed=False,
                evidence_source=str(cfg_path),
                remediation=f"register project using project-activate --repo {resolved_repo or project_id}",
                failure_class="recoverable_orchestration",
                owner_gate_required=False,
            )
        )

    # 2. Check Repository truth & Git cleanliness
    truth = None
    if resolved_repo is not None:
        if not resolved_repo.is_dir():
            blockers.append(
                Blocker(
                    code="REPOSITORY_PATH_MISSING",
                    predicate="repo_path_exists",
                    expected=True,
                    observed=str(resolved_repo),
                    evidence_source=str(resolved_repo),
                    remediation=f"ensure repository path {resolved_repo} exists on disk",
                    failure_class="terminal",
                    owner_gate_required=True,
                )
            )
        else:
            truth = read_repository_truth(resolved_repo)
            if not truth.valid:
                fc = classify_failure_class("GIT_TRUTH_INVALID", truth.error)
                is_transient = (fc == "transient_infrastructure")
                blockers.append(
                    Blocker(
                        code="TRANSIENT_GIT_TIMEOUT" if is_transient else "GIT_TRUTH_INVALID",
                        predicate="git_truth_valid",
                        expected=True,
                        observed=truth.error,
                        evidence_source=str(resolved_repo),
                        remediation="retry after transient timeout" if is_transient else "ensure repository is a valid Git worktree with checked-out HEAD",
                        failure_class=fc,
                        owner_gate_required=not is_transient,
                    )
                )
            elif truth.dirty:
                blockers.append(
                    Blocker(
                        code="DIRTY_WORKTREE",
                        predicate="worktree_clean",
                        expected="clean",
                        observed={
                            "dirty_entries": list(truth.dirty_entries),
                            "expected_entries": list(truth.expected_entries),
                            "unexpected_entries": list(truth.unexpected_entries),
                        },
                        evidence_source=str(resolved_repo),
                        remediation="clean or commit unexpected working tree changes",
                        failure_class="terminal",
                        owner_gate_required=True,
                    )
                )

    # 3. Task Identity & Readiness checks
    current_task_id: Optional[str] = None
    next_status: Optional[str] = None
    next_updated_at: Optional[str] = None

    if snapshot is not None:
        telemetry = snapshot.get("telemetry") if isinstance(snapshot.get("telemetry"), dict) else {}
        current_task_id = str(telemetry.get("task_id") or "").strip() or None
        next_status = snapshot.get("next_status")
        next_updated_at = snapshot.get("next_updated_at")

    if resolved_repo is not None and resolved_repo.is_dir():
        if not current_task_id:
            from dev_orchestrator.monitor.telemetry import extract_task_id
            next_file = resolved_repo / "agent" / "next.md"
            if next_file.is_file():
                try:
                    text = next_file.read_text(encoding="utf-8", errors="replace")
                    current_task_id = extract_task_id(text)
                except OSError:
                    pass

        readiness = resolve_readiness(
            resolved_repo,
            current_task_id=current_task_id,
            next_status=next_status,
            next_updated_at=next_updated_at,
        )

        if readiness.code == "READINESS_SCHEMA_INVALID":
            blockers.append(
                Blocker(
                    code="READINESS_SCHEMA_INVALID",
                    predicate="valid_schema_version_1",
                    expected={"schema_version": 1, "keys": ["schema_version", "execution_state", "task_id", "updated_at"]},
                    observed=readiness.reason,
                    evidence_source=str(resolved_repo / "agent" / "execution-state.json"),
                    remediation="fix agent/execution-state.json schema or remove corrupt file",
                    failure_class="terminal",
                    owner_gate_required=True,
                )
            )
        elif readiness.code == "READINESS_TASK_ID_MISMATCH":
            blockers.append(
                Blocker(
                    code="READINESS_TASK_ID_MISMATCH",
                    predicate="task_id_matches_current",
                    expected=current_task_id,
                    observed=readiness.task_id,
                    evidence_source=str(resolved_repo / "agent" / "execution-state.json"),
                    remediation=f"update agent/execution-state.json task_id to current task {current_task_id!r}",
                    failure_class="recoverable_orchestration",
                    owner_gate_required=False,
                )
            )
        elif readiness.code == "READINESS_TOKEN_UNSTRUCTURED":
            blockers.append(
                Blocker(
                    code="READINESS_TOKEN_UNSTRUCTURED",
                    predicate="execution_state==ready_to_run",
                    expected="ready_to_run",
                    observed=readiness.raw_token,
                    evidence_source=str(resolved_repo / "agent" / "next.md"),
                    remediation=f"migrate legacy readiness token {readiness.raw_token!r} to structured execution-state.json",
                    failure_class="recoverable_orchestration",
                    owner_gate_required=False,
                )
            )
        elif readiness.code == "READINESS_TOKEN_UNRESOLVABLE":
            blockers.append(
                Blocker(
                    code="READINESS_TOKEN_UNRESOLVABLE",
                    predicate="resolvable_readiness_token",
                    expected="valid Status line in agent/next.md",
                    observed=readiness.raw_token,
                    evidence_source=str(resolved_repo / "agent" / "next.md"),
                    remediation="correct Status line in agent/next.md to a recognized status",
                    failure_class="terminal",
                    owner_gate_required=True,
                )
            )
        elif readiness.state != "ready_to_run" and readiness.valid and action in {"continue", "start"}:
            blockers.append(
                Blocker(
                    code="READINESS_NOT_READY_TO_RUN",
                    predicate="execution_state==ready_to_run",
                    expected="ready_to_run",
                    observed=readiness.state,
                    evidence_source=str(
                        resolved_repo / ("agent/execution-state.json" if readiness.source == "structured" else "agent/next.md")
                    ),
                    remediation=f"task is {readiness.state}; must be ready_to_run before execution",
                    failure_class="lifecycle",
                    owner_gate_required=False,
                )
            )

    # 4. Owner Pause & Owner Gate checks (registered project)
    if project_id:
        from dev_orchestrator.control.owner_store import OwnerControlStore
        owner_store = OwnerControlStore(runtime)
        if owner_store.is_paused(project_id):
            blockers.append(
                Blocker(
                    code="OWNER_PAUSED",
                    predicate="not_paused",
                    expected=False,
                    observed=True,
                    evidence_source=str(runtime / "control" / "owner.json"),
                    remediation="resume project using project-resume",
                    failure_class="owner_gate",
                    owner_gate_required=True,
                )
            )

        gate = None
        if snapshot is not None:
            gate = snapshot.get("gate")
        if not gate:
            from dev_orchestrator.control.surface import _latest_owner_gate
            gate = _latest_owner_gate(runtime, project_id)
        if isinstance(gate, dict) and gate.get("state") == "owner_gate":
            blockers.append(
                Blocker(
                    code="OWNER_GATE_PRESENT",
                    predicate="owner_gate_inactive",
                    expected=None,
                    observed=gate,
                    evidence_source=str(gate.get("gate_source") or "runtime"),
                    remediation=f"resolve active owner gate: {gate.get('reason') or 'gate active'}",
                    failure_class="owner_gate",
                    owner_gate_required=True,
                )
            )

    # 5. Execution Policy check
    if project_config is not None:
        from dev_orchestrator.core.transition_executor import _execution_policy
        policy, err = _execution_policy(project_config)
        if policy is None:
            blockers.append(
                Blocker(
                    code="EXECUTION_POLICY_DISABLED",
                    predicate="execution_policy_enabled",
                    expected="configured execution policy",
                    observed=err,
                    evidence_source=str(cfg_path),
                    remediation="configure valid execution policy in projects config",
                    failure_class="terminal",
                    owner_gate_required=True,
                )
            )

    # 6. Active Execution check
    if project_id and action in {"continue", "start"}:
        from dev_orchestrator.control.surface import _active_execution
        active_exec = _active_execution(runtime, project_id)
        if active_exec is not None and str(active_exec.get("state") or "") in {"launching", "running"}:
            blockers.append(
                Blocker(
                    code="ACTIVE_EXECUTION_PRESENT",
                    predicate="no_active_execution",
                    expected=None,
                    observed=active_exec,
                    evidence_source=str(runtime / "transition-executor.json"),
                    remediation="wait for active execution to complete or stop it",
                    failure_class="terminal",
                    owner_gate_required=False,
                )
            )

    # 7. Execution Intent Exhaustion / Livelock check
    if project_id:
        intent_file = runtime / "execution-intent.json"
        intents_data = read_json(intent_file, {})
        intents = intents_data.get("intents") if isinstance(intents_data, dict) else {}
        intent = intents.get(project_id) if isinstance(intents, dict) else None
        if isinstance(intent, dict) and intent.get("state") == "exhausted":
            fail_class = intent.get("failure_class") or "recovery_exhausted"
            blocker_code = intent.get("blocker_code") or "RECOVERY_BUDGET_EXHAUSTED"
            blockers.append(
                Blocker(
                    code=blocker_code,
                    predicate="recovery_budget_available",
                    expected=True,
                    observed={
                        "actions_used": intent.get("actions_used"),
                        "fingerprints": intent.get("fingerprints"),
                    },
                    evidence_source=str(intent_file),
                    remediation="recovery budget exhausted; inspect recovery history and reset intent",
                    failure_class=fail_class,
                    owner_gate_required=False,
                )
            )

    # 8. Execution Loss Unresolved check
    if project_id and action in {"continue", "start"}:
        wd_file = runtime / "watchdog.json"
        wd_data = read_json(wd_file, {})
        if isinstance(wd_data, dict):
            prow = (wd_data.get("projects") or {}).get(project_id)
            if isinstance(prow, dict):
                loss = prow.get("execution_loss")
                unresolved = loss.get("unresolved_invariants") if isinstance(loss, dict) else []
                if unresolved:
                    blockers.append(
                        Blocker(
                            code="EXECUTION_LOSS_UNRESOLVED",
                            predicate="no_unresolved_execution_loss",
                            expected=None,
                            observed=unresolved,
                            evidence_source=str(wd_file),
                            remediation="reconcile or recover unresolved vanished execution before launching new work",
                            failure_class="execution_loss",
                            owner_gate_required=False,
                        )
                    )

    # Sort blockers: most-blocking first
    _SEVERITY_ORDER = {
        "OWNER_GATE_PRESENT": 0,
        "OWNER_PAUSED": 0,
        "TRANSIENT_INSPECTION_FAILURE": 1,
        "TRANSIENT_INFRASTRUCTURE_FAILURE": 1,
        "TRANSIENT_GIT_TIMEOUT": 1,
        "INSPECTION_FAILED": 2,
        "ORPHANED_PROJECT_STATE": 3,
        "PROJECT_NOT_REGISTERED": 4,
        "REPOSITORY_PATH_MISSING": 5,
        "GIT_TRUTH_INVALID": 6,
        "READINESS_SCHEMA_INVALID": 7,
        "READINESS_TOKEN_UNRESOLVABLE": 8,
        "READINESS_TASK_ID_MISMATCH": 9,
        "READINESS_TOKEN_UNSTRUCTURED": 10,
        "DIRTY_WORKTREE": 11,
        "EXECUTION_POLICY_DISABLED": 12,
        "READINESS_NOT_READY_TO_RUN": 13,
        "EXECUTION_LOSS_UNRESOLVED": 14,
        "ACTIVE_EXECUTION_PRESENT": 15,
        "RECOVERY_LIVELOCK_DETECTED": 16,
        "RECOVERY_BUDGET_EXHAUSTED": 17,
    }
    blockers.sort(key=lambda b: _SEVERITY_ORDER.get(b.code, 99))
    return blockers
