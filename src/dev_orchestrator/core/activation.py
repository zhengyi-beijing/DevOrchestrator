"""Project activation authority and orphan state detection for DevOrchestrator.

Provides:
1. Orphan state detection when repository-local or prior runtime state exists
   while the project is absent from the effective daemon registry.
2. Durable activation requests ledger (runtime/activation-requests.json, schema_version 1)
   as the single bootstrap origin of candidate project identity and repo path.
3. Atomic registration reconciliation from configured activation profiles into
   config/projects.json.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json

ACTIVATION_REQUESTS_SCHEMA_VERSION = 1

VALID_PROJECT_ID_REGEX = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_project_id(value: Any) -> str:
    """Validate project ID against standard allowed characters [A-Za-z0-9_-]."""
    text = str(value or "").strip()
    if not text or not VALID_PROJECT_ID_REGEX.fullmatch(text):
        raise ValueError("project_id must contain only letters, digits, underscore, or hyphen")
    return text


def detect_orphan_state(
    project_id: str,
    repo_path: Optional[Path | str],
    config: Optional[dict[str, Any]],
    runtime_root: Path | str,
) -> Optional[dict[str, Any]]:
    """Detect if project has local or mirror state while absent from daemon registry.

    Returns structured ORPHANED_PROJECT_STATE diagnosis, or None if registered
    or no historical state exists.
    """
    clean_id = str(project_id or "").strip()
    if not clean_id:
        return None

    # Check if project is present in registry
    if isinstance(config, dict):
        projects = config.get("projects") or []
        for p in projects:
            if isinstance(p, dict) and (p.get("project_id") == clean_id or p.get("id") == clean_id):
                return None

    runtime = Path(runtime_root)

    # 1. Check repository-local .devorch/status.json
    if repo_path is not None:
        local_status = Path(repo_path) / ".devorch" / "status.json"
        if local_status.is_file():
            try:
                local_data = json.loads(local_status.read_text(encoding="utf-8"))
                if isinstance(local_data, dict):
                    return {
                        "code": "ORPHANED_PROJECT_STATE",
                        "project_id": clean_id,
                        "repo_path": str(repo_path),
                        "stale_phase": local_data.get("phase"),
                        "stale_state": local_data.get("state") or local_data.get("lifecycle_state"),
                        "stale_task_id": local_data.get("task_id"),
                        "historical_source": "repository_status_json",
                        "evidence_source": str(local_status),
                        "authoritative": False,
                        "message": (
                            "repository-local .devorch/status.json exists but project "
                            f"{clean_id!r} is absent from daemon registry"
                        ),
                    }
            except Exception:
                pass

    # 2. Check prior runtime/projects/{id}.json mirror
    mirror_file = runtime / "projects" / f"{clean_id}.json"
    if mirror_file.is_file():
        try:
            mirror_data = json.loads(mirror_file.read_text(encoding="utf-8"))
            if isinstance(mirror_data, dict):
                return {
                    "code": "ORPHANED_PROJECT_STATE",
                    "project_id": clean_id,
                    "repo_path": mirror_data.get("repo_path") or (str(repo_path) if repo_path else None),
                    "stale_phase": mirror_data.get("phase"),
                    "stale_state": mirror_data.get("state") or mirror_data.get("lifecycle_state"),
                    "stale_task_id": mirror_data.get("task_id"),
                    "historical_source": "runtime_project_mirror",
                    "evidence_source": str(mirror_file),
                    "authoritative": False,
                    "message": (
                        f"historical runtime project mirror exists for {clean_id!r} "
                        "but project is absent from daemon registry"
                    ),
                }
        except Exception:
            pass

    return None


def load_activation_requests(runtime_root: Path | str) -> dict[str, Any]:
    """Load runtime/activation-requests.json or return empty envelope."""
    path = Path(runtime_root) / "activation-requests.json"
    data = read_json(path, None)
    if isinstance(data, dict) and data.get("schema_version") == ACTIVATION_REQUESTS_SCHEMA_VERSION and isinstance(data.get("requests"), dict):
        return data
    return {"schema_version": ACTIVATION_REQUESTS_SCHEMA_VERSION, "requests": {}}


def record_activation_request(
    runtime_root: Path | str,
    repo_path: Path | str,
    project_id: Optional[str] = None,
    profile: Optional[str] = None,
    config_path: Optional[Path | str] = None,
    requested_action: str = "continue",
    source: str = "cli",
) -> dict[str, Any]:
    """Record an owner activation request into runtime/activation-requests.json.

    Also initializes a pending execution intent so the daemon supervisor can
    act upon it.
    """
    runtime = Path(runtime_root)
    resolved_repo = Path(repo_path).resolve(strict=False)
    if project_id is not None:
        resolved_id = validate_project_id(project_id)
    else:
        resolved_id = validate_project_id(resolved_repo.name)
    resolved_config = str(Path(config_path).resolve(strict=False)) if config_path else None

    req_id = f"act-{uuid4().hex[:12]}"
    now = utc_now_iso()

    request_record = {
        "request_id": req_id,
        "project_id": resolved_id,
        "repo_path": str(resolved_repo),
        "config_path": resolved_config,
        "profile": profile.strip() if profile else None,
        "requested_action": requested_action,
        "requested_at": now,
        "source": source,
        "state": "pending",
        "reason": None,
    }

    requests_file = runtime / "activation-requests.json"
    existing = load_activation_requests(runtime)
    existing["requests"][req_id] = request_record
    write_json(requests_file, existing, indent=2)

    # Record / refresh pending intent in runtime/execution-intent.json
    _record_pending_intent_for_activation(runtime, request_record)

    return request_record


def _record_pending_intent_for_activation(runtime: Path, request: dict[str, Any]) -> None:
    """Create or update execution intent for an unregistered project activation."""
    intent_file = runtime / "execution-intent.json"
    intents_data = read_json(intent_file, None)
    if not isinstance(intents_data, dict) or intents_data.get("schema_version") != 1 or not isinstance(intents_data.get("intents"), dict):
        intents_data = {"schema_version": 1, "intents": {}}

    project_id = request["project_id"]
    current_intent = intents_data["intents"].get(project_id)
    if isinstance(current_intent, dict) and current_intent.get("state") in {"pending", "active"}:
        current_intent["repo_path"] = request["repo_path"]
        current_intent["config_path"] = request.get("config_path")
        current_intent["activation_request_id"] = request["request_id"]
        current_intent["profile"] = request.get("profile")
        current_intent["updated_at"] = request["requested_at"]
    else:
        intents_data["intents"][project_id] = {
            "project_id": project_id,
            "task_id": None,
            "target_state": "EXECUTING",
            "command_id": f"bootstrap-{request['request_id']}",
            "source": request.get("source") or "activation_request",
            "control_revision": None,
            "recovery_epoch_id": None,
            "created_at": request["requested_at"],
            "updated_at": request["requested_at"],
            "actions_used": 0,
            "repeats_by_fingerprint": {},
            "fingerprints": [],
            "repo_path": request["repo_path"],
            "config_path": request.get("config_path"),
            "activation_request_id": request["request_id"],
            "profile": request.get("profile"),
            "state": "pending",
        }
    write_json(intent_file, intents_data, indent=2)


def reconcile_project_registration(
    request: dict[str, Any],
    config_path: Path | str,
    runtime_root: Path | str,
) -> tuple[Optional[dict[str, Any]], str]:
    """Reconcile an activation request into config/projects.json.

    Resolves worker/runtime configuration template from config's activation_profiles,
    verifies uniqueness and path validity, and atomically appends to config.
    """
    runtime = Path(runtime_root)
    cfg_path = Path(config_path)
    if not cfg_path.is_file():
        return None, f"configuration file {cfg_path} does not exist"

    repo_path = Path(request.get("repo_path") or "")
    if not repo_path.is_dir():
        return None, f"repository path {repo_path} does not exist"
    if not (repo_path / ".git").exists():
        return None, f"repository path {repo_path} is not a Git worktree"

    # Ensure repository-local .devorch is excluded from Git status
    try:
        from dev_orchestrator.core.project_status import ensure_status_is_git_ignored
        ensure_status_is_git_ignored(repo_path)
    except Exception:
        pass

    project_id = str(request.get("project_id") or "").strip()
    if not project_id:
        return None, "activation request missing project_id"

    # Read config
    try:
        raw_config = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return None, f"cannot read config JSON: {exc}"

    if not isinstance(raw_config, dict) or not isinstance(raw_config.get("projects"), list):
        return None, "config must contain a projects list"

    # Check uniqueness against existing projects
    existing_projects = raw_config["projects"]
    for p in existing_projects:
        if isinstance(p, dict):
            p_id = str(p.get("project_id") or p.get("id") or "").strip()
            if p_id == project_id:
                return None, f"project_id {project_id!r} is already registered"
            p_root = str(p.get("repo_path") or p.get("root") or "").strip()
            if p_root and Path(p_root).resolve(strict=False) == repo_path.resolve(strict=False):
                return None, f"repo_path {str(repo_path)!r} is already registered as project {p_id!r}"

    # Resolve activation profile template
    activation_profiles = raw_config.get("activation_profiles")
    if not isinstance(activation_profiles, dict) or not activation_profiles:
        return None, "REGISTRATION_TEMPLATE_MISSING: no activation_profiles configured in config"

    profile_name = request.get("profile")
    template: Optional[dict[str, Any]] = None
    if profile_name:
        template = activation_profiles.get(profile_name)
        if not isinstance(template, dict):
            return None, f"REGISTRATION_TEMPLATE_MISSING: activation profile {profile_name!r} not found"
    else:
        if "default" in activation_profiles and isinstance(activation_profiles["default"], dict):
            template = activation_profiles["default"]
        elif len(activation_profiles) == 1:
            template = next(iter(activation_profiles.values()))
        else:
            return (
                None,
                f"REGISTRATION_TEMPLATE_MISSING: ambiguous activation_profiles {list(activation_profiles.keys())}; specify profile explicitly",
            )

    # Build new project candidate
    candidate = dict(template)
    candidate["project_id"] = project_id
    candidate["repo_path"] = str(repo_path)

    # Normalize candidate through config normalizer
    from dev_orchestrator.config import _normalize_project
    try:
        normalized = _normalize_project(candidate, len(existing_projects), cfg_path)
    except Exception as exc:
        return None, f"project candidate validation failed: {exc}"

    # Check conversation binding uniqueness if provided
    binding = candidate.get("conversation_binding")
    if isinstance(binding, dict):
        for p in existing_projects:
            if isinstance(p, dict):
                p_bind = p.get("conversation_binding")
                if isinstance(p_bind, dict):
                    if (
                        p_bind.get("transport") == binding.get("transport")
                        and p_bind.get("adapter") == binding.get("adapter")
                        and p_bind.get("binding_id") == binding.get("binding_id")
                    ):
                        return None, f"conversation binding conflicts with existing project {p.get('project_id')!r}"

    # Atomic write to config/projects.json (append new project)
    updated_config = dict(raw_config)
    updated_config["projects"] = list(existing_projects) + [candidate]
    write_json(cfg_path, updated_config, indent=2)

    # Mark activation request as registered
    req_id = request.get("request_id")
    if req_id:
        reqs = load_activation_requests(runtime)
        if req_id in reqs.get("requests", {}):
            reqs["requests"][req_id]["state"] = "registered"
            reqs["requests"][req_id]["reason"] = "reconciled"
            reqs["requests"][req_id]["registered_at"] = utc_now_iso()
            write_json(runtime / "activation-requests.json", reqs, indent=2)

    return normalized, ""
