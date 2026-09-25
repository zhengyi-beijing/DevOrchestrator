"""Safe destination-free operator candidate promotion into verified regression owner repository."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from dev_orchestrator.config import load_projects_config
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.incidents.regression_owner import resolve_regression_owner
from dev_orchestrator.incidents.review import resolve_candidate_review
from dev_orchestrator.incidents.store import load_incident_store
from dev_orchestrator.storage.json_store import read_json, utc_now_iso


def resolve_promotion_state(
    runtime_root: Path | str,
    candidate_id: str,
) -> dict[str, Any]:
    """Derive six-gate promotion state without mutating pre_review_digest."""
    runtime = Path(runtime_root)
    cand_dir = runtime / "incident-packets" / "candidates" / candidate_id
    snapshot_file = cand_dir / "gate_snapshot.json"

    if not snapshot_file.is_file():
        return {
            "executable_gates": {},
            "pre_review_digest": None,
            "review_gate": {"accepted": False, "stale": False, "reason": "snapshot_missing"},
            "promotable": False,
            "promotion_decision_digest": None,
        }

    snapshot = read_json(snapshot_file, {})
    executable_gates = snapshot.get("executable_gates") or {}
    pre_review_digest = snapshot.get("pre_review_digest")

    all_exec_passed = all(
        bool((executable_gates.get(g) or {}).get("verdict"))
        for g in ("reproduction", "discrimination", "stability", "isolation", "deduplication")
    )

    review_gate = resolve_candidate_review(runtime, candidate_id)
    promotable = all_exec_passed and bool(review_gate.get("accepted"))

    promotion_decision_digest = None
    if promotable and pre_review_digest:
        rev_id = str(review_gate.get("review_id") or "")
        decision_raw = f"{pre_review_digest}:{rev_id}:accepted"
        promotion_decision_digest = hashlib.sha256(decision_raw.encode("utf-8")).hexdigest()

    return {
        "executable_gates": executable_gates,
        "pre_review_digest": pre_review_digest,
        "review_gate": review_gate,
        "promotable": promotable,
        "promotion_decision_digest": promotion_decision_digest,
    }


def promote_candidate(
    runtime_root: Path | str,
    candidate_id: str,
    config_path: Path | str,
    expected_head: str | None = None,
    expected_branch: str | None = None,
    _post_write_hook: Any | None = None,
) -> dict[str, Any]:
    """Promote an accepted regression candidate to the verified regression owner repository."""
    runtime = Path(runtime_root)
    store = load_incident_store(runtime)

    with store.lock:
        if store.index.get("promotion_blocked"):
            return {
                "promoted": False,
                "code": "promotion_blocked",
                "reason": store.index.get("promotion_blocked_reason") or "promotions blocked due to prior unclean rollback",
                "candidate_id": candidate_id,
            }

        config = load_projects_config(config_path)
        owner = resolve_regression_owner(config)

        if not owner.get("verified"):
            return {
                "promoted": False,
                "code": "unverified_owner",
                "reason": f"unverified_owner: {owner.get('reason')}",
                "candidate_id": candidate_id,
            }

        repo_path: Path = owner["repo_path"]
        tests_root: Path = owner["tests_root"]

        # Revalidate 6-gate promotion state
        state = resolve_promotion_state(runtime, candidate_id)
        if not state.get("promotable"):
            rev_gate = state.get("review_gate") or {}
            if not rev_gate.get("accepted"):
                return {
                    "promoted": False,
                    "code": "missing_or_stale_review",
                    "reason": f"missing_or_stale_review: {rev_gate.get('reason')}",
                    "candidate_id": candidate_id,
                    "state": state,
                }
            return {
                "promoted": False,
                "code": "incomplete_executable_gates",
                "reason": "incomplete_executable_gates",
                "candidate_id": candidate_id,
                "state": state,
            }

        # Verify repository cleanliness and branch/HEAD anchors
        truth = read_repository_truth(repo_path)
        if truth.dirty:
            return {
                "promoted": False,
                "code": "repository_dirty",
                "reason": "dirty_repository",
                "candidate_id": candidate_id,
                "uncommitted_files": truth.dirty_entries,
            }

        if expected_branch and truth.branch != expected_branch:
            return {
                "promoted": False,
                "code": "anchor_mismatch",
                "reason": f"anchor_mismatch_branch: current {truth.branch} != expected {expected_branch}",
                "candidate_id": candidate_id,
            }

        if expected_head and not truth.head.startswith(expected_head):
            return {
                "promoted": False,
                "code": "anchor_mismatch",
                "reason": f"anchor_mismatch_head: current {truth.head} != expected {expected_head}",
                "candidate_id": candidate_id,
            }

        # Verify source candidate test
        source_file = runtime / "incident-packets" / "candidates" / candidate_id / f"test_candidate_{candidate_id}.py"
        if not source_file.is_file():
            return {
                "promoted": False,
                "code": "candidate_source_not_found",
                "reason": f"candidate_source_not_found: {source_file}",
                "candidate_id": candidate_id,
            }

        # Create-new destination under owner's tests_root
        target_name = f"test_promoted_{candidate_id.replace('-', '_')}.py"
        target_path = tests_root / target_name
        if target_path.exists():
            return {
                "promoted": False,
                "code": "destination_collision",
                "reason": f"destination_collision: {target_path} already exists",
                "candidate_id": candidate_id,
            }

        initial_status_hash = truth.status_hash
        content = source_file.read_text(encoding="utf-8")

        write_error: Exception | None = None
        try:
            target_path.write_text(content, encoding="utf-8")
            if callable(_post_write_hook):
                _post_write_hook(target_path)
            if not target_path.is_file() or target_path.read_text(encoding="utf-8") != content:
                raise IOError(f"post-write verification failed for {target_path}")
        except Exception as exc:
            write_error = exc

        if write_error is not None:
            if target_path.exists():
                try:
                    target_path.unlink(missing_ok=True)
                except Exception:
                    pass

            restored_truth = read_repository_truth(repo_path)
            restored_clean = (not target_path.exists()) and (restored_truth.status_hash == initial_status_hash)

            if not restored_clean:
                def _block_promotions(idx: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
                    idx["promotion_blocked"] = True
                    idx["promotion_blocked_reason"] = f"unclean rollback residue: {target_path}"
                    idx["promotion_blocked_at"] = utc_now_iso()
                    idx["promotion_blocked_candidate"] = candidate_id
                    return idx, {}

                store.execute_txn("block_promotions", _block_promotions)

                gate_id = f"owner_gate:promotion_failed_dirty:{candidate_id}"
                return {
                    "promoted": False,
                    "code": "failed_dirty",
                    "reason": f"destination_write_failed_dirty: {write_error}",
                    "candidate_id": candidate_id,
                    "residue": str(target_path),
                    "owner_gate": {
                        "gate_id": gate_id,
                        "status": "pending_owner",
                        "residue": str(target_path),
                        "reason": f"unclean rollback residue remaining at {target_path}",
                    },
                }

            return {
                "promoted": False,
                "code": "destination_write_failed",
                "reason": f"destination_write_failed: {write_error}",
                "candidate_id": candidate_id,
            }

        def _mark_promoted(idx: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
            cands = idx.setdefault("candidates", {})
            cinfo = cands.setdefault(candidate_id, {})
            cinfo["promoted"] = True
            cinfo["promoted_at"] = utc_now_iso()
            cinfo["target_path"] = str(target_path)
            cinfo["promotion_decision_digest"] = state.get("promotion_decision_digest")
            return idx, {}

        store.execute_txn("mark_promoted", _mark_promoted)

        return {
            "promoted": True,
            "code": "promoted",
            "candidate_id": candidate_id,
            "target_path": str(target_path),
            "promoted_file": str(target_path),
            "reason": "promoted_uncommitted",
        }
