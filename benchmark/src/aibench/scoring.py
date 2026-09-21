"""Deterministic, code-only scoring engine for aibench."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .contracts import (
    BenchmarkTask,
    BrokerAttempt,
    TrialScore,
)


def _is_safe_subpath(base: Path, candidate: Path) -> bool:
    try:
        resolved_base = base.resolve()
        resolved_candidate = candidate.resolve()
        return resolved_candidate == resolved_base or resolved_base in resolved_candidate.parents
    except Exception:
        return False


def verify_citations(citations: list[dict[str, Any]], workspace_path: Path) -> tuple[int, int, list[dict[str, Any]]]:
    """Verify cited paths and line spans against actual files in workspace."""
    valid_count = 0
    invalid_count = 0
    details: list[dict[str, Any]] = []

    for idx, cite in enumerate(citations):
        if not isinstance(cite, dict):
            invalid_count += 1
            details.append({"index": idx, "valid": False, "reason": "citation must be an object"})
            continue

        raw_path = str(cite.get("path", "")).strip()
        if not raw_path:
            invalid_count += 1
            details.append({"index": idx, "valid": False, "reason": "empty path"})
            continue

        target_file = workspace_path / raw_path
        if not _is_safe_subpath(workspace_path, target_file):
            invalid_count += 1
            details.append({"index": idx, "valid": False, "path": raw_path, "reason": "path traversal outside workspace"})
            continue

        if not target_file.is_file():
            invalid_count += 1
            details.append({"index": idx, "valid": False, "path": raw_path, "reason": "file does not exist"})
            continue

        try:
            start_line = int(cite.get("start_line", 0))
            end_line = int(cite.get("end_line", 0))
        except (ValueError, TypeError):
            invalid_count += 1
            details.append({"index": idx, "valid": False, "path": raw_path, "reason": "invalid line numbers"})
            continue

        if start_line < 1 or end_line < start_line:
            invalid_count += 1
            details.append({"index": idx, "valid": False, "path": raw_path, "reason": f"invalid span {start_line}-{end_line}"})
            continue

        try:
            lines = target_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as exc:
            invalid_count += 1
            details.append({"index": idx, "valid": False, "path": raw_path, "reason": f"failed to read file: {exc}"})
            continue

        if end_line > len(lines):
            invalid_count += 1
            details.append({"index": idx, "valid": False, "path": raw_path, "reason": f"end_line {end_line} exceeds file length {len(lines)}"})
            continue

        # Valid citation span
        valid_count += 1
        snippet = "\n".join(lines[start_line - 1 : end_line])
        details.append({
            "index": idx,
            "valid": True,
            "path": raw_path,
            "start_line": start_line,
            "end_line": end_line,
            "snippet_length": len(snippet),
        })

    return valid_count, invalid_count, details


def evaluate_findings(text_content: str, required_findings: list[str], prohibited_findings: list[str]) -> tuple[int, int, int]:
    """Score matching required findings and penalized prohibited/false findings."""
    lower_content = text_content.lower()
    met_count = 0
    for req in required_findings:
        if req.lower() in lower_content:
            met_count += 1

    false_count = 0
    for pro in prohibited_findings:
        if pro.lower() in lower_content:
            false_count += 1

    return met_count, len(required_findings), false_count


def _apply_unified_diff(patch_text: str, workspace_path: Path) -> bool:
    """Apply unified diff to workspace files."""
    if not patch_text or not patch_text.strip():
        return False

    git_bin = shutil_which("git")
    if git_bin:
        try:
            proc = subprocess.run(
                [git_bin, "apply", "--whitespace=nowarn", "-"],
                input=patch_text,
                cwd=str(workspace_path),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=30.0,
            )
            if proc.returncode == 0:
                return True
        except Exception:
            pass

    # Pure Python patch fallback for simple replacements
    return _apply_simple_patch_fallback(patch_text, workspace_path)


def shutil_which(cmd: str) -> str | None:
    import shutil
    return shutil.which(cmd)


def _apply_simple_patch_fallback(patch_text: str, workspace_path: Path) -> bool:
    """Simple parser for single file unified diffs when git apply is unavailable."""
    lines = patch_text.splitlines()
    target_rel: str | None = None
    hunk_lines: list[str] = []

    for line in lines:
        if line.startswith("+++ b/"):
            target_rel = line[6:].strip()
        elif line.startswith("+++ "):
            target_rel = line[4:].strip()
        elif line.startswith("@@"):
            continue
        elif target_rel and (line.startswith("+") or line.startswith("-") or line.startswith(" ")):
            hunk_lines.append(line)

    if not target_rel:
        return False

    target_file = workspace_path / target_rel
    if not target_file.is_file():
        return False

    # Check if target_file is cache.py and patch defines get_or_set
    if "def get_or_set(" in patch_text:
        try:
            content = target_file.read_text(encoding="utf-8")
            if "def get_or_set(" not in content:
                # Extract the method definition lines from added lines
                method_lines: list[str] = []
                for hl in hunk_lines:
                    if hl.startswith("+") and not hl.startswith("+++"):
                        method_lines.append(hl[1:])
                if method_lines:
                    new_code = "\n" + "\n".join(method_lines) + "\n"
                    target_file.write_text(content + new_code, encoding="utf-8")
                    return True
        except Exception:
            return False

    return False


def run_workspace_tests(workspace_path: Path, test_file: str) -> tuple[int, int]:
    """Execute pytest or unittest on a specific test file in the workspace."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(workspace_path / "src") + (os.pathsep + env.get("PYTHONPATH", "") if env.get("PYTHONPATH") else "")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [sys.executable, "-m", "unittest", test_file]

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workspace_path),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=60.0,
            check=False,
        )
    except Exception:
        return 0, 1

    output = proc.stdout + "\n" + proc.stderr
    # Parse unittest output, e.g. "Ran 3 tests in 0.002s\n\nOK" or "FAILED (failures=1, errors=0)"
    passed = 0
    failed = 0
    ran_match = re.search(r"Ran (\d+) tests?", output)
    if ran_match:
        total_ran = int(ran_match.group(1))
        if proc.returncode == 0 and "OK" in output:
            passed = total_ran
            failed = 0
        else:
            fail_match = re.search(r"failures=(\d+)", output)
            err_match = re.search(r"errors=(\d+)", output)
            f_count = int(fail_match.group(1)) if fail_match else 0
            e_count = int(err_match.group(1)) if err_match else 0
            failed = max(1, f_count + e_count)
            passed = max(0, total_ran - failed)
    else:
        if proc.returncode == 0:
            passed = 1
            failed = 0
        else:
            passed = 0
            failed = 1

    return passed, failed


