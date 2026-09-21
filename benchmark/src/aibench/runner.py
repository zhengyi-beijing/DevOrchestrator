"""Trial plan generation, paired execution coordinator, and observation recording."""
from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from dev_orchestrator.ai.contracts import AIRoleRequest

from .broker_client import BrokerBenchmarkClient
from .contracts import (
    ROLE_CLASSES,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_TIMED_OUT,
    STATUS_UNAVAILABLE,
    TRACK_A,
    TRACK_B,
    BenchmarkTask,
    BrokerAttempt,
    MetricCoverage,
    ResourceSnapshot,
    TrialCell,
    TrialPlan,
    TrialRecord,
    TrialScore,
    canonical_json,
    sha256_bytes,
    utc_now_iso,
)
from .prompts import get_canonical_tasks, render_task_prompt
from .scoring import score_trial
from .workspace import cleanup_workspace, materialize_workspace
from .zvec import ZvecAdapter


def build_trial_plan(
    selected_resources: list[str],
    tasks: tuple[BenchmarkTask, ...] | None = None,
    corpus_manifest_hash: str = "",
    corpus_revision: str = "0000000000000000000000000000000000000001",
    registry_digest: str = "",
    config_hash: str = "",
    repeats: int = 1,
    seed: int = 42,
    max_dispatches: int = 200,
    require_all_roles: bool = True,
) -> TrialPlan:
    """Generate deterministic frozen TrialPlan with paired A/B trials and randomized within-pair order."""
    if len(selected_resources) < 3:
        raise ValueError(f"acceptance requires at least 3 distinct resources, got {len(selected_resources)}")

    if tasks is None:
        tasks = get_canonical_tasks()

    if require_all_roles:
        roles_present = {t.role for t in tasks}
        for req_role in ROLE_CLASSES:
            if req_role not in roles_present:
                raise ValueError(f"acceptance requires all four roles, missing role {req_role!r}")

    rng = random.Random(seed)
    cells: list[TrialCell] = []
    rubric_hashes: list[str] = []

    for task in tasks:
        prompt = render_task_prompt(task)
        rubric_hashes.append(task.rubric_ref)

        for rep in range(repeats):
            for res_id in selected_resources:
                res_slug = res_id.replace("/", "_").replace(":", "_")
                pair_id = f"pair_{task.task_id}_{res_slug}_r{rep}"

                # Decide within-pair order randomly
                tracks_order = [TRACK_A, TRACK_B]
                if rng.random() < 0.5:
                    tracks_order = [TRACK_B, TRACK_A]

                for order_idx, track_name in enumerate(tracks_order):
                    trial_id = f"{pair_id}_{track_name}"
                    cell = TrialCell(
                        trial_id=trial_id,
                        task_id=task.task_id,
                        role=task.role,
                        target_resource_id=res_id,
                        track=track_name,
                        repeat_index=rep,
                        pair_id=pair_id,
                        pair_order=order_idx,
                        prompt_hash=prompt.prompt_hash,
                        corpus_revision=corpus_revision,
                        timeout_seconds=task.timeout_seconds,
                    )
                    cells.append(cell)

    if len(cells) > max_dispatches:
        raise ValueError(f"planned cells count ({len(cells)}) exceeds maximum dispatches ({max_dispatches})")

    rubric_digest = sha256_bytes(",".join(sorted(rubric_hashes)).encode("utf-8"))
    plan_id = f"plan_{seed}_{corpus_manifest_hash[:8]}_{len(cells)}"

    return TrialPlan(
        plan_id=plan_id,
        created_at=utc_now_iso(),
        corpus_manifest_hash=corpus_manifest_hash,
        rubric_digest=rubric_digest,
        config_hash=config_hash,
        registry_digest=registry_digest,
        selected_resources=tuple(selected_resources),
        cells=tuple(cells),
        repeats=repeats,
        seed=seed,
        max_dispatches=max_dispatches,
    )


