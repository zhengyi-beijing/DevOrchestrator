"""Invariant-driven control-plane development and validation contract.

Enforces task-level classification, canonical declaration grammar,
committed-artifact loading, prompt injection, review scope-gap analysis,
and read-only convergence/traversal evidence projections.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable

from dev_orchestrator.core.control_plane_faults import (
    TRANSITION_BOUNDARIES,
    fault_scenarios,
    required_scenarios_for,
)
from dev_orchestrator.core.lifecycle_authority import INVARIANT_CODES
from dev_orchestrator.platform.process import hidden_subprocess_kwargs


# Exact inventory of protected runtime surfaces.
# Documentation, UI, provider adapters, and benchmarks are not protected surfaces.
CONTROL_PLANE_RUNTIME_SURFACES = frozenset({
    "src/dev_orchestrator/core/lifecycle_authority.py",
    "src/dev_orchestrator/core/transition_executor.py",
    "src/dev_orchestrator/core/successor_consistency.py",
    "src/dev_orchestrator/core/control_plane_contract.py",
    "src/dev_orchestrator/core/control_plane_faults.py",
    "src/dev_orchestrator/core/watchdog.py",
    "src/dev_orchestrator/core/staged_roadmap.py",
    # Exact symbols
    "AUTHORITY_SCHEMA_VERSION",
    "INVARIANT_CODES",
    "evaluate_lifecycle_invariants",
    "new_authority",
    "active_owners",
    "source_ownership_blockers",
    "epoch_for",
    "transition_id_for",
    "open_declaration_gate",
    "resolve_declaration_gate",
    "_lifecycle_launch_guard",
    "_launch",
    "_launch_aibroker",
    "_advance_decisions",
    "reconcile_successor_handoff",
    "_record_handoff",
    "mark_handoff_blocked",
    "reconcile_lifecycle_authority",
    "_transient_remediation_state_block",
    "_is_declaration_gate_replayable",
    "resolve_successor",
    "reconcile_roadmap_successor",
    "read_successor",
})

_PROTECTED_PATH_STEMS = frozenset({
    "lifecycle_authority.py",
    "successor_consistency.py",
    "control_plane_contract.py",
    "control_plane_faults.py",
})

_PROTECTED_SYMBOLS = frozenset({
    "evaluate_lifecycle_invariants",
    "open_declaration_gate",
    "resolve_declaration_gate",
    "_lifecycle_launch_guard",
    "reconcile_successor_handoff",
    "reconcile_lifecycle_authority",
    "resolve_successor",
    "reconcile_roadmap_successor",
    "_is_declaration_gate_replayable",
})

MAX_DECLARATION_SECTION_CHARS = 8192
DECLARATION_HEADER = "## Control-Plane Impact"


@dataclass(frozen=True)
class ControlPlaneDeclaration:
    kind: str  # declared | absent | invalid | ambiguous | unavailable
    source_path: str
    revision: str | None
    content_hash: str
    invariants: tuple[str, ...]
    transition_boundaries: tuple[str, ...]
    fault_scenarios: tuple[str, ...]
    convergence_evidence: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source_path": self.source_path,
            "revision": self.revision,
            "content_hash": self.content_hash,
            "invariants": list(self.invariants),
            "transition_boundaries": list(self.transition_boundaries),
            "fault_scenarios": list(self.fault_scenarios),
            "convergence_evidence": self.convergence_evidence,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ControlPlaneScope:
    kind: str  # control_plane | ordinary | invalid | unevaluable
    matched_surfaces: tuple[str, ...]
    evidence_source: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "matched_surfaces": list(self.matched_surfaces),
            "evidence_source": self.evidence_source,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ControlPlaneGate:
    allowed: bool
    code: str | None
    reason: str
    gate_id: str | None
    required_scenarios: tuple[str, ...]
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "reason": self.reason,
            "gate_id": self.gate_id,
            "required_scenarios": list(self.required_scenarios),
            "evidence": self.evidence,
        }


def _empty_declaration(kind: str, reason: str, source_path: str = "agent/next.md", revision: str | None = None) -> ControlPlaneDeclaration:
    return ControlPlaneDeclaration(
        kind=kind,
        source_path=source_path,
        revision=revision,
        content_hash="",
        invariants=(),
        transition_boundaries=(),
        fault_scenarios=(),
        convergence_evidence="",
        reason=reason,
    )


def parse_control_plane_declaration(
    text: str,
    task_id: str | None = None,
    source_path: str = "agent/next.md",
    revision: str | None = None,
    allow_bare: bool = False,
) -> ControlPlaneDeclaration:
    """Parse and validate exactly one canonical ## Control-Plane Impact section."""
    if not isinstance(text, str):
        return _empty_declaration("invalid", "text must be a string", source_path, revision)

    matches = list(re.finditer(r"^##\s+Control-Plane Impact\s*$", text, flags=re.MULTILINE))
    is_bare = False
    if len(matches) == 0:
        if allow_bare and ("invariants:" in text.lower()):
            is_bare = True
            section_text = text
        else:
            return _empty_declaration("absent", "no '## Control-Plane Impact' section found", source_path, revision)
    elif len(matches) > 1:
        return _empty_declaration("ambiguous", f"multiple ({len(matches)}) '## Control-Plane Impact' sections found", source_path, revision)
    else:
        start_pos = matches[0].end()
        # Find next top-level or second-level heading, or EOF
        next_heading = re.search(r"^##?\s+", text[start_pos:], flags=re.MULTILINE)
        if next_heading:
            section_text = text[start_pos:start_pos + next_heading.start()]
        else:
            section_text = text[start_pos:]

    if len(section_text) > MAX_DECLARATION_SECTION_CHARS:
        return _empty_declaration(
            "invalid",
            f"section length ({len(section_text)}) exceeds limit ({MAX_DECLARATION_SECTION_CHARS})",
            source_path, revision,
        )

    raw_lines = section_text.strip().splitlines()
    if not raw_lines:
        return _empty_declaration("invalid", "section is empty", source_path, revision)

    parsed_fields: dict[str, str] = {}
    field_patterns = {
        "Invariants": re.compile(r"^[-*]?\s*Invariants\s*:\s*(.*)$", re.IGNORECASE),
        "Transition boundaries": re.compile(r"^[-*]?\s*Transition boundaries\s*:\s*(.*)$", re.IGNORECASE),
        "Fault scenarios": re.compile(r"^[-*]?\s*Fault scenarios\s*:\s*(.*)$", re.IGNORECASE),
        "Convergence evidence": re.compile(r"^[-*]?\s*Convergence evidence\s*:\s*(.*)$", re.IGNORECASE),
    }

    current_field: str | None = None
    for line in raw_lines:
        line_str = line.strip()
        if not line_str:
            current_field = None
            continue
        matched_any = False
        for field_name, pattern in field_patterns.items():
            m = pattern.match(line_str)
            if m:
                if field_name in parsed_fields:
                    return _empty_declaration("invalid", f"duplicate field '{field_name}'", source_path, revision)
                parsed_fields[field_name] = m.group(1).strip()
                matched_any = True
                current_field = field_name
                break
        if not matched_any and not line_str.startswith("#"):
            if is_bare:
                # In bare mode, only indented continuation lines directly following Convergence evidence are joined
                if (
                    current_field == "Convergence evidence"
                    and (line.startswith(" ") or line.startswith("\t"))
                    and not line_str.startswith(("-", "*", "def ", "class ", "import ", "from "))
                ):
                    parsed_fields["Convergence evidence"] += " " + line_str
                else:
                    current_field = None
                # Other non-matching lines in bare mode are ignored (e.g. interfaces, other text)
            else:
                # In canonical section mode, allow continuation of convergence evidence if indented or continuation
                if (
                    current_field == "Convergence evidence"
                    and (line.startswith(" ") or line.startswith("\t") or not line_str.startswith(("-", "*")))
                ):
                    parsed_fields["Convergence evidence"] += " " + line_str
                else:
                    return _empty_declaration("invalid", f"unrecognized declaration line '{line_str}'", source_path, revision)

    if is_bare and not parsed_fields:
        return _empty_declaration("absent", "no control-plane declaration found in text", source_path, revision)

    # Check required fields
    for field_name in field_patterns:
        if field_name not in parsed_fields:
            return _empty_declaration("invalid", f"missing required field '{field_name}:'", source_path, revision)
        if not parsed_fields[field_name]:
            return _empty_declaration("invalid", f"field '{field_name}:' is empty", source_path, revision)

    # Parse Invariants
    inv_tokens = [t.strip() for t in parsed_fields["Invariants"].split(",") if t.strip()]
    if not inv_tokens:
        return _empty_declaration("invalid", "Invariants list is empty", source_path, revision)
    if len(inv_tokens) != len(set(inv_tokens)):
        return _empty_declaration("invalid", "duplicate invariant codes in declaration", source_path, revision)
    for code in inv_tokens:
        if code not in INVARIANT_CODES:
            return _empty_declaration("invalid", f"unknown invariant code '{code}'", source_path, revision)

    # Parse Transition boundaries
    tb_tokens = [t.strip() for t in parsed_fields["Transition boundaries"].split(",") if t.strip()]
    if not tb_tokens:
        return _empty_declaration("invalid", "Transition boundaries list is empty", source_path, revision)
    if len(tb_tokens) != len(set(tb_tokens)):
        return _empty_declaration("invalid", "duplicate transition boundaries in declaration", source_path, revision)
    for tb in tb_tokens:
        if tb not in TRANSITION_BOUNDARIES:
            return _empty_declaration("invalid", f"unknown transition boundary '{tb}'", source_path, revision)

    # Parse Fault scenarios
    registered_faults = {s.scenario_id for s in fault_scenarios()}
    fs_tokens = [t.strip() for t in parsed_fields["Fault scenarios"].split(",") if t.strip()]
    if not fs_tokens:
        return _empty_declaration("invalid", "Fault scenarios list is empty", source_path, revision)
    if len(fs_tokens) != len(set(fs_tokens)):
        return _empty_declaration("invalid", "duplicate fault scenario IDs in declaration", source_path, revision)
    for fs in fs_tokens:
        if fs not in registered_faults:
            return _empty_declaration("invalid", f"unknown fault scenario ID '{fs}'", source_path, revision)

    convergence = parsed_fields["Convergence evidence"].strip()
    if not convergence:
        return _empty_declaration("invalid", "Convergence evidence is empty", source_path, revision)

    canonical_repr = (
        f"Invariants:{','.join(inv_tokens)}|"
        f"Boundaries:{','.join(tb_tokens)}|"
        f"Scenarios:{','.join(fs_tokens)}|"
        f"Evidence:{convergence}"
    )
    content_hash = hashlib.sha256(canonical_repr.encode("utf-8")).hexdigest()

    return ControlPlaneDeclaration(
        kind="declared",
        source_path=source_path,
        revision=revision,
        content_hash=content_hash,
        invariants=tuple(inv_tokens),
        transition_boundaries=tuple(tb_tokens),
        fault_scenarios=tuple(fs_tokens),
        convergence_evidence=convergence,
        reason=None,
    )


