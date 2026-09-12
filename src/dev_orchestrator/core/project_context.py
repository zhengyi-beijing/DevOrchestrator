"""Durable project context schema, fail-closed validation, and prompt injection.

Provides project-scoped context (goals, architecture, protected scope, safety
constraints, validation commands, runtime assumptions, key decisions) that
survives conversation boundaries and is injected deterministically into AI
Planner, Worker, and Reviewer prompts.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

PROJECT_CONTEXT_SCHEMA_VERSION = 1
CONTEXT_DOMAINS = (
    "goals",
    "architecture",
    "protected_scope",
    "safety_constraints",
    "validation_commands",
    "runtime_assumptions",
    "key_decisions",
)
DEFAULT_DOCUMENT_RELATIVE_PATH = "agent/project-context.json"
MAX_ENTRIES_PER_DOMAIN = 40
MAX_ENTRY_CHAR_LENGTH = 600
MIN_MAX_CHARS = 500
MAX_MAX_CHARS = 20000
DEFAULT_MAX_CHARS = 6000
DEFAULT_INJECT_ROLES = ("planner", "worker", "reviewer")

FORBIDDEN_SECRET_MARKERS = (
    "password",
    "api_key",
    "api key",
    "secret",
    "token",
    "bearer ",
    "client_secret",
    "private_key",
    "-----begin",
)


@dataclass(frozen=True)
class ProjectContextDocument:
    schema_version: int
    project_id: Optional[str]
    updated_at: Optional[str]
    domains: dict[str, tuple[str, ...]]
    provenance: dict[str, str]
    digest: str


@dataclass(frozen=True)
class ProjectContextResolution:
    state: str
    reason: str
    document: Optional[ProjectContextDocument]
    sources: dict[str, Any]
    status: dict[str, Any]


def validate_context_document(
    payload: Any,
    *,
    expected_project_id: Optional[str] = None,
    allow_partial_domains: bool = False,
) -> tuple[Optional[ProjectContextDocument], str]:
    """Strict fail-closed document validation for durable project context.

    Rejects unknown top-level keys, unsupported schema_version, non-list
    domains, oversized entries, blank entries, project_id mismatches, and
    forbidden secret markers.
    """
    if not isinstance(payload, dict):
        return None, "payload must be a JSON object"

    allowed_keys = {"schema_version", "project_id", "updated_at"} | set(CONTEXT_DOMAINS)
    unknown = set(payload.keys()) - allowed_keys
    if unknown:
        return None, f"unknown top-level keys: {sorted(unknown)}"

    schema_ver = payload.get("schema_version")
    if isinstance(schema_ver, bool) or not isinstance(schema_ver, int):
        return None, "schema_version must be an integer"
    if schema_ver != PROJECT_CONTEXT_SCHEMA_VERSION:
        return None, "unsupported project context schema_version"

    if not allow_partial_domains:
        missing = set(CONTEXT_DOMAINS) - set(payload.keys())
        if missing:
            return None, f"missing required context domains: {sorted(missing)}"

    pid = payload.get("project_id")
    if pid is not None:
        if not isinstance(pid, str) or not pid.strip():
            return None, "project_id must be a non-blank string"
        if expected_project_id is not None and pid.strip() != expected_project_id.strip():
            return None, f"project_id mismatch: expected {expected_project_id!r}, found {pid.strip()!r}"

    updated = payload.get("updated_at")
    if updated is not None:
        if not isinstance(updated, str) or not updated.strip():
            return None, "updated_at must be a non-blank string"

    validated_domains: dict[str, tuple[str, ...]] = {}
    for domain in CONTEXT_DOMAINS:
        if domain in payload:
            val = payload[domain]
            if not isinstance(val, list):
                return None, f"domain {domain!r} must be a list"
            if len(val) > MAX_ENTRIES_PER_DOMAIN:
                return None, f"domain {domain!r} exceeds {MAX_ENTRIES_PER_DOMAIN} entries"
            entries: list[str] = []
            for entry in val:
                if not isinstance(entry, str) or not entry.strip():
                    return None, f"domain {domain!r} entry must be a non-blank string"
                if len(entry) > MAX_ENTRY_CHAR_LENGTH:
                    return None, f"domain {domain!r} entry exceeds {MAX_ENTRY_CHAR_LENGTH} characters"
                lower_entry = entry.lower()
                for marker in FORBIDDEN_SECRET_MARKERS:
                    if marker in lower_entry:
                        return None, f"domain {domain!r} entry contains forbidden secret marker {marker!r}"
                entries.append(entry)
            validated_domains[domain] = tuple(entries)
        else:
            validated_domains[domain] = ()

    provenance = {d: "declared" for d in CONTEXT_DOMAINS}
    canonical = json.dumps(validated_domains, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()[:16]

    doc = ProjectContextDocument(
        schema_version=schema_ver,
        project_id=pid.strip() if isinstance(pid, str) else None,
        updated_at=updated.strip() if isinstance(updated, str) else None,
        domains=validated_domains,
        provenance=provenance,
        digest=digest,
    )
    return doc, ""


def resolve_document_path(repo_path: Path | str, declared_relative: str) -> tuple[Optional[Path], str]:
    """Resolve a repo-relative path and verify it stays strictly within the repository."""
    if not isinstance(declared_relative, str) or not declared_relative.strip():
        return None, "document path must be a non-blank string"
    stripped = declared_relative.strip()
    raw = Path(stripped)
    if raw.is_absolute() or stripped.startswith(("/", "\\")):
        return None, "document path must be relative to repository"
    if raw.drive:
        return None, "document path must not contain a drive specification"
    repo = Path(repo_path).resolve()
    target = (repo / raw).resolve()
    try:
        target.relative_to(repo)
    except ValueError:
        return None, "document path escapes repository root"
    return target, ""


def load_declared_document(project: dict[str, Any]) -> tuple[Optional[ProjectContextDocument], str]:
    """Read and validate the declared context document from the project repository."""
    repo_path = project.get("repo_path") or project.get("root")
    if not repo_path:
        return None, "project missing repo_path"
    ctx_decl = project.get("project_context")
    if not isinstance(ctx_decl, dict):
        rel_path = DEFAULT_DOCUMENT_RELATIVE_PATH
    else:
        rel_path = ctx_decl.get("document_path") or DEFAULT_DOCUMENT_RELATIVE_PATH
    doc_path, path_err = resolve_document_path(str(repo_path), str(rel_path))
    if doc_path is None:
        return None, path_err
    if not doc_path.is_file():
        return None, f"context document not found: {doc_path}"
    try:
        text = doc_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return None, f"cannot read context document: {exc}"
    try:
        payload = json.loads(text)
    except Exception as exc:
        return None, f"invalid JSON in context document: {exc}"
    expected_pid = project.get("project_id") or project.get("id")
    return validate_context_document(payload, expected_project_id=str(expected_pid) if expected_pid else None)


def load_supplement(project: dict[str, Any]) -> tuple[Optional[ProjectContextDocument], str]:
    """Read and validate the optional supplement context document (e.g. Graphify output)."""
    repo_path = project.get("repo_path") or project.get("root")
    if not repo_path:
        return None, "project missing repo_path"
    ctx_decl = project.get("project_context")
    if not isinstance(ctx_decl, dict):
        return None, "absent"
    supp_rel = ctx_decl.get("supplement_path")
    if not supp_rel or not isinstance(supp_rel, str) or not supp_rel.strip():
        return None, "absent"
    doc_path, path_err = resolve_document_path(str(repo_path), supp_rel.strip())
    if doc_path is None:
        return None, path_err
    if not doc_path.is_file():
        return None, "absent"
    try:
        text = doc_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return None, f"cannot read supplement document: {exc}"
    try:
        payload = json.loads(text)
    except Exception as exc:
        return None, f"invalid JSON in supplement document: {exc}"
    expected_pid = project.get("project_id") or project.get("id")
    return validate_context_document(
        payload,
        expected_project_id=str(expected_pid) if expected_pid else None,
        allow_partial_domains=True,
    )


def resolve_project_context(project: dict[str, Any]) -> ProjectContextResolution:
    """Resolve durable project context with declared-authoritative precedence.

    Never raises an unhandled exception: errors degrade to state='invalid'.
    """
    try:
        ctx_decl = project.get("project_context")
        if not isinstance(ctx_decl, dict) or not ctx_decl.get("enabled"):
            status = {
                "schema_version": PROJECT_CONTEXT_SCHEMA_VERSION,
                "state": "absent",
                "reason": "project context not enabled",
                "digest": None,
                "updated_at": None,
                "document_path": None,
                "supplement_used": False,
                "domains": {},
            }
            return ProjectContextResolution(
                state="absent",
                reason="project context not enabled",
                document=None,
                sources={},
                status=status,
            )

        repo_path = project.get("repo_path") or project.get("root")
        doc_rel = ctx_decl.get("document_path") or DEFAULT_DOCUMENT_RELATIVE_PATH
        supp_rel = ctx_decl.get("supplement_path")

        sources = {
            "document_path": str(doc_rel),
            "supplement_path": str(supp_rel) if supp_rel else None,
        }

        doc_file, path_err = resolve_document_path(str(repo_path), str(doc_rel))
        if doc_file is None:
            status = {
                "schema_version": PROJECT_CONTEXT_SCHEMA_VERSION,
                "state": "invalid",
                "reason": path_err,
                "digest": None,
                "updated_at": None,
                "document_path": str(doc_rel),
                "supplement_used": False,
                "domains": {},
            }
            return ProjectContextResolution(
                state="invalid",
                reason=path_err,
                document=None,
                sources=sources,
                status=status,
            )

        declared_doc, decl_err = load_declared_document(project)
        if declared_doc is None:
            status = {
                "schema_version": PROJECT_CONTEXT_SCHEMA_VERSION,
                "state": "invalid",
                "reason": decl_err,
                "digest": None,
                "updated_at": None,
                "document_path": str(doc_rel),
                "supplement_used": False,
                "domains": {},
            }
            return ProjectContextResolution(
                state="invalid",
                reason=decl_err,
                document=None,
                sources=sources,
                status=status,
            )

        supplement_doc = None
        supplement_used = False
        if supp_rel:
            supp_doc, _ = load_supplement(project)
            if supp_doc is not None:
                supplement_doc = supp_doc

        merged_domains: dict[str, tuple[str, ...]] = {}
        provenance: dict[str, str] = {}
        for d in CONTEXT_DOMAINS:
            decl_entries = declared_doc.domains.get(d, ())
            if decl_entries:
                merged_domains[d] = decl_entries
                provenance[d] = "declared"
            else:
                supp_entries = supplement_doc.domains.get(d, ()) if supplement_doc else ()
                if supp_entries:
                    merged_domains[d] = supp_entries
                    provenance[d] = "generated"
                    supplement_used = True
                else:
                    merged_domains[d] = ()
                    provenance[d] = "declared"

        canonical = json.dumps(merged_domains, sort_keys=True).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()[:16]

        final_doc = ProjectContextDocument(
            schema_version=PROJECT_CONTEXT_SCHEMA_VERSION,
            project_id=declared_doc.project_id,
            updated_at=declared_doc.updated_at,
            domains=merged_domains,
            provenance=provenance,
            digest=digest,
        )

        domain_counts = {d: len(merged_domains[d]) for d in CONTEXT_DOMAINS}
        status = {
            "schema_version": PROJECT_CONTEXT_SCHEMA_VERSION,
            "state": "ready",
            "reason": "context resolved",
            "digest": digest,
            "updated_at": declared_doc.updated_at,
            "document_path": str(doc_rel),
            "supplement_used": supplement_used,
            "domains": domain_counts,
        }

        return ProjectContextResolution(
            state="ready",
            reason="context resolved",
            document=final_doc,
            sources=sources,
            status=status,
        )
    except Exception as exc:
        status = {
            "schema_version": PROJECT_CONTEXT_SCHEMA_VERSION,
            "state": "invalid",
            "reason": f"unexpected error resolving project context: {exc}",
            "digest": None,
            "updated_at": None,
            "document_path": None,
            "supplement_used": False,
            "domains": {},
        }
        return ProjectContextResolution(
            state="invalid",
            reason=f"unexpected error resolving project context: {exc}",
            document=None,
            sources={},
            status=status,
        )


def context_status(resolution: ProjectContextResolution) -> dict[str, Any]:
    """Return bounded, safe status metadata without raw document contents or secrets."""
    return copy.deepcopy(resolution.status)


def render_context_block(document: ProjectContextDocument, *, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Emit deterministic prompt block delimited by [PROJECT_CONTEXT_BEGIN]/[PROJECT_CONTEXT_END].

    Truncates cleanly at the entry boundary with an explicit [PROJECT_CONTEXT_TRUNCATED] marker.
    """
    closer_normal = "\n[PROJECT_CONTEXT_END]"
    closer_truncated = "\n[PROJECT_CONTEXT_TRUNCATED]\n[PROJECT_CONTEXT_END]"

    header_lines = [
        "[PROJECT_CONTEXT_BEGIN]",
        f"schema_version={document.schema_version} digest={document.digest} provenance={json.dumps(document.provenance, sort_keys=True)}",
    ]

    all_domain_lines: list[tuple[str, list[str]]] = []
    for domain in CONTEXT_DOMAINS:
        entries = document.domains.get(domain, ())
        if not entries:
            continue
        prov = document.provenance.get(domain, "declared")
        title = f"## {domain} (supplemental, unverified)" if prov == "generated" else f"## {domain}"
        all_domain_lines.append((title, [f"- {entry}" for entry in entries]))

    # Test if everything fits without truncation
    full_lines = list(header_lines)
    for title, entries in all_domain_lines:
        full_lines.append("")
        full_lines.append(title)
        full_lines.extend(entries)

    full_rendered = "\n".join(full_lines) + closer_normal
    if len(full_rendered) <= max_chars:
        return full_rendered

    # Truncation required: accumulate entries while remaining within max_chars
    rendered_lines = list(header_lines)
    for title, entries in all_domain_lines:
        if not entries:
            continue
        candidate_first = "\n".join(rendered_lines) + f"\n\n{title}\n{entries[0]}"
        if len(candidate_first) + len(closer_truncated) > max_chars:
            break
        rendered_lines.append("")
        rendered_lines.append(title)
        rendered_lines.append(entries[0])

        hit_limit = False
        for entry_line in entries[1:]:
            candidate = "\n".join(rendered_lines) + f"\n{entry_line}"
            if len(candidate) + len(closer_truncated) > max_chars:
                hit_limit = True
                break
            rendered_lines.append(entry_line)
        if hit_limit:
            break

    return "\n".join(rendered_lines) + closer_truncated


def context_prompt_block(project: dict[str, Any], role: str) -> tuple[str, ProjectContextResolution]:
    """Resolve project context and render prompt block if ready and role is enabled."""
    resolution = resolve_project_context(project)
    if resolution.state != "ready" or resolution.document is None:
        return "", resolution
    ctx_decl = project.get("project_context") or {}
    inject_roles = ctx_decl.get("inject_roles")
    if inject_roles is None:
        inject_roles = DEFAULT_INJECT_ROLES
    if role not in inject_roles:
        return "", resolution
    max_chars = ctx_decl.get("max_chars", DEFAULT_MAX_CHARS)
    if isinstance(max_chars, bool) or not isinstance(max_chars, int):
        max_chars = DEFAULT_MAX_CHARS
    max_chars = max(MIN_MAX_CHARS, min(MAX_MAX_CHARS, max_chars))
    block = render_context_block(resolution.document, max_chars=max_chars)
    return block, resolution
