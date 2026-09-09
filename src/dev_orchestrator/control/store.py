"""Persistent live-session and runtime conversation-binding registry."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from dev_orchestrator.storage.json_store import parse_utc, read_json, utc_now, write_json


class ControlConflictError(RuntimeError):
    """Raised when a binding mutation would violate control-plane identity."""


def _as_utc(value: Optional[datetime]) -> datetime:
    moment = value or utc_now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _require_non_blank(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string")
    return value.strip()


def _validate_conversation_url(url: str, binding_id: str) -> str:
    text = _require_non_blank(url, "url")
    parsed = urlsplit(text)
    if parsed.scheme != "https" or parsed.netloc.lower() != "chatgpt.com":
        raise ValueError("url must be an https://chatgpt.com conversation URL")
    expected = "/c/" + binding_id
    if not parsed.path.startswith(expected):
        raise ValueError("url conversation id does not match binding_id")
    return text


class ConversationControlStore:
    """One-process persistent registry for sessions and runtime bindings."""

    def __init__(
        self,
        runtime_root: Path | str,
        *,
        session_presence_seconds: int = 300,
    ) -> None:
        if int(session_presence_seconds) <= 0:
            raise ValueError("session_presence_seconds must be positive")
        self.runtime_root = Path(runtime_root)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.session_presence_seconds = int(session_presence_seconds)
        self.sessions_path = self.runtime_root / "conversation-sessions.json"
        self.bindings_path = self.runtime_root / "conversation-bindings.json"
        self._lock = threading.RLock()

    @staticmethod
    def _session_key(adapter: str, binding_id: str) -> str:
        return adapter + "\n" + binding_id

    def _load_sessions(self) -> dict[str, dict[str, Any]]:
        data = read_json(self.sessions_path, {})
        return data if isinstance(data, dict) else {}

    def _load_bindings(self) -> dict[str, dict[str, Any]]:
        data = read_json(self.bindings_path, {})
        return data if isinstance(data, dict) else {}

    def heartbeat(
        self,
        adapter: str,
        binding_id: str,
        *,
        title: str,
        url: str,
        tab_instance_id: str,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        adapter = _require_non_blank(adapter, "adapter")
        binding_id = _require_non_blank(binding_id, "binding_id")
        title = _require_non_blank(title, "title")
        tab_instance_id = _require_non_blank(tab_instance_id, "tab_instance_id")
        url = _validate_conversation_url(url, binding_id)
        moment = _as_utc(now)
        key = self._session_key(adapter, binding_id)
        with self._lock:
            sessions = self._load_sessions()
            previous = sessions.get(key) if isinstance(sessions.get(key), dict) else {}
            tabs = dict(previous.get("tabs") or {}) if isinstance(previous, dict) else {}
            tabs[tab_instance_id] = {"last_seen_at": _iso(moment)}
            record = {
                "adapter": adapter,
                "binding_id": binding_id,
                "title": title,
                "url": url,
                "first_seen_at": previous.get("first_seen_at") or _iso(moment),
                "last_seen_at": _iso(moment),
                "tabs": tabs,
            }
            sessions[key] = record
            write_json(self.sessions_path, sessions, indent=2)
            return dict(record)

    def session_status(
        self, adapter: str, binding_id: str, *, now: Optional[datetime] = None
    ) -> dict[str, Any]:
        adapter = _require_non_blank(adapter, "adapter")
        binding_id = _require_non_blank(binding_id, "binding_id")
        moment = _as_utc(now)
        with self._lock:
            sessions = self._load_sessions()
            record = sessions.get(self._session_key(adapter, binding_id))
        if not isinstance(record, dict):
            return {"state": "undiscovered", "adapter": adapter, "binding_id": binding_id, "last_seen_at": None}
        last_seen = parse_utc(record.get("last_seen_at"))
        live = bool(last_seen and (moment - last_seen).total_seconds() < self.session_presence_seconds)
        item = dict(record)
        item["state"] = "live" if live else "stale"
        return item

    def list_sessions(self, *, now: Optional[datetime] = None) -> list[dict[str, Any]]:
        moment = _as_utc(now)
        with self._lock:
            sessions = self._load_sessions()
        result: list[dict[str, Any]] = []
        for record in sessions.values():
            if not isinstance(record, dict):
                continue
            last_seen = parse_utc(record.get("last_seen_at"))
            live = bool(last_seen and (moment - last_seen).total_seconds() < self.session_presence_seconds)
            active_tabs = 0
            tabs = record.get("tabs")
            if isinstance(tabs, dict):
                for tab in tabs.values():
                    seen = parse_utc(tab.get("last_seen_at")) if isinstance(tab, dict) else None
                    if seen and (moment - seen).total_seconds() < self.session_presence_seconds:
                        active_tabs += 1
            item = dict(record)
            item["state"] = "live" if live else "stale"
            item["active_tab_count"] = active_tabs
            result.append(item)
        result.sort(key=lambda item: (str(item.get("title") or ""), str(item.get("binding_id") or "")))
        return result

    def runtime_record_for_project(self, project_id: str) -> Optional[dict[str, Any]]:
        project_id = _require_non_blank(project_id, "project_id")
        with self._lock:
            bindings = self._load_bindings()
            record = bindings.get(project_id)
        return dict(record) if isinstance(record, dict) else None

    def binding_for_project(self, project_id: str) -> Optional[dict[str, Any]]:
        record = self.runtime_record_for_project(project_id)
        if not isinstance(record, dict) or record.get("state") == "unbound":
            return None
        return record

    def project_for_binding(self, adapter: str, binding_id: str) -> Optional[str]:
        adapter = _require_non_blank(adapter, "adapter")
        binding_id = _require_non_blank(binding_id, "binding_id")
        with self._lock:
            bindings = self._load_bindings()
        for project_id, record in bindings.items():
            if not isinstance(record, dict) or record.get("state") == "unbound":
                continue
            if record.get("adapter") == adapter and record.get("binding_id") == binding_id:
                return str(project_id)
        return None

    def list_bindings(self) -> list[dict[str, Any]]:
        with self._lock:
            bindings = self._load_bindings()
        return [dict(record) for record in bindings.values() if isinstance(record, dict)]

    def _session_snapshot(self, adapter: str, binding_id: str) -> dict[str, Any]:
        sessions = self._load_sessions()
        record = sessions.get(self._session_key(adapter, binding_id))
        if not isinstance(record, dict):
            raise ControlConflictError("conversation has not been discovered by the control plane")
        return record

    @staticmethod
    def _binding_owned_by_other(
        bindings: dict[str, dict[str, Any]], project_id: str, adapter: str, binding_id: str
    ) -> Optional[str]:
        for other_id, record in bindings.items():
            if other_id == project_id or not isinstance(record, dict) or record.get("state") == "unbound":
                continue
            if record.get("adapter") == adapter and record.get("binding_id") == binding_id:
                return str(other_id)
        return None

    def bind(
        self,
        project_id: str,
        adapter: str,
        binding_id: str,
        *,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        project_id = _require_non_blank(project_id, "project_id")
        adapter = _require_non_blank(adapter, "adapter")
        binding_id = _require_non_blank(binding_id, "binding_id")
        moment = _as_utc(now)
        with self._lock:
            bindings = self._load_bindings()
            current = bindings.get(project_id)
            if isinstance(current, dict) and current.get("state") != "unbound":
                if current.get("adapter") == adapter and current.get("binding_id") == binding_id:
                    return dict(current)
                raise ControlConflictError("project already has a different runtime binding; use rebind")
            owner = self._binding_owned_by_other(bindings, project_id, adapter, binding_id)
            if owner is not None:
                raise ControlConflictError(f"conversation is already bound to project {owner}")
            session = self._session_snapshot(adapter, binding_id)
            record = {
                "project_id": project_id,
                "state": "bound",
                "adapter": adapter,
                "binding_id": binding_id,
                "title": session.get("title"),
                "url": session.get("url"),
                "bound_at": _iso(moment),
                "updated_at": _iso(moment),
                "provenance": "owner_control",
            }
            bindings[project_id] = record
            write_json(self.bindings_path, bindings, indent=2)
            return dict(record)

    def unbind(
        self, project_id: str, *, now: Optional[datetime] = None
    ) -> dict[str, Any]:
        project_id = _require_non_blank(project_id, "project_id")
        moment = _as_utc(now)
        with self._lock:
            bindings = self._load_bindings()
            current = bindings.get(project_id)
            if isinstance(current, dict) and current.get("state") == "unbound":
                return dict(current)
            record = {
                "project_id": project_id,
                "state": "unbound",
                "updated_at": _iso(moment),
                "provenance": "owner_control",
            }
            bindings[project_id] = record
            write_json(self.bindings_path, bindings, indent=2)
            return dict(record)

    def rebind(
        self,
        project_id: str,
        adapter: str,
        binding_id: str,
        *,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        project_id = _require_non_blank(project_id, "project_id")
        adapter = _require_non_blank(adapter, "adapter")
        binding_id = _require_non_blank(binding_id, "binding_id")
        moment = _as_utc(now)
        with self._lock:
            bindings = self._load_bindings()
            owner = self._binding_owned_by_other(bindings, project_id, adapter, binding_id)
            if owner is not None:
                raise ControlConflictError(f"conversation is already bound to project {owner}")
            session = self._session_snapshot(adapter, binding_id)
            previous = bindings.get(project_id)
            bound_at = previous.get("bound_at") if isinstance(previous, dict) else None
            record = {
                "project_id": project_id,
                "state": "bound",
                "adapter": adapter,
                "binding_id": binding_id,
                "title": session.get("title"),
                "url": session.get("url"),
                "bound_at": bound_at or _iso(moment),
                "updated_at": _iso(moment),
                "provenance": "owner_control",
            }
            bindings[project_id] = record
            write_json(self.bindings_path, bindings, indent=2)
            return dict(record)
