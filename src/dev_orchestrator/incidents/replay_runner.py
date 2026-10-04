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
    payload = {
        k: v for k, v in semantic.items()
        if k not in ("outcome", "__code__", "expected_signature", "source_failure_signature", "expected_outcome", "repeats")
    }
    sig = failure_signature(payload)
    expected_sig = anchor.get("expected_signature") or anchor.get("source_failure_signature")
    if expected_sig:
        computed_outcome = "signature_matches" if sig == expected_sig else "signature_drift"
    else:
        computed_outcome = sig
    return {
        "signature": sig,
        "outcome": computed_outcome,
        "status": "ok",
    }


def _op_incident_capture_dedup(anchor: Mapping[str, Any]) -> dict[str, Any]:
    """Execute incident capture dedup against an isolated temporary store."""
    from dev_orchestrator.incidents.capture import capture_incident
    from dev_orchestrator.incidents.store import load_incident_store

    tmp = tempfile.mkdtemp()
    try:
        project_id = anchor.get("project_id", "test-proj")
        task_id = anchor.get("task_id", "P20.2")
        classification = anchor.get("classification", "lost_handoff")
        semantic = anchor.get("semantic", {"failure_class": classification})
        base_key = anchor.get("occurrence_key", "occ-test-01")
        repeats = int(anchor.get("repeats", 2))
        keys = anchor.get("occurrence_keys")
        if not keys:
            if anchor.get("varying_keys"):
                keys = [f"{base_key}_{i}" for i in range(repeats)]
            else:
                keys = [base_key] * repeats
        c = None
        for k in keys:
            c = capture_incident(
                tmp,
                project_id=project_id,
                task_id=task_id,
                classification=classification,
                semantic=semantic,
                occurrence_key=k,
            )
        store = load_incident_store(tmp)
        fams = store.index.get("families", {})
        total_occs = sum(len(f.get("occurrences", [])) for f in fams.values() if isinstance(f, dict))
        computed_outcome = "dedup_confirmed" if total_occs == 1 else "dedup_failed"
        return {
            "family_id": c.get("family_id") if c else None,
            "total_occurrences": total_occs,
            "outcome": computed_outcome,
            "status": "ok",
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _op_store_journal_reconcile(anchor: Mapping[str, Any]) -> dict[str, Any]:
    """Execute journal reconciliation replay against an isolated store."""
    from dev_orchestrator.incidents.store import load_incident_store
    from dev_orchestrator.storage.json_store import write_json

    tmp = tempfile.mkdtemp()
    try:
        intent_info = anchor.get("intent")
        if isinstance(intent_info, dict):
            jdir = Path(tmp) / "incident-packets" / "journal"
            jdir.mkdir(parents=True, exist_ok=True)
            txn_id = intent_info.get("txn_id", "txn-01")
            write_json(jdir / f"{txn_id}.json", intent_info)
            side_files = anchor.get("side_files", {})
            if isinstance(side_files, dict):
                for rel_p, content in side_files.items():
                    fp = Path(tmp) / "incident-packets" / rel_p
                    fp.parent.mkdir(parents=True, exist_ok=True)
                    fp.write_text(content, encoding="utf-8")
        store = load_incident_store(tmp)
        results = store.reconcile_journal()
        if results and all(r.get("status") in ("committed_confirmed", "reapplied") for r in results):
            computed_outcome = "journal_reconciled"
        elif results and any(r.get("status") == "payload_unverified" for r in results):
            computed_outcome = "intent_unreconciled"
        else:
            computed_outcome = "journal_reconciled" if not intent_info else "intent_unreconciled"
        return {
            "reconciled_count": len(results),
            "results": results,
            "outcome": computed_outcome,
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
    computed_outcome = res.v0_decision if res else "unknown"
    return {
        "passed": res.passed if res else False,
        "v0_decision": res.v0_decision if res else "",
        "outcome": computed_outcome,
        "status": "ok",
    }


def _op_convergence_invariants(anchor: Mapping[str, Any]) -> dict[str, Any]:
    """Execute evaluate_convergence_invariants on anchor state."""
    from dev_orchestrator.convergence.invariants import evaluate_convergence_invariants

    verdicts = evaluate_convergence_invariants(anchor)
    all_passed = all(v.holds if hasattr(v, "holds") else bool(v) for v in verdicts.values())
    computed_outcome = "invariants_satisfied" if all_passed else "invariant_violation"
    return {
        "verdicts": {k: (v.holds if hasattr(v, "holds") else bool(v)) for k, v in verdicts.items()},
        "all_passed": all_passed,
        "outcome": computed_outcome,
        "status": "ok",
    }


REPLAY_OPS: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
    "fingerprint_stability": _op_fingerprint_stability,
    "incident_capture_dedup": _op_incident_capture_dedup,
    "store_journal_reconcile": _op_store_journal_reconcile,
    "convergence_replay_case": _op_convergence_replay_case,
    "convergence_invariants": _op_convergence_invariants,
}


def run_replay_case(
    spec: ReplayCaseSpec,
    *,
    repeats: int = 2,
    max_fixture_bytes: int = 65536,
) -> dict[str, Any]:
    """Execute allowlisted replay operation against defect and corrected anchors.

    Runs each anchor at least twice to test determinism and returns discriminating outcome.
    Derives outcome strictly from executed behavior.
    """
    if spec.replay_op not in REPLAY_OPS:
        raise ReplayOpNotAllowedError(f"Replay op {spec.replay_op!r} is not allowed")

    _validate_anchor_safety(spec.defect_anchor)
    _validate_anchor_safety(spec.corrected_anchor)
    _check_fixture_size(spec.defect_anchor, max_bytes=max_fixture_bytes)
    _check_fixture_size(spec.corrected_anchor, max_bytes=max_fixture_bytes)

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

    defect_outcome = defect_results[0].get("outcome")
    corrected_outcome = corrected_results[0].get("outcome")

    declared_defect = spec.defect_anchor.get("outcome")
    declared_corrected = spec.corrected_anchor.get("outcome")
    defect_diverged = (declared_defect is not None and declared_defect != defect_outcome)
    corrected_diverged = (declared_corrected is not None and declared_corrected != corrected_outcome)

    discriminating = bool(
        deterministic
        and not defect_diverged
        and not corrected_diverged
        and (defect_outcome != corrected_outcome)
    )

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
    elif defect_diverged:
        reason = "defect_anchor_outcome_mismatch"
    elif corrected_diverged:
        reason = "corrected_anchor_outcome_mismatch"
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
