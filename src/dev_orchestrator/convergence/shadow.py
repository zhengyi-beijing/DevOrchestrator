"""Read-only shadow adapter and fenced evaluation runner.

Consumes legacy evidence read-only, emits proposed v0 decisions with source evidence
digests, and enforces strict output fences prohibiting mutation of live/canonical state.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from dev_orchestrator.convergence.evaluator import Decision, decide
from dev_orchestrator.convergence.evidence import (
    EvidenceItem,
    EvidenceSnapshot,
    compute_item_digest,
    snapshot_digest,
)
from dev_orchestrator.convergence.policy import build_policy
from dev_orchestrator.convergence.work_record import (
    WorkRecord,
    validate_work_record,
    work_record_digest,
)

SHADOW_NAMESPACE = "p17-shadow"


class FenceViolation(RuntimeError):
    """Raised when an operation attempts to write to a fenced or protected location."""


class ReadOnlyEvidenceRoot:
    """Wrapper around an evidence directory enforcing strictly read-only access."""

    def __init__(self, root_path: str | Path) -> None:
        self._root = Path(root_path).resolve()
        if not self._root.exists():
            raise FileNotFoundError(f"Evidence root does not exist: {self._root}")

    @property
    def path(self) -> Path:
        return self._root

    def read_file(self, relative_path: str) -> str:
        target = (self._root / relative_path).resolve()
        if not str(target).startswith(str(self._root)):
            raise FenceViolation(f"Path traversal outside evidence root: {relative_path}")
        if not target.exists():
            raise FileNotFoundError(f"File not found in evidence root: {relative_path}")
        with open(target, "r", encoding="utf-8") as f:
            return f.read()

    def read_json(self, relative_path: str) -> dict[str, Any]:
        content = self.read_file(relative_path)
        return json.loads(content)

    def write_file(self, relative_path: str, content: str) -> None:
        raise FenceViolation(f"Attempted write to read-only evidence root: {relative_path}")

    def delete_file(self, relative_path: str) -> None:
        raise FenceViolation(f"Attempted delete in read-only evidence root: {relative_path}")


def validate_shadow_sink(
    output_dir: str | Path | None,
    *,
    evidence_root: str | Path,
    repo_root: str | Path,
    canonical_state_root: str | Path | None = None,
) -> Path:
    """Validate that output_dir resolves strictly inside runtime/p17-shadow.

    Fails closed if unset, inside evidence root, or inside canonical state root.
    """
    if output_dir is None:
        raise FenceViolation("Shadow output directory cannot be unset when writing to disk")

    out_path = Path(output_dir).resolve()
    ev_path = Path(evidence_root).resolve()
    repo_path = Path(repo_root).resolve()
    allowed_base = (repo_path / "runtime" / SHADOW_NAMESPACE).resolve()

    # Reject if inside evidence root
    if str(out_path).startswith(str(ev_path)):
        raise FenceViolation(f"Shadow sink cannot be located inside evidence root: {out_path}")

    # Reject if inside canonical state root
    if canonical_state_root:
        canon_path = Path(canonical_state_root).resolve()
        if str(out_path).startswith(str(canon_path)):
            raise FenceViolation(f"Shadow sink cannot be located inside canonical state root: {out_path}")

    # Must resolve inside allowed_base
    if not (str(out_path) == str(allowed_base) or str(out_path).startswith(str(allowed_base) + os.sep)):
        raise FenceViolation(
            f"Shadow sink {out_path} is outside the allowed development namespace {allowed_base}"
        )

    return out_path


class ShadowEvaluator:
    """Read-only evaluator that projects legacy evidence into v0 decisions."""

    def __init__(self, evidence_root: ReadOnlyEvidenceRoot) -> None:
        self.evidence_root = evidence_root

    def build_snapshot_from_legacy(
        self,
        project_id: str = "devorchestrator",
        goal_id: str = "P17",
    ) -> tuple[WorkRecord, EvidenceSnapshot]:
        """Project legacy state files into a WorkRecord v0 and EvidenceSnapshot."""
        items: list[EvidenceItem] = []

        # Read owner control if available
        paused = False
        try:
            oc_data = self.evidence_root.read_json("runtime/control/owner-control.json")
            paused = bool(oc_data.get("paused", False))
            items.append(
                EvidenceItem(
                    source="owner_command",
                    source_id="owner-control",
                    timestamp=oc_data.get("updated_at", ""),
                    anchor=oc_data.get("last_command_id", ""),
                    data=oc_data,
                    digest=compute_item_digest("owner_command", "owner-control", oc_data),
                    read_status="OK",
                )
            )
        except (FileNotFoundError, json.JSONDecodeError):
            pass

        # Read status.json if available
        status_data: dict[str, Any] = {}
        try:
            status_data = self.evidence_root.read_json(".devorch/status.json")
            items.append(
                EvidenceItem(
                    source="lifecycle_authority",
                    source_id="status.json",
                    timestamp=status_data.get("updated_at", ""),
                    anchor=status_data.get("head", ""),
                    data=status_data,
                    digest=compute_item_digest("lifecycle_authority", "status.json", status_data),
                    read_status="OK",
                )
            )
        except (FileNotFoundError, json.JSONDecodeError):
            pass

        ev_snapshot = EvidenceSnapshot(
            items=tuple(items),
            conflicts=(),
            shared_leases=(),
            exact_anchors={"head": status_data.get("head", "head-unknown")},
            emergency_pause_asserted=paused,
            metadata={"source": "legacy_shadow_projection"},
        )

        work_record = validate_work_record({
            "schema_version": 1,
            "project_id": project_id,
            "goal_id": goal_id,
            "goal_revision": 1,
            "goal_spec_digest": "sha256:goal-spec-digest",
            "predecessor_goal_id": "P16.14",
            "repository_identity": {
                "repo_path": str(self.evidence_root.path),
                "branch": status_data.get("branch", "main"),
            },
            "status": "OPEN",
            "active_lease": None,
            "current_problem": None,
            "attempts": [],
            "acceptance": {"kind": "NONE"},
            "authority_revision": "rev-1",
        })

        return work_record, ev_snapshot

    def evaluate_shadow(
        self,
        project_id: str = "devorchestrator",
        goal_id: str = "P17",
    ) -> dict[str, Any]:
        """Perform a pure shadow evaluation and return a structured decision record."""
        work_record, ev_snapshot = self.build_snapshot_from_legacy(project_id, goal_id)
        policy = build_policy()
        decision = decide(work_record, ev_snapshot, policy)

        record = {
            "record_type": "shadow_decision",
            "project_id": project_id,
            "goal_id": goal_id,
            "decision": decision.to_dict(),
            "source_work_record_digest": work_record_digest(work_record),
            "source_evidence_digest": snapshot_digest(ev_snapshot),
            "policy_digest": policy.policy_digest,
            "emergency_pause_asserted": ev_snapshot.emergency_pause_asserted,
        }
        record_json = json.dumps(record, sort_keys=True, separators=(",", ":"))
        record["record_hash"] = hashlib.sha256(record_json.encode("utf-8")).hexdigest()
        return record
