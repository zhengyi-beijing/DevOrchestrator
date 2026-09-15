"""Resolve runtime conversation bindings over static migration fallback."""

from __future__ import annotations

from typing import Any, Iterable

from .store import ConversationControlStore


class EffectiveBindingConflictError(ValueError):
    pass


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def resolve_effective_project(project: dict[str, Any], store: ConversationControlStore) -> dict[str, Any]:
    result = dict(project)
    configured_direct_ready = bool(result.get("orchestration_ready")) and not isinstance(
        result.get("conversation_binding"), dict
    )
    project_id = _text(result.get("project_id"))
    runtime = store.runtime_record_for_project(project_id) if project_id else None
    if runtime is not None:
        if runtime.get("state") == "unbound":
            result.update({"conversation_binding": None, "conversation_binding_source": "runtime_unbound", "orchestration_ready": False})
            return result
        adapter, binding_id = _text(runtime.get("adapter")), _text(runtime.get("binding_id"))
        route = {"transport": "browser_bridge", "adapter": adapter, "binding_id": binding_id} if adapter and binding_id else None
        result.update({"conversation_binding": route, "conversation_binding_source": "runtime" if route else "runtime_invalid", "orchestration_ready": route is not None})
        return result
    raw = result.get("conversation_binding")
    route = None
    if isinstance(raw, dict):
        transport, adapter, binding_id = (_text(raw.get(key)) for key in ("transport", "adapter", "binding_id"))
        if transport and adapter and binding_id:
            route = {"transport": transport, "adapter": adapter, "binding_id": binding_id}
    result.update({"conversation_binding": route, "conversation_binding_source": "static" if route else "none"})
    result["orchestration_ready"] = (
        bool(result.get("orchestration_ready", True)) if route else configured_direct_ready
    )
    return result


def resolve_effective_projects(projects: Iterable[dict[str, Any]], store: ConversationControlStore) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: dict[tuple[str, str, str], str] = {}
    for project in projects:
        current = resolve_effective_project(project, store)
        route = current.get("conversation_binding")
        if current.get("orchestration_ready") and isinstance(route, dict):
            key = (str(route.get("transport")), str(route.get("adapter")), str(route.get("binding_id")))
            project_id = str(current.get("project_id") or "")
            if key in seen and seen[key] != project_id:
                raise EffectiveBindingConflictError(f"effective conversation route is shared by {seen[key]!r} and {project_id!r}")
            seen[key] = project_id
        result.append(current)
    return result
