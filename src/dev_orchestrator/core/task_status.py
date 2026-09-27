"""Canonical task status grammar, parser, and editing contract for legacy agent/next.md.

Defines the single source of truth for task status interpretation across
DevOrchestrator control, Planner, plan apply, transition, and terminal handling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

TASK_STATUS_SCHEMA_VERSION = 1

EXECUTION_STATE_VOCABULARY = frozenset(
    {"pending_design", "ready_to_run", "executing", "completed", "blocked"}
)

LEGACY_COMPONENT_MAP: dict[str, str] = {
    "READY": "design_ready",
    "READY_TO_RUN": "design_ready",
    "READY TO RUN": "design_ready",
    "READY-TO-RUN": "design_ready",
    "DESIGN READY": "design_ready",
    "DESIGN_READY": "design_ready",
    "EXECUTABLE": "design_ready",
    "PENDING DESIGN": "pending_design",
    "PENDING_DESIGN": "pending_design",
    "PENDING-DESIGN": "pending_design",
    "BLOCKED": "blocked",
    "COMPLETE": "completed",
    "COMPLETED": "completed",
    "ACCEPTED": "completed",
    "DONE": "completed",
    "OWNER_GOAL_DEFINED": "owner_goal_defined",
    "OWNER GOAL DEFINED": "owner_goal_defined",
    "OWNER_APPROVED": "owner_goal_defined",
    "OWNER APPROVED": "owner_goal_defined",
    "NOT_STARTED": "not_started",
    "NOT STARTED": "not_started",
    "IN_PROGRESS": "executing",
    "IN PROGRESS": "executing",
    "RUNNING": "executing",
}

_STATUS_LINE_RE = re.compile(r"^Status:\s*(.+)$", re.IGNORECASE)
_ANNOTATION_SPLIT_RE = re.compile(r"(\u2014|\u2013|\s-\s)")


class TaskStatusError(ValueError):
    """Exception raised when task status text or lines violate contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TaskStatus:
    """Parsed and validated task status token."""

    raw: str
    canonical: Optional[str]
    components: tuple[str, ...]
    annotation: Optional[str]
    code: str
    valid: bool

    def is_pending_design(self) -> bool:
        return self.valid and self.canonical == "pending_design"

    def is_ready_to_run(self) -> bool:
        return self.valid and self.canonical == "ready_to_run"

    def is_executing(self) -> bool:
        return self.valid and self.canonical == "executing"

    def is_completed(self) -> bool:
        return self.valid and self.canonical == "completed"

    def is_blocked(self) -> bool:
        return self.valid and self.canonical == "blocked"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": TASK_STATUS_SCHEMA_VERSION,
            "raw": self.raw,
            "canonical": self.canonical,
            "components": list(self.components),
            "annotation": self.annotation,
            "code": self.code,
            "valid": self.valid,
        }


