"""P20.4 Local Soak Permit and Isolation Boundary Validation."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.storage.json_store import parse_utc, read_json, utc_now_iso, write_json

SOAK_PERMIT_SCHEMA_VERSION = 1
DEFAULT_DISPOSABLE_MARKER = ".devorch-disposable-soak"


@dataclass
class SoakPermit:
    schema_version: int = SOAK_PERMIT_SCHEMA_VERSION
    run_id: str = ""
    soak_project_id: str = ""
    disposable_repo_path: str = ""
    disposable_runtime_root: str = ""
    canonical_repo_path: str = ""
    canonical_runtime_root: str = ""
    disposable_marker_name: str = DEFAULT_DISPOSABLE_MARKER
    allowed_fault_ids: list[str] = field(default_factory=list)
    permit_source: str = "local_cli"
    created_at: str = field(default_factory=utc_now_iso)
    fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SoakPermit:
        valid_fields = {f.name for f in dataclasses.fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)


def permit_fingerprint(permit: SoakPermit | dict[str, Any]) -> str:
    """Compute sha256 fingerprint over normalized permit fields."""
    d = permit.to_dict() if isinstance(permit, SoakPermit) else dict(permit)
    normalized = {
        "schema_version": int(d.get("schema_version") or SOAK_PERMIT_SCHEMA_VERSION),
        "run_id": str(d.get("run_id") or ""),
        "soak_project_id": str(d.get("soak_project_id") or ""),
        "disposable_repo_path": str(Path(d.get("disposable_repo_path") or "").resolve()),
        "disposable_runtime_root": str(Path(d.get("disposable_runtime_root") or "").resolve()),
        "canonical_repo_path": str(Path(d.get("canonical_repo_path") or "").resolve()),
        "canonical_runtime_root": str(Path(d.get("canonical_runtime_root") or "").resolve()),
        "disposable_marker_name": str(d.get("disposable_marker_name") or DEFAULT_DISPOSABLE_MARKER),
        "allowed_fault_ids": sorted(list(d.get("allowed_fault_ids") or [])),
        "permit_source": str(d.get("permit_source") or ""),
        "created_at": str(d.get("created_at") or ""),
    }
    raw = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_soak_permit(
    run_id: str,
    soak_project_id: str,
    disposable_repo_path: Path | str,
    disposable_runtime_root: Path | str,
    canonical_repo_path: Path | str,
    canonical_runtime_root: Path | str,
    allowed_fault_ids: list[str] | None = None,
    permit_source: str = "local_cli",
    created_at: str | None = None,
) -> SoakPermit:
    """Create a new SoakPermit with canonicalized paths and computed fingerprint."""
    repo_res = str(Path(disposable_repo_path).resolve())
    rt_res = str(Path(disposable_runtime_root).resolve())
    can_repo_res = str(Path(canonical_repo_path).resolve())
    can_rt_res = str(Path(canonical_runtime_root).resolve())

    permit = SoakPermit(
        schema_version=SOAK_PERMIT_SCHEMA_VERSION,
        run_id=run_id,
        soak_project_id=soak_project_id,
        disposable_repo_path=repo_res,
        disposable_runtime_root=rt_res,
        canonical_repo_path=can_repo_res,
        canonical_runtime_root=can_rt_res,
        disposable_marker_name=DEFAULT_DISPOSABLE_MARKER,
        allowed_fault_ids=allowed_fault_ids or [],
        permit_source=permit_source,
        created_at=created_at or utc_now_iso(),
    )
    permit.fingerprint = permit_fingerprint(permit)
    return permit


def save_soak_permit(path: Path | str, permit: SoakPermit) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    write_json(p, permit.to_dict(), indent=2)


def load_soak_permit(path: Path | str) -> SoakPermit | None:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = read_json(p, {})
        if not isinstance(data, dict):
            return None
        return SoakPermit.from_dict(data)
    except Exception:
        return None


def soak_enabled(permit: SoakPermit | None) -> bool:
    if permit is None or not isinstance(permit, SoakPermit):
        return False
    return permit.permit_source == "local_cli" and permit.schema_version == SOAK_PERMIT_SCHEMA_VERSION


def validate_soak_permit(
    permit: SoakPermit | None,
    canonical_repo_path: Path | str,
    canonical_runtime_root: Path | str,
    *, require_marker: bool = True,
) -> tuple[bool, str | None]:
    """Fail-closed validation of soak permit and path isolation."""
    if permit is None:
        return False, "permit_absent"
    if not isinstance(permit, SoakPermit):
        return False, "invalid_permit_type"
    if permit.schema_version != SOAK_PERMIT_SCHEMA_VERSION:
        return False, "unsupported_schema_version"
    if permit.permit_source != "local_cli":
        return False, "invalid_permit_source"
    if any(not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,96}', value)
           for value in (permit.run_id, permit.soak_project_id)):
        return False, 'invalid_run_or_project_identifier'
    if permit.disposable_marker_name != DEFAULT_DISPOSABLE_MARKER:
        return False, 'invalid_marker_name'
    from dev_orchestrator.jobs.config import canonical_path, is_path_contained
    if (canonical_path(permit.canonical_repo_path) != canonical_path(canonical_repo_path)
            or canonical_path(permit.canonical_runtime_root) != canonical_path(canonical_runtime_root)):
        return False, 'canonical_identity_mismatch'

    # Fingerprint check
    try:
        expected_fp = permit_fingerprint(permit)
    except (TypeError, ValueError, OSError):
        return False, 'malformed_permit_fields'
    if permit.fingerprint != expected_fp:
        return False, "fingerprint_mismatch"

    # Resolve real paths
    try:
        can_repo = Path(canonical_repo_path).resolve()
        can_rt = Path(canonical_runtime_root).resolve()
        disp_repo = Path(permit.disposable_repo_path).resolve()
        disp_rt = Path(permit.disposable_runtime_root).resolve()
    except Exception as exc:
        return False, f"path_resolution_error: {exc}"
    for protected in (can_repo, can_rt):
        for disposable in (disp_repo, disp_rt):
            if is_path_contained(protected, disposable) or is_path_contained(disposable, protected):
                return False, 'canonical_overlap'
    if not require_marker:
        return True, None

    # Path traversal / containment / overlap checks
    # Windows paths are case-insensitive
    can_repo_str = str(can_repo).lower()
    can_rt_str = str(can_rt).lower()
    disp_repo_str = str(disp_repo).lower()
    disp_rt_str = str(disp_rt).lower()

    if disp_repo_str == can_repo_str or disp_rt_str == can_rt_str:
        return False, "canonical_overlap_exact"

    # Check parent/child containment
    try:
        if disp_repo.is_relative_to(can_repo) or can_repo.is_relative_to(disp_repo):
            return False, "canonical_overlap_repo"
    except AttributeError:
        # Python <3.9 fallback
        if str(disp_repo).startswith(str(can_repo)) or str(can_repo).startswith(str(disp_repo)):
            return False, "canonical_overlap_repo"

    try:
        if disp_rt.is_relative_to(can_rt) or can_rt.is_relative_to(disp_rt):
            return False, "canonical_overlap_runtime"
    except AttributeError:
        if str(disp_rt).startswith(str(can_rt)) or str(can_rt).startswith(str(disp_rt)):
            return False, "canonical_overlap_runtime"

    try:
        if disp_rt.is_relative_to(can_repo) or can_repo.is_relative_to(disp_rt):
            return False, "canonical_overlap_cross"
    except AttributeError:
        if str(disp_rt).startswith(str(can_repo)) or str(can_repo).startswith(str(disp_rt)):
            return False, "canonical_overlap_cross"

    # Check marker file
    marker_file = disp_repo / permit.disposable_marker_name
    if not marker_file.is_file():
        return False, "missing_disposable_marker"

    try:
        marker_data = read_json(marker_file, {})
        if not isinstance(marker_data, dict):
            return False, "malformed_disposable_marker"
        if marker_data.get("run_id") != permit.run_id:
            return False, "marker_run_id_mismatch"
        if marker_data.get("project_id") != permit.soak_project_id:
            return False, "marker_project_id_mismatch"
    except Exception as exc:
        return False, f"marker_read_error: {exc}"

    return True, None
