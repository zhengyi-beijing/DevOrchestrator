"""Incident management policy resolution."""
from __future__ import annotations

import copy
from typing import Any

DEFAULT_INCIDENT_POLICY: dict[str, Any] = {
    "enabled": True,
    "auto_synthesis": True,
    "max_evidence_references": 10,
    "retention_max_references": 20,
    "max_families": 1000,
    "max_occurrences_per_family": 5000,
    "max_total_occurrences": 50000,
    "max_journal_intents": 100,
    "stale_task_activity_threshold_seconds": 300,
    "progress_obligation_window_seconds": 300,
    "quarantine_corrupt": True,
    "regression_owner": None,
    "escalation_window_seconds": 600,
}


KNOWN_INCIDENT_KEYS = frozenset({
    "enabled",
    "auto_synthesis",
    "max_evidence_references",
    "retention_max_references",
    "max_families",
    "max_occurrences_per_family",
    "max_total_occurrences",
    "max_journal_intents",
    "stale_task_activity_threshold_seconds",
    "progress_obligation_window_seconds",
    "quarantine_corrupt",
    "escalation_window_seconds",
})

FOREIGN_TOP_LEVEL_SECTIONS = frozenset({
    "projects",
    "execution_accounting",
    "daemon",
    "watchdog",
    "bridge",
    "web",
    "storage",
    "core",
    "control",
    "transport",
})


def resolve_incident_policy(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return normalized incident policy, merging defaults with provided configuration."""
    policy = copy.deepcopy(DEFAULT_INCIDENT_POLICY)
    if not isinstance(config, dict):
        return policy

    if isinstance(config.get("incidents"), dict):
        raw = config["incidents"]
    elif not any(k in config for k in FOREIGN_TOP_LEVEL_SECTIONS) and any(k in config for k in KNOWN_INCIDENT_KEYS):
        raw = config
    else:
        raw = None

    if isinstance(raw, dict):
        if "enabled" in raw:
            policy["enabled"] = bool(raw["enabled"])
        if "auto_synthesis" in raw:
            policy["auto_synthesis"] = bool(raw["auto_synthesis"])
        if "max_evidence_references" in raw and isinstance(raw["max_evidence_references"], int):
            policy["max_evidence_references"] = max(1, min(100, raw["max_evidence_references"]))
        if "retention_max_references" in raw and isinstance(raw["retention_max_references"], int):
            policy["retention_max_references"] = max(1, min(500, raw["retention_max_references"]))
        if "max_families" in raw and isinstance(raw["max_families"], int):
            policy["max_families"] = max(1, min(100000, raw["max_families"]))
        if "max_occurrences_per_family" in raw and isinstance(raw["max_occurrences_per_family"], int):
            policy["max_occurrences_per_family"] = max(1, min(500000, raw["max_occurrences_per_family"]))
        if "max_total_occurrences" in raw and isinstance(raw["max_total_occurrences"], int):
            policy["max_total_occurrences"] = max(1, min(1000000, raw["max_total_occurrences"]))
        if "max_journal_intents" in raw and isinstance(raw["max_journal_intents"], int):
            policy["max_journal_intents"] = max(1, min(10000, raw["max_journal_intents"]))
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
