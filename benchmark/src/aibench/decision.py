"""Total, bounded promotion gate evaluation for aibench."""
from __future__ import annotations

from typing import Any

from .contracts import (
    DECISION_NO_PROMOTE,
    DECISION_PROMOTE,
    DEFAULT_PROMOTION_THRESHOLDS,
    GATE_NAMES,
    REASON_BENEFIT_GATE_FAILED,
    REASON_CAPABILITY_UNSUPPORTED,
    REASON_COST_GATE_FAILED,
    REASON_EVIDENCE_GATE_FAILED,
    REASON_FALLBACK_GATE_FAILED,
    REASON_PRIVACY_GATE_FAILED,
    REASON_QUALITY_GATE_FAILED,
    REASON_STALENESS_GATE_FAILED,
    PromotionDecision,
    RunSummary,
    TrialPlan,
    ZvecProbeResult,
    utc_now_iso,
)


def evaluate_promotion_decision(
    summary: RunSummary,
    probe_result: ZvecProbeResult,
    plan: TrialPlan,
    thresholds: dict[str, Any] | None = None,
) -> PromotionDecision:
    """Evaluate all promotion gates and emit bounded PROMOTE or NO_PROMOTE decision."""
    thresh = dict(DEFAULT_PROMOTION_THRESHOLDS)
    if thresholds:
        thresh.update(thresholds)

    gate_results: dict[str, dict[str, Any]] = {}
    failed_gates: list[str] = []
    reasons: list[str] = []

    # 1. Capability Gate
    if not probe_result.supported:
        gate_results["capability_gate"] = {
            "passed": False,
            "status": "failed",
            "detail": f"zvec installation unsupported: {probe_result.error_message or 'not installed'}",
        }
        failed_gates.append("capability_gate")
        reasons.append(REASON_CAPABILITY_UNSUPPORTED)

        # Downstream zvec metrics become not_applicable_due_to_capability_failure
        for g_name in ("benefit_gate", "privacy_gate", "staleness_gate", "cost_gate"):
            gate_results[g_name] = {
                "passed": False,
                "status": "not_applicable_due_to_capability_failure",
                "detail": "zvec retrieval unsupported",
            }
            failed_gates.append(g_name)

        # Evidence and fallback gate can still be evaluated for native track
        ev_pass = len(summary.resource_breakdown) >= thresh["min_distinct_resources"]
        gate_results["evidence_gate"] = {
            "passed": ev_pass,
            "status": "evaluated" if ev_pass else "failed",
            "detail": f"distinct resources={len(summary.resource_breakdown)}",
        }
        if not ev_pass:
            failed_gates.append("evidence_gate")
            reasons.append(REASON_EVIDENCE_GATE_FAILED)

        fb_pass = summary.track_a_summary.get("completed_trials", 0) > 0
        gate_results["fallback_gate"] = {
            "passed": fb_pass,
            "status": "passed" if fb_pass else "failed",
            "detail": f"native track completed {summary.track_a_summary.get('completed_trials', 0)} trials",
        }
        if not fb_pass:
            failed_gates.append("fallback_gate")
            reasons.append(REASON_FALLBACK_GATE_FAILED)

        gate_results["quality_gate"] = {
            "passed": False,
            "status": "not_applicable_due_to_capability_failure",
            "detail": "retrieval track not executed due to capability failure",
        }
        failed_gates.append("quality_gate")

        return PromotionDecision(
            decision=DECISION_NO_PROMOTE,
            reasons=tuple(reasons),
            failed_gates=tuple(failed_gates),
            gate_results=gate_results,
            run_id=summary.run_id,
            plan_id=plan.plan_id,
            timestamp=utc_now_iso(),
        )

    # If zvec is supported, evaluate all gates fully
    gate_results["capability_gate"] = {
        "passed": True,
        "status": "passed",
        "detail": f"zvec version {probe_result.version} supported (hash: {probe_result.executable_hash[:8] if probe_result.executable_hash else 'unknown'})",
    }

    # 2. Evidence Gate
    res_count = len(summary.resource_breakdown)
    roles_present = set(summary.role_breakdown.keys())
    required_roles = set(thresh["required_roles"])
    roles_ok = required_roles.issubset(roles_present)
    res_ok = res_count >= thresh["min_distinct_resources"]
    trials_complete = (summary.completed_trials == summary.total_trials) and (summary.total_trials > 0)

    if res_ok and roles_ok and trials_complete:
        gate_results["evidence_gate"] = {
            "passed": True,
            "status": "passed",
            "detail": f"resources={res_count}, roles={len(roles_present)}, trials={summary.completed_trials}/{summary.total_trials}",
        }
    else:
        gate_results["evidence_gate"] = {
            "passed": False,
            "status": "failed",
            "detail": f"resources={res_count} (min {thresh['min_distinct_resources']}), roles={len(roles_present)}, trials={summary.completed_trials}/{summary.total_trials}",
        }
        failed_gates.append("evidence_gate")
        reasons.append(REASON_EVIDENCE_GATE_FAILED)

    # 3. Quality Gate
    score_a = summary.track_a_summary.get("correctness_mean", 0.0)
    score_b = summary.track_b_summary.get("correctness_mean", 0.0)
    non_inferior = score_b >= (score_a * thresh["min_correctness_retention"])

    false_b = summary.track_b_summary.get("false_findings_total", 0)
    req_tot = summary.track_b_summary.get("required_findings_total", 1)
    false_ratio = (false_b / req_tot) if req_tot > 0 else 0.0
    false_ok = false_ratio <= thresh["max_false_findings_ratio"]

    if non_inferior and false_ok:
        gate_results["quality_gate"] = {
            "passed": True,
            "status": "passed",
            "detail": f"track B correctness {score_b:.4f} >= track A {score_a:.4f}, false ratio {false_ratio:.2f}",
        }
    else:
        gate_results["quality_gate"] = {
            "passed": False,
            "status": "failed",
            "detail": f"non_inferior={non_inferior} (B={score_b:.4f}, A={score_a:.4f}), false_ratio={false_ratio:.2f} (max {thresh['max_false_findings_ratio']})",
        }
        failed_gates.append("quality_gate")
        reasons.append(REASON_QUALITY_GATE_FAILED)

    # 4. Benefit Gate
    wall_delta = summary.comparison.get("mean_wall_time_delta_seconds", 0.0)
    tool_delta = summary.comparison.get("mean_tool_calls_delta", 0.0)
    token_delta = summary.comparison.get("mean_tokens_delta")

    benefits_count = 0
    benefit_details: list[str] = []
    if wall_delta < 0:
        benefits_count += 1
        benefit_details.append(f"wall_time advantage ({wall_delta:+.2f}s)")
    if tool_delta < 0:
        benefits_count += 1
        benefit_details.append(f"tool_calls advantage ({tool_delta:+.2f})")
    if token_delta is not None and token_delta < 0:
        benefits_count += 1
        benefit_details.append(f"token advantage ({token_delta:+.2f})")

    if benefits_count >= thresh["min_benefit_metrics"]:
        gate_results["benefit_gate"] = {
            "passed": True,
            "status": "passed",
            "detail": f"benefits ({benefits_count}): {', '.join(benefit_details)}",
        }
    else:
        gate_results["benefit_gate"] = {
            "passed": False,
            "status": "failed",
            "detail": f"no statistically supported advantage in wall time, tool calls, or tokens (delta wall: {wall_delta:+.2f}s, tool: {tool_delta:+.2f})",
        }
        failed_gates.append("benefit_gate")
        reasons.append(REASON_BENEFIT_GATE_FAILED)

    # 5. Privacy Gate
    if probe_result.local_only_verified:
        gate_results["privacy_gate"] = {
            "passed": True,
            "status": "passed",
            "detail": "verified local_only execution under outbound-deny firewall rule",
        }
    else:
        gate_results["privacy_gate"] = {
            "passed": False,
            "status": "failed",
            "detail": "outbound-deny firewall rule not verified for zvec executable",
        }
        failed_gates.append("privacy_gate")
        reasons.append(REASON_PRIVACY_GATE_FAILED)

    # 6. Staleness Gate
    sm = summary.staleness_metrics or {}
    stale_rate = float(sm.get("stale_error_rate", 1.0))
    if stale_rate <= thresh["max_stale_error_rate"]:
        gate_results["staleness_gate"] = {
            "passed": True,
            "status": "passed",
            "detail": f"stale error rate {stale_rate:.4f} <= threshold {thresh['max_stale_error_rate']}",
        }
    else:
        gate_results["staleness_gate"] = {
            "passed": False,
            "status": "failed",
            "detail": f"stale error rate {stale_rate:.4f} exceeds threshold {thresh['max_stale_error_rate']}",
        }
        failed_gates.append("staleness_gate")
        reasons.append(REASON_STALENESS_GATE_FAILED)

    # 7. Cost Gate
    idx_time = float(sm.get("index_time_seconds", 999.0))
    idx_size_mb = float(sm.get("index_size_bytes", 0)) / (1024.0 * 1024.0)
    time_ok = idx_time <= thresh["max_index_time_seconds"]
    size_ok = idx_size_mb <= thresh["max_index_size_mb"]
    if time_ok and size_ok:
        gate_results["cost_gate"] = {
            "passed": True,
            "status": "passed",
            "detail": f"index time {idx_time:.2f}s, size {idx_size_mb:.2f}MB within bounds",
        }
    else:
        gate_results["cost_gate"] = {
            "passed": False,
            "status": "failed",
            "detail": f"index time {idx_time:.2f}s (max {thresh['max_index_time_seconds']}s) or size {idx_size_mb:.2f}MB (max {thresh['max_index_size_mb']}MB) exceeded",
        }
        failed_gates.append("cost_gate")
        reasons.append(REASON_COST_GATE_FAILED)

    # 8. Fallback Gate
    trials_a = summary.track_a_summary.get("completed_trials", 0)
    score_a = summary.track_a_summary.get("correctness_mean", 0.0)
    if trials_a > 0 and score_a > 0.0:
        gate_results["fallback_gate"] = {
            "passed": True,
            "status": "passed",
            "detail": f"native fallback verified ({trials_a} completed trials, correctness {score_a:.4f})",
        }
    else:
        gate_results["fallback_gate"] = {
            "passed": False,
            "status": "failed",
            "detail": f"native fallback failed (completed {trials_a}, correctness {score_a:.4f})",
        }
        failed_gates.append("fallback_gate")
        reasons.append(REASON_FALLBACK_GATE_FAILED)

    # Final Decision
    if not failed_gates:
        decision = DECISION_PROMOTE
    else:
        decision = DECISION_NO_PROMOTE

    return PromotionDecision(
        decision=decision,
        reasons=tuple(reasons),
        failed_gates=tuple(failed_gates),
        gate_results=gate_results,
        run_id=summary.run_id,
        plan_id=plan.plan_id,
        timestamp=utc_now_iso(),
    )
