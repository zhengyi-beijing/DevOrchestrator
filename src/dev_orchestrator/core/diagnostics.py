"""Bounded read-only diagnostics for stalled projects.

Inspects repository truth, process liveness, broker state, role ledgers, and
agent files under an explicit monotonic deadline. Classifies structured diagnoses
via deterministic rules without model inference or destructive actions.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.platform.process import is_pid_alive
from dev_orchestrator.storage.json_store import read_json, utc_now_iso

DIAGNOSIS_CODES = (
    "healthy_slow",
    "agent_stalled",
    "process_dead",
    "provider_or_quota_blocked",
    "state_desync",
    "external_wait",
    "unknown",
)
WORKER_EXPECTED_LIFECYCLE_STATES = frozenset({"EXECUTING", "REMEDIATING"})
# Vocabulary of active worker states from the executor ledger and overlay projection.
# "starting" is the overlay mapping for "launching" (see transition_executor.overlay_managed_runs).
ACTIVE_WORKER_STATES = frozenset({"running", "active", "launching", "starting"})

_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|bearer|sk-[a-z0-9]{20,})[:=]\s*([^\s]{4,})"
)


def _redact_secrets(text: str) -> str:
    """Redact tokens or secret patterns from diagnostics output tails."""
    return _SECRET_PATTERN.sub(r"\1: [REDACTED]", text)


def _safe_tail(path: Path, max_chars: int = 500) -> str:
    """Read a bounded, secret-redacted tail of a file."""
    try:
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        tail = text[-max_chars:] if len(text) > max_chars else text
        return _redact_secrets(tail.strip())
    except OSError:
        return ""


@dataclass(frozen=True)
class Diagnosis:
    code: str
    confidence: float
    reason: str
    recommended_action: str
    owner_gate_required: bool
    evidence: dict[str, Any]
    evidence_hash: str


def evidence_hash(evidence: dict[str, Any]) -> str:
    """Stable canonical hash of evidence with volatile timing fields removed."""
    normalized = copy.deepcopy(evidence)
    _strip_volatile_fields(normalized)
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _strip_volatile_fields(obj: Any) -> None:
    if isinstance(obj, dict):
        for k in list(obj.keys()):
            lower_k = str(k).lower()
            if any(p in lower_k for p in ("collected_at", "duration", "remaining", "now", "deadline", "elapsed")):
                obj.pop(k, None)
            else:
                _strip_volatile_fields(obj[k])
    elif isinstance(obj, list):
        for item in obj:
            _strip_volatile_fields(item)


def collect_evidence(
    project: dict[str, Any],
    snapshot: dict[str, Any],
    runtime_root: Path | str,
    *,
    ai_execution_port: Any = None,
    deadline: Optional[float] = None,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Collect bounded read-only operational facts across 6 dimensions."""
    start_mono = time.monotonic()
    eff_deadline = deadline or (start_mono + timeout_seconds)
    runtime = Path(runtime_root)
    repo_path = str(project.get("repo_path") or snapshot.get("repo_path") or snapshot.get("root") or "")
    repo = Path(repo_path)
    project_id = str(project.get("project_id") or project.get("id") or "")

    evidence: dict[str, Any] = {
        "collected_at": utc_now_iso(),
        "project_id": project_id,
        "repo_path": repo_path,
    }

    # 1. Repository truth
    if time.monotonic() >= eff_deadline:
        evidence["repository_truth"] = {"status": "skipped_deadline"}
    else:
        truth = read_repository_truth(repo_path)
        evidence["repository_truth"] = {
            "valid": truth.valid,
            "branch": truth.branch,
            "head": truth.head,
            "dirty": truth.dirty,
            "uncommitted_files": truth.uncommitted_files[:20],
            "error": truth.error,
        }

    # 2. Process liveness
    worker = snapshot.get("worker") if isinstance(snapshot.get("worker"), dict) else {}
    pid = worker.get("pid")
    if time.monotonic() >= eff_deadline:
        evidence["process_liveness"] = {"status": "skipped_deadline"}
    else:
        alive = is_pid_alive(pid) if pid is not None else False
        evidence["process_liveness"] = {
            "pid": pid,
            "process_alive": alive,
            "kind": worker.get("kind"),
            "worker_state": worker.get("state"),
            "started_at": worker.get("started_at"),
            "updated_at": worker.get("updated_at"),
        }

    # 3. AIBroker status check (read-only)
    if time.monotonic() >= eff_deadline:
        evidence["broker"] = {"status": "skipped_deadline"}
    else:
        broker_info: dict[str, Any] = {"available": False, "status": None}
        request_id = None
        actuation = snapshot.get("actuation") if isinstance(snapshot.get("actuation"), dict) else {}
        if actuation.get("source_request_id"):
            request_id = actuation["source_request_id"]
        elif worker.get("request_id"):
            request_id = worker["request_id"]

        if ai_execution_port is not None and request_id:
            try:
                b_status = ai_execution_port.status(request_id)
                broker_info = {"available": True, "request_id": request_id, "status": b_status}
            except Exception as exc:
                broker_info = {"available": False, "request_id": request_id, "error": str(exc)}
        else:
            broker_info = {"available": False, "reason": "no_request_id_or_port"}
        evidence["broker"] = broker_info

    # 4. Role ledgers
    if time.monotonic() >= eff_deadline:
        evidence["ledgers"] = {"status": "skipped_deadline"}
    else:
        ledgers: dict[str, Any] = {}
        for ledger_name in ("ai-planner.json", "ai-reviewer.json", "transition-executor.json"):
            l_path = runtime / ledger_name
            if l_path.is_file():
                data = read_json(l_path, {})
                recs = data.get("plans") or data.get("reviews") or data.get("executions") or {}
                if isinstance(recs, dict):
                    matched = [r for r in recs.values() if isinstance(r, dict) and str(r.get("project_id") or "") == project_id]
                    if matched:
                        latest = max(matched, key=lambda r: str(r.get("completed_at") or r.get("started_at") or r.get("updated_at") or ""))
                        ledgers[ledger_name] = {
                            "state": latest.get("state"),
                            "status": latest.get("status"),
                            "request_id": latest.get("request_id"),
                            "started_at": latest.get("started_at"),
                            "updated_at": latest.get("updated_at"),
                        }
        evidence["ledgers"] = ledgers

    # 5. Agent files existence and bounded safe tails
    if time.monotonic() >= eff_deadline:
        evidence["agent_files"] = {"status": "skipped_deadline"}
    else:
        agent_dir = repo / "agent"
        evidence["agent_files"] = {
            "current_md_present": (agent_dir / "CURRENT.md").is_file(),
            "next_md_present": (agent_dir / "next.md").is_file(),
            "result_md_present": (agent_dir / "result.md").is_file(),
            "next_md_tail": _safe_tail(agent_dir / "next.md", max_chars=300),
            "current_md_tail": _safe_tail(agent_dir / "CURRENT.md", max_chars=300),
        }

    # 6. Watchdog activity rollup from snapshot (already collected; no filesystem walk)
    activity = snapshot.get("activity") if isinstance(snapshot.get("activity"), dict) else {}
    evidence["activity_watchdog_safe"] = copy.deepcopy(activity.get("watchdog_safe", {}))

    evidence["duration_seconds"] = round(time.monotonic() - start_mono, 3)
    return evidence


