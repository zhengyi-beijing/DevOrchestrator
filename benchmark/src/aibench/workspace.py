"""Deterministic workspace materialization beneath configured scratch root."""
from __future__ import annotations

import os
import shutil
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
