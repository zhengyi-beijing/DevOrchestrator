"""Candidate independent review recording and validation binding to pre-review digest."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from dev_orchestrator.incidents.store import load_incident_store
from dev_orchestrator.storage.json_store import read_json, utc_now_iso


def record_candidate_review(
    runtime_root: Path | str,
    candidate_id: str,
    reviewer_id: str,
    verdict: str,
    notes: str = "",
) -> dict[str, Any]:
    """Record an independent review bound to the current five-gate digest and candidate content."""
    runtime = Path(runtime_root)
    cand_dir = runtime / "incident-packets" / "candidates" / candidate_id
    manifest_file = cand_dir / "manifest.json"
    snapshot_file = cand_dir / "gate_snapshot.json"
    source_file = cand_dir / f"test_candidate_{candidate_id}.py"

    if not snapshot_file.is_file():
        raise ValueError(f"Cannot record review: five-gate snapshot missing for candidate {candidate_id}")
    if not manifest_file.is_file() or not source_file.is_file():
        raise FileNotFoundError(f"Candidate files missing for {candidate_id}")

    manifest = read_json(manifest_file, {})
    snapshot = read_json(snapshot_file, {})
    content = source_file.read_text(encoding="utf-8")
    content_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()

    generated_by = str(manifest.get("generated_by") or "")
    if reviewer_id == generated_by:
        raise ValueError(f"Independent review rejected: reviewer_id ({reviewer_id}) matches generated_by ({generated_by})")

    pre_review_digest = snapshot.get("pre_review_digest")
    if not pre_review_digest:
        raise ValueError("Snapshot is missing pre_review_digest")

    norm_verdict = str(verdict or "").strip().lower()
    if norm_verdict not in {"accept", "accepted", "reject", "rejected"}:
        raise ValueError(f"Invalid review verdict: {verdict!r}")

    review_id = f"rev-{uuid4().hex[:12]}"
    review_record: dict[str, Any] = {
        "review_id": review_id,
        "candidate_id": candidate_id,
        "reviewer_id": reviewer_id,
        "verdict": norm_verdict,
        "notes": notes,
        "candidate_content_sha256": content_sha,
        "pre_review_digest": pre_review_digest,
        "reviewed_at": utc_now_iso(),
    }

    review_rel = f"candidates/{candidate_id}/review.json"
    store = load_incident_store(runtime)

    def _persist_review(idx: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        cands = idx.setdefault("candidates", {})
        cinfo = cands.setdefault(candidate_id, {})
        cinfo["review_id"] = review_id
        cinfo["reviewer_id"] = reviewer_id
        cinfo["review_verdict"] = norm_verdict
        return idx, {review_rel: json.dumps(review_record, indent=2)}

    store.execute_txn("record_candidate_review", _persist_review)
    return review_record


def resolve_candidate_review(
    runtime_root: Path | str,
    candidate_id: str,
) -> dict[str, Any]:
    """Resolve current independent review validity against current candidate content and gate snapshot."""
    runtime = Path(runtime_root)
    cand_dir = runtime / "incident-packets" / "candidates" / candidate_id
    manifest_file = cand_dir / "manifest.json"
    snapshot_file = cand_dir / "gate_snapshot.json"
    source_file = cand_dir / f"test_candidate_{candidate_id}.py"
    review_file = cand_dir / "review.json"

    if not review_file.is_file():
        return {
            "accepted": False,
            "stale": False,
            "review_id": None,
            "reviewer_id": None,
            "reason": "review_missing",
        }

    review = read_json(review_file, None)
    if not isinstance(review, dict):
        return {
            "accepted": False,
            "stale": False,
            "review_id": None,
            "reviewer_id": None,
            "reason": "review_unreadable",
        }

    review_id = review.get("review_id")
    reviewer_id = review.get("reviewer_id")
    verdict = str(review.get("verdict") or "").lower()

    manifest = read_json(manifest_file, {}) if manifest_file.is_file() else {}
    generated_by = str(manifest.get("generated_by") or "")
    if reviewer_id == generated_by:
        return {
            "accepted": False,
            "stale": False,
            "review_id": review_id,
            "reviewer_id": reviewer_id,
            "reason": "self_review_rejected",
        }

    # Verify content hash has not changed
    if source_file.is_file():
        current_content_sha = hashlib.sha256(source_file.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        if current_content_sha != review.get("candidate_content_sha256"):
            return {
                "accepted": False,
                "stale": True,
                "review_id": review_id,
                "reviewer_id": reviewer_id,
                "reason": "candidate_content_changed",
            }
    else:
        return {
            "accepted": False,
            "stale": True,
            "review_id": review_id,
            "reviewer_id": reviewer_id,
            "reason": "candidate_source_missing",
        }

    # Verify five-gate pre_review_digest has not changed
    if snapshot_file.is_file():
        snapshot = read_json(snapshot_file, {})
        current_digest = snapshot.get("pre_review_digest")
        if current_digest != review.get("pre_review_digest"):
            return {
                "accepted": False,
                "stale": True,
                "review_id": review_id,
                "reviewer_id": reviewer_id,
                "reason": "pre_review_digest_changed",
            }
    else:
        return {
            "accepted": False,
            "stale": True,
            "review_id": review_id,
            "reviewer_id": reviewer_id,
            "reason": "gate_snapshot_missing",
        }

    is_accepted = verdict in {"accept", "accepted"}
    return {
        "accepted": is_accepted,
        "stale": False,
        "review_id": review_id,
        "reviewer_id": reviewer_id,
        "reason": "accepted" if is_accepted else "verdict_rejected",
    }
