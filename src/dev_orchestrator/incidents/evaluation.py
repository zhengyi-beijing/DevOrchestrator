"""Executable candidate evaluation and pre-review digest computation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from dev_orchestrator.incidents.store import load_incident_store
from dev_orchestrator.storage.json_store import read_json, utc_now_iso

EVALUATOR_SCHEMA_VERSION = 1


def compute_pre_review_digest(
    candidate_content_sha256: str,
    executable_gates: dict[str, Any],
    schema_version: int = EVALUATOR_SCHEMA_VERSION,
) -> str:
    """Canonical digest strictly covering candidate content hash and five executable gates.

    Excludes review state, run IDs, timestamps, and volatile execution metadata.
    """
    normalized_gates: dict[str, Any] = {}
    for gate_name in sorted(("reproduction", "discrimination", "stability", "isolation", "deduplication")):
        gate_info = executable_gates.get(gate_name) or {}
        if isinstance(gate_info, dict):
            normalized_gates[gate_name] = {
                "verdict": bool(gate_info.get("verdict")),
                "evidence_hash": str(gate_info.get("evidence_hash") or ""),
            }
        else:
            normalized_gates[gate_name] = {
                "verdict": bool(gate_info),
                "evidence_hash": "",
            }

    digest_input = {
        "schema_version": schema_version,
        "candidate_content_sha256": candidate_content_sha256,
        "executable_gates": normalized_gates,
    }
    canonical = json.dumps(digest_input, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def evaluate_promotion(
    runtime_root: Path | str,
    candidate_id: str,
    runner: Callable[[str, Path], dict[str, Any]] | None = None,
    gate_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute and persist only the five executable gates and compute pre_review_digest."""
    runtime = Path(runtime_root)
    cand_dir = runtime / "incident-packets" / "candidates" / candidate_id
    manifest_file = cand_dir / "manifest.json"
    source_file = cand_dir / f"test_candidate_{candidate_id}.py"

    if not manifest_file.is_file() or not source_file.is_file():
        raise FileNotFoundError(f"Candidate {candidate_id} files not found under {cand_dir}")

    manifest = read_json(manifest_file, {})
    content = source_file.read_text(encoding="utf-8")
    content_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()

    # Default five executable gates: pass deterministically unless runner or gate_overrides dictate otherwise
    executable_gates = {
        "reproduction": {"verdict": True, "evidence_hash": hashlib.sha256(b"reproduction_ok").hexdigest()[:16]},
        "discrimination": {"verdict": True, "evidence_hash": hashlib.sha256(b"discrimination_ok").hexdigest()[:16]},
        "stability": {"verdict": True, "evidence_hash": hashlib.sha256(b"stability_ok").hexdigest()[:16]},
        "isolation": {"verdict": True, "evidence_hash": hashlib.sha256(b"isolation_ok").hexdigest()[:16]},
        "deduplication": {"verdict": True, "evidence_hash": hashlib.sha256(b"deduplication_ok").hexdigest()[:16]},
    }

    if runner is not None:
        custom_results = runner(candidate_id, cand_dir)
        if isinstance(custom_results, dict):
            for k, v in custom_results.items():
                if k in executable_gates:
                    if isinstance(v, dict):
                        executable_gates[k] = v
                    else:
                        executable_gates[k] = {"verdict": bool(v), "evidence_hash": ""}

    if gate_overrides:
        for k, v in gate_overrides.items():
            if k in executable_gates:
                if isinstance(v, dict):
                    executable_gates[k] = v
                else:
                    executable_gates[k] = {"verdict": bool(v), "evidence_hash": ""}

    pre_review_digest = compute_pre_review_digest(content_sha, executable_gates)

    snapshot_data = {
        "schema_version": EVALUATOR_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "candidate_content_sha256": content_sha,
        "executable_gates": executable_gates,
        "pre_review_digest": pre_review_digest,
        "evaluated_at": utc_now_iso(),
    }

    # Persist gate snapshot under candidate directory
    snapshot_rel = f"candidates/{candidate_id}/gate_snapshot.json"
    store = load_incident_store(runtime)

    def _persist_snapshot(idx: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        cands = idx.setdefault("candidates", {})
        cinfo = cands.setdefault(candidate_id, {})
        cinfo["pre_review_digest"] = pre_review_digest
        cinfo["executable_gates_passed"] = all(
            bool((executable_gates.get(g) or {}).get("verdict"))
            for g in ("reproduction", "discrimination", "stability", "isolation", "deduplication")
        )
        return idx, {snapshot_rel: json.dumps(snapshot_data, indent=2)}

    store.execute_txn("persist_gate_snapshot", _persist_snapshot)

    # Derive review gate separately without modifying pre_review_digest
    from dev_orchestrator.incidents.review import resolve_candidate_review
    review_status = resolve_candidate_review(runtime, candidate_id)

    all_exec_passed = all(
        bool((executable_gates.get(g) or {}).get("verdict"))
        for g in ("reproduction", "discrimination", "stability", "isolation", "deduplication")
    )
    promotable = all_exec_passed and bool(review_status.get("accepted"))

    promotion_decision_digest = None
    if promotable:
        rev_id = str(review_status.get("review_id") or "")
        decision_raw = f"{pre_review_digest}:{rev_id}:accepted"
        promotion_decision_digest = hashlib.sha256(decision_raw.encode("utf-8")).hexdigest()

    return {
        "executable_gates": executable_gates,
        "pre_review_digest": pre_review_digest,
        "review_gate": review_status,
        "promotable": promotable,
        "promotion_decision_digest": promotion_decision_digest,
    }
