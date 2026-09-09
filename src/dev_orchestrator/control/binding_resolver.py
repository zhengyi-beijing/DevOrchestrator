"""Resolve runtime conversation bindings over static migration fallback."""

from __future__ import annotations

from typing import Any, Iterable

from dev_orchestrator.control.store import ConversationControlStore

_BROWSER_BRIDGE_TRANSPORT = "browser_bridge"
_BINDING_KEYS = ("transport", "adapter", "binding_id")


class EffectiveBindingConflictError(ValueError):
    """Raised when effective project routes would cross-route one conversation."""


def _non_blank(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _static_route(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    route: dict[str, str] = {}
    for key in _BINDING_KEYS:
        text = _non_blank(value.get(key))
        if text is None:
            return None
        route[key] = text
    return route


def _runtime_route(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    adapter = _non_blank(value.get("adapter"))
    binding_id = _non_blank(value.get("binding_id"))
    if adapter is None or binding_id is None:
        return None
    return {
        "transport": _BROWSER_BRIDGE_TRANSPORT,
        "adapter": adapter,
        "binding_id": binding_id,
    }


def resolve_effective_project(
    project: dict[str, Any], store: ConversationControlStore
) -> dict[str, Any]:
    """Return one project with runtime-first conversation routing metadata."""
    result = dict(project)
    project_id = _non_blank(result.get("project_id"))
    runtime_record = store.runtime_record_for_project(project_id) if project_id else None
    if runtime_record is not None:
        if runtime_record.get("state") == "unbound":
            result["conversation_binding"] = None
            result["conversation_binding_source"] = "runtime_unbound"
            result["orchestration_ready"] = False
            return result
        route = _runtime_route(runtime_record)
        result["conversation_binding"] = route
        result["conversation_binding_source"] = (
            "runtime" if route is not None else "runtime_invalid"
        )
        result["orchestration_ready"] = route is not None
        return result

    route = _static_route(result.get("conversation_binding"))
    result["conversation_binding"] = route
    result["conversation_binding_source"] = "static" if route is not None else "none"
    result["orchestration_ready"] = route is not None
    return result


def resolve_effective_projects(
    projects: Iterable[dict[str, Any]], store: ConversationControlStore
) -> list[dict[str, Any]]:
    """Resolve all project bindings and reject duplicate effective routes."""
    resolved: list[dict[str, Any]] = []
    seen: dict[tuple[str, str, str], str] = {}
    for project in projects:
        current = resolve_effective_project(project, store)
        route = current.get("conversation_binding")
        if current.get("orchestration_ready") and isinstance(route, dict):
            key = (
                str(route.get("transport")),
                str(route.get("adapter")),
                str(route.get("binding_id")),
            )
            project_id = str(current.get("project_id") or "")
            owner = seen.get(key)
            if owner is not None and owner != project_id:
                raise EffectiveBindingConflictError(
                    "effective conversation binding ({0}, {1}, {2}) is shared by projects "
                    "{3!r} and {4!r}".format(key[0], key[1], key[2], owner, project_id)
                )
            seen[key] = project_id
        resolved.append(current)
    return resolved