def render_declaration_section(decl: ControlPlaneDeclaration) -> str:
    """Render canonical ## Control-Plane Impact markdown section."""
    if decl.kind != "declared":
        return ""
    lines = [
        "## Control-Plane Impact",
        f"- Invariants: {', '.join(decl.invariants)}",
        f"- Transition boundaries: {', '.join(decl.transition_boundaries)}",
        f"- Fault scenarios: {', '.join(decl.fault_scenarios)}",
        f"- Convergence evidence: {decl.convergence_evidence}",
    ]
    return "\n".join(lines) + "\n"


def load_control_plane_declaration(
    repo_path: str | Path,
    task_id: str,
    head: str,
    timeout_seconds: float = 10.0,
) -> ControlPlaneDeclaration:
    """Load and parse committed declaration from git show <head>:agent/next.md."""
    repo = Path(repo_path)
    if not repo.is_dir():
        return _empty_declaration("unavailable", f"repo_path does not exist: '{repo_path}'", "agent/next.md", head)
    if not head:
        return _empty_declaration("unavailable", "head commit hash is empty", "agent/next.md", head)

    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "show", f"{head}:agent/next.md"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            **hidden_subprocess_kwargs(),
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout).strip()
            return _empty_declaration(
                "unavailable",
                f"failed to read committed agent/next.md at {head}: {err}",
                "agent/next.md", head,
            )
        committed_text = proc.stdout
    except subprocess.TimeoutExpired:
        return _empty_declaration("unavailable", f"git show timed out after {timeout_seconds}s", "agent/next.md", head)
    except Exception as exc:
        return _empty_declaration("unavailable", f"git show failed with error: {exc}", "agent/next.md", head)

    return parse_control_plane_declaration(
        committed_text,
        task_id=task_id,
        source_path="agent/next.md",
        revision=head,
    )


