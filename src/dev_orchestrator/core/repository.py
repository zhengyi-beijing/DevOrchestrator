"""Strict fresh repository-truth reader used by the Web Sol decision guard.

A ``RepositoryTruth`` is a snapshot of an observed repository's Git truth
(branch, HEAD, porcelain-dirty entries) plus a deterministic status hash. The
reader is strict and read-only: it uses argument-array Git calls with optional
locks disabled, and any failure (missing path, git error, timeout) produces a
``valid=False`` truth so the decision guard fails closed (STOP) instead of
acting on stale or unknown repository state.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dev_orchestrator.platform.process import hidden_subprocess_kwargs

_GIT_ENV = dict(os.environ)
_GIT_ENV["GIT_OPTIONAL_LOCKS"] = "0"


@dataclass(frozen=True)
class RepositoryTruth:
    """Fresh, immutable repository truth consumed by the decision guard.

    ``valid=False`` means truth could not be established (missing repo, git
    failure, timeout): consumers must STOP, never execute.
    """

    repo_path: str
    branch: str = ""
    head: str = ""
    dirty: bool = False
    dirty_entries: tuple[str, ...] = ()
    status_hash: str = ""
    valid: bool = False
    error: str = ""


def _invalid_truth(root: Path | str, message: str) -> RepositoryTruth:
    return RepositoryTruth(repo_path=str(root), error=message, valid=False)


def _run_git(root: Path, timeout: float, *arguments: str) -> subprocess.CompletedProcess:
    argv = ["git", "--no-optional-locks", "-C", str(root), *arguments]
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=_GIT_ENV,
        check=False,
        **hidden_subprocess_kwargs(),
    )


def read_repository_truth(root: Path | str, *, timeout: float = 15.0) -> RepositoryTruth:
    """Read fresh repository truth for ``root``; fails closed on any error."""
    try:
        repo = Path(root)
        if not repo.is_dir():
            return _invalid_truth(root, "repository path does not exist: {0}".format(repo))

        branch_proc = _run_git(repo, timeout, "rev-parse", "--abbrev-ref", "HEAD")
        head_proc = _run_git(repo, timeout, "rev-parse", "HEAD")
        status_proc = _run_git(repo, timeout, "status", "--porcelain")

        if branch_proc.returncode != 0 or head_proc.returncode != 0 or status_proc.returncode != 0:
            detail = (
                branch_proc.stderr.strip()
                or head_proc.stderr.strip()
                or status_proc.stderr.strip()
            )
            return _invalid_truth(root, "git truth read failed: {0}".format(detail or "unknown git error"))

        branch = branch_proc.stdout.splitlines()[0].strip() if branch_proc.stdout.splitlines() else ""
        head = head_proc.stdout.splitlines()[0].strip() if head_proc.stdout.splitlines() else ""
        if not branch or not head:
            return _invalid_truth(root, "repository has no checked-out HEAD/branch")

        dirty_entries = tuple(line for line in status_proc.stdout.splitlines() if line.strip())
        digest = hashlib.sha256()
        for line in sorted(dirty_entries):
            digest.update(line.encode("utf-8", errors="replace"))
        return RepositoryTruth(
            repo_path=str(repo),
            branch=branch,
            head=head,
            dirty=bool(dirty_entries),
            dirty_entries=dirty_entries,
            status_hash=digest.hexdigest(),
            valid=True,
            error="",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _invalid_truth(root, str(exc))
