"""Structured execution readiness authority and migration for DevOrchestrator.

Manages machine-readable repository readiness (agent/execution-state.json,
schema_version 1) bound to current task identity. Replaces free-form Markdown
regex authority while providing a deterministic legacy component grammar and
an audited daemon-authoritative migration path.
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.platform.process import hidden_subprocess_kwargs
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json

READINESS_SCHEMA_VERSION = 1
EXECUTION_STATE_VOCABULARY = frozenset(
    {"pending_design", "ready_to_run", "executing", "completed", "blocked"}
)
READINESS_VOCABULARY = EXECUTION_STATE_VOCABULARY
_REQUIRED_STRUCTURED_KEYS = frozenset(
    {"schema_version", "execution_state", "task_id", "updated_at"}
)

_NEXT_TITLE_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_NEXT_STATUS_RE = re.compile(r"^Status:\s*(.+)$", re.MULTILINE | re.IGNORECASE)

LEGACY_COMPONENT_MAP: dict[str, str] = {
    "READY": "design_ready",
    "READY_TO_RUN": "design_ready",
    "READY TO RUN": "design_ready",
    "READY-TO-RUN": "design_ready",
    "DESIGN READY": "design_ready",
    "EXECUTABLE": "design_ready",
    "PENDING DESIGN": "pending_design",
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

_DIRECT_LIVE_REGEX = re.compile(
    r"READY[_ -]?TO[_ -]?RUN|DESIGN READY|EXECUTABLE", re.IGNORECASE
)


def parse_legacy_status_components(raw_token: str) -> tuple[list[str], str, Optional[str]]:
    """Parse legacy status components and derive migration candidate."""
    cleaned = raw_token.replace("*", "").replace("`", "").strip()
    parts = [p.strip().upper() for p in cleaned.split("/") if p.strip()]
    if not parts:
        return [], "EMPTY", None

    mapped_components: list[str] = []
    for part in parts:
        mapped = LEGACY_COMPONENT_MAP.get(part)
        if mapped is None:
            return mapped_components, f"UNKNOWN: {part}", None
        mapped_components.append(mapped)

    comp_set = set(mapped_components)
    conflicts = {
        frozenset({"design_ready", "pending_design"}),
        frozenset({"design_ready", "blocked"}),
        frozenset({"not_started", "executing"}),
        frozenset({"not_started", "completed"}),
    }
    for conf in conflicts:
        if conf.issubset(comp_set):
            return mapped_components, "CONFLICT", None

    candidate = None
    if "design_ready" in comp_set and ("not_started" in comp_set or len(comp_set) == 1):
        candidate = "ready_to_run"
    elif "pending_design" in comp_set and len(comp_set) == 1:
        candidate = "pending_design"
    elif "blocked" in comp_set:
        candidate = "blocked"
    elif "completed" in comp_set:
        candidate = "completed"

    return mapped_components, "OK", candidate


@dataclass(frozen=True)
class ReadinessResolution:
    """Resolved readiness contract for a repository."""

    state: str
    source: str
    task_id: Optional[str]
    valid: bool
    stale: bool
    code: str
    reason: str
    migration_candidate: Optional[str] = None
    migration_required: bool = False
    schema_version: int = READINESS_SCHEMA_VERSION
    raw_token: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "state": self.state,
            "source": self.source,
            "task_id": self.task_id,
            "valid": self.valid,
            "stale": self.stale,
            "code": self.code,
            "reason": self.reason,
            "migration_candidate": self.migration_candidate,
            "migration_required": self.migration_required,
        }


def write_structured_readiness(
    repo_path: Path | str,
    execution_state: str,
    task_id: str,
    *,
    updated_at: Optional[str] = None,
) -> Path:
    """Write schema_version 1 agent/execution-state.json atomically."""
    if execution_state not in EXECUTION_STATE_VOCABULARY:
        raise ValueError(
            f"execution_state must be in {sorted(EXECUTION_STATE_VOCABULARY)}, got {execution_state!r}"
        )
    clean_task_id = str(task_id or "").strip()
    if not clean_task_id:
        raise ValueError("task_id must be a non-blank string")

    target_dir = Path(repo_path) / "agent"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / "execution-state.json"
    data = {
        "schema_version": READINESS_SCHEMA_VERSION,
        "execution_state": execution_state,
        "task_id": clean_task_id,
        "updated_at": updated_at or utc_now_iso(),
    }
    write_json(target_file, data, indent=2)
    return target_file


def _resolve_legacy_markdown(
    repo: Path,
    current_task_id: Optional[str],
    next_status: Optional[str],
) -> ReadinessResolution:
    """Resolve readiness using the bounded closed component grammar."""
    next_file = repo / "agent" / "next.md"
    next_text = ""
    if next_file.is_file():
        try:
            next_text = next_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            next_text = ""

    status_matches = list(_NEXT_STATUS_RE.finditer(next_text))
    if len(status_matches) > 1:
        return ReadinessResolution(
            state="invalid",
            source="legacy_markdown",
            task_id=current_task_id,
            valid=False,
            stale=False,
            code="READINESS_TOKEN_UNRESOLVABLE",
            reason=f"expected at most one Status line in agent/next.md; found {len(status_matches)}",
            migration_candidate=None,
            migration_required=False,
            raw_token=status_matches[0].group(1).strip() if status_matches else None,
        )

    raw_token = next_status
    if raw_token is None and status_matches:
        raw_token = status_matches[0].group(1).strip()

    if not raw_token:
        return ReadinessResolution(
            state="idle",
            source="legacy_markdown",
            task_id=current_task_id,
            valid=False,
            stale=False,
            code="READINESS_NOT_FOUND",
            reason="no Status line found in agent/next.md",
            migration_candidate=None,
            migration_required=False,
            raw_token=None,
        )

    # Strip markdown bold/italic asterisks or backticks
    cleaned = raw_token.replace("*", "").replace("`", "").strip()
    parts = [p.strip().upper() for p in cleaned.split("/") if p.strip()]
    if not parts:
        return ReadinessResolution(
            state="invalid",
            source="legacy_markdown",
            task_id=current_task_id,
            valid=False,
            stale=False,
            code="READINESS_TOKEN_UNRESOLVABLE",
            reason=f"empty Status token in agent/next.md: {raw_token!r}",
            migration_candidate=None,
            migration_required=False,
            raw_token=raw_token,
        )

    mapped_components: list[str] = []
    for part in parts:
        mapped = LEGACY_COMPONENT_MAP.get(part)
        if mapped is None:
            return ReadinessResolution(
                state="invalid",
                source="legacy_markdown",
                task_id=current_task_id,
                valid=False,
                stale=False,
                code="READINESS_TOKEN_UNRESOLVABLE",
                reason=f"unknown legacy readiness token component: {part!r}",
                migration_candidate=None,
                migration_required=False,
                raw_token=raw_token,
            )
        mapped_components.append(mapped)

    mapped_set = set(mapped_components)

    # Conflict check: contradictory lifecycle families cannot coexist
    conflicting_pairs = [
        ({"blocked", "design_ready"}),
        ({"completed", "design_ready"}),
        ({"pending_design", "design_ready"}),
        ({"executing", "design_ready"}),
        ({"executing", "completed"}),
        ({"executing", "blocked"}),
        ({"completed", "blocked"}),
    ]
    for conf in conflicting_pairs:
        if conf.issubset(mapped_set):
            return ReadinessResolution(
                state="invalid",
                source="legacy_markdown",
                task_id=current_task_id,
                valid=False,
                stale=False,
                code="READINESS_TOKEN_UNRESOLVABLE",
                reason=f"conflicting lifecycle components in token: {parts}",
                migration_candidate=None,
                migration_required=False,
                raw_token=raw_token,
            )

    # Determine migration candidate
    migration_candidate: Optional[str] = None
    if "design_ready" in mapped_set and "not_started" in mapped_set:
        migration_candidate = "ready_to_run"
    elif "design_ready" in mapped_set and not (
        mapped_set & {"pending_design", "blocked", "completed", "executing"}
    ):
        migration_candidate = "ready_to_run"
    elif "pending_design" in mapped_set and not (
        mapped_set & {"design_ready", "blocked", "completed", "executing"}
    ):
        migration_candidate = "pending_design"
    elif "completed" in mapped_set:
        migration_candidate = "completed"
    elif "blocked" in mapped_set:
        migration_candidate = "blocked"
    elif "executing" in mapped_set:
        migration_candidate = "executing"

    # Live projection rule:
    # Only tokens the legacy regex directly accepts (READY_TO_RUN | DESIGN READY | EXECUTABLE)
    # project ready_to_run from Markdown. Composite tokens (e.g. READY / OWNER_GOAL_DEFINED / NOT_STARTED)
    # project unchanged IDLE state until authoritative migration.
    live_regex_match = bool(_DIRECT_LIVE_REGEX.search(raw_token))
    live_state = "ready_to_run" if live_regex_match else (
        "pending_design" if "pending_design" in mapped_set else (
            "blocked" if "blocked" in mapped_set else "idle"
        )
    )

    code = "READINESS_TOKEN_UNSTRUCTURED" if migration_candidate else "OK"
    reason = (
        f"legacy markdown readiness token {raw_token!r} requires structured migration"
        if migration_candidate
        else f"legacy token {raw_token!r}"
    )

    return ReadinessResolution(
        state=live_state,
        source="legacy_markdown",
        task_id=current_task_id,
        valid=True,
        stale=False,
        code=code,
        reason=reason,
        migration_candidate=migration_candidate,
        migration_required=bool(migration_candidate),
        raw_token=raw_token,
    )


def resolve_readiness(
    repo_path: Path | str,
    current_task_id: Optional[str] = None,
    next_status: Optional[str] = None,
    next_updated_at: Optional[str] = None,
) -> ReadinessResolution:
    """Resolve authoritative execution readiness for a repository.

    Reads agent/execution-state.json if present. If absent, resolves through
    the legacy component grammar. Strictly read-only; performs no filesystem
    mutations.
    """
    repo = Path(repo_path)
    exec_state_file = repo / "agent" / "execution-state.json"

    if exec_state_file.is_file():
        raw_bytes = b""
        try:
            raw_bytes = exec_state_file.read_bytes()
        except OSError as exc:
            return ReadinessResolution(
                state="invalid",
                source="structured",
                task_id=None,
                valid=False,
                stale=False,
                code="READINESS_SCHEMA_INVALID",
                reason=f"cannot read execution-state.json: {exc}",
            )

        try:
            data = json.loads(raw_bytes.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            return ReadinessResolution(
                state="invalid",
                source="structured",
                task_id=None,
                valid=False,
                stale=False,
                code="READINESS_SCHEMA_INVALID",
                reason=f"unparsable JSON in execution-state.json: {exc}",
            )

        if not isinstance(data, dict):
            return ReadinessResolution(
                state="invalid",
                source="structured",
                task_id=None,
                valid=False,
                stale=False,
                code="READINESS_SCHEMA_INVALID",
                reason="execution-state.json must contain a JSON object",
            )

        # Require exact schema version 1 keys
        keys_set = set(data.keys())
        if keys_set != _REQUIRED_STRUCTURED_KEYS:
            missing = sorted(_REQUIRED_STRUCTURED_KEYS - keys_set)
            unknown = sorted(keys_set - _REQUIRED_STRUCTURED_KEYS)
            err_parts = []
            if missing:
                err_parts.append(f"missing required keys: {missing}")
            if unknown:
                err_parts.append(f"unknown keys: {unknown}")
            return ReadinessResolution(
                state="invalid",
                source="structured",
                task_id=str(data.get("task_id") or "") or None,
                valid=False,
                stale=False,
                code="READINESS_SCHEMA_INVALID",
                reason="; ".join(err_parts),
            )

        if data.get("schema_version") != READINESS_SCHEMA_VERSION:
            return ReadinessResolution(
                state="invalid",
                source="structured",
                task_id=str(data.get("task_id") or "") or None,
                valid=False,
                stale=False,
                code="READINESS_SCHEMA_INVALID",
                reason=f"unsupported schema_version: {data.get('schema_version')!r} (expected {READINESS_SCHEMA_VERSION})",
            )

        exec_state = data.get("execution_state")
        if exec_state not in EXECUTION_STATE_VOCABULARY:
            return ReadinessResolution(
                state="invalid",
                source="structured",
                task_id=str(data.get("task_id") or "") or None,
                valid=False,
                stale=False,
                code="READINESS_SCHEMA_INVALID",
                reason=f"invalid execution_state: {exec_state!r}; allowed: {sorted(EXECUTION_STATE_VOCABULARY)}",
            )

        file_task_id = str(data.get("task_id") or "").strip()
        if not file_task_id:
            return ReadinessResolution(
                state="invalid",
                source="structured",
                task_id=None,
                valid=False,
                stale=False,
                code="READINESS_SCHEMA_INVALID",
                reason="task_id in execution-state.json is blank",
            )

        # Task ID drift check: structured state is bound to current task
        if current_task_id is not None and file_task_id != current_task_id.strip():
            return ReadinessResolution(
                state="invalid",
                source="structured",
                task_id=file_task_id,
                valid=False,
                stale=True,
                code="READINESS_TASK_ID_MISMATCH",
                reason=(
                    f"structured execution-state task_id {file_task_id!r} "
                    f"does not match current task {current_task_id!r}"
                ),
            )

        return ReadinessResolution(
            state=exec_state,
            source="structured",
            task_id=file_task_id,
            valid=True,
            stale=False,
            code="OK",
            reason="structured execution state valid",
            migration_candidate=None,
            migration_required=False,
        )

    # Structured file absent: fall back to bounded legacy markdown
    return _resolve_legacy_markdown(repo, current_task_id, next_status)


def migrate_legacy_readiness(
    project: dict[str, Any],
    snapshot: dict[str, Any],
    runtime_root: Path | str,
    *,
    recovery_epoch_id: Optional[str] = None,
) -> tuple[bool, str, Optional[dict[str, Any]]]:
    """Authoritative migration of legacy readiness to agent/execution-state.json.

    Callable only from daemon-authoritative supervisor path. Verifies all
    required predicates before modifying git repository.
    """
    runtime = Path(runtime_root)
    project_id = str(project.get("project_id") or project.get("id") or "").strip()
    repo_path_str = str(project.get("repo_path") or project.get("root") or "").strip()
    if not repo_path_str:
        return False, "project repo_path is missing", None

    repo = Path(repo_path_str)
    if not repo.is_dir():
        return False, f"repository directory {repo} does not exist", None

    # Predicate 1: Project present in effective daemon registry
    if not project_id:
        return False, "project_id is missing or blank", None

    # Predicate 2: Structured file absent, or stale for superseded task
    exec_state_file = repo / "agent" / "execution-state.json"
    superseded_task_id: Optional[str] = None
    if exec_state_file.is_file():
        try:
            existing = json.loads(exec_state_file.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                superseded_task_id = str(existing.get("task_id") or "").strip() or None
                if existing.get("execution_state") == "executing":
                    return False, "cannot replace structured state during active execution", None
        except Exception:
            superseded_task_id = None

    # Telemetry and task contract validation
    telemetry = snapshot.get("telemetry") if isinstance(snapshot.get("telemetry"), dict) else {}
    current_task_id = str(telemetry.get("task_id") or "").strip()
    if not current_task_id:
        next_title = str(snapshot.get("next_title") or "")
        match = _NEXT_TITLE_RE.search(next_title)
        if match:
            from dev_orchestrator.monitor.telemetry import extract_task_id
            current_task_id = str(extract_task_id(next_title) or "").strip()

    if not current_task_id:
        return False, "current task identity is unresolved or missing", None

    if superseded_task_id is not None and superseded_task_id == current_task_id:
        return False, f"structured file already matches current task {current_task_id}", None

    # Predicate 3: Exactly one non-conflicting candidate of ready_to_run or pending_design
    readiness_res = resolve_readiness(
        repo,
        current_task_id=current_task_id,
        next_status=snapshot.get("next_status"),
        next_updated_at=snapshot.get("next_updated_at"),
    )

    candidate = readiness_res.migration_candidate
    if candidate not in {"ready_to_run", "pending_design"}:
        return (
            False,
            f"no resolvable migration candidate for task {current_task_id} (code: {readiness_res.code}, reason: {readiness_res.reason})",
            None,
        )

    # Predicate 4: Task contract validates
    next_file = repo / "agent" / "next.md"
    if not next_file.is_file():
        return False, "agent/next.md does not exist", None
    next_text = next_file.read_text(encoding="utf-8", errors="replace")
    title_matches = list(_NEXT_TITLE_RE.finditer(next_text))
    if len(title_matches) != 1:
        return False, f"agent/next.md must have exactly one title heading; found {len(title_matches)}", None
    status_matches = list(_NEXT_STATUS_RE.finditer(next_text))
    if len(status_matches) != 1:
        return False, f"agent/next.md must have exactly one Status line; found {len(status_matches)}", None

    # Predicate 5: Clean Git anchor
    truth = read_repository_truth(repo)
    if not truth.valid:
        return False, f"repository truth invalid: {truth.error}", None
    if truth.dirty:
        return False, f"DIRTY_WORKTREE: cannot migrate readiness on dirty repository ({len(truth.dirty_entries)} dirty entries)", None

    # Predicate 6: No owner gate, owner pause, or active execution
    from dev_orchestrator.control.owner_store import OwnerControlStore
    owner_store = OwnerControlStore(runtime)
    if owner_store.is_paused(project_id):
        return False, "project is paused by owner", None

    worker = snapshot.get("worker") if isinstance(snapshot.get("worker"), dict) else {}
    if worker.get("kind") == "task" and worker.get("state") in {"starting", "running"}:
        return False, "worker execution is currently active", None

    gate = snapshot.get("gate")
    if isinstance(gate, dict) and gate.get("state") == "owner_gate":
        return False, "owner gate is active", None

    # Predicate 7: Check migration budget in runtime/readiness-migrations.jsonl
    # At most one migration per (project_id, task_id, recovery_epoch_id)
    migrations_file = runtime / "readiness-migrations.jsonl"
    if migrations_file.is_file():
        try:
            for line in migrations_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if (
                    row.get("project_id") == project_id
                    and row.get("task_id") == current_task_id
                    and row.get("recovery_epoch_id") == recovery_epoch_id
                ):
                    return (
                        False,
                        f"migration already performed for ({project_id}, {current_task_id}, {recovery_epoch_id})",
                        None,
                    )
        except OSError:
            pass

    before_head = truth.head

    # Write structured readiness file
    try:
        write_structured_readiness(repo, candidate, current_task_id)
        # Git add and commit only agent/execution-state.json
        add_proc = subprocess.run(
            ["git", "-C", str(repo), "add", "--", "agent/execution-state.json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
            **hidden_subprocess_kwargs(),
        )
        if add_proc.returncode != 0:
            raise RuntimeError(f"git add failed: {add_proc.stderr or add_proc.stdout}")

        commit_proc = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "commit",
                "-m",
                f"chore(readiness): adopt structured execution state for {current_task_id}",
                "--",
                "agent/execution-state.json",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
            **hidden_subprocess_kwargs(),
        )
        if commit_proc.returncode != 0:
            raise RuntimeError(f"git commit failed: {commit_proc.stderr or commit_proc.stdout}")

        head_proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
            **hidden_subprocess_kwargs(),
        )
        after_head = head_proc.stdout.strip() if head_proc.returncode == 0 else ""
        if not after_head:
            raise RuntimeError("cannot read after-commit HEAD")
    except Exception as exc:
        # Roll back if commit failed and HEAD moved or didn't move
        try:
            post_truth = read_repository_truth(repo)
            if post_truth.valid and post_truth.head == before_head:
                if superseded_task_id is None:
                    if exec_state_file.is_file():
                        exec_state_file.unlink(missing_ok=True)
                    subprocess.run(
                        ["git", "-C", str(repo), "reset", "--", "agent/execution-state.json"],
                        capture_output=True,
                        timeout=15,
                        **hidden_subprocess_kwargs(),
                    )
        except Exception:
            pass
        return False, f"migration git commit failed: {exc}", None

    # Record migration in runtime/readiness-migrations.jsonl
    migration_record = {
        "project_id": project_id,
        "task_id": current_task_id,
        "superseded_task_id": superseded_task_id,
        "source_token": readiness_res.raw_token,
        "candidate": candidate,
        "satisfied_predicates": [
            "daemon_registry",
            "structured_absent_or_stale",
            "single_candidate",
            "task_contract_valid",
            "clean_git_anchor",
            "no_gate_or_active_execution",
            "budget_available",
        ],
        "before_head": before_head,
        "after_head": after_head,
        "recovery_epoch_id": recovery_epoch_id,
        "migrated_at": utc_now_iso(),
    }
    migrations_file.parent.mkdir(parents=True, exist_ok=True)
    with migrations_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(migration_record, ensure_ascii=False) + "\n")

    return True, "migrated", migration_record
