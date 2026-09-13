from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from dev_orchestrator.monitor.telemetry import extract_task_id


@dataclass(frozen=True)
class RoadmapResult:
    kind: Literal["absent", "invalid", "end_of_roadmap", "successor"]
    reason: str | None = None
    successor_task_id: str | None = None
    spec_path: str | None = None
    spec_sha256: str | None = None
    spec_text: str | None = None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().lower()


def read_raw(repo: Path, rel_path: str) -> bytes | None:
    try:
        target = repo / rel_path
        return target.read_bytes()
    except OSError:
        return None


def read_successor(repo_path: str | Path, completed_task_id: str) -> RoadmapResult:
    repo = Path(repo_path)
    roadmap_file = repo / "agent" / "staged" / "roadmap.json"
    if not roadmap_file.is_file():
        return RoadmapResult(kind="absent", reason="agent/staged/roadmap.json does not exist")

    raw = read_raw(repo, "agent/staged/roadmap.json")
    if raw is None:
        return RoadmapResult(kind="invalid", reason="cannot read agent/staged/roadmap.json")

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return RoadmapResult(kind="invalid", reason=f"cannot parse roadmap.json as JSON: {exc}")

    if not isinstance(data, dict):
        return RoadmapResult(kind="invalid", reason="roadmap root must be a JSON object")

    if data.get("schema_version") != 1:
        return RoadmapResult(kind="invalid", reason="schema_version must be 1")

    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return RoadmapResult(kind="invalid", reason="tasks must be a list")

    seen_task_ids: set[str] = set()
    completed_entry: dict | None = None

    for entry in tasks:
        if not isinstance(entry, dict):
            return RoadmapResult(kind="invalid", reason="task entry must be a dictionary")
        task_id = entry.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            return RoadmapResult(kind="invalid", reason="task entry missing non-blank task_id")
        if task_id in seen_task_ids:
            return RoadmapResult(kind="invalid", reason=f"duplicate task_id: {task_id}")
        seen_task_ids.add(task_id)
        if task_id == completed_task_id:
            completed_entry = entry

    if completed_entry is None:
        return RoadmapResult(kind="invalid", reason=f"completed task {completed_task_id} not found in roadmap")

    successor = completed_entry.get("successor")
    spec_path = completed_entry.get("successor_spec_path")

    if (successor is None) != (spec_path is None):
        return RoadmapResult(kind="invalid", reason="only one of successor and successor_spec_path is null")

    if successor is None and spec_path is None:
        return RoadmapResult(kind="end_of_roadmap")

    if not isinstance(successor, str) or not successor.strip():
        return RoadmapResult(kind="invalid", reason="successor must be a non-blank string")

    if successor == completed_task_id:
        return RoadmapResult(kind="invalid", reason="successor cannot be equal to completed_task_id")

    if not isinstance(spec_path, str) or not spec_path.strip():
        return RoadmapResult(kind="invalid", reason="successor_spec_path must be a non-blank string")

    if "\\" in spec_path:
        return RoadmapResult(kind="invalid", reason="successor_spec_path must be a relative POSIX path")

    posix = PurePosixPath(spec_path)
    if posix.is_absolute() or spec_path.startswith("/"):
        return RoadmapResult(kind="invalid", reason="successor_spec_path cannot be absolute")

    if not spec_path.endswith(".md"):
        return RoadmapResult(kind="invalid", reason="successor_spec_path must end in .md")

    if ".." in posix.parts:
        return RoadmapResult(kind="invalid", reason="successor_spec_path contains '../' traversal")

    staged_dir = (repo / "agent" / "staged").resolve()
    spec_file = (repo / spec_path).resolve()

    try:
        spec_file.relative_to(staged_dir)
    except ValueError:
        return RoadmapResult(kind="invalid", reason="successor_spec_path does not resolve under agent/staged")

    if not spec_file.is_file():
        return RoadmapResult(kind="invalid", reason=f"successor spec file does not exist: {spec_path}")

    spec_bytes = read_raw(repo, spec_path)
    if spec_bytes is None:
        return RoadmapResult(kind="invalid", reason="cannot read successor spec file")

    if b"\r" in spec_bytes:
        return RoadmapResult(kind="invalid", reason="successor spec file contains CRLF/CR line endings")

    try:
        spec_text = spec_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return RoadmapResult(kind="invalid", reason="successor spec file is not valid UTF-8")

    first_heading: str | None = None
    for line in spec_text.splitlines():
        if line.startswith("# "):
            first_heading = line
            break

    if first_heading is None:
        return RoadmapResult(kind="invalid", reason="successor spec file missing top-level heading")

    extracted_id = extract_task_id(first_heading)
    if extracted_id != successor:
        return RoadmapResult(
            kind="invalid",
            reason=f"successor spec title task id mismatch: expected {successor}, got {extracted_id}",
        )

    if "Status: **PENDING DESIGN**" not in spec_text:
        return RoadmapResult(kind="invalid", reason="successor spec missing Status: **PENDING DESIGN**")

    if "## Approved executable design" in spec_text:
        return RoadmapResult(kind="invalid", reason="successor spec already contains approved design marker")

    return RoadmapResult(
        kind="successor",
        successor_task_id=successor,
        spec_path=spec_path,
        spec_sha256=sha256_bytes(spec_bytes),
        spec_text=spec_text,
    )