def evaluate_launch_declaration(
    repo_path: str | Path,
    project_id: str,
    task_id: str,
    head: str,
    timeout_seconds: float = 10.0,
) -> tuple[ControlPlaneScope, ControlPlaneDeclaration, ControlPlaneGate]:
    """Load committed agent/next.md at head, classify, and evaluate declaration gate."""
    repo = Path(repo_path)
    if not repo.is_dir() or not head:
        scope = ControlPlaneScope(
            kind="unevaluable",
            matched_surfaces=(),
            evidence_source="repo_or_head_missing",
            reason=f"invalid repo ({repo_path}) or head ({head})",
        )
        declaration = _empty_declaration("unavailable", "invalid repo or head", "agent/next.md", head)
        gate = require_control_plane_declaration(project_id, task_id, scope, declaration)
        return scope, declaration, gate

    # If the repository does not contain dev_orchestrator/core surfaces and has no agent/next.md,
    # it is an ordinary managed repository that cannot touch the DevOrchestrator control plane.
    has_core_surfaces = (repo / "src" / "dev_orchestrator" / "core").is_dir()
    if not has_core_surfaces and not (repo / "agent" / "next.md").exists():
        scope = ControlPlaneScope(
            kind="ordinary",
            matched_surfaces=(),
            evidence_source="default",
            reason="repository does not contain control-plane surfaces and has no task declaration",
        )
        declaration = _empty_declaration("absent", "task is ordinary", "agent/next.md", head)
        gate = require_control_plane_declaration(project_id, task_id, scope, declaration)
        return scope, declaration, gate

    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "show", f"{head}:agent/next.md"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            **hidden_subprocess_kwargs(),
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout).strip()
            if not has_core_surfaces and not (repo / "agent" / "next.md").exists():
                scope = ControlPlaneScope(
                    kind="ordinary",
                    matched_surfaces=(),
                    evidence_source="default",
                    reason="repository does not contain control-plane surfaces and has no task declaration",
                )
                declaration = _empty_declaration("absent", "task is ordinary", "agent/next.md", head)
                gate = require_control_plane_declaration(project_id, task_id, scope, declaration)
                return scope, declaration, gate

            if "does not exist in" in err.lower() or "does not exist" in err.lower():
                changed_paths: list[str] = []
                show_proc = subprocess.run(
                    ["git", "-C", str(repo), "show", "--name-only", "--format=", head],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_seconds,
                    check=False,
                    **hidden_subprocess_kwargs(),
                )
                if show_proc.returncode == 0:
                    changed_paths = [
                        line.strip() for line in show_proc.stdout.splitlines() if line.strip()
                    ]

                disk_next = repo / "agent" / "next.md"
                task_text: str | None = None
                if disk_next.is_file():
                    try:
                        task_text = disk_next.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        pass

                scope = classify_control_plane_task(
                    task_text=task_text,
                    changed_paths=tuple(changed_paths),
                )
                if scope.kind == "control_plane":
                    declaration = _empty_declaration(
                        "absent",
                        "no '## Control-Plane Impact' section found (agent/next.md not committed at HEAD)",
                        "agent/next.md",
                        head,
                    )
                    gate = require_control_plane_declaration(project_id, task_id, scope, declaration)
                    return scope, declaration, gate
                elif scope.kind == "ordinary":
                    declaration = _empty_declaration("absent", "task is ordinary", "agent/next.md", head)
                    gate = require_control_plane_declaration(project_id, task_id, scope, declaration)
                    return scope, declaration, gate

            scope = ControlPlaneScope(
                kind="unevaluable",
                matched_surfaces=(),
                evidence_source="git_show_failed",
                reason=f"git show {head}:agent/next.md failed: {err}",
            )
            declaration = _empty_declaration("unavailable", f"failed to read committed agent/next.md at {head}: {err}", "agent/next.md", head)
            gate = require_control_plane_declaration(project_id, task_id, scope, declaration)
            return scope, declaration, gate
        committed_text = proc.stdout
    except subprocess.TimeoutExpired:
        if not has_core_surfaces and not (repo / "agent" / "next.md").exists():
            scope = ControlPlaneScope(
                kind="ordinary",
                matched_surfaces=(),
                evidence_source="default",
                reason="repository does not contain control-plane surfaces and has no task declaration",
            )
            declaration = _empty_declaration("absent", "task is ordinary", "agent/next.md", head)
            gate = require_control_plane_declaration(project_id, task_id, scope, declaration)
            return scope, declaration, gate
        scope = ControlPlaneScope(
            kind="unevaluable",
            matched_surfaces=(),
            evidence_source="timeout",
            reason=f"git show timed out after {timeout_seconds}s",
        )
        declaration = _empty_declaration("unavailable", f"git show timed out after {timeout_seconds}s", "agent/next.md", head)
        gate = require_control_plane_declaration(project_id, task_id, scope, declaration)
        return scope, declaration, gate
    except Exception as exc:
        if not has_core_surfaces and not (repo / "agent" / "next.md").exists():
            scope = ControlPlaneScope(
                kind="ordinary",
                matched_surfaces=(),
                evidence_source="default",
                reason="repository does not contain control-plane surfaces and has no task declaration",
            )
            declaration = _empty_declaration("absent", "task is ordinary", "agent/next.md", head)
            gate = require_control_plane_declaration(project_id, task_id, scope, declaration)
            return scope, declaration, gate
        scope = ControlPlaneScope(
            kind="unevaluable",
            matched_surfaces=(),
            evidence_source="exception",
            reason=f"git show failed: {exc}",
        )
        declaration = _empty_declaration("unavailable", f"git show failed: {exc}", "agent/next.md", head)
        gate = require_control_plane_declaration(project_id, task_id, scope, declaration)
        return scope, declaration, gate

    scope = classify_control_plane_task(task_text=committed_text)
    declaration = parse_control_plane_declaration(
        committed_text,
        task_id=task_id,
        source_path="agent/next.md",
        revision=head,
    )
    gate = require_control_plane_declaration(project_id, task_id, scope, declaration)
    return scope, declaration, gate


