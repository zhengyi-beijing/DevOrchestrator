"""Persistent live ChatGPT session and runtime binding registry."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dev_orchestrator.storage.json_store import parse_utc, read_json, utc_now, write_json


class ConversationConflictError(RuntimeError):
    pass


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonblank string")
    return value.strip()


def _moment(value: datetime | None) -> datetime:
    result = value or utc_now()
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


_ACTION_SCAN_FIELDS = {
    "scanned_at",
    "primary_assistant_count",
    "assistant_turn_count",
    "conversation_turn_count",
    "total_element_count",
    "body_text_length",
    "body_has_action_marker",
    "main_count",
    "article_count",
    "iframe_count",
    "data_message_author_role_count",
    "data_testid_count",
    "data_turn_count",
    "data_turn_id_count",
    "agent_turn_class_count",
    "conversation_turn_class_count",
    "role_article_count",
    "shadow_host_count",
    "open_shadow_root_count",
    "shadow_total_element_count",
    "shadow_primary_assistant_count",
    "shadow_conversation_turn_count",
    "shadow_article_count",
    "shadow_agent_turn_class_count",
    "shadow_conversation_turn_class_count",
    "shadow_marker_root_count",
    "shadow_marker_element_count",
    "marker_node_count",
    "parsed_action_count",
    "most_recent_request_id",
    "roleless_fallback_candidate_count",
    "roleless_fallback_dispatch_count",
    "last_roleless_fallback_request_id",
    "prompt_textarea_count",
    "contenteditable_count",
    "textarea_count",
    "legacy_send_button_count",
    "composer_submit_button_count",
    "composer_kind",
    "composer_surface",
    "submit_candidate_path",
    "submit_candidate_surface",
    "websol_submit_attempt_count",
    "websol_user_turn_confirm_count",
    "last_websol_submit_path",
    "last_websol_submit_request_id",
    "last_websol_user_turn_evidence",
    "last_websol_user_turn_request_id",
    "result_submit_attempt_count",
    "result_composer_found_count",
    "result_composer_prepared_count",
    "result_send_click_count",
    "result_confirm_success_count",
    "last_result_submit_request_id",
    "last_result_submit_stage",
    "action_scan_active",
}
_ACTION_SCAN_MAX_COUNT = 10_000
_ACTION_SCAN_MAX_SHADOW_ROOTS = 32
_ACTION_SCAN_MAX_SHADOW_COUNT = _ACTION_SCAN_MAX_SHADOW_ROOTS * _ACTION_SCAN_MAX_COUNT
_ACTION_SCAN_MAX_BODY_TEXT_LENGTH = 1_000_000
_ACTION_SCAN_MAX_REQUEST_ID = 128


def _sanitize_action_scan(value: Any) -> dict[str, Any] | None:
    """Return a bounded metadata-only action scan snapshot, or ignore it."""
    if not isinstance(value, dict) or set(value) != _ACTION_SCAN_FIELDS:
        return None

    scanned_at = value.get("scanned_at")
    if not isinstance(scanned_at, str) or not scanned_at or len(scanned_at) > 40:
        return None
    try:
        parsed_at = datetime.fromisoformat(scanned_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed_at.tzinfo is None:
        return None

    count_fields = (
        "primary_assistant_count",
        "assistant_turn_count",
        "conversation_turn_count",
        "total_element_count",
        "main_count",
        "article_count",
        "iframe_count",
        "data_message_author_role_count",
        "data_testid_count",
        "data_turn_count",
        "data_turn_id_count",
        "agent_turn_class_count",
        "conversation_turn_class_count",
        "role_article_count",
        "shadow_host_count",
        "marker_node_count",
        "parsed_action_count",
        "roleless_fallback_candidate_count",
        "roleless_fallback_dispatch_count",
        "prompt_textarea_count",
        "contenteditable_count",
        "textarea_count",
        "legacy_send_button_count",
        "composer_submit_button_count",
        "websol_submit_attempt_count",
        "websol_user_turn_confirm_count",
        "result_submit_attempt_count",
        "result_composer_found_count",
        "result_composer_prepared_count",
        "result_send_click_count",
        "result_confirm_success_count",
    )
    counts: dict[str, int] = {}
    for field in count_fields:
        count = value.get(field)
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= _ACTION_SCAN_MAX_COUNT:
            return None
        counts[field] = count
    shadow_count_limits = {
        "open_shadow_root_count": _ACTION_SCAN_MAX_SHADOW_ROOTS,
        "shadow_total_element_count": _ACTION_SCAN_MAX_SHADOW_COUNT,
        "shadow_primary_assistant_count": _ACTION_SCAN_MAX_SHADOW_COUNT,
        "shadow_conversation_turn_count": _ACTION_SCAN_MAX_SHADOW_COUNT,
        "shadow_article_count": _ACTION_SCAN_MAX_SHADOW_COUNT,
        "shadow_agent_turn_class_count": _ACTION_SCAN_MAX_SHADOW_COUNT,
        "shadow_conversation_turn_class_count": _ACTION_SCAN_MAX_SHADOW_COUNT,
        "shadow_marker_root_count": _ACTION_SCAN_MAX_SHADOW_ROOTS,
        "shadow_marker_element_count": _ACTION_SCAN_MAX_SHADOW_COUNT,
    }
    for field, limit in shadow_count_limits.items():
        count = value.get(field)
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= limit:
            return None
        counts[field] = count
    body_text_length = value.get("body_text_length")
    if (
        isinstance(body_text_length, bool)
        or not isinstance(body_text_length, int)
        or not 0 <= body_text_length <= _ACTION_SCAN_MAX_BODY_TEXT_LENGTH
    ):
        return None
    body_has_action_marker = value.get("body_has_action_marker")
    if not isinstance(body_has_action_marker, bool):
        return None
    if counts["marker_node_count"] > counts["primary_assistant_count"]:
        return None
    if counts["parsed_action_count"] > counts["marker_node_count"]:
        return None
    if counts["roleless_fallback_dispatch_count"] > counts["roleless_fallback_candidate_count"]:
        return None
    if not (
        counts["result_confirm_success_count"]
        <= counts["result_send_click_count"]
        <= counts["result_composer_prepared_count"]
        <= counts["result_composer_found_count"]
        <= counts["result_submit_attempt_count"]
    ):
        return None
    if counts["shadow_marker_root_count"] > counts["open_shadow_root_count"]:
        return None
    for field in (
        "shadow_primary_assistant_count",
        "shadow_conversation_turn_count",
        "shadow_article_count",
        "shadow_agent_turn_class_count",
        "shadow_conversation_turn_class_count",
        "shadow_marker_element_count",
    ):
        if counts[field] > counts["shadow_total_element_count"]:
            return None

    request_id = value.get("most_recent_request_id")
    if request_id is not None:
        if (
            not isinstance(request_id, str)
            or not request_id.strip()
            or len(request_id) > _ACTION_SCAN_MAX_REQUEST_ID
        ):
            return None
        request_id = request_id.strip()

    fallback_request_id = value.get("last_roleless_fallback_request_id")
    if fallback_request_id is not None:
        if (
            not isinstance(fallback_request_id, str)
            or not fallback_request_id.strip()
            or len(fallback_request_id) > _ACTION_SCAN_MAX_REQUEST_ID
        ):
            return None
        fallback_request_id = fallback_request_id.strip()

    result_request_id = value.get("last_result_submit_request_id")
    if result_request_id is not None:
        if (
            not isinstance(result_request_id, str)
            or not result_request_id.strip()
            or len(result_request_id) > _ACTION_SCAN_MAX_REQUEST_ID
        ):
            return None
        result_request_id = result_request_id.strip()

    websol_submit_request_id = value.get("last_websol_submit_request_id")
    if websol_submit_request_id is not None:
        if (
            not isinstance(websol_submit_request_id, str)
            or not websol_submit_request_id.strip()
            or len(websol_submit_request_id) > _ACTION_SCAN_MAX_REQUEST_ID
        ):
            return None
        websol_submit_request_id = websol_submit_request_id.strip()

    websol_user_turn_request_id = value.get("last_websol_user_turn_request_id")
    if websol_user_turn_request_id is not None:
        if (
            not isinstance(websol_user_turn_request_id, str)
            or not websol_user_turn_request_id.strip()
            or len(websol_user_turn_request_id) > _ACTION_SCAN_MAX_REQUEST_ID
        ):
            return None
        websol_user_turn_request_id = websol_user_turn_request_id.strip()

    composer_kind = value.get("composer_kind")
    if composer_kind not in {"none", "prompt_textarea", "textarea", "contenteditable", "other"}:
        return None
    surface_values = {"none", "document", "open_shadow_root", "same_origin_iframe"}
    composer_surface = value.get("composer_surface")
    submit_candidate_surface = value.get("submit_candidate_surface")
    if composer_surface not in surface_values or submit_candidate_surface not in surface_values:
        return None
    submit_path_values = {
        "none",
        "send_testid",
        "composer_submit_identity",
        "composer_form_submit_control",
        "composer_ancestor_semantic",
        "composer_ancestor_primary",
        "global_semantic",
        "form_request_submit",
        "keyboard_enter",
    }
    submit_candidate_path = value.get("submit_candidate_path")
    last_websol_submit_path = value.get("last_websol_submit_path")
    if submit_candidate_path not in submit_path_values or last_websol_submit_path not in submit_path_values:
        return None
    last_websol_user_turn_evidence = value.get("last_websol_user_turn_evidence")
    if last_websol_user_turn_evidence not in {"none", "role_user", "roleless_surface"}:
        return None

    result_stage = value.get("last_result_submit_stage")
    if result_stage not in {"idle", "no_composer", "prepared", "no_send", "clicked", "confirmed", "timeout"}:
        return None

    active = value.get("action_scan_active")
    if not isinstance(active, bool):
        return None
    return {
        "scanned_at": scanned_at,
        **counts,
        "body_text_length": body_text_length,
        "body_has_action_marker": body_has_action_marker,
        "most_recent_request_id": request_id,
        "last_roleless_fallback_request_id": fallback_request_id,
        "composer_kind": composer_kind,
        "composer_surface": composer_surface,
        "submit_candidate_path": submit_candidate_path,
        "submit_candidate_surface": submit_candidate_surface,
        "last_websol_submit_path": last_websol_submit_path,
        "last_websol_submit_request_id": websol_submit_request_id,
        "last_websol_user_turn_evidence": last_websol_user_turn_evidence,
        "last_websol_user_turn_request_id": websol_user_turn_request_id,
        "last_result_submit_request_id": result_request_id,
        "last_result_submit_stage": result_stage,
        "action_scan_active": active,
    }


class ConversationControlStore:
    def __init__(self, runtime_root: Path | str, *, session_presence_seconds: int = 300) -> None:
        if isinstance(session_presence_seconds, bool) or int(session_presence_seconds) <= 0:
            raise ValueError("session_presence_seconds must be positive")
        self.runtime_root = Path(runtime_root)
        self.sessions_path = self.runtime_root / "conversation-sessions.json"
        self.bindings_path = self.runtime_root / "conversation-bindings.json"
        self.session_presence_seconds = int(session_presence_seconds)
        self._lock = threading.RLock()

    @staticmethod
    def _key(adapter: str, binding_id: str) -> str:
        return adapter + "\n" + binding_id

    def _sessions(self) -> dict[str, Any]:
        value = read_json(self.sessions_path, {})
        return value if isinstance(value, dict) else {}

    def _bindings(self) -> dict[str, Any]:
        value = read_json(self.bindings_path, {})
        return value if isinstance(value, dict) else {}

    def heartbeat(
        self, adapter: Any, binding_id: Any, *, title: Any, url: Any,
        tab_instance_id: Any, capability_id: Any = None,
        capability_source: str = "capability", action_scan: Any = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        adapter = _text(adapter, "adapter")
        binding_id = _text(binding_id, "binding_id")
        title = _text(title, "title")
        tab_instance_id = _text(tab_instance_id, "tab_instance_id")
        url = _text(url, "url")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https" or parsed.netloc.lower() != "chatgpt.com"
            or parsed.path.rstrip("/") != "/c/" + binding_id
        ):
            raise ValueError("url must match an https://chatgpt.com/c/<binding_id> conversation")
        observed = _moment(now).isoformat()
        cap_clean = str(capability_id).strip() if capability_id is not None and str(capability_id).strip() else None
        cap_source = str(capability_source).strip() if capability_source else "capability"
        key = self._key(adapter, binding_id)
        with self._lock:
            sessions = self._sessions()
            prior = sessions.get(key) if isinstance(sessions.get(key), dict) else {}
            prior_cap = prior.get("capability_pairing_id")
            if cap_clean and prior_cap and cap_clean != prior_cap:
                try:
                    from dev_orchestrator.core.websol_health import WebSolHealthStore
                    WebSolHealthStore(self.runtime_root).invalidate_generation(reason="pairing_changed")
                except Exception:
                    pass
            tabs = dict(prior.get("tabs") or {})
            prior_tab = tabs.get(tab_instance_id) if isinstance(tabs.get(tab_instance_id), dict) else {}
            tab_record = {
                "last_seen_at": observed,
                "capability_pairing_id": cap_clean,
                "capability_source": cap_source,
                "verified_at": observed if cap_clean else None,
            }
            clean_action_scan = _sanitize_action_scan(action_scan)
            if clean_action_scan is not None:
                tab_record["action_scan"] = clean_action_scan
            elif isinstance(prior_tab.get("action_scan"), dict):
                tab_record["action_scan"] = prior_tab["action_scan"]
            tabs[tab_instance_id] = tab_record
            record = {
                "adapter": adapter, "binding_id": binding_id, "title": title, "url": url,
                "first_seen_at": prior.get("first_seen_at") or observed,
                "last_seen_at": observed,
                "capability_pairing_id": cap_clean or prior.get("capability_pairing_id"),
                "capability_source": cap_source if cap_clean else (prior.get("capability_source") or "capability"),
                "capability_verified_at": (observed if cap_clean else prior.get("capability_verified_at")),
                "tabs": tabs,
            }
            sessions[key] = record
            write_json(self.sessions_path, sessions, indent=2)
            return dict(record)

    def session_status(self, adapter: str, binding_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        observed = _moment(now)
        with self._lock:
            record = self._sessions().get(self._key(adapter, binding_id))
        if not isinstance(record, dict):
            return {
                "adapter": adapter, "binding_id": binding_id, "state": "undiscovered",
                "last_seen_at": None, "active_tab_count": 0, "stale_tab_count": 0,
                "capability_pairing_id": None, "capability_source": None, "verified": False,
            }
        seen = parse_utc(record.get("last_seen_at"))
        item = dict(record)
        item["state"] = "live" if seen and (observed - seen).total_seconds() < self.session_presence_seconds else "stale"
        tabs = record.get("tabs") if isinstance(record.get("tabs"), dict) else {}
        active_tabs = 0
        stale_tabs = 0
        for tab in tabs.values():
            if not isinstance(tab, dict):
                continue
            tab_seen = parse_utc(tab.get("last_seen_at"))
            if tab_seen and (observed - tab_seen).total_seconds() < self.session_presence_seconds:
                active_tabs += 1
            else:
                stale_tabs += 1
        item["active_tab_count"] = active_tabs
        item["stale_tab_count"] = stale_tabs
        item["verified"] = bool(record.get("capability_pairing_id") and record.get("capability_verified_at"))
        return item

    def list_sessions(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        observed = _moment(now)
        with self._lock:
            records = list(self._sessions().values())
        rows: list[dict[str, Any]] = []
        for raw in records:
            if not isinstance(raw, dict):
                continue
            item = self.session_status(str(raw.get("adapter") or ""), str(raw.get("binding_id") or ""), now=observed)
            rows.append(item)
        return sorted(rows, key=lambda row: (str(row.get("title") or ""), str(row.get("binding_id") or "")))

    def runtime_record_for_project(self, project_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._bindings().get(project_id)
        return dict(value) if isinstance(value, dict) else None

    def binding_for_project(self, project_id: str) -> dict[str, Any] | None:
        value = self.runtime_record_for_project(project_id)
        return None if not value or value.get("state") == "unbound" else value

    def list_bindings(self) -> list[dict[str, Any]]:
        with self._lock:
            values = list(self._bindings().values())
        return [dict(value) for value in values if isinstance(value, dict)]

    @staticmethod
    def _other_owner(bindings: dict[str, Any], project_id: str, adapter: str, binding_id: str) -> str | None:
        for other, value in bindings.items():
            if other != project_id and isinstance(value, dict) and value.get("state") != "unbound" and value.get("adapter") == adapter and value.get("binding_id") == binding_id:
                return str(other)
        return None

    def _live_session(self, adapter: str, binding_id: str) -> dict[str, Any]:
        value = self.session_status(adapter, binding_id)
        if value.get("state") != "live":
            raise ConversationConflictError("target conversation is not currently live")
        return value

    def bind(self, project_id: str, adapter: str, binding_id: str) -> dict[str, Any]:
        project_id, adapter, binding_id = _text(project_id, "project_id"), _text(adapter, "adapter"), _text(binding_id, "binding_id")
        with self._lock:
            bindings = self._bindings()
            current = bindings.get(project_id)
            if isinstance(current, dict) and current.get("state") != "unbound":
                if current.get("adapter") == adapter and current.get("binding_id") == binding_id:
                    return dict(current)
                raise ConversationConflictError("project already has a different runtime binding; use rebind")
            owner = self._other_owner(bindings, project_id, adapter, binding_id)
            if owner:
                raise ConversationConflictError(f"conversation is already bound to project {owner}")
            session = self._live_session(adapter, binding_id)
            now = utc_now().isoformat()
            record = {"project_id": project_id, "state": "bound", "adapter": adapter, "binding_id": binding_id,
                      "title": session.get("title"), "url": session.get("url"), "bound_at": now,
                      "updated_at": now, "provenance": "owner_control"}
            bindings[project_id] = record
            write_json(self.bindings_path, bindings, indent=2)
            try:
                from dev_orchestrator.core.websol_health import WebSolHealthStore
                WebSolHealthStore(self.runtime_root).invalidate_generation(reason=f"bind:{project_id}")
            except Exception:
                pass
            return dict(record)

    def rebind(self, project_id: str, adapter: str, binding_id: str) -> dict[str, Any]:
        project_id, adapter, binding_id = _text(project_id, "project_id"), _text(adapter, "adapter"), _text(binding_id, "binding_id")
        with self._lock:
            bindings = self._bindings()
            owner = self._other_owner(bindings, project_id, adapter, binding_id)
            if owner:
                raise ConversationConflictError(f"conversation is already bound to project {owner}")
            session = self._live_session(adapter, binding_id)
            prior = bindings.get(project_id) if isinstance(bindings.get(project_id), dict) else {}
            now = utc_now().isoformat()
            record = {"project_id": project_id, "state": "bound", "adapter": adapter, "binding_id": binding_id,
                      "title": session.get("title"), "url": session.get("url"), "bound_at": prior.get("bound_at") or now,
                      "updated_at": now, "provenance": "owner_control"}
            bindings[project_id] = record
            write_json(self.bindings_path, bindings, indent=2)
            try:
                from dev_orchestrator.core.websol_health import WebSolHealthStore
                WebSolHealthStore(self.runtime_root).invalidate_generation(reason=f"rebind:{project_id}")
            except Exception:
                pass
            return dict(record)

    def unbind(self, project_id: str) -> dict[str, Any]:
        project_id = _text(project_id, "project_id")
        with self._lock:
            bindings = self._bindings()
            current = bindings.get(project_id)
            if isinstance(current, dict) and current.get("state") == "unbound":
                return dict(current)
            record = {"project_id": project_id, "state": "unbound", "updated_at": utc_now().isoformat(), "provenance": "owner_control"}
            bindings[project_id] = record
            write_json(self.bindings_path, bindings, indent=2)
            try:
                from dev_orchestrator.core.websol_health import WebSolHealthStore
                WebSolHealthStore(self.runtime_root).invalidate_generation(reason=f"unbind:{project_id}")
            except Exception:
                pass
            return dict(record)