def parse_task_status(raw: str | None) -> TaskStatus:
    """Parse legacy status token, stripping markdown decoration and annotations."""
    if raw is None:
        return TaskStatus(
            raw="",
            canonical=None,
            components=(),
            annotation=None,
            code="EMPTY",
            valid=False,
        )

    raw_str = str(raw).strip()
    cleaned = raw_str.replace("*", "").replace("`", "").replace("[", "").replace("]", "").strip()
    if cleaned.lower().startswith("status:"):
        cleaned = cleaned[7:].strip()

    if not cleaned:
        return TaskStatus(
            raw=raw_str,
            canonical=None,
            components=(),
            annotation=None,
            code="EMPTY",
            valid=False,
        )

    # Split trailing annotation using em dash, en dash, or space-delimited hyphen only
    annotation: Optional[str] = None
    dash_match = _ANNOTATION_SPLIT_RE.search(cleaned)
    if dash_match:
        base_token = cleaned[: dash_match.start()].strip()
        annotation = cleaned[dash_match.end() :].strip() or None
    else:
        base_token = cleaned

    parts = [p.strip().upper() for p in base_token.split("/") if p.strip()]
    if not parts:
        return TaskStatus(
            raw=raw_str,
            canonical=None,
            components=(),
            annotation=annotation,
            code="EMPTY",
            valid=False,
        )

    # Historical staged successors use this exact declaration before they
    # become current.  Recognize the pair narrowly; a bare STAGED token or a
    # contradictory STAGED combination remains invalid.
    if parts == ["STAGED", "NOT STARTED"]:
        return TaskStatus(
            raw=raw_str,
            canonical="pending_design",
            components=("staged", "not_started"),
            annotation=annotation,
            code="OK",
            valid=True,
        )

    mapped_components: list[str] = []
    for part in parts:
        mapped = LEGACY_COMPONENT_MAP.get(part)
        if mapped is None:
            return TaskStatus(
                raw=raw_str,
                canonical=None,
                components=tuple(mapped_components),
                annotation=annotation,
                code=f"UNKNOWN: {part}",
                valid=False,
            )
        mapped_components.append(mapped)

    comp_set = set(mapped_components)
    conflicting_pairs = [
        ({"blocked", "design_ready"}),
        ({"completed", "design_ready"}),
        ({"pending_design", "design_ready"}),
        ({"executing", "design_ready"}),
        ({"executing", "completed"}),
        ({"executing", "blocked"}),
        ({"completed", "blocked"}),
        ({"not_started", "executing"}),
        ({"not_started", "completed"}),
    ]
    for conf in conflicting_pairs:
        if conf.issubset(comp_set):
            return TaskStatus(
                raw=raw_str,
                canonical=None,
                components=tuple(mapped_components),
                annotation=annotation,
                code="CONFLICT",
                valid=False,
            )

    candidate: Optional[str] = None
    if "design_ready" in comp_set and "not_started" in comp_set:
        candidate = "ready_to_run"
    elif "design_ready" in comp_set and not (
        comp_set & {"pending_design", "blocked", "completed", "executing"}
    ):
        candidate = "ready_to_run"
    elif "pending_design" in comp_set and not (
        comp_set & {"design_ready", "blocked", "completed", "executing"}
    ):
        candidate = "pending_design"
    elif "completed" in comp_set:
        candidate = "completed"
    elif "blocked" in comp_set:
        candidate = "blocked"
    elif "executing" in comp_set:
        candidate = "executing"

    if candidate is not None and candidate in EXECUTION_STATE_VOCABULARY:
        return TaskStatus(
            raw=raw_str,
            canonical=candidate,
            components=tuple(mapped_components),
            annotation=annotation,
            code="OK",
            valid=True,
        )

    return TaskStatus(
        raw=raw_str,
        canonical=None,
        components=tuple(mapped_components),
        annotation=annotation,
        code="UNRESOLVED",
        valid=False,
    )


def find_status_lines(text: str) -> list[tuple[int, str]]:
    """Return (line_index, raw_status_value) for every line starting with Status:."""
    results: list[tuple[int, str]] = []
    for idx, line in enumerate(text.splitlines()):
        stripped = line.strip()
        match = _STATUS_LINE_RE.match(stripped)
        if match:
            results.append((idx, match.group(1).strip()))
    return results


def require_single_status_line(text: str) -> tuple[int, str]:
    """Find and return exactly one (line_index, raw_status_value) or fail closed."""
    matches = find_status_lines(text)
    if not matches:
        raise TaskStatusError("ZERO_STATUS_LINES", "expected exactly one Status line in text; found 0")
    if len(matches) > 1:
        raise TaskStatusError(
            "MULTIPLE_STATUS_LINES",
            f"expected exactly one Status line in text; found {len(matches)}",
        )
    return matches[0]


def render_status_line(canonical: str, *, annotation: Optional[str] = None) -> str:
    """Render canonical execution state as Markdown Status line."""
    canonical_clean = canonical.strip().lower()
    if canonical_clean in {"ready_to_run", "ready-to-run"}:
        base = "Status: **READY_TO_RUN**"
    elif canonical_clean in {"pending_design", "pending-design"}:
        base = "Status: **PENDING DESIGN**"
    elif canonical_clean in {"completed", "done"}:
        base = "Status: **DONE**"
    elif canonical_clean == "blocked":
        base = "Status: **BLOCKED**"
    elif canonical_clean == "executing":
        base = "Status: **EXECUTING**"
    else:
        base = f"Status: **{canonical_clean.upper()}**"
    if annotation:
        return f"{base} \u2014 {annotation}"
    return base
