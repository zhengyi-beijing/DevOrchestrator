"""Deterministic evidence-based transport selection with safety and identity fences."""

from __future__ import annotations

from typing import Any, Mapping, Optional

from dev_orchestrator.transport.contracts import (
    HostCapabilities,
    TransportSelection,
)
from dev_orchestrator.transport.hosts import (
    TransportHostProfile,
    TransportHostsConfig,
)


def select_transport(
    operation: str,
    *,
    command_ref: Optional[str] = None,
    effect_class: str = "read_only",
    target_host_id: Optional[str] = None,
    hosts_config: TransportHostsConfig,
    policy_digest: Optional[str] = None,
    capabilities: Optional[HostCapabilities] = None,
    capability_discovery_error: Optional[str] = None,
    pre_dispatch_failures: Optional[Mapping[str, str]] = None,
) -> TransportSelection:
    """Deterministically select transport candidate based on host profile, capabilities, and safety fences.
    
    Safety rules:
    1. Hardware commands are unconditionally rejected.
    2. RDC is NEVER automatically selected; returns 'rdc_fallback_required'.
    3. Effectful and write operations never fail over across transports.
    4. Only read-only operations may fail over after an unambiguous pre-dispatch failure.
    5. Approved policy-digest pins must match if configured in host profile.
    """
    candidate_rejections: dict[str, str] = {}
    host_id = target_host_id or hosts_config.default_host
    profile = hosts_config.hosts.get(host_id)

    # 1. Unconditional hardware rejection
    if effect_class == "hardware":
        return TransportSelection(
            selected_transport="rejected",
            candidate_order=[],
            evidence_source="policy_fence",
            reason_code="hardware_execution_not_supported_in_p18",
            candidate_rejections={"all": "hardware_execution_not_supported_in_p18"},
        )

    if profile is None or not profile.enabled:
        return TransportSelection(
            selected_transport="rdc_fallback_required",
            candidate_order=[],
            evidence_source="host_profile",
            reason_code="host_unknown_or_disabled",
            candidate_rejections={host_id: "host_unknown_or_disabled"},
        )

    candidate_order = list(profile.candidate_order)
    pre_failures = dict(pre_dispatch_failures or {})

    # Check approved policy pin if configured
    if command_ref and profile.approved_policy_pins:
        pinned = profile.approved_policy_pins.get(command_ref) or profile.approved_policy_pins.get("*")
        if pinned and pinned != policy_digest:
            return TransportSelection(
                selected_transport="rejected",
                candidate_order=candidate_order,
                evidence_source="policy_pin",
                reason_code="policy_pin_mismatch",
                candidate_rejections={host_id: f"policy_pin_mismatch ({policy_digest} != {pinned})"},
            )

    # Evaluate candidate transports in order
    selected: Optional[str] = None
    for cand in candidate_order:
        # Check if already failed pre-dispatch
        if cand in pre_failures:
            candidate_rejections[cand] = f"pre_dispatch_failure: {pre_failures[cand]}"
            # Only read-only operations may fail over to a subsequent candidate!
            if effect_class != "read_only":
                return TransportSelection(
                    selected_transport="ambiguous",
                    candidate_order=candidate_order,
                    evidence_source="durability_fence",
                    reason_code="effectful_failover_forbidden",
                    candidate_rejections=candidate_rejections,
                )
            continue

        if cand == "rdc":
            # RDC is never automatically selected
            candidate_rejections["rdc"] = "rdc_never_automatically_selected"
            continue

        if cand == "ssh" and not profile.ssh:
            candidate_rejections["ssh"] = "ssh_configuration_missing"
            continue

        if capabilities is None and operation != "capabilities":
            reason = "capabilities_unknown_or_stale"
            if capability_discovery_error:
                reason = f"{reason}: {capability_discovery_error}"
            candidate_rejections[cand] = reason
            continue

        if capabilities is not None:
            if operation != "capabilities" and not capabilities.jobs_config_valid:
                candidate_rejections[cand] = "host_jobs_config_invalid"
                continue
            if operation not in capabilities.supported_operations:
                candidate_rejections[cand] = f"operation_{operation}_not_supported_on_host"
                continue

        selected = cand
        break

    if selected is not None:
        return TransportSelection(
            selected_transport=selected,
            candidate_order=candidate_order,
            evidence_source="deterministic_candidate_order",
            reason_code=f"selected_{selected}",
            candidate_rejections=candidate_rejections,
        )

    capabilities_unknown = any(
        reason.startswith("capabilities_unknown_or_stale")
        for reason in candidate_rejections.values()
    )
    return TransportSelection(
        selected_transport="rdc_fallback_required",
        candidate_order=candidate_order,
        evidence_source="capability_discovery" if capabilities_unknown else "fallback_policy",
        reason_code="capabilities_unknown" if capabilities_unknown else "no_native_candidate_available",
        candidate_rejections=candidate_rejections,
    )
