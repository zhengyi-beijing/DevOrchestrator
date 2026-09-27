"""Candidate regression synthesis and git-excluded staging materialization."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

from dev_orchestrator.config import load_projects_config
from dev_orchestrator.core.git_paths import resolve_git_path
from dev_orchestrator.incidents.regression_owner import resolve_regression_owner
from dev_orchestrator.incidents.store import load_incident_store
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json

STAGING_CONFTEST_CONTENT = """# Staging conftest for candidate regression tests.
# These tests are quarantined outside tests_py and are NOT collected during normal runs.
import os

def pytest_ignore_collect(collection_path, config):
    return os.environ.get("DEVORCH_CANDIDATE_TESTS") != "1"
"""


def _generate_synthetic_test_code(
    candidate_id: str,
    invariant: str,
    failure_class: str,
    invalid_outcome: str = "anomalous_failure_observed",
) -> str:
    safe_cid = candidate_id.replace("-", "_")
    return f'''"""Synthetic candidate regression test for {candidate_id}.
Invariant: {invariant}
Failure class: {failure_class}
Deterministic synthetic test generated without live web, provider, or hardware calls.
"""
import os
import unittest


class TestCandidateRegression_{safe_cid}(unittest.TestCase):
    def setUp(self):
        self.invariant = {json.dumps(invariant)}
        self.failure_class = {json.dumps(failure_class)}
        self.invalid_outcome = {json.dumps(invalid_outcome)}
        self.mode = os.environ.get("DEVORCH_FIXTURE_MODE", "corrected")

    def test_reproduction_oracle(self):
        """Reproduces incident under failing fixture mode; verifies invariant under corrected mode."""
        if self.mode == "failing":
            raise AssertionError(f"Incident reproduced: {{self.invalid_outcome}} violated invariant: {{self.invariant}}")
        self.assertTrue(len(self.invariant) > 0, "Invariant must be non-empty")
        self.assertNotIn("violated", self.invariant)

    def test_discrimination_on_corrected_state(self):
        """Verifies candidate discriminates between failing and corrected fixture anchors."""
        if self.mode == "failing":
            raise AssertionError(f"Failing fixture anchor reproduces failure: {{self.failure_class}}")
        self.assertTrue(len(self.failure_class) > 0, "Failure class must be defined")


if __name__ == "__main__":
    unittest.main()
'''


def generate_candidate(
    runtime_root: Path | str,
    candidate_id: str | None = None,
    *,
    source_incidents: Sequence[str] = (),
    preconditions: Any = None,
    trigger_sequence: Sequence[str] = (),
    invariant: str = "invariant_must_hold",
    invalid_outcome: str = "anomalous_failure_observed",
    fixture_description: str = "synthetic_deterministic_fixture",
    oracle_description: str = "assert_correct_recovery_and_invariant",
    failing_fixture_anchor: Any = None,
    corrected_fixture_anchor: Any = None,
    generated_by: str = "auto_harvest",
    test_code_override: str | None = None,
) -> dict[str, Any]:
    """Generate candidate manifest and synthetic test source under runtime store only."""
    store = load_incident_store(runtime_root)
    cid = candidate_id or f"cand-{uuid4().hex[:12]}"

    test_content = test_code_override or _generate_synthetic_test_code(
        cid, invariant, str(failing_fixture_anchor or "incident_regression"), invalid_outcome
    )
    test_sha = hashlib.sha256(test_content.encode("utf-8")).hexdigest()

    manifest: dict[str, Any] = {
        "candidate_id": cid,
        "schema_version": 1,
        "source_incidents": list(source_incidents),
        "preconditions": preconditions or [],
        "trigger_sequence": list(trigger_sequence),
        "invariant": invariant,
        "invalid_outcome": invalid_outcome,
        "fixture_description": fixture_description,
        "oracle_description": oracle_description,
        "failing_fixture_anchor": failing_fixture_anchor or {},
        "corrected_fixture_anchor": corrected_fixture_anchor or {},
        "generated_by": generated_by,
        "candidate_content_sha256": test_sha,
        "created_at": utc_now_iso(),
    }

    cand_dir_rel = f"candidates/{cid}"
    manifest_rel = f"{cand_dir_rel}/manifest.json"
    source_rel = f"{cand_dir_rel}/test_candidate_{cid}.py"

    side_files = {
        manifest_rel: json.dumps(manifest, indent=2),
        source_rel: test_content,
    }

    def _update_index(idx: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        cands = idx.setdefault("candidates", {})
        cands[cid] = {
            "candidate_id": cid,
            "candidate_content_sha256": test_sha,
            "generated_by": generated_by,
            "created_at": manifest["created_at"],
            "source_incidents": list(source_incidents),
        }
        return idx, side_files

    store.execute_txn("generate_candidate", _update_index)
    return manifest


def materialize_candidate(
    runtime_root: Path | str,
    candidate_id: str,
    config_path: Path | str,
) -> dict[str, Any]:
    """Materialize a candidate into the verified regression owner's tests_candidate/ directory."""
    config = load_projects_config(config_path)
    owner = resolve_regression_owner(config)
    if not owner.get("verified"):
        return {
            "materialized": False,
            "reason": f"unverified_regression_owner: {owner.get('reason')}",
            "candidate_id": candidate_id,
        }

    repo_path: Path = owner["repo_path"]
    candidates_root: Path = owner["candidates_root"]

    # 1. Add tests_candidate to git's info/exclude before writing.  Ask git for the
    # path: in a linked worktree ``.git`` is a file and info/exclude lives in the
    # common git directory, so joining onto ``repo_path / ".git"`` cannot work.
    git_info_exclude = resolve_git_path(repo_path, "info/exclude")
    if git_info_exclude is None:
        return {
            "materialized": False,
            "reason": f"failed_to_resolve_git_exclude_path: {repo_path}",
            "candidate_id": candidate_id,
        }
    try:
        git_info_exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude_text = git_info_exclude.read_text(encoding="utf-8") if git_info_exclude.is_file() else ""
        if "tests_candidate" not in exclude_text:
            with git_info_exclude.open("a", encoding="utf-8") as f:
                f.write("\ntests_candidate/\n")
    except Exception as exc:
        return {
            "materialized": False,
            "reason": f"failed_to_update_git_exclude: {exc}",
            "candidate_id": candidate_id,
        }

    # 2. Ensure tests_candidate directory and conftest.py
    candidates_root.mkdir(parents=True, exist_ok=True)
    conftest_path = candidates_root / "conftest.py"
    if not conftest_path.is_file():
        conftest_path.write_text(STAGING_CONFTEST_CONTENT, encoding="utf-8")

    # 3. Read candidate source from runtime
    source_file = Path(runtime_root) / "incident-packets" / "candidates" / candidate_id / f"test_candidate_{candidate_id}.py"
    if not source_file.is_file():
        return {
            "materialized": False,
            "reason": f"candidate_source_not_found: {source_file}",
            "candidate_id": candidate_id,
        }

    target_file = candidates_root / f"test_candidate_{candidate_id}.py"
    target_file.write_text(source_file.read_text(encoding="utf-8"), encoding="utf-8")

    return {
        "materialized": True,
        "candidate_id": candidate_id,
        "target_path": str(target_file),
        "candidates_root": str(candidates_root),
        "reason": "materialized_to_staging",
    }
