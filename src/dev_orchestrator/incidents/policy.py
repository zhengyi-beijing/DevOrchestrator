"""Incident management policy resolution."""
from __future__ import annotations

import copy
from typing import Any

DEFAULT_INCIDENT_POLICY: dict[str, Any] = {
    "enabled": True,
    "auto_synthesis": True,
    "max_evidence_references": 10,
    "retention_max_references": 20,
    "stale_task_activity_threshold_seconds": 300,
    "progress_obligation_window_seconds": 300,
    "quarantine_corrupt": True,
    "regression_owner": None,
    "escalation_window_seconds": 600,
}


def resolve_incident_policy(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return normalized incident policy, merging defaults with provided configuration."""
    policy = copy.deepcopy(DEFAULT_INCIDENT_POLICY)
    if not isinstance(config, dict):
        return policy

    raw = config.get("incidents")
    if isinstance(raw, dict):
        if "enabled" in raw:
            policy["enabled"] = bool(raw["enabled"])
        if "auto_synthesis" in raw:
            policy["auto_synthesis"] = bool(raw["auto_synthesis"])
        if "max_evidence_references" in raw and isinstance(raw["max_evidence_references"], int):
            policy["max_evidence_references"] = max(1, min(100, raw["max_evidence_references"]))
        if "retention_max_references" in raw and isinstance(raw["retention_max_references"], int):
            policy["retention_max_references"] = max(1, min(500, raw["retention_max_references"]))
        if "stale_task_activity_threshold_seconds" in raw and isinstance(raw["stale_task_activity_threshold_seconds"], (int, float)):
            policy["stale_task_activity_threshold_seconds"] = max(1.0, float(raw["stale_task_activity_threshold_seconds"]))
        if "progress_obligation_window_seconds" in raw and isinstance(raw["progress_obligation_window_seconds"], (int, float)):
            policy["progress_obligation_window_seconds"] = max(1.0, float(raw["progress_obligation_window_seconds"]))
        if "quarantine_corrupt" in raw:
            policy["quarantine_corrupt"] = bool(raw["quarantine_corrupt"])
        if "escalation_window_seconds" in raw and isinstance(raw["escalation_window_seconds"], (int, float)):
            policy["escalation_window_seconds"] = max(1.0, float(raw["escalation_window_seconds"]))

    if "regression_owner" in config:
        policy["regression_owner"] = config["regression_owner"]

    return policy
