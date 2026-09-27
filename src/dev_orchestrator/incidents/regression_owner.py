"""Verified regression owner repository resolution for candidate materialization and promotion."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dev_orchestrator.core.git_paths import resolve_git_dir
from dev_orchestrator.core.watchdog import canonical_path


def _verify_owner_repo(repo_path: Path, tests_root: Path) -> tuple[bool, str]:
    """Verify repository truth, pyproject identity, src/dev_orchestrator/__init__.py, and tests_root."""
    if not repo_path.is_dir():
        return False, f"repo_path does not exist: {repo_path}"

    # ``.git`` is a directory in a primary checkout and a *file* in a linked
    # worktree; require its presence to keep the check anchored at the checkout
    # root, then let git confirm the repository, failing closed if it cannot.
    if not (repo_path / ".git").exists():
        return False, f"not a git repository: {repo_path}"
    if resolve_git_dir(repo_path) is None:
        return False, f"git cannot resolve the repository git directory: {repo_path}"

    pyproject_file = repo_path / "pyproject.toml"
    if not pyproject_file.is_file():
        return False, f"missing pyproject.toml in {repo_path}"

    try:
        content = pyproject_file.read_text(encoding="utf-8")
        if "dev-orchestrator" not in content and "DevOrchestrator" not in content and "dev_orchestrator" not in content:
            return False, f"pyproject.toml does not declare dev-orchestrator project identity in {repo_path}"
    except Exception as exc:
        return False, f"unreadable pyproject.toml: {exc}"

    init_file = repo_path / "src" / "dev_orchestrator" / "__init__.py"
    if not init_file.is_file():
        return False, f"missing src/dev_orchestrator/__init__.py in {repo_path}"

    if not tests_root.is_dir():
        return False, f"tests root does not exist: {tests_root}"

    return True, "verified"


def resolve_regression_owner(config: dict[str, Any] | None) -> dict[str, Any]:
    """Resolve single configured and verified DevOrchestrator regression owner repository.

    Returns dict with keys:
        project_id: str | None
        repo_path: Path | None
        tests_root: Path | None
        candidates_root: Path | None
        verified: bool
        reason: str
    """
    if not isinstance(config, dict):
        return {
            "project_id": None,
            "repo_path": None,
            "tests_root": None,
            "candidates_root": None,
            "verified": False,
            "reason": "missing_or_invalid_config",
        }

    projects = config.get("projects") or []
    if not isinstance(projects, list):
        projects = []

    target_pid: str | None = None

    # 1. Top-level regression_owner
    reg_cfg = config.get("regression_owner")
    if isinstance(reg_cfg, str) and reg_cfg.strip():
        target_pid = reg_cfg.strip()
    elif isinstance(reg_cfg, dict) and reg_cfg.get("project_id"):
        target_pid = str(reg_cfg["project_id"]).strip()

    # 2. Project-level regression_owner flag
    if not target_pid:
        flagged = [
            str(p.get("project_id")) for p in projects
            if isinstance(p, dict) and p.get("regression_owner") is True
        ]
        if len(flagged) == 1:
            target_pid = flagged[0]
        elif len(flagged) > 1:
            return {
                "project_id": None,
                "repo_path": None,
                "tests_root": None,
                "candidates_root": None,
                "verified": False,
                "reason": f"multiple projects flagged as regression_owner: {flagged}",
            }

    # 3. Inference fallback: repository containing dev_orchestrator package
    if not target_pid:
        # Find repo containing this file: Path(__file__).resolve().parents[3] -> repo root
        current_repo_root = Path(__file__).resolve().parents[3]
        matches: list[dict[str, Any]] = []
        for p in projects:
            if not isinstance(p, dict) or not p.get("repo_path"):
                continue
            try:
                if canonical_path(p["repo_path"]) == canonical_path(current_repo_root):
                    matches.append(p)
            except Exception:
                pass
        if len(matches) == 1:
            target_pid = str(matches[0].get("project_id"))
        elif len(matches) > 1:
            return {
                "project_id": None,
                "repo_path": None,
                "tests_root": None,
                "candidates_root": None,
                "verified": False,
                "reason": "ambiguous regression owner: multiple configured projects match dev_orchestrator repository",
            }
        else:
            return {
                "project_id": None,
                "repo_path": None,
                "tests_root": None,
                "candidates_root": None,
                "verified": False,
                "reason": "unconfigured regression owner and no matching project found for dev_orchestrator repository",
            }

    # Locate project row
    matching_project: dict[str, Any] | None = None
    for p in projects:
        if isinstance(p, dict) and str(p.get("project_id") or p.get("id")) == target_pid:
            matching_project = p
            break

    if not matching_project or not matching_project.get("repo_path"):
        return {
            "project_id": target_pid,
            "repo_path": None,
            "tests_root": None,
            "candidates_root": None,
            "verified": False,
            "reason": f"project {target_pid!r} not found in configuration or lacks repo_path",
        }

    repo_path = Path(matching_project["repo_path"]).resolve()
    tests_root = (repo_path / "tests_py").resolve()
    candidates_root = (repo_path / "tests_candidate").resolve()

    verified, reason = _verify_owner_repo(repo_path, tests_root)
    return {
        "project_id": target_pid,
        "repo_path": repo_path,
        "tests_root": tests_root,
        "candidates_root": candidates_root,
        "verified": verified,
        "reason": reason,
    }
