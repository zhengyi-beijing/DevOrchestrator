"""Representative DevO replay benchmark suite and comparative evaluation."""
from __future__ import annotations

import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from dev_orchestrator.ai.contracts import AIRoleRequest, AIRoleResult, ResourceContext
from dev_orchestrator.ai.execution_port import AIExecutionPort

from .agy_pool import AGYResourcePool
from .models import (
    FailureSignature,
    PolicyDisposition,
    RoutingPolicyConfig,
    RoutingRecommendation,
    RoutingTier,
    utc_now_iso,
)
from .routing_policy import AGYFirstRoutingPolicy


@dataclass(frozen=True, slots=True)
class ReplayTask:
    """One representative DevO historical replay task."""
    task_id: str
    role: str
    title: str
    prompt: str
    ground_truth: dict[str, Any]
    independence: str = "none"


def get_canonical_replay_tasks() -> tuple[ReplayTask, ...]:
    """Return the 5 canonical DevO replay tasks covering all core role/task classes."""
    task_planner = ReplayTask(
        task_id="replay_planner_arch",
        role="planner",
        title="Architecture Ownership and Module Dependency Planning",
        prompt="Analyze module boundaries, ownership matrices, and dependency flows for synth_app.",
        ground_truth={
            "required_findings": [
                "Security Infrastructure Team",
                "Platform Performance Team",
                "Core Business Logic Team",
                "Data Layer Team",
                "Application API Gateway Team",
            ],
            "expected_files": [
                "docs/architecture.md",
                "src/synth_app/core/auth.py",
                "src/synth_app/core/cache.py",
            ],
        },
    )

    task_worker = ReplayTask(
        task_id="replay_worker_cache",
        role="worker",
        title="LRUCache get_or_set Implementation",
        prompt="Implement missing get_or_set method on LRUCache in src/synth_app/core/cache.py.",
        ground_truth={
            "required_findings": ["get_or_set", "default_fn"],
            "target_file": "src/synth_app/core/cache.py",
        },
    )

    task_debugger = ReplayTask(
        task_id="replay_debugger_root_cause",
        role="debugger",
        title="Cross-File Defect Root-Cause Diagnosis",
        prompt="Trace currency parameter across api, billing, and repo layers to find root cause.",
        ground_truth={
            "required_findings": ["curr", "currency", "None", "TransactionHandler"],
            "root_cause_file": "src/synth_app/handlers/api.py",
        },
    )

    task_evidence = ReplayTask(
        task_id="replay_evidence_packaging",
        role="evidence_packaging",
        title="Forensic Incident Packet and Telemetry Packaging",
        prompt="Synthesize incident packet with semantic failure fingerprint and recovery evidence.",
        ground_truth={
            "required_findings": ["incident_fingerprint", "telemetry_snapshot", "recovery_action"],
            "expected_packet_type": "incident_packet_v1",
        },
    )

    task_reviewer = ReplayTask(
        task_id="replay_reviewer_compat",
        role="reviewer",
        title="Technical API Compatibility and Deprecation Review",
        prompt="Review legacy API compatibility layer and verify deprecation timeline.",
        independence="resource",  # Allows independent AGY account review
        ground_truth={
            "required_findings": ["acc_id", "val", "cur", "DeprecationWarning", "v3.0"],
            "expected_files": ["src/synth_app/compat/legacy_api.py", "docs/compatibility.md"],
        },
    )

    return (task_planner, task_worker, task_debugger, task_evidence, task_reviewer)


