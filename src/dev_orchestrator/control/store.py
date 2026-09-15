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
        tab_instance_id: Any, now: datetime | None = None,
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
        key = self._key(adapter, binding_id)
        with self._lock:
            sessions = self._sessions()
            prior = sessions.get(key) if isinstance(sessions.get(key), dict) else {}
            tabs = dict(prior.get("tabs") or {})
            tabs[tab_instance_id] = {"last_seen_at": observed}
            record = {
                "adapter": adapter, "binding_id": binding_id, "title": title, "url": url,
                "first_seen_at": prior.get("first_seen_at") or observed,
                "last_seen_at": observed, "tabs": tabs,
            }
            sessions[key] = record
            write_json(self.sessions_path, sessions, indent=2)
            return dict(record)

    def session_status(self, adapter: str, binding_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        observed = _moment(now)
        with self._lock:
            record = self._sessions().get(self._key(adapter, binding_id))
        if not isinstance(record, dict):
            return {"adapter": adapter, "binding_id": binding_id, "state": "undiscovered", "last_seen_at": None}
        seen = parse_utc(record.get("last_seen_at"))
        item = dict(record)
        item["state"] = "live" if seen and (observed - seen).total_seconds() < self.session_presence_seconds else "stale"
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
            tabs = raw.get("tabs") if isinstance(raw.get("tabs"), dict) else {}
            item["active_tab_count"] = sum(
                1 for tab in tabs.values()
                if isinstance(tab, dict) and parse_utc(tab.get("last_seen_at"))
                and (observed - parse_utc(tab.get("last_seen_at"))).total_seconds() < self.session_presence_seconds
            )
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
            return dict(record)
