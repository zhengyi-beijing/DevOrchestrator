"""Loopback control authentication, browser CSRF sessions and adapter pairing."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dev_orchestrator.accounting.events import InterProcessFileLock
from dev_orchestrator.mobile.authorizer import MobileDeviceAuthorizer, MobileDevicePrincipal
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json, write_text
from enum import Enum


class CapabilityVerdict(str, Enum):
    VALID = "valid"
    REVOKED = "revoked"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class CapabilityStoreUnavailableError(RuntimeError, ValueError):
    """Raised when canonical capability state cannot be accessed or is malformed."""
    pass


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
        self.runtime_root = Path(runtime_root)
        self.root = self.runtime_root / "control"
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

    def token(self) -> str:
        """Return the master control authorization token."""
        return self._master

    @staticmethod
    def valid_origin(origin: str | None, host: str | None, port: int) -> bool:
        if not origin or not host:
            return False
        try:
            parsed = urlsplit(origin)
            parsed_port = parsed.port
        except ValueError:
            return False
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
            and parsed_port == port
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
        return {
            "version": 1,
            "pairings": {},
            "capabilities": {},
            "mobile_pairings": {},
            "mobile_devices": {},
            "mobile_revocation_generation": 0,
            "transport_capabilities": {},
        }

    def _load_canonical_pairings(self, *, for_mutation: bool = False) -> dict[str, Any] | None:
        if not self.pairings_path.exists():
            return self._empty_pairings() if for_mutation else None
        try:
            raw = self.pairings_path.read_bytes()
        except OSError as exc:
            raise CapabilityStoreUnavailableError(f"cannot read canonical capability state: {exc}") from exc
        if not raw or not raw.strip():
            raise CapabilityStoreUnavailableError("canonical capability state is empty or truncated")
        try:
            val = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CapabilityStoreUnavailableError(f"canonical capability state is non-JSON or malformed: {exc}") from exc
        if not isinstance(val, dict) or val.get("version") != 1:
            raise CapabilityStoreUnavailableError("canonical capability state has unsupported version or schema")
        return {
            "version": 1,
            "pairings": val.get("pairings") if isinstance(val.get("pairings"), dict) else {},
            "capabilities": val.get("capabilities") if isinstance(val.get("capabilities"), dict) else {},
            "mobile_pairings": val.get("mobile_pairings") if isinstance(val.get("mobile_pairings"), dict) else {},
            "mobile_devices": val.get("mobile_devices") if isinstance(val.get("mobile_devices"), dict) else {},
            "mobile_revocation_generation": int(val.get("mobile_revocation_generation", 0)),
            "transport_capabilities": val.get("transport_capabilities") if isinstance(val.get("transport_capabilities"), dict) else {},
        }

    def _pairings(self) -> dict[str, Any]:
        val = self._load_canonical_pairings(for_mutation=True)
        return val if isinstance(val, dict) else self._empty_pairings()

    def capability_state(self, pairing_id: str) -> CapabilityVerdict:
        """Return the structured capability verdict for a pairing_id."""
        if not pairing_id or not isinstance(pairing_id, str):
            return CapabilityVerdict.UNKNOWN
        clean_id = pairing_id.strip()
        try:
            data = self._load_canonical_pairings(for_mutation=False)
            if data is None:
                return CapabilityVerdict.UNKNOWN
        except CapabilityStoreUnavailableError:
            return CapabilityVerdict.UNAVAILABLE
        except Exception:
            return CapabilityVerdict.UNAVAILABLE

        capabilities = data.get("capabilities") or {}
        pairings = data.get("pairings") or {}
        row = capabilities.get(clean_id) or pairings.get(clean_id)
        if not isinstance(row, dict):
            for item in capabilities.values():
                if isinstance(item, dict) and (item.get("pairing_id") == clean_id or item.get("capability_id") == clean_id):
                    row = item
                    break
        if not isinstance(row, dict):
            return CapabilityVerdict.UNKNOWN
        if row.get("revoked"):
            return CapabilityVerdict.REVOKED
        return CapabilityVerdict.VALID

    def capability_status(self, header_or_token: str | None) -> tuple[CapabilityVerdict, str, dict[str, Any] | None]:
        """Validate an adapter capability token returning verdict, reason and capability row."""
        if not header_or_token or not isinstance(header_or_token, str):
            return CapabilityVerdict.UNKNOWN, "missing capability token", None
        token = header_or_token
        if token.startswith("Bearer "):
            token = token[7:].strip()
        if not token:
            return CapabilityVerdict.UNKNOWN, "empty capability token", None

        try:
            data = self._load_canonical_pairings(for_mutation=False)
            if data is None:
                return CapabilityVerdict.UNKNOWN, "canonical capability state missing", None
        except CapabilityStoreUnavailableError as exc:
            return CapabilityVerdict.UNAVAILABLE, f"capability_store_unavailable: {exc}", None
        except Exception as exc:
            return CapabilityVerdict.UNAVAILABLE, f"capability_store_unavailable: {exc}", None

        digest = _digest(token)
        capabilities = data.get("capabilities")
        if not isinstance(capabilities, dict):
            return CapabilityVerdict.UNKNOWN, "no capabilities registered", None

        now = time.time()
        for row in capabilities.values():
            if not isinstance(row, dict):
                continue
            if row.get("scope") != "session_heartbeat":
                continue

            matches_primary = hmac.compare_digest(str(row.get("token_hash") or ""), digest)
            matches_previous = False
            prev_hash = row.get("previous_token_hash")
            if prev_hash and hmac.compare_digest(str(prev_hash), digest):
                prev_expires = float(row.get("previous_token_expires_at") or 0)
                if now < prev_expires:
                    matches_previous = True

            if not matches_primary and not matches_previous:
                continue

            if row.get("revoked"):
                return CapabilityVerdict.REVOKED, "capability revoked", row

            return CapabilityVerdict.VALID, "valid", row

        return CapabilityVerdict.UNKNOWN, "unknown capability token", None

    def capability_identity(self, header_or_token: str | None) -> str | None:
        """Return the stable pairing_id if header is valid, else None."""
        verdict, _, row = self.capability_status(header_or_token)
        if verdict == CapabilityVerdict.VALID and isinstance(row, dict):
            return str(row.get("pairing_id") or row.get("capability_id") or "")
        return None

    def renew_session_capability(
        self, header_or_token: str | None, *, grace_period_seconds: int = 300
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """Rotate a valid session-heartbeat capability token under the canonical lock."""
        if not header_or_token or not isinstance(header_or_token, str):
            return False, "missing capability token", None
        token = header_or_token
        if token.startswith("Bearer "):
            token = token[7:].strip()
        if not token:
            return False, "empty capability token", None

        digest = _digest(token)
        now = time.time()
        with InterProcessFileLock(self.lock_path):
            try:
                data = self._load_canonical_pairings(for_mutation=True)
                if not isinstance(data, dict):
                    return False, "canonical capability state unavailable", None
            except CapabilityStoreUnavailableError as exc:
                return False, f"capability_store_unavailable: {exc}", None
            except Exception as exc:
                return False, f"capability_store_unavailable: {exc}", None

            capabilities = data.setdefault("capabilities", {})
            matched_key = None
            matched_row = None
            for key, row in capabilities.items():
                if not isinstance(row, dict):
                    continue
                if row.get("scope") != "session_heartbeat":
                    continue
                if hmac.compare_digest(str(row.get("token_hash") or ""), digest):
                    matched_key = key
                    matched_row = row
                    break

            if matched_row is None:
                return False, "cannot renew: unknown or previous token", None
            if matched_row.get("revoked"):
                return False, "cannot renew: capability revoked", None

            new_token = secrets.token_urlsafe(32)
            matched_row["previous_token_hash"] = matched_row.get("token_hash")
            matched_row["previous_token_expires_at"] = now + max(10, int(grace_period_seconds))
            matched_row["token_hash"] = _digest(new_token)
            matched_row["renewed_at"] = utc_now_iso()
            write_json(self.pairings_path, data, indent=2)

            return True, "renewed", {
                "pairing_id": matched_row.get("pairing_id") or matched_key,
                "token": new_token,
                "grace_period_seconds": grace_period_seconds,
            }

    def adapter_authorized(self, header: str | None) -> bool:
        verdict, _, _ = self.capability_status(header)
        return verdict == CapabilityVerdict.VALID

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
        try:
            from dev_orchestrator.core.websol_health import WebSolHealthStore
            WebSolHealthStore(self.runtime_root).invalidate_generation(reason="pairing_redeemed")
        except Exception:
            pass
        return {"pairing_id": pairing_id, "capability": capability, "scope": "session_heartbeat"}

    def revoke_pairing(self, pairing_id: str) -> dict[str, Any]:
        res = self.revoke_capability(pairing_id)
        return {
            "pairing_id": pairing_id,
            "capability_id": res.get("capability_id", pairing_id),
            "revoked": bool(res.get("revoked", True)),
        }

    def create_web_bridge_capability(
        self,
        project_id: str,
        binding_id: str,
        adapter: str = "chatgpt",
        *,
        expires_in_seconds: int = 3600,
    ) -> dict[str, Any]:
        """Create an owner-authorized, revocable WebBridge control capability bound to exact project and binding."""
        if not project_id or not isinstance(project_id, str):
            raise ValueError("project_id must be a nonblank string")
        if not binding_id or not isinstance(binding_id, str):
            raise ValueError("binding_id must be a nonblank string")
        if not adapter or not isinstance(adapter, str):
            raise ValueError("adapter must be a nonblank string")

        cap_id = secrets.token_urlsafe(12)
        token = secrets.token_urlsafe(32)
        ttl = max(60, min(86400 * 30, int(expires_in_seconds)))

        with InterProcessFileLock(self.lock_path):
            data = self._pairings()
            data["capabilities"][cap_id] = {
                "capability_id": cap_id,
                "token_hash": _digest(token),
                "scope": "web_bridge_control",
                "project_id": project_id.strip(),
                "binding_id": binding_id.strip(),
                "adapter": adapter.strip(),
                "created_at": utc_now_iso(),
                "expires_at_epoch": time.time() + ttl,
                "revoked": False,
            }
            write_json(self.pairings_path, data, indent=2)

        return {
            "capability_id": cap_id,
            "token": token,
            "scope": "web_bridge_control",
            "project_id": project_id.strip(),
            "binding_id": binding_id.strip(),
            "adapter": adapter.strip(),
            "expires_in_seconds": ttl,
        }

    def validate_web_bridge_capability(
        self,
        header_or_token: str | None,
        project_id: str,
        binding_id: str,
        adapter: str | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """Validate a WebBridge control token against project and live binding."""
        if not header_or_token or not isinstance(header_or_token, str):
            return False, "missing capability token", None
        token = header_or_token
        if token.startswith("Bearer "):
            token = token[7:].strip()
        if not token:
            return False, "empty capability token", None

        digest = _digest(token)
        data = self._pairings()
        now = time.time()

        for row in data["capabilities"].values():
            if not isinstance(row, dict):
                continue
            if row.get("scope") != "web_bridge_control":
                continue
            if row.get("revoked"):
                continue
            if not hmac.compare_digest(str(row.get("token_hash") or ""), digest):
                continue

            # Check expiration
            expires_at = float(row.get("expires_at_epoch") or 0)
            if expires_at <= now:
                return False, "capability token expired", None

            # Check project match
            if row.get("project_id") != project_id:
                return False, f"capability project mismatch: bound to {row.get('project_id')!r}, requested {project_id!r}", None

            # Check binding match
            if row.get("binding_id") != binding_id:
                return False, f"capability binding mismatch: bound to {row.get('binding_id')!r}, requested {binding_id!r}", None

            # Check adapter match if provided
            if adapter and row.get("adapter") and row.get("adapter") != adapter:
                return False, f"capability adapter mismatch: bound to {row.get('adapter')!r}, requested {adapter!r}", None

            return True, "", row

        return False, "invalid or revoked capability token", None

    def revoke_capability(self, capability_id: str) -> dict[str, Any]:
        with InterProcessFileLock(self.lock_path):
            data = self._pairings()
            pairing = data["pairings"].get(capability_id)
            capability = data["capabilities"].get(capability_id)
            if not isinstance(pairing, dict) and not isinstance(capability, dict):
                # Try finding by capability_id in capabilities values
                matched_key = next((k for k, v in data["capabilities"].items() if isinstance(v, dict) and v.get("capability_id") == capability_id), None)
                if matched_key:
                    capability = data["capabilities"][matched_key]
                else:
                    raise ValueError(f"capability or pairing not found: {capability_id!r}")
            now = utc_now_iso()
            if isinstance(pairing, dict):
                pairing.update({"used": True, "revoked": True, "revoked_at": now})
            if isinstance(capability, dict):
                capability.update({"revoked": True, "revoked_at": now})
            write_json(self.pairings_path, data, indent=2)
        try:
            from dev_orchestrator.core.websol_health import WebSolHealthStore
            WebSolHealthStore(self.runtime_root).invalidate_generation(reason="capability_revoked")
        except Exception:
            pass
        return {"capability_id": capability_id, "pairing_id": capability_id, "revoked": True}

    def create_mobile_pairing(self, *, expires_in_seconds: int = 300) -> dict[str, Any]:
        """Create a single-use, TTL-bounded mobile pairing code."""
        pairing_id, code = secrets.token_urlsafe(12), secrets.token_urlsafe(18)
        ttl = max(1, int(expires_in_seconds))
        with InterProcessFileLock(self.lock_path):
            data = self._load_canonical_pairings(for_mutation=True)
            if not isinstance(data, dict):
                data = self._empty_pairings()
            pairings = data.setdefault("mobile_pairings", {})
            pairings[pairing_id] = {
                "pairing_id": pairing_id,
                "code_hash": _digest(code),
                "created_at": utc_now_iso(),
                "expires_at_epoch": time.time() + ttl,
                "used": False,
                "attempts": 0,
                "max_attempts": 3,
            }
            write_json(self.pairings_path, data, indent=2)
        return {"pairing_id": pairing_id, "code": code, "expires_in_seconds": ttl}

    def redeem_mobile_pairing(
        self,
        pairing_id: Any,
        code: Any,
        device_label: str = "android_device",
        *,
        expires_in_seconds: int = 86400 * 30,
    ) -> dict[str, Any]:
        """Redeem a mobile pairing code for an authoritative device bearer token."""
        if not isinstance(pairing_id, str) or not isinstance(code, str):
            raise ValueError("pairing_id and code are required")
        pid_clean = pairing_id.strip()
        code_clean = code.strip()
        if not pid_clean or not code_clean:
            raise ValueError("pairing_id and code must be nonblank")

        with InterProcessFileLock(self.lock_path):
            data = self._load_canonical_pairings(for_mutation=True)
            if not isinstance(data, dict):
                raise ValueError("cannot access canonical capability state")
            pairings = data.setdefault("mobile_pairings", {})
            row = pairings.get(pid_clean)
            now = time.time()
            if (
                not isinstance(row, dict)
                or row.get("used")
                or float(row.get("expires_at_epoch", 0)) <= now
                or int(row.get("attempts", 0)) >= int(row.get("max_attempts", 3))
            ):
                if isinstance(row, dict):
                    row["attempts"] = int(row.get("attempts", 0)) + 1
                    write_json(self.pairings_path, data, indent=2)
                raise ValueError("pairing is missing, expired, or already used")

            if not hmac.compare_digest(str(row.get("code_hash") or ""), _digest(code_clean)):
                row["attempts"] = int(row.get("attempts", 0)) + 1
                write_json(self.pairings_path, data, indent=2)
                raise ValueError("pairing is missing, expired, or already used")

            row["used"] = True
            row["used_at"] = utc_now_iso()

            device_id = f"dev-{secrets.token_hex(8)}"
            token = secrets.token_urlsafe(32)
            ttl = max(60, int(expires_in_seconds))
            devices = data.setdefault("mobile_devices", {})
            devices[device_id] = {
                "device_id": device_id,
                "token_hash": _digest(token),
                "scope": "mobile_device",
                "device_label": str(device_label or "android_device").strip()[:64],
                "created_at": utc_now_iso(),
                "expires_at_epoch": now + ttl,
                "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat(),
                "revoked": False,
                "revoked_at": None,
            }
            write_json(self.pairings_path, data, indent=2)

        return {
            "device_id": device_id,
            "token": token,
            "scope": "mobile_device",
            "expires_in_seconds": ttl,
        }

    def revoke_mobile_device(self, device_id: str) -> dict[str, Any]:
        """Revoke an authorized mobile device by device_id."""
        if not device_id or not isinstance(device_id, str):
            raise ValueError("device_id must be a nonblank string")
        clean_id = device_id.strip()
        with InterProcessFileLock(self.lock_path):
            data = self._load_canonical_pairings(for_mutation=True)
            if not isinstance(data, dict):
                raise ValueError("cannot access canonical capability state")
            devices = data.setdefault("mobile_devices", {})
            row = devices.get(clean_id)
            if not isinstance(row, dict):
                raise ValueError(f"mobile device not found: {clean_id!r}")
            now = utc_now_iso()
            row["revoked"] = True
            row["revoked_at"] = now
            data["mobile_revocation_generation"] = int(data.get("mobile_revocation_generation", 0)) + 1
            write_json(self.pairings_path, data, indent=2)
        return {"device_id": clean_id, "revoked": True}

    def revoke_all_mobile_devices(self) -> dict[str, Any]:
        """Revoke all registered mobile devices."""
        with InterProcessFileLock(self.lock_path):
            data = self._load_canonical_pairings(for_mutation=True)
            if not isinstance(data, dict):
                raise ValueError("cannot access canonical capability state")
            devices = data.setdefault("mobile_devices", {})
            now = utc_now_iso()
            count = 0
            for row in devices.values():
                if isinstance(row, dict) and not row.get("revoked"):
                    row["revoked"] = True
                    row["revoked_at"] = now
                    count += 1
            data["mobile_revocation_generation"] = int(data.get("mobile_revocation_generation", 0)) + 1
            write_json(self.pairings_path, data, indent=2)
        return {"revoked_count": count, "revoked": True}

    def list_mobile_devices(self) -> list[dict[str, Any]]:
        """List non-secret metadata for all registered mobile devices."""
        try:
            data = self._load_canonical_pairings(for_mutation=False)
            if not isinstance(data, dict):
                return []
            devices = data.get("mobile_devices", {})
            results = []
            for row in devices.values():
                if isinstance(row, dict):
                    results.append({
                        "device_id": row.get("device_id"),
                        "device_label": row.get("device_label"),
                        "scope": row.get("scope"),
                        "created_at": row.get("created_at"),
                        "expires_at": row.get("expires_at"),
                        "revoked": bool(row.get("revoked")),
                        "revoked_at": row.get("revoked_at"),
                    })
            return sorted(results, key=lambda x: str(x.get("created_at") or ""))
        except Exception:
            return []

    def validate_mobile_bearer(
        self, header_or_token: str | None
    ) -> tuple[bool, str, MobileDevicePrincipal | None]:
        """Validate an incoming mobile device bearer token."""
        if not header_or_token or not isinstance(header_or_token, str):
            return False, "missing bearer token", None
        token = header_or_token
        if token.startswith("Bearer "):
            token = token[7:].strip()
        if not token:
            return False, "empty bearer token", None

        try:
            data = self._load_canonical_pairings(for_mutation=False)
            if data is None:
                return False, "canonical capability state missing or device not found", None
        except Exception as exc:
            return False, f"canonical capability state unreadable: {exc}", None

        digest = _digest(token)
        devices = data.get("mobile_devices")
        if not isinstance(devices, dict):
            return False, "device not found", None

        now = time.time()
        for row in devices.values():
            if not isinstance(row, dict):
                continue
            if row.get("scope") != "mobile_device":
                continue
            if not hmac.compare_digest(str(row.get("token_hash") or ""), digest):
                continue
            if row.get("revoked"):
                return False, "device has been revoked", None
            if float(row.get("expires_at_epoch", 0)) <= now:
                return False, "device token expired", None

            principal = MobileDevicePrincipal(
                device_id=str(row.get("device_id")),
                scope="mobile_device",
                device_label=str(row.get("device_label") or ""),
                created_at=str(row.get("created_at") or ""),
                expires_at=row.get("expires_at"),
                revoked=False,
            )
            return True, "authorized", principal

        return False, "invalid or revoked mobile device token", None

    def lookup_mobile_device(
        self, device_id: str
    ) -> tuple[bool, str, MobileDevicePrincipal | None]:
        """Re-read canonical state and validate device_id without needing a token."""
        if not device_id or not isinstance(device_id, str):
            return False, "invalid device_id", None
        clean_id = device_id.strip()

        try:
            data = self._load_canonical_pairings(for_mutation=False)
            if data is None:
                return False, "canonical capability state missing or device not found", None
        except Exception as exc:
            return False, f"canonical capability state unreadable: {exc}", None

        devices = data.get("mobile_devices")
        if not isinstance(devices, dict):
            return False, "device not found", None

        row = devices.get(clean_id)
        if not isinstance(row, dict):
            return False, "device not found", None

        if row.get("scope") != "mobile_device":
            return False, "device scope mismatch", None

        if row.get("revoked"):
            return False, "device has been revoked", None

        now = time.time()
        if float(row.get("expires_at_epoch", 0)) <= now:
            return False, "device token expired", None

        principal = MobileDevicePrincipal(
            device_id=str(row.get("device_id")),
            scope="mobile_device",
            device_label=str(row.get("device_label") or ""),
            created_at=str(row.get("created_at") or ""),
            expires_at=row.get("expires_at"),
            revoked=False,
        )
        return True, "authorized", principal

    def mobile_revocation_generation(self) -> int:
        """Return monotonic mobile revocation generation counter."""
        try:
            data = self._load_canonical_pairings(for_mutation=False)
            if isinstance(data, dict):
                return int(data.get("mobile_revocation_generation", 0))
        except Exception:
            pass
        return 0

    def create_transport_capability(
        self,
        *,
        project_id: str,
        host_id: str,
        allowed_operations: list[str],
        allowed_commands: Optional[list[str]] = None,
        allowed_roots: Optional[list[str]] = None,
        ttl_seconds: int = 3600,
        label: str = "",
    ) -> dict[str, Any]:
        """Mint a scoped transport capability. Returns token once."""
        cap_id = "tc-" + secrets.token_urlsafe(16)
        raw_secret = f"tc_{secrets.token_urlsafe(32)}"
        token_hash = _digest(raw_secret)
        now = time.time()
        now_iso = utc_now_iso()
        expires_at_epoch = now + ttl_seconds
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()

        row = {
            "capability_id": cap_id,
            "token_hash": token_hash,
            "project_id": project_id,
            "host_id": host_id,
            "allowed_operations": sorted(allowed_operations),
            "allowed_commands": sorted(allowed_commands or []),
            "allowed_roots": sorted(allowed_roots or []),
            "label": label,
            "created_at": now_iso,
            "expires_at": expires_at,
            "expires_at_epoch": expires_at_epoch,
            "revoked": False,
            "revoked_at": None,
        }

        with InterProcessFileLock(self.lock_path):
            data = self._load_canonical_pairings(for_mutation=True)
            if not isinstance(data, dict):
                raise ValueError("cannot access canonical capability state")
            t_caps = data.setdefault("transport_capabilities", {})
            t_caps[cap_id] = row
            write_json(self.pairings_path, data, indent=2)

        return {
            "capability_id": cap_id,
            "token": raw_secret,  # disclosed only once upon creation
            "project_id": project_id,
            "host_id": host_id,
            "allowed_operations": sorted(allowed_operations),
            "allowed_commands": sorted(allowed_commands or []),
            "allowed_roots": sorted(allowed_roots or []),
            "expires_at": expires_at,
        }

    def list_transport_capabilities(self, project_id: Optional[str] = None) -> list[dict[str, Any]]:
        """List non-secret metadata for transport capabilities."""
        try:
            data = self._load_canonical_pairings(for_mutation=False)
            if not isinstance(data, dict):
                return []
            t_caps = data.get("transport_capabilities", {})
            results = []
            for row in t_caps.values():
                if isinstance(row, dict):
                    if project_id and row.get("project_id") != "*" and row.get("project_id") != project_id:
                        continue
                    results.append({
                        "capability_id": row.get("capability_id"),
                        "project_id": row.get("project_id"),
                        "host_id": row.get("host_id"),
                        "allowed_operations": row.get("allowed_operations", []),
                        "allowed_commands": row.get("allowed_commands", []),
                        "allowed_roots": row.get("allowed_roots", []),
                        "label": row.get("label", ""),
                        "created_at": row.get("created_at"),
                        "expires_at": row.get("expires_at"),
                        "revoked": bool(row.get("revoked")),
                        "revoked_at": row.get("revoked_at"),
                    })
            return sorted(results, key=lambda x: str(x.get("created_at") or ""))
        except Exception:
            return []

    def revoke_transport_capability(self, capability_id: str) -> dict[str, Any]:
        """Revoke a transport capability by ID."""
        clean_id = capability_id.strip()
        with InterProcessFileLock(self.lock_path):
            data = self._load_canonical_pairings(for_mutation=True)
            if not isinstance(data, dict):
                raise ValueError("cannot access canonical capability state")
            t_caps = data.setdefault("transport_capabilities", {})
            row = t_caps.get(clean_id)
            if not isinstance(row, dict):
                return {"capability_id": clean_id, "revoked": False, "error": "not_found"}
            row["revoked"] = True
            row["revoked_at"] = utc_now_iso()
            write_json(self.pairings_path, data, indent=2)
            return {"capability_id": clean_id, "revoked": True}

    def validate_transport_capability(
        self,
        token_or_header: Optional[str],
        *,
        project_id: str,
        host_id: str,
        operation: str,
        command_ref: Optional[str] = None,
        path: Optional[str] = None,
    ) -> tuple[bool, str, Optional[dict[str, Any]]]:
        """Validate token and scope for a transport operation."""
        if not token_or_header:
            return False, "missing_capability_token", None
        tok = token_or_header[7:].strip() if token_or_header.startswith("Bearer ") else token_or_header.strip()
        if not tok:
            return False, "empty_capability_token", None

        try:
            data = self._load_canonical_pairings(for_mutation=False)
            if data is None:
                return False, "capability_store_missing", None
        except Exception as exc:
            return False, f"capability_store_unreadable: {exc}", None

        t_caps = data.get("transport_capabilities")
        if not isinstance(t_caps, dict):
            return False, "no_transport_capabilities_registered", None

        t_digest = _digest(tok)
        now = time.time()
        for row in t_caps.values():
            if not isinstance(row, dict):
                continue
            if not hmac.compare_digest(str(row.get("token_hash") or ""), t_digest):
                continue
            if row.get("revoked"):
                return False, "capability_revoked", None
            if float(row.get("expires_at_epoch", 0)) <= now:
                return False, "capability_expired", None

            # Project scope check
            cap_proj = row.get("project_id")
            if cap_proj and cap_proj != "*" and cap_proj != project_id:
                return False, f"capability_project_mismatch ({project_id} != {cap_proj})", None

            # Host scope check
            cap_host = row.get("host_id")
            if cap_host and cap_host != "*" and cap_host != host_id:
                return False, f"capability_host_mismatch ({host_id} != {cap_host})", None

            # Operation scope check
            allowed_ops = row.get("allowed_operations", [])
            if allowed_ops and "*" not in allowed_ops and operation not in allowed_ops:
                return False, f"operation_{operation}_not_allowed_by_capability", None

            # Command scope check
            if command_ref:
                allowed_cmds = row.get("allowed_commands", [])
                if allowed_cmds and "*" not in allowed_cmds and command_ref not in allowed_cmds:
                    return False, f"command_{command_ref}_not_allowed_by_capability", None

            # Path / roots scope check
            if path:
                allowed_roots = row.get("allowed_roots", [])
                if allowed_roots and "*" not in allowed_roots:
                    from dev_orchestrator.jobs.config import canonical_path, is_path_contained
                    c_path = canonical_path(path)
                    is_ok = any(is_path_contained(r, c_path) for r in allowed_roots)
                    if not is_ok:
                        return False, "path_not_within_capability_roots", None

            return True, "valid", row

        return False, "invalid_or_revoked_capability_token", None

    def consume_request_nonce(
        self, capability_id: str, nonce: str, *, max_age_seconds: float = 300.0
    ) -> bool:
        """Atomically validate and consume a single-use request nonce.

        Rejects previously seen nonces for this capability within max_age_seconds window.
        """
        clean_cap = capability_id.strip()
        clean_nonce = nonce.strip()
        if not clean_cap or not clean_nonce:
            return False

        nonce_file = self.root / "nonces.json"
        now = time.time()
        with InterProcessFileLock(self.lock_path):
            data = read_json(nonce_file, {"nonces": {}})
            if not isinstance(data, dict):
                data = {"nonces": {}}
            nonces = data.setdefault("nonces", {})

            # Clean expired nonces
            active_nonces: dict[str, float] = {}
            for n_key, ts in nonces.items():
                if isinstance(ts, (int, float)) and now - ts < max_age_seconds:
                    active_nonces[n_key] = float(ts)

            full_key = f"{clean_cap}:{clean_nonce}"
            if full_key in active_nonces:
                # Replay detected!
                return False

            active_nonces[full_key] = now
            data["nonces"] = active_nonces
            data["updated_at"] = utc_now_iso()
            write_json(nonce_file, data, indent=2)
            return True


def capability_state(pairing_id: str, runtime_root: Path | str | None = None) -> CapabilityVerdict:
    return ControlSecurity(runtime_root=runtime_root).capability_state(pairing_id)


def capability_status(
    header_or_token: str | None, runtime_root: Path | str | None = None
) -> tuple[CapabilityVerdict, str, dict[str, Any] | None]:
    return ControlSecurity(runtime_root=runtime_root).capability_status(header_or_token)


def capability_identity(
    header_or_token: str | None, runtime_root: Path | str | None = None
) -> str | None:
    return ControlSecurity(runtime_root=runtime_root).capability_identity(header_or_token)


def renew_session_capability(
    header_or_token: str | None,
    *,
    grace_period_seconds: int = 300,
    runtime_root: Path | str | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    return ControlSecurity(runtime_root=runtime_root).renew_session_capability(
        header_or_token, grace_period_seconds=grace_period_seconds
    )


def create_transport_capability(
    *,
    project_id: str,
    host_id: str,
    allowed_operations: list[str],
    allowed_commands: Optional[list[str]] = None,
    allowed_roots: Optional[list[str]] = None,
    ttl_seconds: int = 3600,
    label: str = "",
    runtime_root: Path | str | None = None,
) -> dict[str, Any]:
    return ControlSecurity(runtime_root=runtime_root).create_transport_capability(
        project_id=project_id,
        host_id=host_id,
        allowed_operations=allowed_operations,
        allowed_commands=allowed_commands,
        allowed_roots=allowed_roots,
        ttl_seconds=ttl_seconds,
        label=label,
    )


def list_transport_capabilities(
    project_id: Optional[str] = None,
    runtime_root: Path | str | None = None,
) -> list[dict[str, Any]]:
    return ControlSecurity(runtime_root=runtime_root).list_transport_capabilities(project_id=project_id)


def revoke_transport_capability(
    capability_id: str,
    runtime_root: Path | str | None = None,
) -> dict[str, Any]:
    return ControlSecurity(runtime_root=runtime_root).revoke_transport_capability(capability_id)


def validate_transport_capability(
    token_or_header: Optional[str],
    *,
    project_id: str,
    host_id: str,
    operation: str,
    command_ref: Optional[str] = None,
    path: Optional[str] = None,
    runtime_root: Path | str | None = None,
) -> tuple[bool, str, Optional[dict[str, Any]]]:
    return ControlSecurity(runtime_root=runtime_root).validate_transport_capability(
        token_or_header,
        project_id=project_id,
        host_id=host_id,
        operation=operation,
        command_ref=command_ref,
        path=path,
    )


def consume_request_nonce(
    capability_id: str,
    nonce: str,
    *,
    max_age_seconds: float = 300.0,
    runtime_root: Path | str | None = None,
) -> bool:
    return ControlSecurity(runtime_root=runtime_root).consume_request_nonce(
        capability_id, nonce, max_age_seconds=max_age_seconds
    )