def _matches_protected_surface(text: str) -> list[str]:
    matched = []
    for surface in CONTROL_PLANE_RUNTIME_SURFACES:
        if "/" in surface or surface.endswith(".py"):
            if surface in text:
                matched.append(surface)
        else:
            pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(surface)}(?![A-Za-z0-9_])")
            if pattern.search(text):
                matched.append(surface)
    return matched


def _is_ordinary_claim(text: str) -> bool:
    low = text.lower()
    return (
        "classification: ordinary" in low
        or "classification: **ordinary**" in low
        or "task is ordinary" in low
        or "is_control_plane: false" in low
        or "control_plane: false" in low
    )


def classify_control_plane_task(
    *,
    task_text: str | None = None,
    candidate_plan: dict[str, Any] | None = None,
    changed_paths: list[str] | tuple[str, ...] | None = None,
) -> ControlPlaneScope:
    """Classify whether a task affects the control plane.

    Repository identity and the presence of control plane files on disk are
    NEVER classification inputs.
    """
    if task_text is None and candidate_plan is None and changed_paths is None:
        return ControlPlaneScope(
            kind="unevaluable",
            matched_surfaces=(),
            evidence_source="none",
            reason="insufficient input to classify task: task_text, candidate_plan, and changed_paths are all absent",
        )

    matched_surfaces: set[str] = set()

    # 1. Inspect changed paths (used at review time or during git diff check)
    if changed_paths:
        for p in changed_paths:
            normalized_p = p.replace("\\", "/")
            if normalized_p in CONTROL_PLANE_RUNTIME_SURFACES:
                matched_surfaces.add(normalized_p)
            else:
                stem = Path(normalized_p).name
                if stem in _PROTECTED_PATH_STEMS and "dev_orchestrator/core" in normalized_p:
                    matched_surfaces.add(normalized_p)

    # 2. Inspect candidate plan (used at plan-freeze time)
    if candidate_plan and isinstance(candidate_plan, dict):
        plan_blob = " ".join([
            str(candidate_plan.get("summary") or ""),
            " ".join(str(s) for s in candidate_plan.get("implementation_steps") or []),
            " ".join(str(s) for s in candidate_plan.get("interfaces") or []),
            " ".join(str(s) for s in candidate_plan.get("validation") or []),
            " ".join(str(s) for s in candidate_plan.get("risks") or []),
            " ".join(str(s) for s in candidate_plan.get("out_of_scope") or []),
        ])
        for surf in _matches_protected_surface(plan_blob):
            matched_surfaces.add(surf)

    # 3. Inspect task text
    if task_text:
        # Check if text contains explicit ## Control-Plane Impact declaration
        decl = parse_control_plane_declaration(task_text)
        if decl.kind == "declared":
            return ControlPlaneScope(
                kind="control_plane",
                matched_surfaces=tuple(sorted(matched_surfaces)),
                evidence_source="explicit_declaration",
                reason="task explicitly includes valid ## Control-Plane Impact declaration",
            )
        elif decl.kind in {"invalid", "ambiguous"}:
            return ControlPlaneScope(
                kind="invalid",
                matched_surfaces=tuple(sorted(matched_surfaces)),
                evidence_source="malformed_declaration",
                reason=f"task declaration is {decl.kind}: {decl.reason}",
            )

        # Check for protected surfaces in task text
        for surf in _matches_protected_surface(task_text):
            matched_surfaces.add(surf)

    # Check for conflict: explicit ordinary claim vs matched protected surface
    full_text = (task_text or "") + " " + json.dumps(candidate_plan or {})
    if matched_surfaces:
        if _is_ordinary_claim(full_text):
            return ControlPlaneScope(
                kind="invalid",
                matched_surfaces=tuple(sorted(matched_surfaces)),
                evidence_source="conflict",
                reason=(
                    "explicit ordinary classification conflicts with planned modification "
                    f"of protected runtime surfaces: {sorted(matched_surfaces)}"
                ),
            )
        evidence_src = "changed_paths" if changed_paths else ("candidate_plan" if candidate_plan else "task_text")
        return ControlPlaneScope(
            kind="control_plane",
            matched_surfaces=tuple(sorted(matched_surfaces)),
            evidence_source=evidence_src,
            reason=f"task touches protected control-plane runtime surfaces: {sorted(matched_surfaces)}",
        )

    return ControlPlaneScope(
        kind="ordinary",
        matched_surfaces=(),
        evidence_source="default",
        reason="task does not change protected control-plane runtime surfaces and has no control-plane declaration",
    )


