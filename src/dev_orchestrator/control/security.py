"""Loopback control authentication, browser CSRF sessions and adapter pairing."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dev_orchestrator.accounting.events import InterProcessFileLock
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json, write_text


def is_loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value.lower() == "localhost"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _host_identity(value: str) -> str:
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError:
        return value.lower()


class ControlSecurity:
    SESSION_TTL_SECONDS = 15 * 60
    PAIRING_TTL_SECONDS = 5 * 60

    def __init__(self, runtime_root: Path | str) -> None:
        self.root = Path(runtime_root) / "control"
        self.secret_path = self.root / "api-token"
        self.pairings_path = self.root / "adapter-capabilities.json"
        self.lock_path = self.root / "security.lock"
        self._lock = threading.RLock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._master = self._load_or_create_master()

    def _load_or_create_master(self) -> str:
        with InterProcessFileLock(self.lock_path):
            try:
                value = self.secret_path.read_text(encoding="utf-8").strip()
            except OSError:
                value = ""
            if len(value) < 32:
                value = secrets.token_urlsafe(32)
                write_text(self.secret_path, value + "\n")
                try:
                    self.secret_path.chmod(0o600)
                except OSError:
                    pass
            return value

    @staticmethod
    def valid_origin(origin: str | None, host: str | None, port: int) -> bool:
        if not origin or not host:
            return False
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "http" or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or parsed.username or parsed.password
        ):
            return False
        try:
            advertised = urlsplit("http://" + host)
            advertised_port = advertised.port
        except ValueError:
            return False
        return (
            advertised.username is None
            and advertised.password is None
            and is_loopback(advertised.hostname or "")
            and advertised_port == port
            and is_loopback(parsed.hostname or "")
            and parsed.port == port
            and _host_identity(advertised.hostname or "") == _host_identity(parsed.hostname or "")
        )

    def bearer_authorized(self, header: str | None) -> bool:
        if not isinstance(header, str) or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:].strip(), self._master)

    def create_browser_session(self) -> tuple[str, str]:
        session_id, csrf = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        with self._lock:
            self._sessions[session_id] = {"csrf": csrf, "expires": time.time() + self.SESSION_TTL_SECONDS}
        return session_id, csrf

    def browser_authorized(self, cookie: str | None, csrf: str | None) -> bool:
        session_id = None
        for part in (cookie or "").split(";"):
            key, sep, value = part.strip().partition("=")
            if sep and key == "devorch_control":
                session_id = value
                break
        if not session_id or not csrf:
            return False
        with self._lock:
            row = self._sessions.get(session_id)
            if not row or float(row.get("expires", 0)) <= time.time():
                self._sessions.pop(session_id, None)
                return False
            return hmac.compare_digest(str(row.get("csrf") or ""), csrf)

    @staticmethod
    def _empty_pairings() -> dict[str, Any]:
        return {"version": 1, "pairings": {}, "capabilities": {}}

    def _pairings(self) -> dict[str, Any]:
        value = read_json(self.pairings_path, None)
        if not isinstance(value, dict):
            return self._empty_pairings()
        return {
            "version": 1,
            "pairings": value.get("pairings") if isinstance(value.get("pairings"), dict) else {},
            "capabilities": value.get("capabilities") if isinstance(value.get("capabilities"), dict) else {},
        }

    def create_pairing(self) -> dict[str, Any]:
        pairing_id, code = secrets.token_urlsafe(12), secrets.token_urlsafe(18)
        with InterProcessFileLock(self.lock_path):
            data = self._pairings()
            data["pairings"][pairing_id] = {
                "pairing_id": pairing_id, "code_hash": _digest(code),
                "expires_at_epoch": time.time() + self.PAIRING_TTL_SECONDS,
                "created_at": utc_now_iso(), "used": False,
            }
            write_json(self.pairings_path, data, indent=2)
        return {"pairing_id": pairing_id, "code": code, "expires_in_seconds": self.PAIRING_TTL_SECONDS}

    def redeem_pairing(self, pairing_id: Any, code: Any) -> dict[str, Any]:
        if not isinstance(pairing_id, str) or not isinstance(code, str):
            raise ValueError("pairing_id and code are required")
        capability = secrets.token_urlsafe(32)
        with InterProcessFileLock(self.lock_path):
            data = self._pairings()
            row = data["pairings"].get(pairing_id)
            if not isinstance(row, dict) or row.get("used") or float(row.get("expires_at_epoch", 0)) <= time.time():
                raise ValueError("pairing is missing, expired, or already used")
            if not hmac.compare_digest(str(row.get("code_hash") or ""), _digest(code)):
                raise ValueError("invalid pairing code")
            row["used"] = True
            row["used_at"] = utc_now_iso()
            data["capabilities"][pairing_id] = {
                "pairing_id": pairing_id, "token_hash": _digest(capability),
                "scope": "session_heartbeat", "created_at": utc_now_iso(), "revoked": False,
            }
            write_json(self.pairings_path, data, indent=2)
        return {"pairing_id": pairing_id, "capability": capability, "scope": "session_heartbeat"}

    def adapter_authorized(self, header: str | None) -> bool:
        if not isinstance(header, str) or not header.startswith("Bearer "):
            return False
        digest = _digest(header[7:].strip())
        data = self._pairings()
        return any(
            isinstance(row, dict) and not row.get("revoked")
            and row.get("scope") == "session_heartbeat"
            and hmac.compare_digest(str(row.get("token_hash") or ""), digest)
            for row in data["capabilities"].values()
        )

    def revoke_pairing(self, pairing_id: str) -> dict[str, Any]:
        with InterProcessFileLock(self.lock_path):
            data = self._pairings()
            row = data["capabilities"].get(pairing_id)
            if not isinstance(row, dict):
                raise ValueError("pairing capability not found")
            row["revoked"] = True
            row["revoked_at"] = utc_now_iso()
            write_json(self.pairings_path, data, indent=2)
            return {"pairing_id": pairing_id, "revoked": True}
