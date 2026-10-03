"""Restart-safe incident packet capture and semantic deduplication."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from dev_orchestrator.incidents.candidate import generate_candidate
from dev_orchestrator.incidents.fingerprint import failure_signature
from dev_orchestrator.incidents.policy import resolve_incident_policy
from dev_orchestrator.incidents.sanitize import build_evidence_reference, sanitize_incident_payload
from dev_orchestrator.incidents.store import IncidentCapacityExceededError, load_incident_store
from dev_orchestrator.storage.json_store import utc_now_iso


def capture_incident(
    runtime_root: Path | str,
    project_id: str,
    task_id: str | None,
    classification: str,
    semantic: dict[str, Any],
    evidence: dict[str, Any] | None,
    occurrence_key: str,
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Capture an incident packet or update an existing incident family.

    Idempotent for a (failure_signature, occurrence_key) pair.
    Deduplication decision is made inside the inter-process lock.
    """
    runtime = Path(runtime_root)
    policy = resolve_incident_policy(config)
    if not policy.get("enabled"):
        return {"captured": False, "reason": "incident_system_disabled"}

    fp = failure_signature(semantic)
    clean_semantic = sanitize_incident_payload(semantic)
    clean_evidence = sanitize_incident_payload(evidence) if evidence else None

    store = load_incident_store(runtime)

    max_refs = int(policy.get("max_evidence_references", 10))
    max_families = int(policy.get("max_families", 1000))
    max_occ_per_fam = int(policy.get("max_occurrences_per_family", 5000))
    max_total_occ = int(policy.get("max_total_occurrences", 50000))

    cand_id_to_generate: str | None = None
    packet_id_for_cand: str | None = None

    def _capture_mutator(idx: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        nonlocal cand_id_to_generate, packet_id_for_cand
        fams = idx.setdefault("families", {})
        total_occurrences = sum(len(f.get("occurrences", [])) for f in fams.values() if isinstance(f, dict))

        if fp in fams:
            fam = fams[fp]
            occs = fam.setdefault("occurrences", [])
            if occurrence_key in occs:
                # Idempotent: already recorded this occurrence key
                return idx, {}

            if len(occs) >= max_occ_per_fam:
                raise IncidentCapacityExceededError(
                    f"max_occurrences_per_family exceeded ({len(occs)} >= {max_occ_per_fam})"
                )
            if total_occurrences >= max_total_occ:
                raise IncidentCapacityExceededError(
                    f"max_total_occurrences exceeded ({total_occurrences} >= {max_total_occ})"
                )

            fam["recurrence_count"] = int(fam.get("recurrence_count", len(occs))) + 1
            fam["last_seen_at"] = utc_now_iso()
            occs.append(occurrence_key)

            ev_refs = fam.setdefault("evidence_references", [])
            ev_ref_entry = build_evidence_reference(occurrence_key, clean_evidence, recorded_at=fam["last_seen_at"])
            ev_refs.append(ev_ref_entry)
            if len(ev_refs) > max_refs:
                dropped = len(ev_refs) - max_refs
                fam["evidence_references"] = ev_refs[-max_refs:]
                fam["dropped_evidence_count"] = int(fam.get("dropped_evidence_count", 0)) + dropped
            return idx, {}

        # First occurrence: create immutable packet and initial family
        if len(fams) >= max_families:
            raise IncidentCapacityExceededError(
                f"max_families exceeded ({len(fams)} >= {max_families})"
            )
        if total_occurrences >= max_total_occ:
            raise IncidentCapacityExceededError(
                f"max_total_occurrences exceeded ({total_occurrences} >= {max_total_occ})"
            )

        packet_id = f"pkt-{uuid4().hex[:12]}"
        family_id = f"fam-{fp[:12]}"
        now_ts = utc_now_iso()

        packet_data = {
            "packet_id": packet_id,
            "schema_version": 1,
            "project_id": project_id,
            "task_id": task_id,
            "classification": classification,
            "fingerprint": fp,
            "failure_signature": fp,
            "failure_family": family_id,
            "failure_class": clean_semantic.get("failure_class", classification),
            "semantic": copy.deepcopy(clean_semantic),
            "evidence": copy.deepcopy(clean_evidence or {}),
            "occurrence_key": occurrence_key,
            "captured_at": now_ts,
            "first_seen_at": now_ts,
            "last_seen_at": now_ts,
            "occurrence_count": 1,
        }

        new_family = {
            "family_id": family_id,
            "fingerprint": fp,
            "failure_signature": fp,
            "classification": classification,
            "failure_class": clean_semantic.get("failure_class", classification),
            "project_id": project_id,
            "task_id": task_id,
            "first_seen_at": now_ts,
            "last_seen_at": now_ts,
            "recurrence_count": 1,
            "occurrences": [occurrence_key],
            "packet_id": packet_id,
            "packet_ids": [packet_id],
            "evidence_references": [build_evidence_reference(occurrence_key, clean_evidence, recorded_at=now_ts)],
            "dropped_evidence_count": 0,
            "candidate_id": None,
        }

        packet_rel = f"packets/{packet_id}.json"
        side_files = {packet_rel: json.dumps(packet_data, indent=2)}

        if policy.get("auto_synthesis"):
            cand_id = f"cand-{fp[:12]}"
            new_family["candidate_id"] = cand_id
            cand_id_to_generate = cand_id
            packet_id_for_cand = packet_id

        fams[fp] = new_family
        return idx, side_files

    updated_index, _ = store.execute_txn("capture_incident", _capture_mutator)

    if cand_id_to_generate and packet_id_for_cand:
        try:
            generate_candidate(
                runtime,
                candidate_id=cand_id_to_generate,
                source_incidents=[packet_id_for_cand],
                preconditions=[f"lifecycle_class={clean_semantic.get('lifecycle_class')}"],
                trigger_sequence=[f"occurrence_key={occurrence_key}"],
                invariant=clean_semantic.get("invariant_identifier") or "progress_must_not_stall",
                invalid_outcome=classification,
                fixture_description="synthetic_deterministic_fixture",
                oracle_description="assert_deterministic_invariant",
                failing_fixture_anchor={"fingerprint": fp, "classification": classification},
                corrected_fixture_anchor={"expected_recovery": True},
                generated_by="auto_harvest",
            )
        except Exception:
            pass

    return updated_index["families"][fp]
