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


def scan_staged_claims(repo_path: str | Path) -> tuple[StagedClaim, ...]:
    repo = Path(repo_path)
    staged = repo / "agent" / "staged"
    claims: list[StagedClaim] = []
    if not staged.is_dir():
        return ()
    for path in sorted(staged.glob("*.md"), key=lambda item: item.name.casefold()):
        try:
            raw = path.read_bytes()
            if b"\r" in raw:
                continue
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        heading = next((line for line in text.splitlines() if line.startswith("# ")), None)
        task_id = extract_task_id(heading)
        if not task_id:
            continue
        predecessor = _explicit_predecessor(text, task_id)
        if not predecessor:
            continue
        try:
            status_line = next(
                line.split(":", 1)[1].strip()
                for line in text.splitlines()
                if line.strip().casefold().startswith("status:")
            )
        except (StopIteration, IndexError):
            continue
        if not parse_task_status(status_line).is_pending_design():
            continue
        if "## Approved executable design" in text:
            continue
        claims.append(StagedClaim(
            task_id=task_id,
            predecessor_task_id=predecessor,
            spec_path=path.relative_to(repo).as_posix(),
            spec_sha256=hashlib.sha256(raw).hexdigest().lower(),
        ))
    return tuple(claims)


def resolve_successor(repo_path: str | Path, completed_task_id: str) -> SuccessorResolution:
    roadmap = read_successor(repo_path, completed_task_id)
    claims = [
        claim for claim in scan_staged_claims(repo_path)
        if claim.predecessor_task_id == completed_task_id
    ]
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
    if roadmap.kind in {"end_of_roadmap", "unlisted"} and claim is not None:
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
    lock_path = repo / ".git" / "devorch-successor.lock"
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
            return ReconcileOutcome(status="rejected", reason=str(exc))