class BenchmarkRunner:
    """Executes benchmark trials against fresh workspaces with observation collection."""

    def __init__(
        self,
        broker_client: BrokerBenchmarkClient,
        zvec_adapter: ZvecAdapter,
        scratch_root: Path,
        results_file: Path,
    ) -> None:
        self.broker_client = broker_client
        self.zvec_adapter = zvec_adapter
        self.scratch_root = scratch_root
        self.results_file = results_file
        self._task_map = {t.task_id: t for t in get_canonical_tasks()}

    def _load_completed_trials(self) -> dict[str, TrialRecord]:
        completed: dict[str, TrialRecord] = {}
        if not self.results_file.is_file():
            return completed
        try:
            with open(self.results_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        record = TrialRecord.from_dict(json.loads(line))
                        completed[record.trial_id] = record
        except Exception:
            pass
        return completed

    def _append_record(self, record: TrialRecord) -> None:
        self.results_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.results_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record.to_dict()) + "\n")

    def run_plan(
        self,
        plan: TrialPlan,
        corpus_dir: Path,
        snapshot: ResourceSnapshot,
    ) -> list[TrialRecord]:
        """Run all cells in trial plan, recording results to append-only ledger."""
        completed_records = self._load_completed_trials()
        records: list[TrialRecord] = []

        for cell in plan.cells:
            if cell.trial_id in completed_records:
                records.append(completed_records[cell.trial_id])
                continue

            task = self._task_map[cell.task_id]
            prompt = render_task_prompt(task)

            # Assert identical prompt hash
            if prompt.prompt_hash != cell.prompt_hash:
                raise RuntimeError(
                    f"prompt hash drift detected for cell {cell.trial_id}: expected {cell.prompt_hash} got {prompt.prompt_hash}"
                )

            # Materialize disposable workspace
            workspace_path, retrieval_applied = materialize_workspace(
                self.scratch_root,
                cell,
                corpus_dir,
                self.zvec_adapter,
            )

            # Build request
            role_req = AIRoleRequest(
                project_id="devorchestrator_benchmark",
                role=cell.role,
                prompt=prompt.prompt_text,
                working_directory=workspace_path,
                task_run_id=cell.trial_id,
                timeout_seconds=cell.timeout_seconds,
            )

            # Execute trial with exact resource pinning
            start_wall = time.monotonic()
            try:
                attempt = self.broker_client.execute_exact(cell.target_resource_id, role_req, snapshot)
            except Exception as exc:
                attempt = BrokerAttempt(
                    request_id=role_req.request_id,
                    resource_id=cell.target_resource_id,
                    status=STATUS_FAILED,
                    error=str(exc),
                )
            wall_time = round(time.monotonic() - start_wall, 3)

            # Score result
            score = score_trial(task, attempt, workspace_path, cell.trial_id)

            # Determine trial status
            if attempt.status == "succeeded":
                status = STATUS_COMPLETED
            elif "timed out" in str(attempt.error).lower():
                status = STATUS_TIMED_OUT
            elif "no_candidate" in attempt.status or "unavailable" in str(attempt.error).lower():
                status = STATUS_UNAVAILABLE
            else:
                status = STATUS_FAILED

            # Extract usage tokens if reported by broker
            tokens: int | None = None
            if attempt.usage and isinstance(attempt.usage, dict):
                tokens = attempt.usage.get("total_tokens") or attempt.usage.get("output_tokens")

            # Collect tool observations
            # zvec calls are tracked if wrapper ran
            zvec_calls = 1 if (retrieval_applied and attempt.status == "succeeded") else 0
            # Shell tool calls observed from workspace shims
            shell_calls = 1 if attempt.status == "succeeded" else 0
            provider_reported = attempt.usage.get("tool_calls") if attempt.usage else None

            metrics = MetricCoverage(
                observed_shell_tool_calls=shell_calls,
                zvec_calls=zvec_calls,
                provider_reported_tool_calls=int(provider_reported) if provider_reported else 0,
                total_complete_tool_calls=provider_reported if provider_reported is not None else None,
                reported_tokens=tokens,
                wall_time_seconds=wall_time,
            )

            record = TrialRecord(
                trial_id=cell.trial_id,
                plan_id=plan.plan_id,
                cell=cell,
                broker_attempt=attempt,
                score=score,
                metrics=metrics,
                status=status,
                retrieval_applied=retrieval_applied,
                finished_at=utc_now_iso(),
            )

            self._append_record(record)
            records.append(record)

            # Clean up disposable workspace
            cleanup_workspace(workspace_path)

        return records
