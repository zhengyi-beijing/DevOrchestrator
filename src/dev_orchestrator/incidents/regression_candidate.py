"""RegressionCandidate contract, normalization, store persistence, verification, and promotion.

Implements the versioned RegressionCandidate schema and runtime store lifecycle under
runtime/incident-packets/, with allowlisted replay verification and strict write fences.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

from dev_orchestrator.incidents.replay_runner import (
    REPLAY_OPS,
    ReplayCaseSpec,
    load_replay_case,
    run_replay_case,
)
from dev_orchestrator.incidents.policy import resolve_incident_policy
from dev_orchestrator.incidents.sanitize import sanitize_incident_payload
from dev_orchestrator.incidents.store import (
    IncidentCapacityExceededError,
    IncidentStoreFencedError,
    load_incident_store,
)
from dev_orchestrator.storage.json_store import parse_utc, read_json, utc_now_iso, write_json

REGRESSION_CANDIDATE_SCHEMA_VERSION = 1
VERIFICATION_STATUSES = frozenset({"proposed", "reproducible", "verified", "rejected", "promoted"})
REPLAYABILITY_VALUES = frozenset({"deterministic_replayable", "non_replayable", "unknown"})


@dataclass(frozen=True)
class RegressionCandidate:
    schema_version: int
    candidate_id: str
    project_id: str
    source_family_id: str
    source_failure_signature: str
    source_incident_ids: list[str]
    minimal_reproduction: dict[str, Any]
    expected_invariant: str
    actual_outcome: str
    regression_class: str
    replayability: str
    verification_status: str
    rejection_reason: str | None = None
    verification_evidence: dict[str, Any] | None = None
    verified_at: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RegressionCandidate:
        return cls(
            schema_version=int(data.get("schema_version", REGRESSION_CANDIDATE_SCHEMA_VERSION)),
            candidate_id=str(data["candidate_id"]),
            project_id=str(data["project_id"]),
            source_family_id=str(data["source_family_id"]),
            source_failure_signature=str(data.get("source_failure_signature", "")),
            source_incident_ids=list(data.get("source_incident_ids", ())),
            minimal_reproduction=dict(data.get("minimal_reproduction", {})),
            expected_invariant=str(data.get("expected_invariant", "")),
            actual_outcome=str(data.get("actual_outcome", "")),
            regression_class=str(data.get("regression_class", "")),
            replayability=str(data.get("replayability", "deterministic_replayable")),
            verification_status=str(data.get("verification_status", "proposed")),
            rejection_reason=str(data["rejection_reason"]) if data.get("rejection_reason") is not None else None,
            verification_evidence=dict(data["verification_evidence"]) if isinstance(data.get("verification_evidence"), dict) else None,
            verified_at=str(data["verified_at"]) if data.get("verified_at") is not None else None,
            created_at=str(data.get("created_at") or utc_now_iso()),
            updated_at=str(data.get("updated_at") or utc_now_iso()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "candidate_id": self.candidate_id,
            "project_id": self.project_id,
            "source_family_id": self.source_family_id,
            "source_failure_signature": self.source_failure_signature,
            "source_incident_ids": list(self.source_incident_ids),
            "minimal_reproduction": dict(self.minimal_reproduction),
            "expected_invariant": self.expected_invariant,
            "actual_outcome": self.actual_outcome,
            "regression_class": self.regression_class,
            "replayability": self.replayability,
            "verification_status": self.verification_status,
            "rejection_reason": self.rejection_reason,
            "verification_evidence": self.verification_evidence,
            "verified_at": self.verified_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def validate_regression_candidate(data: Mapping[str, Any]) -> list[str]:
    """Validate a candidate dictionary against the RegressionCandidate contract."""
    errors: list[str] = []
    if not isinstance(data, Mapping):
        return ["candidate data must be a mapping"]

    schema_version = data.get("schema_version")
    if schema_version != REGRESSION_CANDIDATE_SCHEMA_VERSION:
        errors.append(f"unsupported schema_version {schema_version}: expected {REGRESSION_CANDIDATE_SCHEMA_VERSION}")

    candidate_id = data.get("candidate_id")
    if not candidate_id or not isinstance(candidate_id, str):
        errors.append("candidate_id must be a non-empty string")

    project_id = data.get("project_id")
    if not project_id or not isinstance(project_id, str):
        errors.append("project_id must be a non-empty string")

    source_family_id = data.get("source_family_id")
    if not source_family_id or not isinstance(source_family_id, str):
        errors.append("source_family_id must be a non-empty string")

    status = data.get("verification_status")
    if status not in VERIFICATION_STATUSES:
        errors.append(f"unknown verification_status {status!r}: must be one of {sorted(VERIFICATION_STATUSES)}")

    replayability = data.get("replayability")
    if replayability not in REPLAYABILITY_VALUES:
        errors.append(f"unknown replayability {replayability!r}: must be one of {sorted(REPLAYABILITY_VALUES)}")

    if not isinstance(data.get("source_incident_ids"), (list, tuple)):
        errors.append("source_incident_ids must be a list or tuple of incident IDs")

    if not isinstance(data.get("minimal_reproduction"), Mapping):
        errors.append("minimal_reproduction must be a mapping")

    return errors


def normalize_regression_candidate(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a candidate dictionary; raises ValueError on violation."""
    errors = validate_regression_candidate(data)
    if errors:
        raise ValueError(f"Invalid RegressionCandidate:\n" + "\n".join(errors))
    cand = RegressionCandidate.from_dict(data)
    return cand.to_dict()