def require_control_plane_declaration(
    project_id: str,
    task_id: str,
    scope: ControlPlaneScope,
    declaration: ControlPlaneDeclaration,
    plan: dict[str, Any] | None = None,
) -> ControlPlaneGate:
    """Evaluate whether task may proceed past the control-plane declaration gate."""
    if scope.kind == "ordinary":
        return ControlPlaneGate(
            allowed=True,
            code=None,
            reason="task is ordinary",
            gate_id=None,
            required_scenarios=(),
            evidence={"scope": scope.to_dict()},
        )

    if scope.kind == "unevaluable":
        return ControlPlaneGate(
            allowed=False,
            code="CONTROL_PLANE_DECLARATION_REQUIRED",
            reason=f"task classification unevaluable: {scope.reason}",
            gate_id=f"cp-gate:{project_id}:{task_id}:unevaluable",
            required_scenarios=(),
            evidence={"scope": scope.to_dict(), "declaration": declaration.to_dict(), "transient": True},
        )

    if scope.kind == "invalid":
        return ControlPlaneGate(
            allowed=False,
            code="CONTROL_PLANE_DECLARATION_REQUIRED",
            reason=f"task classification invalid: {scope.reason}",
            gate_id=f"cp-gate:{project_id}:{task_id}:invalid_scope",
            required_scenarios=(),
            evidence={"scope": scope.to_dict(), "declaration": declaration.to_dict()},
        )

    # scope is control_plane
    if declaration.kind != "declared":
        return ControlPlaneGate(
            allowed=False,
            code="CONTROL_PLANE_DECLARATION_REQUIRED",
            reason=f"control-plane declaration required: {declaration.kind} ({declaration.reason or 'no valid declaration'})",
            gate_id=f"cp-gate:{project_id}:{task_id}:declaration_{declaration.kind}",
            required_scenarios=(),
            evidence={"scope": scope.to_dict(), "declaration": declaration.to_dict()},
        )

    required_scenarios = required_scenarios_for(declaration.invariants)
    missing_from_declaration = set(required_scenarios) - set(declaration.fault_scenarios)
    if missing_from_declaration:
        return ControlPlaneGate(
            allowed=False,
            code="CONTROL_PLANE_DECLARATION_REQUIRED",
            reason=f"declaration omits required fault scenarios for declared invariants: {sorted(missing_from_declaration)}",
            gate_id=f"cp-gate:{project_id}:{task_id}:omitted_scenarios",
            required_scenarios=required_scenarios,
            evidence={"scope": scope.to_dict(), "declaration": declaration.to_dict(), "missing": sorted(missing_from_declaration)},
        )

    if plan is not None:
        plan_text = " ".join([
            str(plan.get("summary") or ""),
            " ".join(str(s) for s in plan.get("implementation_steps") or []),
            " ".join(str(s) for s in plan.get("interfaces") or []),
            " ".join(str(s) for s in plan.get("validation") or []),
        ])
        missing_from_plan = [s_id for s_id in required_scenarios if s_id not in plan_text]
        if missing_from_plan:
            return ControlPlaneGate(
                allowed=False,
                code="CONTROL_PLANE_DECLARATION_REQUIRED",
                reason=f"candidate plan does not reference required fault scenarios in validation/steps: {missing_from_plan}",
                gate_id=f"cp-gate:{project_id}:{task_id}:plan_missing_scenarios",
                required_scenarios=required_scenarios,
                evidence={"scope": scope.to_dict(), "declaration": declaration.to_dict(), "uncovered": missing_from_plan},
            )

    return ControlPlaneGate(
        allowed=True,
        code=None,
        reason="control-plane declaration and required scenario coverage verified",
        gate_id=None,
        required_scenarios=required_scenarios,
        evidence={
            "scope": scope.to_dict(),
            "declaration_hash": declaration.content_hash,
            "required_scenarios": list(required_scenarios),
        },
    )


