"""Cross-check and repair staged successor metadata.

The roadmap remains the normal declaration surface, but a staged task may carry
an explicit predecessor declaration.  A unique staged claim is recovery
evidence; it is never allowed to silently override an inconsistent roadmap.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dev_orchestrator.accounting.events import InterProcessFileLock
from dev_orchestrator.core.git_paths import resolve_git_dir
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.core.staged_roadmap import RoadmapResult, read_successor
from dev_orchestrator.core.task_status import parse_task_status
from dev_orchestrator.monitor.telemetry import extract_task_id
from dev_orchestrator.storage.json_store import write_json


_PREDECESSOR_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:predecessor|after)\s*:\s*(?:\*\*)?\s*([A-Za-z]\d+(?:\.\d+)*)",
    re.IGNORECASE,
)
_SEQUENCE_RE = re.compile(r"^\s*sequence\s*:\s*(.+)$", re.IGNORECASE)
_TASK_RE = re.compile(r"\b[A-Za-z]\d+(?:\.\d+)*\b")


@dataclass(frozen=True)
class StagedClaim:
    task_id: str
    predecessor_task_id: str
    spec_path: str
    spec_sha256: str


@dataclass(frozen=True)
class StagedClaimDefect:
    """A staged spec that declares a predecessor but cannot serve as a claim.

    Dropping such a file silently is a fail-open: the declared predecessor then
    looks like the end of the roadmap and gets terminal-settled as complete.
    """

    task_id: str | None
    predecessor_task_id: str | None
    spec_path: str
    reason: str


@dataclass(frozen=True)
class StagedScan:
    claims: tuple[StagedClaim, ...]
    defects: tuple[StagedClaimDefect, ...]


@dataclass(frozen=True)
class SuccessorResolution:
    kind: Literal[
        "successor", "end_of_roadmap", "inconsistent", "ambiguous",
        "invalid", "absent", "unlisted",
    ]
    roadmap: RoadmapResult
    successor_task_id: str | None = None
    spec_path: str | None = None
    spec_sha256: str | None = None
    spec_text: str | None = None
    evidence: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ReconcileOutcome:
    status: Literal["applied", "already_consistent", "noop", "rejected"]
    successor_task_id: str | None = None
    spec_path: str | None = None
    head: str | None = None
    reason: str | None = None


def _explicit_predecessor(text: str, task_id: str) -> str | None:
    for line in text.splitlines():
        match = _PREDECESSOR_RE.match(line)
        if match:
            return match.group(1)
        match = _SEQUENCE_RE.match(line)
        if not match:
            continue
        ids = _TASK_RE.findall(match.group(1))
        for index, value in enumerate(ids):
            if value == task_id and index:
                return ids[index - 1]
    return None


def _claim_disposition(raw: bytes) -> tuple[Literal["claim", "skip", "defect"], str | None]:
    """Classify a predecessor-declaring staged spec as usable, inert, or broken.

    ``skip`` means the spec is legitimately not a pending successor claim: it
    carries a valid non-pending-design lifecycle status.  ``defect`` means the
    spec wants to be a claim but cannot be trusted as one, which must surface
    rather than disappear.
    """
    if b"\r" in raw:
        # Fail closed rather than normalizing.  ``read_successor`` already
        # rejects a CRLF spec, and the Planner compares sha256 over the bytes on
        # disk against the recorded ``staged_spec_sha256``, so normalizing here
        # would only move the failure to a launch fence with a worse message.
        return "defect", "spec contains CRLF/CR bytes; staged specs must be LF-only"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return "defect", f"spec is not valid UTF-8: {exc}"
    try:
        status_line = next(
            line.split(":", 1)[1].strip()
            for line in text.splitlines()
            if line.strip().casefold().startswith("status:")
        )
    except (StopIteration, IndexError):
        return "defect", "spec declares no Status: line"
    status = parse_task_status(status_line)
    if not status.valid:
        return "defect", f"spec Status line does not parse ({status.code})"
    if not status.is_pending_design():
        return "skip", None
    return "claim", None


def scan_staged_specs(repo_path: str | Path) -> StagedScan:
    """Scan agent/staged for predecessor claims and unusable claim candidates."""
    repo = Path(repo_path)
    staged = repo / "agent" / "staged"
    claims: list[StagedClaim] = []
    defects: list[StagedClaimDefect] = []
    if not staged.is_dir():
        return StagedScan((), ())
    for path in sorted(staged.glob("*.md"), key=lambda item: item.name.casefold()):
        spec_path = path.relative_to(repo).as_posix()
        try:
            raw = path.read_bytes()
        except OSError as exc:
            # An unreadable staged file is unknown successor evidence.  It must
            # be represented explicitly: silently dropping it can turn a real
            # successor into a false end-of-roadmap decision.
            defects.append(StagedClaimDefect(
                task_id=None,
                predecessor_task_id=None,
                spec_path=spec_path,
                reason=f"spec could not be read: {type(exc).__name__}: {exc}",
            ))
            continue
        # Attribution has to survive the very defects being reported, so
        # identify the spec from a lenient decode before judging usability.
        lenient = raw.decode("utf-8", errors="replace")
        heading = next((line for line in lenient.splitlines() if line.startswith("# ")), None)
        task_id = extract_task_id(heading)
        disposition, reason = _claim_disposition(raw)
        if disposition == "defect":
            defects.append(StagedClaimDefect(
                task_id=task_id,
                predecessor_task_id=(
                    _explicit_predecessor(lenient, task_id) if task_id else None
                ),
                spec_path=spec_path,
                reason=str(reason),
            ))
            continue
        if "## Approved executable design" in lenient:
            # An approved design is a materialized spec, never a pending claim.
            continue
        if disposition == "skip":
            continue
        if not task_id:
            defects.append(StagedClaimDefect(
                task_id=None,
                predecessor_task_id=None,
                spec_path=spec_path,
                reason="pending staged spec has no parseable task heading",
            ))
            continue
        predecessor = _explicit_predecessor(lenient, task_id)
        if not predecessor:
            # A roadmap-addressed staged spec is valid without predecessor
            # metadata; it simply cannot serve as an independent recovery
            # claim.  ``read_successor`` validates that path when referenced.
            continue
        claims.append(StagedClaim(
            task_id=task_id,
            predecessor_task_id=predecessor,
            spec_path=spec_path,
            spec_sha256=hashlib.sha256(raw).hexdigest().lower(),
        ))
    return StagedScan(tuple(claims), tuple(defects))


def scan_staged_claims(repo_path: str | Path) -> tuple[StagedClaim, ...]:
    return scan_staged_specs(repo_path).claims


def resolve_successor(repo_path: str | Path, completed_task_id: str) -> SuccessorResolution:
    roadmap = read_successor(repo_path, completed_task_id)
    scan = scan_staged_specs(repo_path)
    claims = [
        claim for claim in scan.claims
        if claim.predecessor_task_id == completed_task_id
    ]
    # Any malformed or unreadable staged metadata makes the successor set
    # unknowable.  Do not scope defects only after parsing a predecessor: the
    # malformed bytes may be exactly what prevents that attribution.
    defects = list(scan.defects)
    if defects:
        # A staged spec that names this task as its predecessor but cannot be
        # read as a claim must never be dropped: dropping it makes the task look
        # like the end of the roadmap and it gets settled as project-complete.
        return SuccessorResolution(
            kind="invalid", roadmap=roadmap,
            reason=("STAGED_SUCCESSOR_CLAIM_UNUSABLE: " + "; ".join(
                f"{defect.spec_path}"
                + (
                    f" declares predecessor {defect.predecessor_task_id}"
                    if defect.predecessor_task_id else " has unreadable or malformed metadata"
                )
                + f": {defect.reason}"
                for defect in defects
            )),
        )
    if len(claims) > 1:
        return SuccessorResolution(
            kind="ambiguous", roadmap=roadmap,
            reason=("multiple staged tasks claim predecessor " + completed_task_id + ": "
                    + ", ".join(sorted(claim.task_id for claim in claims))),
        )
    claim = claims[0] if claims else None
    if roadmap.kind == "invalid":
        return SuccessorResolution(kind="invalid", roadmap=roadmap, reason=roadmap.reason)
    if roadmap.kind == "successor":
        if claim is not None and (
            claim.task_id != roadmap.successor_task_id
            or claim.spec_path != roadmap.spec_path
            or claim.spec_sha256 != roadmap.spec_sha256
        ):
            return SuccessorResolution(
                kind="ambiguous", roadmap=roadmap,
                reason=("roadmap successor disagrees with staged predecessor claim: "
                        f"roadmap={roadmap.successor_task_id}, staged={claim.task_id}"),
            )
        return SuccessorResolution(
            kind="successor", roadmap=roadmap,
            successor_task_id=roadmap.successor_task_id,
            spec_path=roadmap.spec_path,
            spec_sha256=roadmap.spec_sha256,
            spec_text=roadmap.spec_text,
            evidence="roadmap" if claim is None else "roadmap+staged_claim",
        )
    # An absent roadmap is not evidence that the project is finished.  When a
    # staged task unambiguously declares this task as its predecessor, that
    # claim must win over the missing file, or the caller terminal-settles the
    # project as complete while an unambiguous successor is staged.
    if roadmap.kind in {"end_of_roadmap", "unlisted", "absent"} and claim is not None:
        try:
            text = (Path(repo_path) / claim.spec_path).read_text(encoding="utf-8")
        except OSError as exc:
            return SuccessorResolution(kind="invalid", roadmap=roadmap, reason=str(exc))
        return SuccessorResolution(
            kind="inconsistent", roadmap=roadmap,
            successor_task_id=claim.task_id,
            spec_path=claim.spec_path,
            spec_sha256=claim.spec_sha256,
            spec_text=text,
            evidence="staged_claim",
            reason=("ROADMAP_SUCCESSOR_INCONSISTENT: roadmap has no successor for "
                    f"{completed_task_id}, but {claim.spec_path} declares it as predecessor"),
        )
    return SuccessorResolution(kind=roadmap.kind, roadmap=roadmap, reason=roadmap.reason)


def _successor_lock_path(repo: Path) -> Path | None:
    """Return the roadmap-repair lock inside this checkout's real git directory.

    ``repo / ".git"`` is only a directory in a primary checkout.  In a linked
    worktree it is a *file* pointing at ``<common>/.git/worktrees/<name>``, so
    creating the lock beneath it fails with ``FileExistsError`` (WinError 183)
    and the self-hosted controller, which runs from such a worktree, could never
    repair a successor.  Ask git for the per-worktree git directory instead,
    which is also where git keeps this worktree's own ``index.lock``.
    """
    git_dir = resolve_git_dir(repo)
    return None if git_dir is None else git_dir / "devorch-successor.lock"


def reconcile_roadmap_successor(
    repo_path: str | Path,
    completed_task_id: str,
    resolution: SuccessorResolution | None = None,
) -> ReconcileOutcome:
    repo = Path(repo_path)
    resolved = resolution or resolve_successor(repo, completed_task_id)
    if resolved.kind == "successor":
        truth = read_repository_truth(repo)
        return ReconcileOutcome(
            status="already_consistent", successor_task_id=resolved.successor_task_id,
            spec_path=resolved.spec_path, head=truth.head if truth.valid else None,
        )
    if resolved.kind != "inconsistent" or not resolved.successor_task_id or not resolved.spec_path:
        return ReconcileOutcome(status="noop", reason=resolved.reason or resolved.kind)
    truth = read_repository_truth(repo)
    if not truth.valid or truth.dirty:
        return ReconcileOutcome(status="rejected", reason="successor reconcile requires a clean repository")

    roadmap_path = repo / "agent" / "staged" / "roadmap.json"
    lock_path = _successor_lock_path(repo)
    if lock_path is None:
        return ReconcileOutcome(
            status="rejected",
            reason="successor reconcile cannot resolve the repository git directory",
        )
    with InterProcessFileLock(lock_path):
        original = roadmap_path.read_bytes() if roadmap_path.is_file() else b""
        try:
            data = json.loads(original.decode("utf-8")) if original else {"schema_version": 1, "tasks": []}
            tasks = data.get("tasks")
            if not isinstance(tasks, list):
                return ReconcileOutcome(status="rejected", reason="roadmap tasks must be a list")
            entry = next((row for row in tasks if isinstance(row, dict) and row.get("task_id") == completed_task_id), None)
            if entry is None:
                entry = {"task_id": completed_task_id}
                tasks.append(entry)
            entry["successor"] = resolved.successor_task_id
            entry["successor_spec_path"] = resolved.spec_path
            write_json(roadmap_path, data, indent=2)
            checked = read_successor(repo, completed_task_id)
            if checked.kind != "successor" or checked.successor_task_id != resolved.successor_task_id:
                raise RuntimeError("reconciled roadmap did not validate: " + str(checked.reason or checked.kind))
            subprocess.run(
                ["git", "-C", str(repo), "add", "--", "agent/staged/roadmap.json"],
                check=True, capture_output=True, text=True,
            )
            commit = subprocess.run(
                ["git", "-C", str(repo), "commit", "-m",
                 f"lifecycle({completed_task_id}): reconcile successor {resolved.successor_task_id}",
                 "--", "agent/staged/roadmap.json"],
                check=False, capture_output=True, text=True,
            )
            if commit.returncode != 0:
                raise RuntimeError("git commit failed: " + (commit.stderr or commit.stdout).strip())
            after = read_repository_truth(repo)
            if not after.valid or after.dirty:
                raise RuntimeError("successor reconcile did not leave clean repository truth")
            return ReconcileOutcome(
                status="applied", successor_task_id=resolved.successor_task_id,
                spec_path=resolved.spec_path, head=after.head,
            )
        except Exception as exc:
            if original:
                roadmap_path.write_bytes(original)
                subprocess.run(
                    ["git", "-C", str(repo), "add", "--", "agent/staged/roadmap.json"],
                    check=False, capture_output=True, text=True,
                )
            else:
                # The roadmap did not exist before this attempt, so leaving the
                # file behind would dirty the tree and permanently block the
                # clean-worktree precondition that every later recovery needs.
                roadmap_path.unlink(missing_ok=True)
                subprocess.run(
                    ["git", "-C", str(repo), "rm", "--cached", "--quiet", "--ignore-unmatch",
                     "--", "agent/staged/roadmap.json"],
                    check=False, capture_output=True, text=True,
                )
            return ReconcileOutcome(status="rejected", reason=str(exc))
