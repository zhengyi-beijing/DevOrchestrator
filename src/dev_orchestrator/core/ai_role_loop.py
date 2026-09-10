"""Minimal deterministic Worker -> Reviewer -> NEXT orchestration loop."""
from __future__ import annotations

from dataclasses import dataclass

from dev_orchestrator.ai import AIExecutionPort, AIRoleRequest, AIRoleResult


def _final_review_token(output: str | None) -> str:
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    if not lines:
        return ""
    return lines[-1].strip("`*_ 	")


@dataclass(frozen=True, slots=True)
class MinimalLoopOutcome:
    state: str
    worker: AIRoleResult
    reviewer: AIRoleResult | None
    next_action: str | None
    reason: str


class MinimalAIRoleLoop:
    """Small P3.5 loop; project lifecycle decisions stay in DevOrchestrator."""

    def __init__(self, port: AIExecutionPort) -> None:
        self.port = port

    def run(
        self,
        worker_request: AIRoleRequest,
        *,
        reviewer_prompt: str,
        reviewer_request_id: str,
        reviewer_role_run_id: str,
        pass_token: str = "REVIEW_PASS",
        reviewer_quality: str = "high",
    ) -> MinimalLoopOutcome:
        if worker_request.role != "worker":
            raise ValueError("minimal loop requires a worker request")
        if not reviewer_prompt.strip() or not pass_token.strip():
            raise ValueError("reviewer_prompt and pass_token must be nonblank")

        worker = self.port.execute(worker_request)
        if worker.status != "succeeded":
            return MinimalLoopOutcome(
                "stopped", worker, None, None, f"worker_{worker.status}"
            )
        if worker.resource_context is None:
            return MinimalLoopOutcome(
                "stopped", worker, None, None, "worker_resource_context_missing"
            )

        review_text = (reviewer_prompt.rstrip() + "\n\n[WORKER_OUTPUT_BEGIN]\n" + (worker.output or "") + "\n[WORKER_OUTPUT_END]\n")
        reviewer_request = AIRoleRequest(
            project_id=worker_request.project_id,
            task_run_id=worker_request.task_run_id,
            stage_run_id=worker_request.stage_run_id,
            role_run_id=reviewer_role_run_id,
            request_id=reviewer_request_id,
            role="reviewer",
            prompt=review_text,
            working_directory=worker_request.working_directory,
            quality=reviewer_quality,
            independence="resource",
            previous_resource_context=worker.resource_context,
            timeout_seconds=worker_request.timeout_seconds,
            metadata={"worker_role_run_id": worker_request.role_run_id},
        )

        reviewer = self.port.execute(reviewer_request)
        if reviewer.status != "succeeded":
            return MinimalLoopOutcome(
                "stopped", worker, reviewer, None, f"reviewer_{reviewer.status}"
            )
        if _final_review_token(reviewer.output) != pass_token:
            return MinimalLoopOutcome(
                "stopped", worker, reviewer, None, "review_rejected"
            )
        return MinimalLoopOutcome(
            "next", worker, reviewer, "NEXT", "review_passed"
        )