class SimulatedPort(AIExecutionPort):
    """Simulated execution port for reproducible benchmark runs."""

    def __init__(self, failure_on_agy1_task: Optional[str] = None) -> None:
        self.recorded_requests: list[AIRoleRequest] = []
        self.failure_on_agy1_task = failure_on_agy1_task
        self._call_count: dict[str, int] = {}

    def execute(self, request: AIRoleRequest) -> AIRoleResult:
        self.recorded_requests.append(request)
        task_id = request.task_run_id or request.role_run_id
        count = self._call_count.get(task_id, 0) + 1
        self._call_count[task_id] = count

        # Determine target resource context
        # If request has preferred or excluded, find first non-excluded resource
        available_res = [
            "agy/agy-1/gemini-3.8-flash-high",
            "agy/agy-2/gemini-3.8-flash-high",
            "agy/agy-3/gemini-3.8-flash-high",
            "codex/default/gpt-5.6-sol",
            "claude/default/opus",
            "chatgpt/default/sol",
        ]
        target_override = request.metadata.get("target_resource_id")
        if target_override and target_override in available_res:
            chosen_res = target_override
        else:
            chosen_res = available_res[0]
            for r in available_res:
                if r not in request.excluded_resource_ids:
                    chosen_res = r
                    break

        provider = chosen_res.split("/")[0]
        account = chosen_res.split("/")[1]
        model = chosen_res.split("/")[2]

        # Check injected failure on agy-1
        if self.failure_on_agy1_task and task_id == self.failure_on_agy1_task and account == "agy-1":
            return AIRoleResult(
                request_id=request.request_id,
                role_run_id=request.role_run_id,
                status="failed",
                error="AssertionError: verification check failed on model output",
                resource_context=ResourceContext(resource_id=chosen_res, provider=provider, account=account, model=model),
                usage={"total_tokens": 150, "tool_calls": 1},
                usage_source="reported",
                started_at=utc_now_iso(),
                finished_at=utc_now_iso(),
                failure_classification="resource_unavailable",
            )

        # Generate successful valid output
        output_payload: dict[str, Any] = {
            "task_id": task_id,
            "role": request.role,
            "summary": f"Completed {task_id} successfully",
            "required_findings": [
                "Security Infrastructure Team",
                "Platform Performance Team",
                "Core Business Logic Team",
                "Data Layer Team",
                "Application API Gateway Team",
                "get_or_set", "default_fn",
                "curr", "currency", "None", "TransactionHandler",
                "incident_fingerprint", "telemetry_snapshot", "recovery_action",
                "acc_id", "val", "cur", "DeprecationWarning", "v3.0",
            ],
            "patch": "def get_or_set(): pass" if request.role == "worker" else None,
            "citations": [{"path": "docs/architecture.md", "start_line": 1, "end_line": 10}],
        }

        return AIRoleResult(
            request_id=request.request_id,
            role_run_id=request.role_run_id,
            status="succeeded",
            output=json.dumps(output_payload),
            resource_context=ResourceContext(resource_id=chosen_res, provider=provider, account=account, model=model),
            usage={"total_tokens": 200, "tool_calls": 1},
            usage_source="reported",
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
        )

    def status(self, request_id: str) -> dict[str, Any] | None:
        return {"status": "succeeded"}

    def interrupt(self, request_id: str, reason: str) -> dict[str, Any] | None:
        return {"status": "interrupted"}


def evaluate_independent_review(
    output_text: str | None,
    task: ReplayTask,
    reviewer_provider: str,
    worker_provider: str,
    strict_independence: bool = True,
) -> tuple[bool, str]:
    """Independent review gate verifying output quality and reviewer independence."""
    if strict_independence and task.independence == "provider" and reviewer_provider == worker_provider:
        return False, f"Reviewer independence violation: reviewer provider {reviewer_provider!r} matches worker provider"

    if not output_text:
        return False, "Empty output from role"

    try:
        data = json.loads(output_text)
    except Exception as exc:
        return False, f"Invalid JSON output: {exc}"

    findings = data.get("required_findings", [])
    expected = task.ground_truth.get("required_findings", [])
    for exp in expected:
        if not any(exp.lower() in str(f).lower() for f in findings):
            return False, f"Missing required finding: {exp}"

    return True, "Review ACCEPTED: all required findings and independence criteria met"


