"""Aggregation, metric analysis, and markdown report generation for aibench."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .contracts import (
    TRACK_A,
    TRACK_B,
    PromotionDecision,
    RunSummary,
    StalenessProbeResult,
    TrialPlan,
    TrialRecord,
    utc_now_iso,
)


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 4)


def _calc_delta_stats(deltas: list[float]) -> dict[str, Any]:
    """Calculate mean, standard error, and statistical significance of paired reduction."""
    n = len(deltas)
    if n == 0:
        return {"n": 0, "mean": 0.0, "se": 0.0, "t_stat": None, "statistically_supported_benefit": False}
    mean = sum(deltas) / n
    if n < 2:
        return {"n": n, "mean": round(mean, 4), "se": 0.0, "t_stat": None, "statistically_supported_benefit": False}
    variance = sum((x - mean) ** 2 for x in deltas) / (n - 1)
    se = (variance / n) ** 0.5
    if se <= 0:
        return {"n": n, "mean": round(mean, 4), "se": 0.0, "t_stat": None, "statistically_supported_benefit": (mean < 0)}
    t_stat = mean / se
    supported = (mean < 0 and t_stat <= -1.71)
    return {
        "n": n,
        "mean": round(mean, 4),
        "se": round(se, 4),
        "t_stat": round(t_stat, 3),
        "statistically_supported_benefit": supported,
    }


def build_run_summary(
    records: list[TrialRecord],
    plan: TrialPlan,
    staleness_result: StalenessProbeResult | None = None,
) -> RunSummary:
    """Compute deterministic aggregation across Track A (Native) and Track B (Retrieval)."""
    track_a_records = [r for r in records if r.cell.track == TRACK_A]
    track_b_records = [r for r in records if r.cell.track == TRACK_B]

    def _track_stats(track_recs: list[TrialRecord]) -> dict[str, Any]:
        completed = [r for r in track_recs if r.status == "completed"]
        correctness_list = [r.score.correctness for r in completed]
        wall_time_list = [r.metrics.wall_time_seconds for r in completed]
        tokens_list = [r.metrics.reported_tokens for r in completed if r.metrics.reported_tokens is not None]
        shell_calls_list = [r.metrics.observed_shell_tool_calls for r in completed]
        zvec_calls_list = [r.metrics.zvec_calls for r in completed]

        valid_cites = sum(r.score.cited_spans_valid for r in track_recs)
        invalid_cites = sum(r.score.cited_spans_invalid for r in track_recs)
        req_met = sum(r.score.required_findings_met for r in track_recs)
        req_tot = sum(r.score.required_findings_total for r in track_recs)
        false_finds = sum(r.score.false_findings for r in track_recs)

        return {
            "total_trials": len(track_recs),
            "completed_trials": len(completed),
            "correctness_mean": _mean(correctness_list),
            "wall_time_seconds_mean": _mean(wall_time_list),
            "reported_tokens_mean": _mean(tokens_list) if tokens_list else None,
            "observed_shell_calls_mean": _mean(shell_calls_list),
            "zvec_calls_mean": _mean(zvec_calls_list),
            "cited_spans_valid_total": valid_cites,
            "cited_spans_invalid_total": invalid_cites,
            "required_findings_met_total": req_met,
            "required_findings_total": req_tot,
            "false_findings_total": false_finds,
        }

    stats_a = _track_stats(track_a_records)
    stats_b = _track_stats(track_b_records)

    # Compute paired comparisons
    pairs: dict[str, dict[str, TrialRecord]] = {}
    for r in records:
        pid = r.cell.pair_id
        if pid not in pairs:
            pairs[pid] = {}
        pairs[pid][r.cell.track] = r

    correctness_deltas: list[float] = []
    wall_time_deltas: list[float] = []
    token_deltas: list[float] = []
    tool_call_deltas: list[float] = []

    for pid, pdata in pairs.items():
        if TRACK_A in pdata and TRACK_B in pdata:
            rec_a = pdata[TRACK_A]
            rec_b = pdata[TRACK_B]
            if rec_a.status == "completed" and rec_b.status == "completed":
                correctness_deltas.append(rec_b.score.correctness - rec_a.score.correctness)
                wall_time_deltas.append(rec_b.metrics.wall_time_seconds - rec_a.metrics.wall_time_seconds)
                tools_a = rec_a.metrics.observed_shell_tool_calls + rec_a.metrics.zvec_calls
                tools_b = rec_b.metrics.observed_shell_tool_calls + rec_b.metrics.zvec_calls
                tool_call_deltas.append(tools_b - tools_a)
                if rec_a.metrics.reported_tokens is not None and rec_b.metrics.reported_tokens is not None:
                    token_deltas.append(rec_b.metrics.reported_tokens - rec_a.metrics.reported_tokens)

    comparison = {
        "completed_pairs": len(correctness_deltas),
        "mean_correctness_delta": _mean(correctness_deltas),
        "mean_wall_time_delta_seconds": _mean(wall_time_deltas),
        "mean_tool_calls_delta": _mean(tool_call_deltas),
        "mean_tokens_delta": _mean(token_deltas) if token_deltas else None,
        "wall_time_stats": _calc_delta_stats(wall_time_deltas),
        "tool_calls_stats": _calc_delta_stats(tool_call_deltas),
        "tokens_stats": _calc_delta_stats(token_deltas) if token_deltas else None,
    }

    # Resource breakdown
    res_map: dict[str, list[TrialRecord]] = {}
    for r in records:
        res_map.setdefault(r.cell.target_resource_id, []).append(r)

    resource_breakdown: dict[str, Any] = {}
    for res_id, rlist in res_map.items():
        comp = [r for r in rlist if r.status == "completed"]
        c_a = [r.score.correctness for r in rlist if r.cell.track == TRACK_A and r.status == "completed"]
        c_b = [r.score.correctness for r in rlist if r.cell.track == TRACK_B and r.status == "completed"]
        resource_breakdown[res_id] = {
            "total_trials": len(rlist),
            "completed_trials": len(comp),
            "track_a_correctness": _mean(c_a),
            "track_b_correctness": _mean(c_b),
            "mean_wall_time_seconds": _mean([r.metrics.wall_time_seconds for r in comp]),
        }

    # Role breakdown
    role_map: dict[str, list[TrialRecord]] = {}
    for r in records:
        role_map.setdefault(r.cell.role, []).append(r)

    role_breakdown: dict[str, Any] = {}
    for role_name, rlist in role_map.items():
        comp = [r for r in rlist if r.status == "completed"]
        c_a = [r.score.correctness for r in rlist if r.cell.track == TRACK_A and r.status == "completed"]
        c_b = [r.score.correctness for r in rlist if r.cell.track == TRACK_B and r.status == "completed"]
        role_breakdown[role_name] = {
            "total_trials": len(rlist),
            "completed_trials": len(comp),
            "track_a_correctness": _mean(c_a),
            "track_b_correctness": _mean(c_b),
        }

    # Quota and rate-limit tracking
    quota_counts: dict[str, int] = {}
    rate_limits: dict[str, int] = {}
    for r in records:
        if r.broker_attempt.quota_observation:
            bucket = str(r.broker_attempt.quota_observation.get("bucket", "default"))
            quota_counts[bucket] = quota_counts.get(bucket, 0) + 1
        if r.broker_attempt.rate_limit_observation:
            rl = str(r.broker_attempt.rate_limit_observation.get("reason", "rate_limit"))
            rate_limits[rl] = rate_limits.get(rl, 0) + 1

    quota_consumed = {
        "quota_observations_count": sum(quota_counts.values()),
        "quota_buckets": quota_counts,
        "rate_limit_events": rate_limits,
    }

    return RunSummary(
        run_id=f"run_{plan.plan_id}",
        plan_id=plan.plan_id,
        created_at=utc_now_iso(),
        total_trials=len(records),
        completed_trials=len([r for r in records if r.status == "completed"]),
        track_a_summary=stats_a,
        track_b_summary=stats_b,
        comparison=comparison,
        resource_breakdown=resource_breakdown,
        role_breakdown=role_breakdown,
        quota_consumed=quota_consumed,
        staleness_metrics=staleness_result.to_dict() if staleness_result else None,
    )


def generate_report_markdown(summary: RunSummary, decision: PromotionDecision | None = None) -> str:
    """Generate human-readable GitHub-flavored markdown report."""
    lines: list[str] = [
        f"# AI Capability Benchmark Report — {summary.run_id}",
        "",
        f"- **Plan ID**: `{summary.plan_id}`",
        f"- **Generated At**: `{summary.created_at}`",
        f"- **Total Trials**: `{summary.total_trials}` (Completed: `{summary.completed_trials}`)",
        "",
    ]

    if decision is not None:
        badge = "🟢 PROMOTE" if decision.decision == "PROMOTE" else "🔴 NO_PROMOTE"
        lines.extend([
            f"## Final Promotion Decision: **{badge}**",
            "",
            f"- **Decision**: `{decision.decision}`",
            f"- **Reason Codes**: `{', '.join(decision.reasons) if decision.reasons else 'none'}`",
            f"- **Failed Gates**: `{', '.join(decision.failed_gates) if decision.failed_gates else 'none'}`",
            "",
            "### Gate Results Summary",
            "",
            "| Gate | Passed | Status | Details |",
            "| --- | --- | --- | --- |",
        ])
        for gate_name, g_info in decision.gate_results.items():
            g_pass = "PASS" if g_info.get("passed") else "FAIL"
            stat = g_info.get("status", "evaluated")
            detail = g_info.get("detail", "")
            lines.append(f"| `{gate_name}` | **{g_pass}** | `{stat}` | {detail} |")
        lines.append("")

    # A/B Comparison Table
    sa = summary.track_a_summary
    sb = summary.track_b_summary
    cmp = summary.comparison

    lines.extend([
        "## Retrieval A/B Track Comparison",
        "",
        "| Metric | Track A (Native) | Track B (Retrieval) | Delta (B - A) |",
        "| --- | --- | --- | --- |",
        f"| **Correctness Mean** | {sa.get('correctness_mean', 0.0):.4f} | {sb.get('correctness_mean', 0.0):.4f} | {cmp.get('mean_correctness_delta', 0.0):+.4f} |",
        f"| **Wall Time Mean (s)** | {sa.get('wall_time_seconds_mean', 0.0):.2f}s | {sb.get('wall_time_seconds_mean', 0.0):.2f}s | {cmp.get('mean_wall_time_delta_seconds', 0.0):+.2f}s |",
        f"| **Reported Tokens Mean** | {sa.get('reported_tokens_mean') or 'N/A'} | {sb.get('reported_tokens_mean') or 'N/A'} | {cmp.get('mean_tokens_delta') or 'N/A'} |",
        f"| **Observed Shell Calls** | {sa.get('observed_shell_calls_mean', 0.0):.2f} | {sb.get('observed_shell_calls_mean', 0.0):.2f} | {cmp.get('mean_tool_calls_delta', 0.0):+.2f} |",
        f"| **Zvec Calls** | {sa.get('zvec_calls_mean', 0.0):.2f} | {sb.get('zvec_calls_mean', 0.0):.2f} | — |",
        f"| **Valid Cited Spans** | {sa.get('cited_spans_valid_total', 0)} | {sb.get('cited_spans_valid_total', 0)} | — |",
        f"| **Invalid Cited Spans** | {sa.get('cited_spans_invalid_total', 0)} | {sb.get('cited_spans_invalid_total', 0)} | — |",
        f"| **False Findings Total** | {sa.get('false_findings_total', 0)} | {sb.get('false_findings_total', 0)} | — |",
        "",
    ])

    # Resource Breakdown
    lines.extend([
        "## Resource Breakdown",
        "",
        "| Resource ID | Completed / Total | Track A Correctness | Track B Correctness | Wall Time (s) |",
        "| --- | --- | --- | --- | --- |",
    ])
    for res_id, r_info in summary.resource_breakdown.items():
        lines.append(
            f"| `{res_id}` | {r_info['completed_trials']}/{r_info['total_trials']} | {r_info['track_a_correctness']:.4f} | {r_info['track_b_correctness']:.4f} | {r_info['mean_wall_time_seconds']:.2f}s |"
        )
    lines.append("")

    # Role Breakdown
    lines.extend([
        "## Role Breakdown",
        "",
        "| Role | Completed / Total | Track A Correctness | Track B Correctness |",
        "| --- | --- | --- | --- |",
    ])
    for role_name, r_info in summary.role_breakdown.items():
        lines.append(
            f"| `{role_name}` | {r_info['completed_trials']}/{r_info['total_trials']} | {r_info['track_a_correctness']:.4f} | {r_info['track_b_correctness']:.4f} |"
        )
    lines.append("")

    # Staleness Probe
    if summary.staleness_metrics:
        sm = summary.staleness_metrics
        lines.extend([
            "## Staleness and Cost Metrics",
            "",
            f"- **Index Build Time**: `{sm.get('index_time_seconds', 0.0)}s`",
            f"- **Index Size**: `{sm.get('index_size_bytes', 0)} bytes`",
            f"- **Stale False Hits**: `{sm.get('stale_false_hits', 0)}`",
            f"- **Stale Misses**: `{sm.get('stale_misses', 0)}`",
            f"- **Stale Error Rate**: `{sm.get('stale_error_rate', 0.0):.4f}`",
            f"- **Query Latency**: `{sm.get('latency_ms', 0.0)} ms`",
            "",
        ])

    return "\n".join(lines)
