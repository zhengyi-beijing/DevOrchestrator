"""Semantic incident fingerprint generation with volatile field rejection."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

ALLOWED_FINGERPRINT_FIELDS = frozenset({
    "failure_class",
    "diagnosis_code",
    "blocker_code",
    "lifecycle_class",
    "contract_class",
    "role_class",
    "role_state_class",
    "broker_state_class",
    "git_anchor_relation",
    "continuation_relation",
    "invariant_identifier",
})

FORBIDDEN_VOLATILE_KEYS = frozenset({
    "timestamp",
    "timestamps",
    "time",
    "created_at",
    "updated_at",
    "started_at",
    "completed_at",
    "last_seen_at",
    "first_seen_at",
    "age",
    "age_seconds",
    "pid",
    "process_id",
    "uuid",
    "id",
    "request_id",
    "path",
    "repo_path",
    "runtime_root",
    "traceback",
    "stdout",
    "stderr",
})

_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]|^\/")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_ISO_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _validate_non_volatile(value: Any, key_name: str = "") -> None:
    if isinstance(value, str):
        if _UUID_RE.match(value.strip()):
            raise ValueError(f"Volatile UUID value detected in semantic fingerprint for field {key_name!r}")
        if _ISO_TIMESTAMP_RE.match(value.strip()):
            raise ValueError(f"Volatile timestamp value detected in semantic fingerprint for field {key_name!r}")
        if _ABSOLUTE_PATH_RE.match(value.strip()):
            raise ValueError(f"Volatile absolute path detected in semantic fingerprint for field {key_name!r}")
    elif isinstance(value, (int, float)):
        # Large integers or floats resembling timestamps (e.g. > 1e9)
        if value > 1_000_000_000:
            raise ValueError(f"Volatile epoch timestamp detected in semantic fingerprint for field {key_name!r}")
    elif isinstance(value, dict):
        for k, v in value.items():
            if str(k).lower() in FORBIDDEN_VOLATILE_KEYS:
                raise ValueError(f"Forbidden volatile key {k!r} detected in semantic fingerprint")
            _validate_non_volatile(v, key_name=str(k))
    elif isinstance(value, list):
        for item in value:
            _validate_non_volatile(item, key_name=key_name)


def incident_fingerprint(semantic: dict[str, Any]) -> str:
    """Compute stable truncated SHA-256 fingerprint over normalized semantic fields.

    Strictly rejects unallowlisted or volatile fields.
    """
    if not isinstance(semantic, dict):
        raise ValueError("Semantic payload must be a dictionary")

    # Reject unallowlisted keys
    unknown_keys = set(semantic.keys()) - ALLOWED_FINGERPRINT_FIELDS
    if unknown_keys:
        raise ValueError(
            f"Unallowlisted keys in semantic fingerprint payload: {sorted(unknown_keys)}. "
            f"Allowed keys are {sorted(ALLOWED_FINGERPRINT_FIELDS)}"
        )

    # Reject volatile keys and values
    for k, v in semantic.items():
        if str(k).lower() in FORBIDDEN_VOLATILE_KEYS:
            raise ValueError(f"Forbidden volatile key {k!r} in semantic payload")
        _validate_non_volatile(v, key_name=str(k))

    canonical_json = json.dumps(semantic, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()[:32]