def _check_store_fence(runtime_root: Path | str) -> None:
    fence_path = Path(runtime_root) / "incident-packets" / "corruption-fence.json"
    if fence_path.is_file():
        raise IncidentStoreFencedError("Store is fenced due to active corruption fence")


def create_regression_candidate(
    runtime_root: Path | str,
    project_id: str,
    family_id: str,
    source_incident_ids: Sequence[str],
    corpus_case_id: str,
    expected_invariant: str,
    actual_outcome: str,
    regression_class: str,
    *,
    replay_op: str = "fingerprint_stability",
    candidate_id: str | None = None,
    replayability: str = "deterministic_replayable",
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a new regression candidate in proposed status and persist it in the store."""
    _check_store_fence(runtime_root)
    policy = resolve_incident_policy(config)
    if not policy.get("typed_replay_candidates", True):
        raise RuntimeError("typed_replay_candidates is disabled by policy")

    store = load_incident_store(runtime_root)
    cands = store.index.get("regression_candidates", {})
    if isinstance(cands, dict) and len(cands) >= int(policy.get("max_regression_candidates", 1000)):
        raise IncidentCapacityExceededError(
            f"max_regression_candidates exceeded ({len(cands)} >= {policy['max_regression_candidates']})"
        )

    cid = candidate_id or f"cand-{uuid4().hex[:12]}"
    now = utc_now_iso()

    # Determine source failure signature from store if known
    source_sig = ""
    fams = store.index.get("families", {})
    if isinstance(fams, dict):
        for sig, fam in fams.items():
            if isinstance(fam, dict) and fam.get("family_id") == family_id:
                source_sig = sig
                break
    if not source_sig:
        source_sig = family_id[4:] if family_id.startswith("fam-") else family_id

    inputs_digest = f"sha256:{hashlib.sha256(corpus_case_id.encode('utf-8')).hexdigest()[:16]}"
    minimal_reproduction = {
        "corpus_case_id": corpus_case_id,
        "replay_op": replay_op,
        "inputs_digest": inputs_digest,
    }

    cand = RegressionCandidate(
        schema_version=REGRESSION_CANDIDATE_SCHEMA_VERSION,
        candidate_id=cid,
        project_id=project_id,
        source_family_id=family_id,
        source_failure_signature=source_sig,
        source_incident_ids=list(source_incident_ids),
        minimal_reproduction=minimal_reproduction,
        expected_invariant=expected_invariant,
        actual_outcome=actual_outcome,
        regression_class=regression_class,
        replayability=replayability,
        verification_status="proposed",
        rejection_reason=None,
        verification_evidence=None,
        verified_at=None,
        created_at=now,
        updated_at=now,
    )
    cand_dict = normalize_regression_candidate(cand.to_dict())

    def _create_mutator(idx: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        if "regression_candidates" not in idx:
            idx["regression_candidates"] = {}
        idx["regression_candidates"][cid] = {
            "candidate_id": cid,
            "family_id": family_id,
            "verification_status": "proposed",
            "replayability": replayability,
            "rejection_reason": None,
            "verification_evidence": None,
        }
        rel_path = f"candidates/{cid}/regression_candidate.json"
        return idx, {rel_path: json.dumps(cand_dict, indent=2)}

    store.execute_txn("create_regression_candidate", _create_mutator)
    return cand_dict


def resolve_regression_candidate(runtime_root: Path | str, candidate_id: str) -> dict[str, Any]:
    """Resolve a persisted regression candidate by ID."""
    cand_file = Path(runtime_root) / "incident-packets" / "candidates" / candidate_id / "regression_candidate.json"
    if not cand_file.is_file():
        raise KeyError(f"Regression candidate {candidate_id} not found at {cand_file}")
    data = read_json(cand_file)
    return normalize_regression_candidate(data)


def verify_regression_candidate(
    runtime_root: Path | str,
    candidate_id: str,
    *,
    corpus_dir: Path | str | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute replay verification for candidate and durably persist outcome."""
    _check_store_fence(runtime_root)

    cand_file = Path(runtime_root) / "incident-packets" / "candidates" / candidate_id / "regression_candidate.json"
    if not cand_file.is_file():
        raise KeyError(f"Candidate {candidate_id} not found at {cand_file}")

    raw_text = cand_file.read_text(encoding="utf-8")
    cand_data = json.loads(raw_text)
    errors = validate_regression_candidate(cand_data)
    if errors:
        raise ValueError(f"Corrupt or invalid candidate record: {errors}")

    policy = resolve_incident_policy(config)
    now = utc_now_iso()

    # Step 12 check: legacy synthetic candidate cand-7dd84495b777 is permanently ineligible
    min_rep = cand_data.get("minimal_reproduction", {})
    corpus_case_id = min_rep.get("corpus_case_id") or cand_data.get("corpus_case_id", "")
    if candidate_id == "cand-7dd84495b777" or corpus_case_id == "synthetic_fixture_mode":
        status = "rejected"
        rejection_reason = "vacuous_synthetic_oracle_ineligible"
        verification_evidence = {
            "reason": "vacuous_synthetic_oracle_ineligible",
            "evaluated_at": now,
        }
    else:
        # Resolve corpus directory from stable anchor
        if corpus_dir is None:
            repo_root = Path(__file__).resolve().parents[3]
            c_dir = (repo_root / "tests_py" / "data" / "p20_2_replay_corpus").resolve()
            if not c_dir.is_dir():
                c_dir = Path("tests_py/data/p20_2_replay_corpus").resolve()
        else:
            c_dir = Path(corpus_dir).resolve()

        if c_dir.is_dir():
            corpus_files = list(c_dir.glob("*.json"))
            max_cases = int(policy.get("max_replay_corpus_cases", 500))
            if len(corpus_files) > max_cases:
                raise IncidentCapacityExceededError(
                    f"max_replay_corpus_cases exceeded ({len(corpus_files)} > {max_cases})"
                )

        case_file = c_dir / (f"{corpus_case_id}.json" if not str(corpus_case_id).endswith(".json") else str(corpus_case_id))
        if not case_file.is_file():
            status = "rejected"
            rejection_reason = f"missing_corpus_case_{corpus_case_id}"
            verification_evidence = {"error": f"Case file {case_file} not found"}
        else:
            spec = load_replay_case(case_file)
            # Replay op from candidate override if specified
            op_override = min_rep.get("replay_op")
            if op_override and op_override in REPLAY_OPS:
                spec = ReplayCaseSpec(
                    case_id=spec.case_id,
                    replay_op=op_override,
                    defect_anchor=spec.defect_anchor,
                    corrected_anchor=spec.corrected_anchor,
                    expected_invariant=spec.expected_invariant,
                    expected_defect_outcome=spec.expected_defect_outcome,
                    cpf_scenarios=spec.cpf_scenarios,
                    source_failure_signature=spec.source_failure_signature,
                    regression_class=spec.regression_class,
                    notes=spec.notes,
                )

            repeats = int(policy.get("max_replay_repeats", 2))
            max_bytes = int(policy.get("max_replay_fixture_bytes", 65536))
            replay_res = run_replay_case(spec, repeats=repeats, max_fixture_bytes=max_bytes)
            verification_evidence = replay_res

            if not replay_res["deterministic"]:
                status = "rejected"
                rejection_reason = "non_deterministic_replay"
            elif not replay_res["discriminating"]:
                status = "rejected"
                rejection_reason = replay_res.get("reason") or "vacuous_oracle_non_discriminating"
            else:
                defect_out = replay_res["defect_outcome"]
                actual_out = cand_data.get("actual_outcome")
                # Non-vacuous discrimination established
                if defect_out == actual_out or (isinstance(defect_out, str) and isinstance(actual_out, str) and defect_out.lower() == actual_out.lower()):
                    status = "verified"
                    rejection_reason = None
                else:
                    status = "rejected"
                    rejection_reason = "defect_anchor_outcome_mismatch"

    cand_data["verification_status"] = status
    cand_data["rejection_reason"] = rejection_reason
    cand_data["verification_evidence"] = verification_evidence
    cand_data["verified_at"] = now if status == "verified" else None
    cand_data["updated_at"] = now

    store = load_incident_store(runtime_root)

    def _verify_mutator(idx: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        if "regression_candidates" not in idx:
            idx["regression_candidates"] = {}
        idx["regression_candidates"][candidate_id] = {
            "candidate_id": candidate_id,
            "family_id": cand_data.get("source_family_id"),
            "verification_status": status,
            "replayability": cand_data.get("replayability"),
            "rejection_reason": rejection_reason,
            "verification_evidence": verification_evidence.get("outcome_digest") if isinstance(verification_evidence, dict) else None,
        }
        cand_rel = f"candidates/{candidate_id}/regression_candidate.json"
        verif_rel = f"candidates/{candidate_id}/verification.json"
        verif_record = {
            "schema_version": 1,
            "candidate_id": candidate_id,
            "verification_status": status,
            "rejection_reason": rejection_reason,
            "verification_evidence": verification_evidence,
            "verified_at": cand_data.get("verified_at"),
        }
        return idx, {
            cand_rel: json.dumps(cand_data, indent=2),
            verif_rel: json.dumps(verif_record, indent=2),
        }

    store.execute_txn("verify_regression_candidate", _verify_mutator)
    return cand_data


def promote_verified_regression(
    runtime_root: Path | str,
    candidate_id: str,
    *,
    destination: Path | str | None = None,
) -> dict[str, Any]:
    """Promote a verified regression candidate into runtime store only.

    Enforces path fence (CPF-05): refuses writes outside runtime/incident-packets,
    specifically refusing tests_py or tracked repository paths.
    """
    _check_store_fence(runtime_root)
    allowed_base = (Path(runtime_root) / "incident-packets").resolve()

    if destination is not None:
        dest_p = Path(destination).resolve()
        dest_str = str(dest_p).replace("\\", "/")
        if "tests_py" in dest_str or not dest_p.is_relative_to(allowed_base):
            raise ValueError(
                f"Promotion to destination '{destination}' is refused: automatic promotion writes only beneath runtime/incident-packets"
            )

    cand_file = allowed_base / "candidates" / candidate_id / "regression_candidate.json"
    if not cand_file.is_file():
        raise KeyError(f"Candidate {candidate_id} not found at {cand_file}")

    cand_data = json.loads(cand_file.read_text(encoding="utf-8"))
    if cand_data.get("verification_status") != "verified":
        reason = cand_data.get("rejection_reason") or cand_data.get("verification_status")
        raise ValueError(
            f"Candidate {candidate_id} is not verified (status={cand_data.get('verification_status')}, reason={reason}) and cannot be promoted"
        )

    now = utc_now_iso()
    cand_data["verification_status"] = "promoted"
    cand_data["updated_at"] = now

    store = load_incident_store(runtime_root)

    def _promote_mutator(idx: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        if "regression_candidates" not in idx:
            idx["regression_candidates"] = {}
        entry = idx["regression_candidates"].setdefault(candidate_id, {})
        entry["verification_status"] = "promoted"
        entry["promoted_at"] = now
        cand_rel = f"candidates/{candidate_id}/regression_candidate.json"
        return idx, {cand_rel: json.dumps(cand_data, indent=2)}

    store.execute_txn("promote_verified_regression", _promote_mutator)
    return cand_data
