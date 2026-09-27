"""Authoritative resolution of git metadata paths for any checkout shape.

``repo / ".git"`` is a directory only in a primary checkout.  In a linked
worktree it is a *file* containing ``gitdir: <common>/.git/worktrees/<name>``,
so joining onto it and calling ``mkdir(parents=True)`` raises ``FileExistsError``
(WinError 183 on Windows).  The self-hosted DevOrchestrator controller runs from
exactly such a worktree, so every control-plane path that needs git metadata must
ask git instead of assuming a layout.

Both helpers fail closed: they return ``None`` when git cannot answer, and
callers must treat that as a refusal rather than guessing a fallback path.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from dev_orchestrator.platform.process import hidden_subprocess_kwargs


def _rev_parse(repo: Path | str, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(repo), "rev-parse", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, check=False, **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def resolve_git_path(repo: Path | str, relative: str) -> Path | None:
    """Return the absolute location git uses for ``relative`` inside its git dir.

    Shared metadata such as ``info/exclude`` lives in the *common* git directory,
    so a linked worktree resolves it to the primary checkout's ``.git/info/exclude``.
    Returns ``None`` when git cannot resolve the path.
    """
    value = _rev_parse(repo, "--git-path", relative)
    if value is None:
        return None
    path = Path(value)
    return path if path.is_absolute() else Path(repo) / path


def resolve_git_dir(repo: Path | str) -> Path | None:
    """Return this checkout's own git directory, per-worktree for linked worktrees.

    Returns ``None`` when git cannot resolve it or the result is not a directory.
    """
    value = _rev_parse(repo, "--absolute-git-dir")
    if value is None:
        return None
    path = Path(value)
    return path if path.is_dir() else None