def declaration_prompt_block(declaration: ControlPlaneDeclaration) -> str:
    """Produce the canonical bounded adversarial prompt fragment for control-plane tasks."""
    if declaration.kind != "declared":
        return ""
    lines = [
        "[CONTROL_PLANE_CONTRACT_BEGIN]",
        f"Invariants: {', '.join(declaration.invariants)}",
        f"Transition boundaries: {', '.join(declaration.transition_boundaries)}",
        f"Fault scenarios: {', '.join(declaration.fault_scenarios)}",
        f"Convergence evidence: {declaration.convergence_evidence}",
        "Adversarial verification checklist:",
        "- Invariant preservation: verify declared invariants hold across normal and failure paths",
        "- Idempotence: verify repeated ticks/restarts do not duplicate handoffs, workers, or transitions",
        "- Restart/replay behavior: verify recovery converges to single owner from journal",
        "- Fail-closed boundaries: verify ambiguous or contradictory authority refuses to OWNER_GATE",
        "[CONTROL_PLANE_CONTRACT_END]",
    ]
    return "\n".join(lines)


def declaration_requirement_prompt_block(role: str = "planner") -> str:
    lines = [
        "[CONTROL_PLANE_CONTRACT_BEGIN]",
        "CONTROL-PLANE CONTRACT REQUIREMENT:",
        "This task touches protected control-plane runtime surfaces. Your plan MUST include a canonical Control-Plane Impact declaration in its interfaces or plan text with exactly these four fields:",
        f"- Invariants: <comma-separated subset of: {', '.join(sorted(INVARIANT_CODES))}>",
        f"- Transition boundaries: <comma-separated subset of: {', '.join(sorted(TRANSITION_BOUNDARIES))}>",
        "- Fault scenarios: <comma-separated CPF scenario IDs covering all declared invariants, e.g. CPF-01, CPF-02>",
        "- Convergence evidence: <human-readable description of convergence criteria>",
        "Adversarial verification checklist:",
        "- Invariant preservation: verify declared invariants hold across normal and failure paths",
        "- Idempotence: verify repeated ticks/restarts do not duplicate handoffs, workers, or transitions",
        "- Restart/replay behavior: verify recovery converges to single owner from journal",
        "- Fail-closed boundaries: verify ambiguous or contradictory authority refuses to OWNER_GATE",
        "[CONTROL_PLANE_CONTRACT_END]",
    ]
    return "\n".join(lines)


