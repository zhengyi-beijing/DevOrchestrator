"""Executable candidate evaluation and pre-review digest computation."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from dev_orchestrator.incidents.store import load_incident_store
from dev_orchestrator.storage.json_store import read_json, utc_now_iso

EVALUATOR_SCHEMA_VERSION = 1

_FORBIDDEN_MODULES = {
    "socket",
    "requests",
    "paramiko",
    "telnetlib",
    "ftplib",
    "http.client",
    "aiohttp",
    "ctypes",
}


def _check_isolation_ast(source_code: str) -> tuple[bool, str]:
    try:
        tree = ast.parse(source_code)
    except SyntaxError as exc:
        return False, f"syntax_error: {exc}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name.split(".")[0]
                if mod in _FORBIDDEN_MODULES:
                    return False, f"forbidden_import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod in _FORBIDDEN_MODULES:
                return False, f"forbidden_import_from: {node.module}"
    return True, "clean_isolated_ast"


def _check_deduplication(
    candidate_id: str,
    manifest: dict[str, Any],
    source_code: str,
    runtime_root: Path,
) -> tuple[bool, str]:
    try:
        store = load_incident_store(runtime_root)
        cands = store.index.get("candidates", {})
        cand_sha = hashlib.sha256(source_code.encode("utf-8")).hexdigest()
        for cid, cdata in cands.items():
            if cid != candidate_id:
                if cdata.get("candidate_content_sha256") == cand_sha:
                    return False, f"duplicate_candidate_content: {cid}"
    except Exception:
        pass
    return True, "unique_candidate"


def run_executable_candidate_gates(
    candidate_id: str,
    cand_dir: Path,
    manifest: dict[str, Any],
    source_code: str,
    runtime_root: Path,
) -> dict[str, Any]:
    """Execute the five real promotion gates in a temporary directory."""
    with tempfile.TemporaryDirectory() as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        test_file = tmp_dir / f"test_candidate_{candidate_id}.py"
        test_file.write_text(source_code, encoding="utf-8")

        # Gate 4: Isolation
        iso_ok, iso_reason = _check_isolation_ast(source_code)
        isolation = {
            "verdict": iso_ok,
            "evidence_hash": hashlib.sha256(f"isolation:{iso_reason}".encode("utf-8")).hexdigest()[:16],
        }

        # Gate 5: Deduplication
        dedup_ok, dedup_reason = _check_deduplication(candidate_id, manifest, source_code, runtime_root)
        deduplication = {
            "verdict": dedup_ok,
            "evidence_hash": hashlib.sha256(f"dedup:{dedup_reason}".encode("utf-8")).hexdigest()[:16],
        }

        # Gate 1: Reproduction (fail-before)
        # Running the test in failing mode MUST produce a failure (exit code != 0)
        env_failing = dict(os.environ)
        env_failing["DEVORCH_FIXTURE_MODE"] = "failing"
        proc_failing = subprocess.run(
            [sys.executable, "-m", "unittest", test_file.name],
            cwd=str(tmp_dir),
            capture_output=True,
            text=True,
            env=env_failing,
        )
        reproduced = proc_failing.returncode != 0
        repro_evidence = (proc_failing.stderr + proc_failing.stdout).strip()
        reproduction = {
            "verdict": reproduced,
            "evidence_hash": hashlib.sha256(f"repro:{reproduced}:{repro_evidence[:200]}".encode("utf-8")).hexdigest()[:16],
        }

        # Gate 2: Discrimination (pass-after)
        # Running the test in corrected mode MUST pass (exit code == 0)
        env_corrected = dict(os.environ)
        env_corrected["DEVORCH_FIXTURE_MODE"] = "corrected"
        proc_corrected = subprocess.run(
            [sys.executable, "-m", "unittest", test_file.name],
            cwd=str(tmp_dir),
            capture_output=True,
            text=True,
            env=env_corrected,
        )
        discriminated = proc_corrected.returncode == 0
        discrim_evidence = (proc_corrected.stdout + proc_corrected.stderr).strip()
        discrimination = {
            "verdict": discriminated,
            "evidence_hash": hashlib.sha256(f"discrim:{discriminated}:{discrim_evidence[:200]}".encode("utf-8")).hexdigest()[:16],
        }

        # Gate 3: Stability (repeated runs produce identical passing outcome)
        stable = True
        stability_outputs = []
        if discriminated:
            for _ in range(3):
                proc_st = subprocess.run(
                    [sys.executable, "-m", "unittest", test_file.name],
                    cwd=str(tmp_dir),
                    capture_output=True,
                    text=True,
                    env=env_corrected,
                )
                stability_outputs.append(proc_st.returncode)
                if proc_st.returncode != 0:
                    stable = False
                    break
        else:
            stable = False

        stability = {
            "verdict": stable,
            "evidence_hash": hashlib.sha256(f"stability:{stable}:{stability_outputs}".encode("utf-8")).hexdigest()[:16],
        }

        return {
            "reproduction": reproduction,
            "discrimination": discrimination,
            "stability": stability,
            "isolation": isolation,
            "deduplication": deduplication,
        }


def compute_pre_review_digest(
    candidate_content_sha256: str,
    executable_gates: dict[str, Any],
    schema_version: int = EVALUATOR_SCHEMA_VERSION,
) -> str:
    """Canonical digest strictly covering candidate content hash and five executable gates.

    Excludes review state, run IDs, timestamps, and volatile execution metadata.
    """
    normalized_gates: dict[str, Any] = {}
    for gate_name in sorted(("reproduction", "discrimination", "stability", "isolation", "deduplication")):
        gate_info = executable_gates.get(gate_name) or {}
        if isinstance(gate_info, dict):
            normalized_gates[gate_name] = {
                "verdict": bool(gate_info.get("verdict")),
                "evidence_hash": str(gate_info.get("evidence_hash") or ""),
            }
        else:
            normalized_gates[gate_name] = {
                "verdict": bool(gate_info),
                "evidence_hash": "",
            }

    digest_input = {
        "schema_version": schema_version,
        "candidate_content_sha256": candidate_content_sha256,
        "executable_gates": normalized_gates,
    }
    canonical = json.dumps(digest_input, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def evaluate_promotion(
    runtime_root: Path | str,
    candidate_id: str,
    runner: Callable[[str, Path], dict[str, Any]] | None = None,
    gate_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute and persist only the five executable gates and compute pre_review_digest."""
    runtime = Path(runtime_root)
    cand_dir = runtime / "incident-packets" / "candidates" / candidate_id
    manifest_file = cand_dir / "manifest.json"
    source_file = cand_dir / f"test_candidate_{candidate_id}.py"

    if not manifest_file.is_file() or not source_file.is_file():
        raise FileNotFoundError(f"Candidate {candidate_id} files not found under {cand_dir}")

    manifest = read_json(manifest_file, {})
    content = source_file.read_text(encoding="utf-8")
    content_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()

    if runner is not None:
        executable_gates = runner(candidate_id, cand_dir)
    else:
        executable_gates = run_executable_candidate_gates(candidate_id, cand_dir, manifest, content, runtime)

    if gate_overrides:
        for k, v in gate_overrides.items():
            if k in executable_gates:
                if isinstance(v, dict):
                    executable_gates[k] = v
                else:
                    executable_gates[k] = {"verdict": bool(v), "evidence_hash": ""}

    pre_review_digest = compute_pre_review_digest(content_sha, executable_gates)

    snapshot_data = {
        "schema_version": EVALUATOR_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "candidate_content_sha256": content_sha,
        "executable_gates": executable_gates,
        "pre_review_digest": pre_review_digest,
        "evaluated_at": utc_now_iso(),
    }

    # Persist gate snapshot under candidate directory
    snapshot_rel = f"candidates/{candidate_id}/gate_snapshot.json"
    store = load_incident_store(runtime)

    def _persist_snapshot(idx: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        cands = idx.setdefault("candidates", {})
        cinfo = cands.setdefault(candidate_id, {})
        cinfo["pre_review_digest"] = pre_review_digest
        cinfo["executable_gates_passed"] = all(
            bool((executable_gates.get(g) or {}).get("verdict"))
            for g in ("reproduction", "discrimination", "stability", "isolation", "deduplication")
        )
        return idx, {snapshot_rel: json.dumps(snapshot_data, indent=2)}

    store.execute_txn("persist_gate_snapshot", _persist_snapshot)

    # Derive review gate separately without modifying pre_review_digest
    from dev_orchestrator.incidents.review import resolve_candidate_review
    review_status = resolve_candidate_review(runtime, candidate_id)

    all_exec_passed = all(
        bool((executable_gates.get(g) or {}).get("verdict"))
        for g in ("reproduction", "discrimination", "stability", "isolation", "deduplication")
    )
    promotable = all_exec_passed and bool(review_status.get("accepted"))

    promotion_decision_digest = None
    if promotable:
        rev_id = str(review_status.get("review_id") or "")
        decision_raw = f"{pre_review_digest}:{rev_id}:accepted"
        promotion_decision_digest = hashlib.sha256(decision_raw.encode("utf-8")).hexdigest()

    return {
        "executable_gates": executable_gates,
        "pre_review_digest": pre_review_digest,
        "review_gate": review_status,
        "promotable": promotable,
        "promotion_decision_digest": promotion_decision_digest,
    }