def classify_evidence(evidence: dict[str, Any], assessment: Any) -> Diagnosis:
    """Deterministic rule-based classification over the 7 diagnosis codes."""
    ev_hash = evidence_hash(evidence)

    # If any essential collector was skipped due to deadline
    for comp in ("repository_truth", "process_liveness", "agent_files"):
        part = evidence.get(comp)
        if isinstance(part, dict) and part.get("status") == "skipped_deadline":
            return Diagnosis(
                code="unknown",
                confidence=0.0,
                reason="diagnostic collector skipped due to deadline exhaustion",
                recommended_action="investigate system latency and require owner intervention",
                owner_gate_required=True,
                evidence=evidence,
                evidence_hash=ev_hash,
            )

    proc = evidence.get("process_liveness") if isinstance(evidence.get("process_liveness"), dict) else {}
    pid = proc.get("pid")
    is_alive = proc.get("process_alive")
    lifecycle = str(getattr(assessment, "lifecycle_state", "")).upper()
    # A worker process is expected ONLY when the lifecycle is an execution-phase state.
    # Worker.state from the executor ledger is not used here because it may be stale from a
    # prior EXECUTING run and would otherwise cause spurious process_dead during PLANNING /
    # REVIEWING_PLAN / APPLYING_PLAN / REVIEWING.
    worker_active = lifecycle in WORKER_EXPECTED_LIFECYCLE_STATES

    # Check process_dead
    if pid is not None and is_alive is False and worker_active:
        return Diagnosis(
            code="process_dead",
            confidence=1.0,
            reason=f"worker process (PID {pid}) is dead while lifecycle is {lifecycle or 'active'}",
            recommended_action="launch safe continue recovery to restart worker",
            owner_gate_required=False,
            evidence=evidence,
            evidence_hash=ev_hash,
        )

    # Check broker quota / rate-limiting
    broker = evidence.get("broker") if isinstance(evidence.get("broker"), dict) else {}
    broker_status = str(broker.get("status") or "").lower()
    broker_err = str(broker.get("error") or "").lower()
    if any(q in broker_status or q in broker_err for q in ("429", "rate_limit", "quota", "resource_exhausted")):
        return Diagnosis(
            code="provider_or_quota_blocked",
            confidence=0.95,
            reason=f"AIBroker reported quota or rate limit block: {broker_status or broker_err}",
            recommended_action="wait for quota replenishment or require owner gate",
            owner_gate_required=True,
            evidence=evidence,
            evidence_hash=ev_hash,
        )

    # Check state desync
    ledgers = evidence.get("ledgers") if isinstance(evidence.get("ledgers"), dict) else {}
    exec_ledger = ledgers.get("transition-executor.json") if isinstance(ledgers.get("transition-executor.json"), dict) else {}
    if exec_ledger.get("state") == "completed" and getattr(assessment, "lifecycle_state", None) in ("EXECUTING", "APPLYING_PLAN"):
        return Diagnosis(
            code="state_desync",
            confidence=0.9,
            reason="transition executor completed but lifecycle state remained active",
            recommended_action="reconcile transition executor state via owner gate",
            owner_gate_required=True,
            evidence=evidence,
            evidence_hash=ev_hash,
        )

    # Check external wait
    if broker_status in ("waiting_user", "waiting_bridge", "unbound"):
        return Diagnosis(
            code="external_wait",
            confidence=0.85,
            reason=f"worker is awaiting external user/bridge resolution: {broker_status}",
            recommended_action="wait for external bridge action or owner input",
            owner_gate_required=True,
            evidence=evidence,
            evidence_hash=ev_hash,
        )

    # Check healthy_slow (telemetry shows worker alive and uncommitted changes or recent progress)
    truth = evidence.get("repository_truth") if isinstance(evidence.get("repository_truth"), dict) else {}
    no_prog_sec = getattr(assessment, "no_progress_seconds", None)
    thresh_sec = getattr(assessment, "threshold_seconds", 900.0)
    if is_alive is True and no_prog_sec is not None and no_prog_sec < thresh_sec * 1.2:
        if truth.get("dirty") or truth.get("uncommitted_files"):
            return Diagnosis(
                code="healthy_slow",
                confidence=0.8,
                reason="worker process is actively computing with uncommitted worktree changes",
                recommended_action="allow worker additional time before escalating",
                owner_gate_required=False,
                evidence=evidence,
                evidence_hash=ev_hash,
            )

    # Check agent_stalled (process is alive, but no progress within threshold).
    # R3-F1: Only valid in worker-expected lifecycle states (EXECUTING / REMEDIATING).
    # In non-worker lifecycles a live PID is likely stale or reused from a prior execution;
    # classifying it as agent_stalled would allow spurious wd-continue recovery that could
    # launch a second planner cycle or interfere with an in-progress review/plan phase.
    if is_alive is True:
        if not worker_active:
            return Diagnosis(
                code="unknown",
                confidence=0.3,
                reason=(
                    f"worker process (PID {pid}) is alive but lifecycle {lifecycle!r} is not "
                    f"a worker-expected execution state (EXECUTING/REMEDIATING); PID may be "
                    f"stale or reused from a prior run"
                ),
                recommended_action="inspect system state and require owner gate",
                owner_gate_required=True,
                evidence=evidence,
                evidence_hash=ev_hash,
            )
        return Diagnosis(
            code="agent_stalled",
            confidence=0.9,
            reason=f"worker process is alive but produced no progress for {no_prog_sec}s (threshold {thresh_sec}s)",
            recommended_action="initiate safe continue recovery",
            owner_gate_required=False,
            evidence=evidence,
            evidence_hash=ev_hash,
        )

    # Fallback to unknown
    return Diagnosis(
        code="unknown",
        confidence=0.0,
        reason="inconclusive operational evidence for stall assessment",
        recommended_action="inspect system logs and require owner gate",
        owner_gate_required=True,
        evidence=evidence,
        evidence_hash=ev_hash,
    )