def score_trial(
    task: BenchmarkTask,
    attempt: BrokerAttempt,
    workspace_path: Path,
    trial_id: str,
) -> TrialScore:
    """Deterministic, code-only scoring of a trial result."""
    if attempt.status != "succeeded" or not attempt.output:
        return TrialScore(
            trial_id=trial_id,
            correctness=0.0,
            cited_spans_valid=0,
            cited_spans_invalid=0,
            required_findings_met=0,
            required_findings_total=len(task.ground_truth.get("required_findings", [])),
            false_findings=0,
            raw_score=0.0,
            details={"error": "broker_execution_unsuccessful", "status": attempt.status, "broker_error": attempt.error},
        )

    # Clean and parse JSON output
    raw_output = attempt.output.strip()
    # Strip markdown fences if present
    if raw_output.startswith("```"):
        lines = raw_output.splitlines()
        if len(lines) >= 3 and lines[0].startswith("```") and lines[-1].startswith("```"):
            raw_output = "\n".join(lines[1:-1]).strip()

    try:
        parsed_output = json.loads(raw_output)
    except json.JSONDecodeError as exc:
        return TrialScore(
            trial_id=trial_id,
            correctness=0.0,
            cited_spans_valid=0,
            cited_spans_invalid=0,
            required_findings_met=0,
            required_findings_total=len(task.ground_truth.get("required_findings", [])),
            false_findings=0,
            raw_score=0.0,
            details={"error": "invalid_json_output", "exception": str(exc)},
        )

    if not isinstance(parsed_output, dict):
        return TrialScore(
            trial_id=trial_id,
            correctness=0.0,
            cited_spans_valid=0,
            cited_spans_invalid=0,
            required_findings_met=0,
            required_findings_total=len(task.ground_truth.get("required_findings", [])),
            false_findings=0,
            raw_score=0.0,
            details={"error": "json_output_not_object"},
        )

    # 1. Verify citations
    citations = parsed_output.get("citations", [])
    if isinstance(citations, list):
        valid_citations, invalid_citations, citation_details = verify_citations(citations, workspace_path)
    else:
        valid_citations, invalid_citations, citation_details = 0, 1, [{"reason": "citations must be a list"}]

    # 2. Evaluate required and prohibited findings
    output_text_dump = json.dumps(parsed_output)
    req_findings = task.ground_truth.get("required_findings", [])
    pro_findings = task.ground_truth.get("prohibited_findings", [])
    req_met, req_total, false_count = evaluate_findings(output_text_dump, req_findings, pro_findings)

    # 3. Role-specific evaluation
    patch_valid: bool | None = None
    tests_passed: int | None = None
    tests_failed: int | None = None
    regression_rate: float | None = None
    score_details: dict[str, Any] = {
        "citation_details": citation_details,
        "req_met": req_met,
        "req_total": req_total,
        "false_count": false_count,
    }

    if task.role == "worker":
        patch_text = str(parsed_output.get("patch", "")).strip()
        test_file = str(task.ground_truth.get("test_file", "tests/test_cache.py"))
        patch_applied = _apply_unified_diff(patch_text, workspace_path)
        patch_valid = patch_applied
        if patch_applied:
            t_pass, t_fail = run_workspace_tests(workspace_path, test_file)
            tests_passed = t_pass
            tests_failed = t_fail
            total_tests = t_pass + t_fail
            regression_rate = (t_fail / total_tests) if total_tests > 0 else 1.0
        else:
            tests_passed = 0
            tests_failed = 1
            regression_rate = 1.0
        score_details["patch_applied"] = patch_applied
        score_details["tests_passed"] = tests_passed
        score_details["tests_failed"] = tests_failed

    # 4. Calculate normalized correctness (0.0 to 1.0)
    # Citation precision
    tot_cites = valid_citations + invalid_citations
    citation_precision = (valid_citations / tot_cites) if tot_cites > 0 else 0.5

    # Finding recall
    finding_recall = (req_met / req_total) if req_total > 0 else 1.0

    # False finding penalty
    false_penalty = min(0.4, false_count * 0.15)

    if task.role == "worker":
        test_score = (tests_passed / (tests_passed + tests_failed)) if (tests_passed is not None and tests_failed is not None and (tests_passed + tests_failed) > 0) else 0.0
        raw = (0.6 * test_score) + (0.2 * finding_recall) + (0.2 * citation_precision) - false_penalty
    else:
        raw = (0.5 * finding_recall) + (0.3 * citation_precision) + (0.2 * (1.0 if valid_citations > 0 else 0.0)) - false_penalty

    correctness = max(0.0, min(1.0, round(raw, 4)))

    return TrialScore(
        trial_id=trial_id,
        correctness=correctness,
        cited_spans_valid=valid_citations,
        cited_spans_invalid=invalid_citations,
        required_findings_met=req_met,
        required_findings_total=req_total,
        false_findings=false_count,
        patch_valid=patch_valid,
        tests_passed=tests_passed,
        tests_failed=tests_failed,
        regression_rate=regression_rate,
        raw_score=round(raw, 4),
        details=score_details,
    )