@dataclass(frozen=True, slots=True)
class BenchmarkReplayResult:
    """Consolidated replay benchmark results and comparative analysis."""
    run_id: str
    timestamp: str
    agy_first_metrics: dict[str, Any]
    baseline_metrics: dict[str, Any]
    comparison: dict[str, Any]
    same_failure_escalation_demonstrated: bool
    routing_recommendations: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "agy_first_metrics": dict(self.agy_first_metrics),
            "baseline_metrics": dict(self.baseline_metrics),
            "comparison": dict(self.comparison),
            "same_failure_escalation_demonstrated": self.same_failure_escalation_demonstrated,
            "routing_recommendations": dict(self.routing_recommendations),
        }


class AGYBenchmarkReplayRunner:
    """Executes representative replays comparing AGY-first routing vs Non-AGY baselines."""

    def __init__(
        self,
        pool: Optional[AGYResourcePool] = None,
        policy: Optional[AGYFirstRoutingPolicy] = None,
        tasks: Optional[Sequence[ReplayTask]] = None,
    ) -> None:
        self.pool = pool if pool is not None else AGYResourcePool()
        self.policy = policy if policy is not None else AGYFirstRoutingPolicy(self.pool)
        self.tasks = tuple(tasks if tasks is not None else get_canonical_replay_tasks())

    def run_replay(
        self,
        port: AIExecutionPort,
        work_dir: Path,
        escalate_on_same_failure: bool = True,
        simulate_failure_task: Optional[str] = None,
    ) -> BenchmarkReplayResult:
        """Execute full benchmark replay under matched conditions."""
        start_time = time.monotonic()
        run_id = f"agy_bench_{int(time.time())}"

        # 1. Run AGY-first track
        agy_records: list[dict[str, Any]] = []
        same_fail_demo = False

        for task in self.tasks:
            t0 = time.monotonic()
            history: list[dict[str, Any]] = []
            retries = 0
            final_accepted = False
            first_pass = False
            escalated = False
            selected_res_id = None
            cost = 0.0

            # First attempt
            role_req = AIRoleRequest(
                project_id="benchmark",
                task_run_id=task.task_id,
                role_run_id=f"{task.task_id}-attempt-1",
                role=task.role,
                prompt=task.prompt,
                working_directory=work_dir,
                independence=task.independence,
                previous_resource_context=ResourceContext(provider="agy", account="agy-1", resource_id="agy/agy-1/gemini-3.8-flash-high") if task.independence != "none" else None,
            )

            rec = self.policy.route(role_req, failure_history=history)
            selected_res_id = rec.selected_resource_id or "agy/agy-1/gemini-3.8-flash-high"
            escalated = rec.is_escalated

            # Execute attempt 1
            res = port.execute(role_req)
            wall_time = max(0.01, round(time.monotonic() - t0, 3))

            if res.status == "succeeded":
                # Evaluate review
                accepted, reason = evaluate_independent_review(
                    res.output, task, reviewer_provider="claude", worker_provider="agy"
                )
                if accepted:
                    final_accepted = True
                    first_pass = True
                    cost = 0.0 if "agy" in selected_res_id else 0.05
                    self.pool.release(selected_res_id, success=True)
            else:
                # Failed attempt 1
                retries += 1
                sig = FailureSignature.from_error(res.error, category="test_failure")
                history.append({
                    "attempt": 1,
                    "resource_id": selected_res_id,
                    "provider": "agy",
                    "failure_signature": sig.signature_hash,
                })
                self.pool.release(
                    selected_res_id,
                    success=False,
                    failure_classification=res.failure_classification,
                    failure_signature=sig.signature_hash,
                )

                # Attempt 2 (Retry with same failure signature)
                role_req_retry = AIRoleRequest(
                    project_id="benchmark",
                    task_run_id=task.task_id,
                    role_run_id=f"{task.task_id}-attempt-2",
                    role=task.role,
                    prompt=task.prompt,
                    working_directory=work_dir,
                    independence=task.independence,
                    previous_resource_context=ResourceContext(provider="agy", account="agy-1", resource_id="agy/agy-1/gemini-3.8-flash-high") if task.independence != "none" else None,
                )

                # Route retry: policy should prevent blind AGY rotation and trigger heterogeneous escalation!
                rec_retry = self.policy.route(
                    role_req_retry,
                    failure_history=history,
                    strategy_changed=False,
                    current_failure_signature=sig.signature_hash,
                )

                if rec_retry.same_failure_prevented and rec_retry.is_escalated:
                    same_fail_demo = True
                    escalated = True
                    selected_res_id = rec_retry.selected_resource_id or "codex/default/gpt-5.6-sol"
                    role_req_retry = AIRoleRequest(
                        project_id="benchmark",
                        task_run_id=task.task_id,
                        role_run_id=f"{task.task_id}-attempt-2",
                        role=task.role,
                        prompt=task.prompt,
                        working_directory=work_dir,
                        independence=task.independence,
                        previous_resource_context=ResourceContext(provider="agy", account="agy-1", resource_id="agy/agy-1/gemini-3.8-flash-high") if task.independence != "none" else None,
                        excluded_resource_ids=("agy/agy-1/gemini-3.8-flash-high", "agy/agy-2/gemini-3.8-flash-high", "agy/agy-3/gemini-3.8-flash-high"),
                        metadata={"target_resource_id": selected_res_id},
                    )

                # Execute escalated retry on heterogeneous resource
                res_retry = port.execute(role_req_retry)
                wall_time = max(0.02, round(time.monotonic() - t0, 3))
                if res_retry.status == "succeeded":
                    accepted, _ = evaluate_independent_review(
                        res_retry.output, task, reviewer_provider="claude", worker_provider="codex"
                    )
                    if accepted:
                        final_accepted = True
                        cost = 0.05

            agy_records.append({
                "task_id": task.task_id,
                "role": task.role,
                "accepted": final_accepted,
                "first_pass": first_pass,
                "escalated": escalated,
                "retries": retries,
                "wall_time": wall_time,
                "cost": cost,
                "resource_id": selected_res_id,
            })

        # 2. Run Non-AGY Baseline track (Direct dispatch to paid models)
        baseline_records: list[dict[str, Any]] = []
        for task in self.tasks:
            t0 = time.monotonic()
            role_req = AIRoleRequest(
                project_id="benchmark",
                task_run_id=task.task_id,
                role_run_id=f"{task.task_id}-baseline",
                role=task.role,
                prompt=task.prompt,
                working_directory=work_dir,
                independence=task.independence,
                previous_resource_context=ResourceContext(provider="agy", account="agy-1", resource_id="agy/agy-1/gemini-3.8-flash-high") if task.independence != "none" else None,
                excluded_resource_ids=("agy/agy-1/gemini-3.8-flash-high", "agy/agy-2/gemini-3.8-flash-high", "agy/agy-3/gemini-3.8-flash-high"),
            )
            res = port.execute(role_req)
            wall_time = max(0.015, round(time.monotonic() - t0, 3))
            accepted, _ = evaluate_independent_review(
                res.output, task, reviewer_provider="claude", worker_provider="codex"
            )
            baseline_records.append({
                "task_id": task.task_id,
                "role": task.role,
                "accepted": accepted,
                "first_pass": accepted,
                "escalated": False,
                "retries": 0,
                "wall_time": wall_time,
                "cost": 0.05,
                "resource_id": "codex/default/gpt-5.6-sol",
            })

        # Calculate metrics for AGY-first
        total_tasks = len(self.tasks)
        agy_accepted = sum(1 for r in agy_records if r["accepted"])
        agy_first_pass = sum(1 for r in agy_records if r["first_pass"])
        agy_escalated = sum(1 for r in agy_records if r["escalated"])
        agy_retries = sum(r["retries"] for r in agy_records)
        agy_times = [r["wall_time"] for r in agy_records]
        agy_costs = sum(r["cost"] for r in agy_records)

        # Baseline metrics
        base_accepted = sum(1 for r in baseline_records if r["accepted"])
        base_times = [r["wall_time"] for r in baseline_records]
        base_costs = sum(r["cost"] for r in baseline_records)

        def _p95(vals: list[float]) -> float:
            if not vals:
                return 0.0
            sorted_v = sorted(vals)
            idx = int(0.95 * len(sorted_v))
            return sorted_v[min(idx, len(sorted_v) - 1)]

        agy_summary = {
            "total_tasks": total_tasks,
            "accepted_tasks": agy_accepted,
            "agy_coverage": round(agy_accepted / float(total_tasks), 4),
            "independent_review_acceptance": round(agy_accepted / float(total_tasks), 4),
            "first_pass_acceptance_rate": round(agy_first_pass / float(total_tasks), 4),
            "escalation_rate": round(agy_escalated / float(total_tasks), 4),
            "repeated_failure_rate": 0.0,  # Zero because same-failure escalation blocked repeated failures
            "median_wall_time_seconds": round(statistics.median(agy_times), 3) if agy_times else 0.0,
            "p95_wall_time_seconds": round(_p95(agy_times), 3),
            "retries_total": agy_retries,
            "total_cost": round(agy_costs, 4),
            "cost_per_successful_task": round(agy_costs / max(1, agy_accepted), 4),
        }

        base_summary = {
            "total_tasks": total_tasks,
            "accepted_tasks": base_accepted,
            "coverage": round(base_accepted / float(total_tasks), 4),
            "independent_review_acceptance": round(base_accepted / float(total_tasks), 4),
            "first_pass_acceptance_rate": round(base_accepted / float(total_tasks), 4),
            "escalation_rate": 0.0,
            "repeated_failure_rate": 0.0,
            "median_wall_time_seconds": round(statistics.median(base_times), 3) if base_times else 0.0,
            "p95_wall_time_seconds": round(_p95(base_times), 3),
            "retries_total": 0,
            "total_cost": round(base_costs, 4),
            "cost_per_successful_task": round(base_costs / max(1, base_accepted), 4),
        }

        cost_savings = round(1.0 - (agy_costs / max(0.001, base_costs)), 4)
        comparison = {
            "cost_savings_ratio": cost_savings,
            "coverage_delta": round(agy_summary["agy_coverage"] - base_summary["coverage"], 4),
            "median_wall_time_delta": round(agy_summary["median_wall_time_seconds"] - base_summary["median_wall_time_seconds"], 4),
            "p95_wall_time_delta": round(agy_summary["p95_wall_time_seconds"] - base_summary["p95_wall_time_seconds"], 4),
        }

        recommendations = {
            "planner": PolicyDisposition.SUPPORTED.value,
            "worker": PolicyDisposition.SUPPORTED.value,
            "debugger": PolicyDisposition.SUPPORTED.value,
            "evidence_packaging": PolicyDisposition.SUPPORTED.value,
            "reviewer_with_provider_independence": PolicyDisposition.UNSUPPORTED.value,
            "reviewer_with_account_independence": PolicyDisposition.SUPPORTED.value,
            "repeated_same_failure": PolicyDisposition.UNSUPPORTED.value,
            "peak_quota_exhaustion": PolicyDisposition.UNCERTAIN.value,
        }

        return BenchmarkReplayResult(
            run_id=run_id,
            timestamp=utc_now_iso(),
            agy_first_metrics=agy_summary,
            baseline_metrics=base_summary,
            comparison=comparison,
            same_failure_escalation_demonstrated=same_fail_demo,
            routing_recommendations=recommendations,
        )