def inject_control_plane_contract(
    prompt: str,
    role: str,
    declaration: ControlPlaneDeclaration | None,
) -> str:
    """Inject control-plane contract prompt block idempotently."""
    if "[CONTROL_PLANE_CONTRACT_BEGIN]" in prompt:
        return prompt
    if declaration is not None and declaration.kind == "declared":
        block = declaration_prompt_block(declaration)
    elif role in ("planner", "plan_reviewer"):
        block = declaration_requirement_prompt_block(role)
    else:
        return prompt
    if not block:
        return prompt
    return f"{prompt.rstrip()}\n\n{block}\n"


PROTECTED_SURFACE_BOUNDARIES: dict[str, set[str]] = {
    "lifecycle_authority.py": {"authority_reconciliation", "owner_gate_transition"},
    "transition_executor.py": {"worker_launch", "special_gate_reconciliation", "authority_reconciliation", "owner_gate_transition"},
    "successor_consistency.py": {"successor_handoff", "handoff_publication"},
    "watchdog.py": {"watchdog_recovery"},
    "staged_roadmap.py": {"successor_handoff"},
    "control_plane_contract.py": {"plan_freeze", "worker_launch", "special_gate_reconciliation"},
    "control_plane_faults.py": {"special_gate_reconciliation", "worker_launch"},
}


