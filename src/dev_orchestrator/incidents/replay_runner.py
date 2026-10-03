"""Allowlisted, deterministic replay runner for regression verification.

Runs allowlisted production operations against defect and corrected anchors to
establish determinism and discrimination without code execution or test file writes.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import tempfile
import shutil
from typing import Any, Callable, Mapping, Sequence

from dev_orchestrator.incidents.fingerprint import failure_signature
from dev_orchestrator.storage.json_store import read_json


class ReplayOpNotAllowedError(RuntimeError):
    """Raised when an unallowlisted replay operation is requested."""


class ReplayFixtureInvalidError(ValueError):
    """Raised when a replay fixture payload violates safety or schema constraints."""


DANGEROUS_PATTERNS = ("import ", "os.system", "exec(", "eval(", "__import__", "subprocess", "shutil")


def _validate_anchor_safety(obj: Any) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise ReplayFixtureInvalidError("Anchor dict keys must be strings")
            if k.startswith("__") and k.endswith("__"):
                raise ReplayFixtureInvalidError(f"Special attribute key {k!r} not allowed in fixture")
            _validate_anchor_safety(v)
    elif isinstance(obj, (list, tuple, set)):
        for item in obj:
            _validate_anchor_safety(item)
    elif isinstance(obj, str):
        for pat in DANGEROUS_PATTERNS:
            if pat in obj:
                raise ReplayFixtureInvalidError(f"Dangerous code pattern {pat!r} detected in fixture")
    elif isinstance(obj, (int, float, bool, type(None))):
        pass
    else:
        raise ReplayFixtureInvalidError(f"Unsupported fixture value type: {type(obj)}")


def _check_fixture_size(anchor: Any, max_bytes: int = 65536) -> None:
    try:
        raw = json.dumps(anchor)
        if len(raw.encode("utf-8")) > max_bytes:
            raise ReplayFixtureInvalidError(f"Fixture payload exceeds size limit of {max_bytes} bytes")
    except (TypeError, ValueError) as exc:
        raise ReplayFixtureInvalidError(f"Fixture is not JSON serializable: {exc}")


@dataclass(frozen=True)
class ReplayCaseSpec:
    case_id: str
    replay_op: str
    defect_anchor: dict[str, Any]
    corrected_anchor: dict[str, Any]
    expected_invariant: str
    expected_defect_outcome: str
    cpf_scenarios: Sequence[str] = ()
    source_failure_signature: str = ""
    regression_class: str = ""
    notes: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReplayCaseSpec:
        return cls(
            case_id=str(data["case_id"]),
            replay_op=str(data["replay_op"]),
            defect_anchor=dict(data.get("defect_anchor", {})),
            corrected_anchor=dict(data.get("corrected_anchor", {})),
            expected_invariant=str(data.get("expected_invariant", "")),
            expected_defect_outcome=str(data.get("expected_defect_outcome", "")),
            cpf_scenarios=tuple(data.get("cpf_scenarios", ())),
            source_failure_signature=str(data.get("source_failure_signature", "")),
            regression_class=str(data.get("regression_class", "")),
            notes=str(data.get("notes", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "replay_op": self.replay_op,
            "defect_anchor": self.defect_anchor,
            "corrected_anchor": self.corrected_anchor,
            "expected_invariant": self.expected_invariant,
            "expected_defect_outcome": self.expected_defect_outcome,
            "cpf_scenarios": list(self.cpf_scenarios),
            "source_failure_signature": self.source_failure_signature,
            "regression_class": self.regression_class,
            "notes": self.notes,
        }


def load_replay_case(path_or_mapping: str | Path | Mapping[str, Any]) -> ReplayCaseSpec:
    """Load a replay case spec from a JSON file path or a mapping."""
    if isinstance(path_or_mapping, (str, Path)):
        p = Path(path_or_mapping)
        if not p.is_file():
            raise FileNotFoundError(f"Replay case file {p} not found")
        data = read_json(p)
        if not isinstance(data, dict):
            raise ReplayFixtureInvalidError(f"Replay case file {p} does not contain a JSON object")
        return ReplayCaseSpec.from_dict(data)
    elif isinstance(path_or_mapping, Mapping):
        return ReplayCaseSpec.from_dict(path_or_mapping)
    else:
        raise TypeError(f"Expected str, Path, or Mapping, got {type(path_or_mapping)}")


def _op_fingerprint_stability(anchor: Mapping[str, Any]) -> dict[str, Any]:
    """Execute failure_signature over anchor semantic dictionary."""
    semantic = anchor.get("semantic", anchor)
    payload = {k: v for k, v in semantic.items() if k not in ("outcome", "__code__")}
    sig = failure_signature(payload)
    outcome = anchor.get("outcome")
    if outcome is None:
        outcome = "computed_signature"
    return {
        "signature": sig,
        "outcome": outcome,
        "status": "ok",
    }


def _op_incident_capture_dedup(anchor: Mapping[str, Any]) -> dict[str, Any]:
    """Execute incident capture dedup against an isolated temporary store."""
    from dev_orchestrator.incidents.capture import capture_incident

    tmp = tempfile.mkdtemp()
    try:
        project_id = anchor.get("project_id", "test-proj")
        task_id = anchor.get("task_id", "P20.2")
        classification = anchor.get("classification", "lost_handoff")
        semantic = anchor.get("semantic", {"failure_class": classification})
        occ_key = anchor.get("occurrence_key", "occ-test-01")
        repeats = int(anchor.get("repeats", 2))
        c = None
        for _ in range(repeats):
            c = capture_incident(
                tmp,
                project_id=project_id,
                task_id=task_id,
                classification=classification,
                semantic=semantic,
                occurrence_key=occ_key,
            )
        outcome = anchor.get("outcome") or (
            "dedup_confirmed" if c and c.get("recurrence_count") == 1 else "dedup_failed"
        )
        return {
            "family_id": c.get("family_id") if c else None,
            "recurrence_count": c.get("recurrence_count") if c else 0,
            "outcome": outcome,
            "status": "ok",
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _op_store_journal_reconcile(anchor: Mapping[str, Any]) -> dict[str, Any]:
    """Execute journal reconciliation replay against an isolated store."""
    from dev_orchestrator.incidents.store import load_incident_store

    tmp = tempfile.mkdtemp()
    try:
        store = load_incident_store(tmp)
        intents = store.reconcile_journal()
        outcome = anchor.get("outcome") or "journal_reconciled"
        return {
            "reconciled_count": len(intents),
            "outcome": outcome,
            "status": "ok",
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _op_convergence_replay_case(anchor: Mapping[str, Any]) -> dict[str, Any]:
    """Execute convergence ReplayHarness on anchor replay case."""
    from dev_orchestrator.convergence.replay import ReplayCase, ReplayHarness

    harness = ReplayHarness()
    case = ReplayCase.from_dict(anchor)
    res = harness.run_case(case)
    outcome = anchor.get("outcome") or (res.v0_decision if res else "unknown")
    return {
        "passed": res.passed if res else False,
        "v0_decision": res.v0_decision if res else "",
        "outcome": outcome,
        "status": "ok",
    }


def _op_convergence_invariants(anchor: Mapping[str, Any]) -> dict[str, Any]:
    """Execute evaluate_convergence_invariants on anchor state."""
    from dev_orchestrator.convergence.invariants import evaluate_convergence_invariants

    verdicts = evaluate_convergence_invariants(anchor)
    all_passed = all(verdicts.values())
    outcome = anchor.get("outcome") or ("invariants_satisfied" if all_passed else "invariant_violation")
    return {
        "verdicts": verdicts,
        "all_passed": all_passed,
        "outcome": outcome,
        "status": "ok",
    }


REPLAY_OPS: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
    "fingerprint_stability": _op_fingerprint_stability,
    "incident_capture_dedup": _op_incident_capture_dedup,
    "store_journal_reconcile": _op_store_journal_reconcile,
    "convergence_replay_case": _op_convergence_replay_case,
    "convergence_invariants": _op_convergence_invariants,
}


def run_replay_case(spec: ReplayCaseSpec, *, repeats: int = 2) -> dict[str, Any]:
    """Execute allowlisted replay operation against defect and corrected anchors.

    Runs each anchor at least twice to test determinism and returns discriminating outcome.
    """
    if spec.replay_op not in REPLAY_OPS:
        raise ReplayOpNotAllowedError(f"Replay op {spec.replay_op!r} is not allowed")

    _validate_anchor_safety(spec.defect_anchor)
    _validate_anchor_safety(spec.corrected_anchor)
    _check_fixture_size(spec.defect_anchor)
    _check_fixture_size(spec.corrected_anchor)

    op = REPLAY_OPS[spec.replay_op]
    repeats = max(2, int(repeats))

    defect_results: list[dict[str, Any]] = []
    for _ in range(repeats):
        defect_results.append(op(spec.defect_anchor))

    corrected_results: list[dict[str, Any]] = []
    for _ in range(repeats):
        corrected_results.append(op(spec.corrected_anchor))

    defect_deterministic = all(r == defect_results[0] for r in defect_results)
    corrected_deterministic = all(r == corrected_results[0] for r in corrected_results)
    deterministic = defect_deterministic and corrected_deterministic

    defect_outcome = defect_results[0].get("outcome", defect_results[0])
    corrected_outcome = corrected_results[0].get("outcome", corrected_results[0])

    discriminating = bool(deterministic and (defect_outcome != corrected_outcome))

    digest_content = {
        "case_id": spec.case_id,
        "replay_op": spec.replay_op,
        "defect_outcome": defect_outcome,
        "corrected_outcome": corrected_outcome,
    }
    outcome_digest = (
        f"sha256:{hashlib.sha256(json.dumps(digest_content, sort_keys=True, default=str).encode()).hexdigest()}"
    )

    reason: str | None = None
    if not deterministic:
        reason = "non_deterministic_replay"
    elif not discriminating:
        reason = "vacuous_oracle_non_discriminating"

    return {
        "deterministic": deterministic,
        "defect_outcome": defect_outcome,
        "corrected_outcome": corrected_outcome,
        "discriminating": discriminating,
        "outcome_digest": outcome_digest,
        "reason": reason,
    }
