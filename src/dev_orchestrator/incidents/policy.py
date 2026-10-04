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
    "typed_replay_candidates": True,
    "max_regression_candidates": 1000,
    "max_replay_corpus_cases": 500,
    "max_replay_fixture_bytes": 65536,
    "max_replay_repeats": 2,
    "max_same_strategy_retries": 1,
    "max_recoveries_per_incident": 3,
    "max_recovery_attempts_per_family": 5,
    "recovery_attempt_reservation_ttl_seconds": 300.0,
    "heterogeneous_required_classes": (
        "malformed_ai_output",
        "reviewer_rejected",
        "retry_without_progress",
        "unknown_requires_owner",
    ),
    "recovery_enabled": True,
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
    "typed_replay_candidates",
    "max_regression_candidates",
    "max_replay_corpus_cases",
    "max_replay_fixture_bytes",
    "max_replay_repeats",
    "max_same_strategy_retries",
    "max_recoveries_per_incident",
    "max_recovery_attempts_per_family",
    "recovery_attempt_reservation_ttl_seconds",
    "heterogeneous_required_classes",
    "recovery_enabled",
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
    elif isinstance(config.get("incident_policy"), dict):
        raw = config["incident_policy"]
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
        if "typed_replay_candidates" in raw:
            policy["typed_replay_candidates"] = bool(raw["typed_replay_candidates"])
        if "max_regression_candidates" in raw and isinstance(raw["max_regression_candidates"], int):
            policy["max_regression_candidates"] = max(1, min(100000, raw["max_regression_candidates"]))
        if "max_replay_corpus_cases" in raw and isinstance(raw["max_replay_corpus_cases"], int):
            policy["max_replay_corpus_cases"] = max(0, min(50000, raw["max_replay_corpus_cases"]))
        if "max_replay_fixture_bytes" in raw and isinstance(raw["max_replay_fixture_bytes"], int):
            policy["max_replay_fixture_bytes"] = max(1, min(1048576, raw["max_replay_fixture_bytes"]))
        if "max_replay_repeats" in raw and isinstance(raw["max_replay_repeats"], int):
            policy["max_replay_repeats"] = max(1, min(10, raw["max_replay_repeats"]))
        if "max_same_strategy_retries" in raw and isinstance(raw["max_same_strategy_retries"], int):
            policy["max_same_strategy_retries"] = max(0, min(10, raw["max_same_strategy_retries"]))
        if "max_recoveries_per_incident" in raw and isinstance(raw["max_recoveries_per_incident"], int):
            policy["max_recoveries_per_incident"] = max(1, min(20, raw["max_recoveries_per_incident"]))
        if "max_recovery_attempts_per_family" in raw and isinstance(raw["max_recovery_attempts_per_family"], int):
            policy["max_recovery_attempts_per_family"] = max(1, min(50, raw["max_recovery_attempts_per_family"]))
        if "recovery_attempt_reservation_ttl_seconds" in raw and isinstance(raw["recovery_attempt_reservation_ttl_seconds"], (int, float)):
            policy["recovery_attempt_reservation_ttl_seconds"] = max(1.0, float(raw["recovery_attempt_reservation_ttl_seconds"]))
        if "heterogeneous_required_classes" in raw and isinstance(raw["heterogeneous_required_classes"], (list, tuple, set, frozenset)):
            policy["heterogeneous_required_classes"] = tuple(str(x) for x in raw["heterogeneous_required_classes"])
        if "recovery_enabled" in raw:
            policy["recovery_enabled"] = bool(raw["recovery_enabled"])

    if "regression_owner" in config:
        policy["regression_owner"] = config["regression_owner"]

    return policy