def declared_scope_gap(
    declaration: ControlPlaneDeclaration | None,
    changed_paths: Iterable[str],
) -> list[str]:
    """Detect review-time scope gap when actual changed paths touch protected surfaces without declaration."""
    gaps: list[str] = []
    for path in changed_paths:
        norm = path.replace("\\", "/")
        is_protected = (
            norm in CONTROL_PLANE_RUNTIME_SURFACES
            or (Path(norm).name in _PROTECTED_PATH_STEMS and "dev_orchestrator/core" in norm)
        )
        if not is_protected:
            continue
        if declaration is None or declaration.kind != "declared":
            gaps.append(f"changed protected runtime surface '{norm}' has no control-plane declaration")
        else:
            stem = Path(norm).name
            expected_boundaries = PROTECTED_SURFACE_BOUNDARIES.get(stem) or PROTECTED_SURFACE_BOUNDARIES.get(norm)
            if expected_boundaries:
                declared_boundaries = set(declaration.transition_boundaries)
                if not (declared_boundaries & expected_boundaries):
                    gaps.append(
                        f"changed protected runtime surface '{norm}' requires declaration of transition boundaries: "
                        f"{sorted(expected_boundaries)}"
                    )
    return gaps


def convergence_evidence(
    executor_state: dict[str, Any],
    planner_state: dict[str, Any] | None = None,
    reviewer_state: dict[str, Any] | None = None,
    decisions_state: dict[str, Any] | None = None,
    project_id: str = "devorchestrator",
) -> dict[str, Any]:
    """Read-only projection of lifecycle convergence evidence."""
    authorities = (executor_state.get("lifecycle") or {}) if isinstance(executor_state, dict) else {}
    authority = authorities.get(project_id) if isinstance(authorities, dict) else None

    executions = (executor_state.get("executions") or {}) if isinstance(executor_state, dict) else {}
    transitions = (executor_state.get("transitions") or {}) if isinstance(executor_state, dict) else {}

    handoffs = [
        row for row in executions.values()
        if isinstance(row, dict) and row.get("project_id") == project_id and row.get("state") == "handoff"
    ]
    project_transitions = [
        row for row in transitions.values()
        if isinstance(row, dict) and row.get("project_id") == project_id
    ]

    active_gate = authority.get("owner_gate") if isinstance(authority, dict) else None
    resolved_gates = authority.get("resolved_owner_gates") if isinstance(authority, dict) else []

    return {
        "project_id": project_id,
        "current_task_id": authority.get("current_task_id") if isinstance(authority, dict) else None,
        "lifecycle_state": authority.get("lifecycle_state") if isinstance(authority, dict) else "UNKNOWN",
        "generation": authority.get("generation", 0) if isinstance(authority, dict) else 0,
        "active_owner": authority.get("active_owner") if isinstance(authority, dict) else None,
        "active_transition_id": authority.get("active_transition_id") if isinstance(authority, dict) else None,
        "active_gate": active_gate,
        "resolved_gates_count": len(resolved_gates or []),
        "handoffs_count": len(handoffs),
        "transitions_count": len(project_transitions),
        "manual_intervention_required": active_gate is not None,
        "has_authoritative_record": authority is not None,
    }


def traversal_evidence(
    executor_state: dict[str, Any],
    planner_state: dict[str, Any] | None = None,
    reviewer_state: dict[str, Any] | None = None,
    decisions_state: dict[str, Any] | None = None,
    project_id: str = "devorchestrator",
) -> dict[str, Any]:
    """Read-only projection of lifecycle task traversal stages."""
    conv = convergence_evidence(executor_state, planner_state, reviewer_state, decisions_state, project_id)
    plans = (planner_state.get("plans") or {}) if isinstance(planner_state, dict) else {}
    reviews = (reviewer_state.get("reviews") or {}) if isinstance(reviewer_state, dict) else {}

    task_id = conv.get("current_task_id") or "UNKNOWN"

    project_plans = [
        p for p in plans.values()
        if isinstance(p, dict) and p.get("project_id") == project_id and p.get("task_id") == task_id
    ]
    project_reviews = [
        r for r in reviews.values()
        if isinstance(r, dict) and r.get("project_id") == project_id and r.get("task_id") == task_id
    ]

    return {
        "task_id": task_id,
        "project_id": project_id,
        "plan_frozen": bool(project_plans),
        "plan_reviewed": any(p.get("state") in {"accepted", "ready_to_run"} for p in project_plans),
        "worker_launched": any(
            isinstance(row, dict) and row.get("project_id") == project_id and row.get("task_id") == task_id
            for row in (executor_state.get("executions") or {}).values()
        ),
        "review_settled": any(r.get("state") == "completed" for r in project_reviews),
        "convergence": conv,
    }
