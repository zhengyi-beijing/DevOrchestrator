"""Deterministic workspace materialization beneath configured scratch root."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .contracts import TRACK_A, TRACK_B, TrialCell
from .zvec import ZvecAdapter


def materialize_workspace(
    scratch_root: Path,
    cell: TrialCell,
    pristine_corpus_dir: Path,
    zvec_adapter: ZvecAdapter | None = None,
) -> tuple[Path, bool]:
    """Materialize fresh disposable workspace copy of the pristine corpus revision.

    Returns (workspace_path, retrieval_applied).
    """
    scratch_root.mkdir(parents=True, exist_ok=True)
    workspace_path = scratch_root / cell.trial_id

    # If workspace already exists, remove it cleanly to ensure pristine state
    if workspace_path.exists():
        shutil.rmtree(workspace_path, ignore_errors=True)

    workspace_path.mkdir(parents=True, exist_ok=True)

    # Copy files from pristine corpus (excluding existing .aibench, .git or temporary files)
    for root_dir, dirs, files in os.walk(pristine_corpus_dir):
        # Skip .git and .aibench in source if present
        dirs[:] = [d for d in dirs if d not in {".git", ".aibench", "__pycache__", ".pytest_cache"}]
        rel_root = Path(root_dir).relative_to(pristine_corpus_dir)
        dest_dir = workspace_path / rel_root
        dest_dir.mkdir(parents=True, exist_ok=True)
        for f in files:
            if f.endswith((".pyc", ".pyo")):
                continue
            src_file = Path(root_dir) / f
            dest_file = dest_dir / f
            shutil.copy2(src_file, dest_file)

    _initialize_git_baseline(workspace_path)

    retrieval_applied = False

    # Track A receives pristine files only.
    # Track B receives untracked .aibench retrieval materials ONLY if zvec is supported.
    if cell.track == TRACK_B and zvec_adapter is not None:
        probe = zvec_adapter.probe()
        if probe.supported and probe.executable_path:
            aibench_dir = workspace_path / ".aibench"
            tools_dir = aibench_dir / "tools"
            index_dir = aibench_dir / "index"
            tools_dir.mkdir(parents=True, exist_ok=True)
            index_dir.mkdir(parents=True, exist_ok=True)

            # Build index from pristine revision
            try:
                zvec_adapter.build_index(workspace_path, index_dir)
                # Create wrapper shim script in .aibench/tools/zvec-grep.bat
                shim_bat = tools_dir / "zvec-grep.bat"
                shim_content = (
                    f"@echo off\r\n"
                    f'"{probe.executable_path}" query --index "{index_dir}" --query "%~1" %*\r\n'
                )
                shim_bat.write_text(shim_content, encoding="utf-8")
                retrieval_applied = True
            except Exception:
                retrieval_applied = False

    return workspace_path, retrieval_applied


def cleanup_workspace(workspace_path: Path) -> None:
    """Remove disposable workspace directory safely."""
    if workspace_path.exists() and workspace_path.is_dir():
        shutil.rmtree(workspace_path, ignore_errors=True)


def _initialize_git_baseline(workspace_path: Path) -> None:
    """Create a clean standalone Git baseline for broker worktree safety.

    Benchmark workspaces are disposable copies, not linked worktrees.  The
    persistent harness still requires a real Git worktree for writable roles.
    Track-B retrieval artifacts live under .aibench/ and are ignored via the
    repository-local info/exclude file so they do not dirty the baseline.
    """
    commands = (
        ["git", "init"],
        ["git", "config", "user.email", "aibench@local"],
        ["git", "config", "user.name", "AIBench"],
        ["git", "add", "-A"],
        ["git", "commit", "-m", "benchmark baseline"],
    )
    for argv in commands:
        completed = subprocess.run(
            argv,
            cwd=str(workspace_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"failed to initialize benchmark Git baseline: {detail}")

    exclude = workspace_path / ".git" / "info" / "exclude"
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write("\n.aibench/\n")
