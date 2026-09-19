"""Fixed review runner process executing review packets and producing durable artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.ai.contracts import AIRoleRequest, ResourceContext
from dev_orchestrator.ai.execution_port import AIExecutionPort
from dev_orchestrator.ai.runtime_config import load_aibroker_execution_port
from dev_orchestrator.jobs.models import JobRecord
from dev_orchestrator.jobs.store import ExecutionJobStore
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json

from .models import (
    FINDING_SEVERITIES,
    ReviewCoverage,
    ReviewFinding,
    ReviewManifest,
    ReviewPacket,
    ReviewRequest,
    ReviewResult,
    ReviewSession,
    _normalize_repo_path,
    compute_finding_fingerprint,
    to_sarif,
)


def partition_packets(
    selected_files: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    limits: dict[str, int],
    session_id: str,
) -> list[ReviewPacket]:
    """Deterministically partition selected files into bounded packets."""
    max_files = int(limits.get("max_packet_files", 10))
    max_bytes = int(limits.get("max_packet_bytes", 200 * 1024))

    packets: list[ReviewPacket] = []
    current_files: list[dict[str, Any]] = []
    current_bytes = 0

    sorted_files = sorted(selected_files, key=lambda f: str(f.get("path", "")))

    for f in sorted_files:
        f_size = int(f.get("size_bytes", 0))
        if current_files and (len(current_files) >= max_files or current_bytes + f_size > max_bytes):
            p_idx = len(packets)
            p_id = f"{session_id}:packet:{p_idx}"
            packets.append(
                ReviewPacket(
                    packet_id=p_id,
                    session_id=session_id,
                    packet_index=p_idx,
                    files=list(current_files),
                    rules=list(rules),
                    broker_request_id=f"ocr_review:{session_id}:{p_idx}",
                )
            )
            current_files = []
            current_bytes = 0

        current_files.append(f)
        current_bytes += f_size

    if current_files:
        p_idx = len(packets)
        p_id = f"{session_id}:packet:{p_idx}"
        packets.append(
            ReviewPacket(
                packet_id=p_id,
                session_id=session_id,
                packet_index=p_idx,
                files=list(current_files),
                rules=list(rules),
                broker_request_id=f"ocr_review:{session_id}:{p_idx}",
            )
        )

    return packets


def build_packet_prompt(
    packet: ReviewPacket,
    repo_path: Path,
    request: ReviewRequest,
) -> str:
    """Build review prompt with strict evidence-only instructions and no lifecycle tokens."""
    prompt_parts = [
        "You are an independent semantic code reviewer inspecting code for compliance with project rules.",
        "Inspect the repository files provided below against the active rules.",
        "CRITICAL REQUIREMENTS:",
        "1. You return EVIDENCE ONLY: localized findings, reviewed files, and skipped files.",
        "2. Do NOT output any lifecycle decision (e.g. 'next', 'remediate', 'approve', 'reject') or next action.",
        "   Any lifecycle tokens are strictly forbidden and will cause rejection.",
        "3. Output exactly ONE JSON object with no markdown fences, no explanatory text, and this exact schema:",
        json.dumps({
            "reviewed_files": ["<repo_relative_path>", "..."],
            "skipped_files": [{"path": "<repo_relative_path>", "reason": "<reason>"}],
            "findings": [
                {
                    "file": "<repo_relative_path>",
                    "start_line": 1,
                    "end_line": 10,
                    "severity": "blocking|warning|info",
                    "category": "<category>",
                    "rule_id": "<rule_id>",
                    "message": "<clear explanation>",
                    "evidence": "<code snippet>",
                }
            ]
        }, indent=2),
        "\n[ACTIVE_RULES]",
    ]

    for r in packet.rules:
        prompt_parts.append(
            f"- Rule ID: {r.get('rule_id')}\n"
            f"  Title: {r.get('title')}\n"
            f"  Severity: {r.get('severity')}\n"
            f"  Category: {r.get('category')}\n"
            f"  Description: {r.get('description')}\n"
        )
    prompt_parts.append("[/ACTIVE_RULES]\n\n[FILES_TO_REVIEW]")

    for f in packet.files:
        rel_p = str(f.get("path", ""))
        full_p = repo_path / rel_p
        prompt_parts.append(f"\n--- File: {rel_p} ---")
        if full_p.is_file():
            try:
                content = full_p.read_text(encoding="utf-8", errors="replace")
                lines = content.splitlines()
                # Bounded snippet (up to 500 lines)
                numbered_lines = [f"{i+1}: {line}" for i, line in enumerate(lines[:500])]
                prompt_parts.append("\n".join(numbered_lines))
                if len(lines) > 500:
                    prompt_parts.append(f"\n[Truncated: {len(lines) - 500} lines remaining]")
            except Exception as exc:
                prompt_parts.append(f"[Error reading file: {exc}]")
        else:
            prompt_parts.append("[File not found on disk]")
        prompt_parts.append(f"--- End File: {rel_p} ---")

    prompt_parts.append("[/FILES_TO_REVIEW]")
    return "\n".join(prompt_parts)


def parse_packet_output(
    raw_output: str,
    packet: ReviewPacket,
    repo_path: Path,
    resource_context: Optional[dict[str, Any]] = None,
) -> tuple[list[ReviewFinding], list[str], list[dict[str, Any]]]:
    """Parse and validate reviewer model response, rejecting lifecycle tokens."""
    if not isinstance(raw_output, str) or not raw_output.strip():
        raise ValueError("reviewer model output is empty")

    cleaned = raw_output.strip()
    if cleaned.startswith("```"):
        # Strip markdown code block if model wrapped it
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"reviewer output is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("reviewer JSON output must be an object")

    # Reject forbidden lifecycle tokens
    if "decision" in data or "next_action" in data or "disposition" in data:
        raise ValueError("reviewer output contains forbidden lifecycle tokens (decision/next_action)")

    reviewed_raw = data.get("reviewed_files", [])
    skipped_raw = data.get("skipped_files", [])
    findings_raw = data.get("findings", [])

    if not isinstance(reviewed_raw, list):
        raise ValueError("reviewed_files must be a list")
    if not isinstance(skipped_raw, list):
        raise ValueError("skipped_files must be a list")
    if not isinstance(findings_raw, list):
        raise ValueError("findings must be a list")

    packet_paths = {str(f.get("path")) for f in packet.files}

    reviewed_files: list[str] = []
    for p in reviewed_raw:
        if isinstance(p, str):
            try:
                norm_p = _normalize_repo_path(p)
                if norm_p in packet_paths:
                    reviewed_files.append(norm_p)
            except ValueError:
                pass

    skipped_files: list[dict[str, Any]] = []
    for s in skipped_raw:
        raw_p: Any = None
        raw_reason: Any = None
        if isinstance(s, dict) and s.get("path"):
            raw_p = s.get("path")
            raw_reason = s.get("reason")
        elif isinstance(s, str):
            raw_p = s
            raw_reason = None

        if raw_p:
            try:
                norm_p = _normalize_repo_path(str(raw_p))
            except ValueError:
                continue
            if norm_p not in packet_paths:
                continue

            cleaned_reason = str(raw_reason or "").strip()
            is_valid = bool(cleaned_reason) and cleaned_reason.lower() not in {
                "skipped", "skip", "skipped_by_reviewer", "none", "n/a", "no reason", "uninspected"
            }
            skipped_files.append({
                "path": norm_p,
                "reason": cleaned_reason if is_valid else (cleaned_reason or "missing_skip_reason"),
                "valid": is_valid,
            })

    findings: list[ReviewFinding] = []
    for f in findings_raw:
        if not isinstance(f, dict):
            continue
        raw_file = f.get("file")
        if not raw_file:
            continue
        norm_file = _normalize_repo_path(str(raw_file))
        if norm_file not in packet_paths:
            # Reject finding on file not in packet
            continue

        start_l = int(f.get("start_line", 1))
        end_l = int(f.get("end_line", start_l))
        sev = str(f.get("severity", "blocking")).lower()
        if sev not in FINDING_SEVERITIES:
            sev = "blocking"

        cat = str(f.get("category", "correctness"))
        rid = str(f.get("rule_id", "general"))
        msg = str(f.get("message", "Rule violation detected"))
        ev = f.get("evidence")

        # Validate line bounds against file on disk if readable
        f_disk = repo_path / norm_file
        if f_disk.is_file():
            try:
                line_count = len(f_disk.read_text(encoding="utf-8", errors="replace").splitlines())
                if line_count > 0:
                    start_l = min(max(1, start_l), line_count)
                    end_l = min(max(start_l, end_l), line_count)
            except Exception:
                pass

        rf = ReviewFinding(
            file=norm_file,
            start_line=start_l,
            end_line=end_l,
            severity=sev,
            category=cat,
            rule_id=rid,
            message=msg,
            evidence=str(ev) if ev else None,
            resource_context=resource_context,
            session_id=packet.session_id,
            packet_id=packet.packet_id,
        )
        findings.append(rf)

    return findings, reviewed_files, skipped_files


class ReviewRunner:
    """Orchestrates packet execution, model delegation, coverage accounting and artifact writing."""

    def __init__(
        self,
        job_dir: Path | str,
        *,
        port: Optional[AIExecutionPort] = None,
    ) -> None:
        self.job_dir = Path(job_dir).resolve()
        self.runtime_root = self.job_dir.parent.parent
        self.store = ExecutionJobStore(self.runtime_root)
        self.port = port or load_aibroker_execution_port(self.runtime_root)

    def run(self) -> int:
        input_file = self.job_dir / "input.json"
        if not input_file.is_file():
            sys.stderr.write(f"runner error: {input_file} not found\n")
            return 1

        rec = self.store.get(self.job_dir.name)
        if rec is None:
            sys.stderr.write(f"runner error: job record {self.job_dir.name} not found\n")
            return 1

        if rec.state == "queued":
            def _mark_running(r: JobRecord) -> None:
                if r.state == "queued":
                    r.transition_to("running", reason="review_execution_started", timestamp=utc_now_iso())
            self.store.update(rec.job_id, _mark_running)
            rec = self.store.get(self.job_dir.name) or rec

        try:
            raw_input = json.loads(input_file.read_text(encoding="utf-8"))
        except Exception as exc:
            sys.stderr.write(f"runner error: invalid input.json: {exc}\n")
            return 1

        req_data = raw_input.get("request", {})
        man_data = raw_input.get("manifest", {})
        try:
            request = ReviewRequest.from_dict(req_data)
            manifest = ReviewManifest.from_dict(man_data)
        except Exception as exc:
            sys.stderr.write(f"runner error: invalid request/manifest schema: {exc}\n")
            return 1

        repo_path = Path(rec.working_directory).resolve()
        if not repo_path.is_dir():
            sys.stderr.write(f"runner error: repo_path {repo_path} is not a directory\n")
            return 1

        packets = partition_packets(
            manifest.selected_files,
            manifest.rules,
            request.file_limits,
            request.request_id,
        )

        all_findings: list[ReviewFinding] = []
        all_reviewed_files: set[str] = set()
        all_valid_skipped_files: dict[str, str] = {}
        all_invalid_skipped_files: dict[str, str] = {}
        failed_files: set[str] = set()

        packet_records: list[dict[str, Any]] = []

        prev_res = request.metadata.get("previous_resource_context") or request.metadata.get("worker_resource_context")
        prev_ctx = None
        if isinstance(prev_res, dict) and prev_res.get("resource_id"):
            prev_ctx = ResourceContext(
                resource_id=prev_res.get("resource_id"),
                provider=prev_res.get("provider"),
                account=prev_res.get("account"),
                model=prev_res.get("model"),
            )

        independence = request.metadata.get("independence", "resource" if prev_ctx else "none")
        if independence != "none" and prev_ctx is None:
            independence = "none"

        failed_packets = 0
        for pkt in packets:
            prompt = build_packet_prompt(pkt, repo_path, request)
            ai_req = AIRoleRequest(
                project_id=request.project_id,
                task_run_id=request.task_id,
                stage_run_id="review",
                role_run_id=f"reviewer-{pkt.packet_id.replace(':', '-')}",
                request_id=pkt.broker_request_id,
                role="reviewer",
                prompt=prompt,
                working_directory=repo_path,
                quality=request.metadata.get("quality", "high"),
                independence=independence,
                previous_resource_context=prev_ctx,
                timeout_seconds=float(request.metadata.get("timeout_seconds", 300.0)),
            )

            res = None
            if self.port is not None:
                try:
                    res = self.port.execute(ai_req)
                except Exception as exc:
                    sys.stderr.write(f"runner packet {pkt.packet_id} dispatch error: {exc}\n")

            if res is not None and res.status == "succeeded" and res.output:
                res_ctx = None
                if res.resource_context:
                    res_ctx = {
                        "resource_id": res.resource_context.resource_id,
                        "provider": res.resource_context.provider,
                        "account": res.resource_context.account,
                        "model": res.resource_context.model,
                    }
                try:
                    p_findings, p_reviewed, p_skipped = parse_packet_output(
                        res.output, pkt, repo_path, resource_context=res_ctx
                    )
                    all_findings.extend(p_findings)
                    all_reviewed_files.update(p_reviewed)
                    for sk in p_skipped:
                        if sk.get("valid"):
                            all_valid_skipped_files[sk["path"]] = sk.get("reason", "skipped")
                        else:
                            all_invalid_skipped_files[sk["path"]] = sk.get("reason", "invalid_skip_reason")
                except Exception as exc:
                    sys.stderr.write(f"runner packet {pkt.packet_id} parse error: {exc}\n")
                    failed_packets += 1
                    for f in pkt.files:
                        failed_files.add(str(f.get("path")))
            else:
                failed_packets += 1
                for f in pkt.files:
                    failed_files.add(str(f.get("path")))

            packet_records.append(pkt.to_dict())

        # Coverage accounting
        selected_set = {str(f.get("path")) for f in manifest.selected_files}
        files_coverage: dict[str, dict[str, Any]] = {}
        for fp in selected_set:
            if fp in failed_files:
                files_coverage[fp] = {"status": "failed", "reason": "packet_execution_failure"}
            elif fp in all_reviewed_files:
                files_coverage[fp] = {"status": "reviewed", "reason": "reviewed"}
            elif fp in all_valid_skipped_files:
                files_coverage[fp] = {"status": "skipped", "reason": all_valid_skipped_files[fp]}
            elif fp in all_invalid_skipped_files:
                files_coverage[fp] = {"status": "unreviewed", "reason": f"invalid_skip_reason: {all_invalid_skipped_files[fp]}"}
            else:
                files_coverage[fp] = {"status": "unreviewed", "reason": "uninspected"}

        for exf in manifest.excluded_files:
            ep = str(exf.get("path"))
            files_coverage[ep] = {"status": "excluded", "reason": exf.get("reason", "excluded_during_prep")}

        reviewed_count = len([f for f in files_coverage.values() if f["status"] == "reviewed"])
        skipped_count = len([f for f in files_coverage.values() if f["status"] == "skipped"])
        failed_count = len([f for f in files_coverage.values() if f["status"] == "failed"])
        excluded_count = len([f for f in files_coverage.values() if f["status"] == "excluded"])
        selected_count = len(selected_set)

        if failed_count > 0 or failed_packets > 0:
            completeness = "failed"
        elif selected_count == 0:
            completeness = "failed"
        elif (reviewed_count + skipped_count) < selected_count:
            completeness = "partial"
        else:
            completeness = "complete"

        cov_rate = (reviewed_count + skipped_count) / max(1, selected_count)

        coverage = ReviewCoverage(
            completeness=completeness,
            selected_count=selected_count,
            reviewed_count=reviewed_count,
            skipped_count=skipped_count,
            failed_count=failed_count,
            excluded_count=excluded_count,
            coverage_rate=round(cov_rate, 4),
            files=files_coverage,
        )

        # Disposition check
        blocking_findings = [
            f for f in all_findings if f.severity in request.blocking_severities
        ]
        if completeness != "complete":
            disposition = "failed"
            if selected_count == 0:
                reason = "Review scope is empty: zero files selected for inspection"
            else:
                reason = f"Review coverage is {completeness} ({reviewed_count}/{selected_count} files reviewed)"
        elif blocking_findings:
            disposition = "remediate"
            reason = f"Detected {len(blocking_findings)} blocking finding(s)"
        else:
            disposition = "next"
            reason = "Review passed with complete coverage and zero blocking findings"

        # Write artifacts
        sarif_doc = to_sarif(all_findings, {"start_time": rec.timestamps.get("started_at"), "end_time": utc_now_iso()})
        findings_payload = [f.to_dict() for f in all_findings]
        coverage_payload = coverage.to_dict()

        f_desc = self.store.save_output_artifact(rec.job_id, "findings.json", findings_payload)
        c_desc = self.store.save_output_artifact(rec.job_id, "coverage.json", coverage_payload)
        s_desc = self.store.save_output_artifact(rec.job_id, "review.sarif", sarif_doc)

        artifacts_dict = {
            "findings.json": f_desc,
            "coverage.json": c_desc,
            "review.sarif": s_desc,
        }

        review_result = ReviewResult(
            session_id=request.request_id,
            job_id=rec.job_id,
            disposition=disposition,
            completeness=completeness,
            findings=all_findings,
            coverage=coverage,
            artifacts=artifacts_dict,
            reason=reason,
        )

        session = ReviewSession(
            session_id=request.request_id,
            request=request,
            state="completed",
            job_id=rec.job_id,
            manifest=manifest,
            packets=packets,
            result=review_result,
            timestamps={
                "created_at": rec.timestamps.get("created_at"),
                "started_at": rec.timestamps.get("started_at"),
                "completed_at": utc_now_iso(),
            },
        )
        self.store.save_output_artifact(rec.job_id, "session.json", session.to_dict())

        # Save result.json to mark P14 job completion
        result_record = {
            "job_id": rec.job_id,
            "exit_code": 0,
            "outcome": "completed",
            "error": None,
            "finished_at": utc_now_iso(),
            "artifacts": artifacts_dict,
            "disposition": disposition,
            "completeness": completeness,
            "findings_count": len(all_findings),
        }
        self.store.save_result(rec.job_id, result_record)

        def _mark_done(r: JobRecord) -> None:
            if r.state == "queued":
                r.transition_to("running", reason="review_execution_started", timestamp=utc_now_iso())
            r.transition_to("completed", reason=reason, timestamp=utc_now_iso())
            r.exit_code = 0
            r.terminal = result_record

        self.store.update(rec.job_id, _mark_done)
        return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="DevOrchestrator Durable Review Runner")
    parser.add_argument("--job-dir", required=True, help="Path to supervisor job directory")
    args = parser.parse_args(argv)

    runner = ReviewRunner(args.job_dir)
    return runner.run()


if __name__ == "__main__":
    sys.exit(main())
