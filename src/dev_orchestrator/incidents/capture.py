"""Restart-safe incident packet capture and semantic deduplication."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from dev_orchestrator.incidents.candidate import generate_candidate
from dev_orchestrator.incidents.fingerprint import incident_fingerprint
from dev_orchestrator.incidents.policy import resolve_incident_policy
from dev_orchestrator.incidents.store import load_incident_store
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

    Idempotent for a (fingerprint, occurrence_key) pair.
    Repeated semantic occurrences increase family recurrence count and bounded evidence references
    without creating duplicate packet files.
    """
    runtime = Path(runtime_root)
    policy = resolve_incident_policy(config)
    if not policy.get("enabled"):
        return {"captured": False, "reason": "incident_system_disabled"}

    fp = incident_fingerprint(semantic)
    store = load_incident_store(runtime)
    store.reconcile_journal()

    # Read current state
    current_index = store.reload()
    families = current_index.get("families") or {}

    max_refs = int(policy.get("max_evidence_references", 10))

    if fp in families:
        family = copy.deepcopy(families[fp])
        occurrences = family.setdefault("occurrences", [])
        if occurrence_key in occurrences:
            # Idempotent: already recorded this occurrence
            return family

        # Update existing family
        def _update_family(idx: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
            fam = idx.setdefault("families", {}).setdefault(fp, family)
            fam["recurrence_count"] = int(fam.get("recurrence_count", 1)) + 1
            fam["last_seen_at"] = utc_now_iso()
            occs = fam.setdefault("occurrences", [])
            if occurrence_key not in occs:
                occs.append(occurrence_key)

            ev_refs = fam.setdefault("evidence_references", [])
            ev_ref_entry = {
                "occurrence_key": occurrence_key,
                "recorded_at": utc_now_iso(),
                "evidence_hash": evidence.get("evidence_hash") if isinstance(evidence, dict) else None,
            }
            ev_refs.append(ev_ref_entry)
            if len(ev_refs) > max_refs:
                dropped = len(ev_refs) - max_refs
                fam["evidence_references"] = ev_refs[-max_refs:]
                fam["dropped_evidence_count"] = int(fam.get("dropped_evidence_count", 0)) + dropped
            return idx, {}

        updated_index, _ = store.execute_txn("update_incident_family", _update_family)
        return updated_index["families"][fp]

    # First occurrence: create immutable packet and initial family
    packet_id = f"pkt-{uuid4().hex[:12]}"
    packet_data = {
        "packet_id": packet_id,
        "schema_version": 1,
        "project_id": project_id,
        "task_id": task_id,
        "classification": classification,
        "fingerprint": fp,
        "semantic": copy.deepcopy(semantic),
        "evidence": copy.deepcopy(evidence or {}),
        "occurrence_key": occurrence_key,
        "captured_at": utc_now_iso(),
    }

    family_id = f"fam-{fp[:12]}"
    ev_refs = [{
        "occurrence_key": occurrence_key,
        "recorded_at": packet_data["captured_at"],
        "evidence_hash": (evidence or {}).get("evidence_hash"),
    }]

    new_family = {
        "family_id": family_id,
        "fingerprint": fp,
        "classification": classification,
        "project_id": project_id,
        "task_id": task_id,
        "first_seen_at": packet_data["captured_at"],
        "last_seen_at": packet_data["captured_at"],
        "recurrence_count": 1,
        "occurrences": [occurrence_key],
        "packet_id": packet_id,
        "packet_ids": [packet_id],
        "evidence_references": ev_refs,
        "dropped_evidence_count": 0,
        "candidate_id": None,
    }

    packet_rel = f"packets/{packet_id}.json"
    side_files = {packet_rel: json.dumps(packet_data, indent=2)}

    # Optionally auto-synthesize candidate test
    cand_id = None
    if policy.get("auto_synthesis"):
        cand_id = f"cand-{fp[:12]}"
        new_family["candidate_id"] = cand_id

    def _create_family(idx: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        fams = idx.setdefault("families", {})
        fams[fp] = new_family
        return idx, side_files

    updated_index, _ = store.execute_txn("create_incident_family", _create_family)

    if cand_id:
        generate_candidate(
            runtime,
            candidate_id=cand_id,
            source_incidents=[packet_id],
            preconditions=[f"lifecycle_class={semantic.get('lifecycle_class')}"],
            trigger_sequence=[f"occurrence_key={occurrence_key}"],
            invariant=semantic.get("invariant_identifier") or "progress_must_not_stall",
            invalid_outcome=classification,
            fixture_description="synthetic_deterministic_fixture",
            oracle_description="assert_deterministic_invariant",
            failing_fixture_anchor={"fingerprint": fp, "classification": classification},
            corrected_fixture_anchor={"expected_recovery": True},
            generated_by="auto_harvest",
        )

    return updated_index["families"][fp]
