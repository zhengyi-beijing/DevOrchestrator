"""Immutable typed EvidenceSnapshot and EvidenceItem models.

Models read outcome, freshness, exact anchors, digests, typed conflicts,
source precedence, shared-credential leases, and missing/ambiguous required sources.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping, Sequence

READ_STATUSES = frozenset({"OK", "MISSING", "CORRUPT", "AMBIGUOUS"})

# Explicit source precedence ordering (highest to lowest authority)
DEFAULT_SOURCE_PRECEDENCE = (
    "owner_command",
    "lifecycle_authority",
    "verification_record",
    "review_verdict",
    "test_runner",
    "process_probe",
    "git_repository",
    "markdown_projection",
)


@dataclass(frozen=True)
class EvidenceItem:
    source: str
    source_id: str
    timestamp: str
    anchor: str
    data: dict[str, Any]
    digest: str
    read_status: str = "OK"

    def __post_init__(self) -> None:
        if self.read_status not in READ_STATUSES:
            raise ValueError(f"read_status must be one of {sorted(READ_STATUSES)}, got {self.read_status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_id": self.source_id,
            "timestamp": self.timestamp,
            "anchor": self.anchor,
            "data": dict(self.data),
            "digest": self.digest,
            "read_status": self.read_status,
        }


@dataclass(frozen=True)
class ConflictClaim:
    source_a: str
    source_b: str
    conflict_type: str
    details: str
    resolved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_a": self.source_a,
            "source_b": self.source_b,
            "conflict_type": self.conflict_type,
            "details": self.details,
            "resolved": self.resolved,
        }


@dataclass(frozen=True)
class SharedCredentialLease:
    session_id: str
    credential_id: str
    owner_role: str
    lease_until: str
    acquired_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "credential_id": self.credential_id,
            "owner_role": self.owner_role,
            "lease_until": self.lease_until,
            "acquired_at": self.acquired_at,
        }


@dataclass(frozen=True)
class EvidenceSnapshot:
    items: tuple[EvidenceItem, ...] = field(default_factory=tuple)
    conflicts: tuple[ConflictClaim, ...] = field(default_factory=tuple)
    shared_leases: tuple[SharedCredentialLease, ...] = field(default_factory=tuple)
    exact_anchors: dict[str, str] = field(default_factory=dict)
    emergency_pause_asserted: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "conflicts": [c.to_dict() for c in self.conflicts],
            "shared_leases": [l.to_dict() for l in self.shared_leases],
            "exact_anchors": dict(self.exact_anchors),
            "emergency_pause_asserted": self.emergency_pause_asserted,
            "metadata": dict(self.metadata),
        }


def compute_item_digest(source: str, source_id: str, data: Mapping[str, Any]) -> str:
    """Compute deterministic sha256 digest of an evidence item."""
    raw = json.dumps({"source": source, "source_id": source_id, "data": dict(data)}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def snapshot_digest(snapshot: EvidenceSnapshot) -> str:
    """Compute deterministic sha256 digest over the entire EvidenceSnapshot."""
    c_json = json.dumps(snapshot.to_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(c_json.encode("utf-8")).hexdigest()


def get_required_source(snapshot: EvidenceSnapshot, source_name: str) -> EvidenceItem | None:
    """Find the highest-precedence EvidenceItem for a given source name."""
    matching = [item for item in snapshot.items if item.source == source_name]
    if not matching:
        return None
    # If multiple, prefer items with read_status == OK
    ok_items = [m for m in matching if m.read_status == "OK"]
    if ok_items:
        return ok_items[0]
    return matching[0]


def has_unresolved_ambiguity(snapshot: EvidenceSnapshot) -> bool:
    """Check if snapshot has unresolved conflicts or ambiguous items."""
    for conflict in snapshot.conflicts:
        if not conflict.resolved:
            return True
    for item in snapshot.items:
        if item.read_status in ("AMBIGUOUS", "CORRUPT"):
            return True
    return False
