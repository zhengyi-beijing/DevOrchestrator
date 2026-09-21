"""Headless Python MobileContractClient for MobileGateway verification and testing.

Covers pairing, snapshot, streaming / reconnect / resync, mid-stream revocation,
alert-policy, guarded control submission, owner-gate approval, and command polling.
"""

from __future__ import annotations

import http.client
import json
import urllib.parse
from typing import Any, Callable, Dict, List, Optional


class MobileContractClientError(RuntimeError):
    """Base error for MobileContractClient."""

    def __init__(self, message: str, status: int = 0, payload: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload


class MobileContractClient:
    """Headless HTTP client interacting with the MobileGateway wire contract."""

    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        *,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.device_id: Optional[str] = None
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        parsed = urllib.parse.urlsplit(self.base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80

        req_headers = {"Accept": "application/json"}
        if self.token:
            req_headers["Authorization"] = f"Bearer {self.token}"
        if headers:
            req_headers.update(headers)

        encoded = None
        if body is not None:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            req_headers["Content-Type"] = "application/json"

        conn = http.client.HTTPConnection(host, port, timeout=self.timeout_seconds)
        try:
            conn.request(method, path, body=encoded, headers=req_headers)
            res = conn.getresponse()
            raw = res.read()
        except OSError as exc:
            raise MobileContractClientError(f"connection error: {exc}") from exc
        finally:
            conn.close()

        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as exc:
            raise MobileContractClientError(f"invalid JSON response (status {res.status}): {exc}", res.status) from exc

        if res.status in (200, 201, 202):
            return payload

        err_msg = str(payload.get("message") or payload.get("error") or f"HTTP {res.status}")
        raise MobileContractClientError(err_msg, res.status, payload)

    def pair(self, pairing_id: str, code: str, device_label: str = "test_device") -> dict[str, Any]:
        """Redeem pairing code for device token."""
        body = {"pairing_id": pairing_id, "code": code, "device_label": device_label}
        res = self._request("POST", "/api/v1/mobile/v1/pair", body=body)
        self.token = res.get("token")
        self.device_id = res.get("device_id")
        return res

    def projects(self) -> dict[str, Any]:
        """Fetch mobile projects overview."""
        return self._request("GET", "/api/v1/mobile/v1/projects")

    def project(self, project_id: str) -> dict[str, Any]:
        """Fetch single project mobile view."""
        safe_id = urllib.parse.quote(project_id.strip(), safe="")
        return self._request("GET", f"/api/v1/mobile/v1/projects/{safe_id}")

    def submit_control(
        self,
        project_id: str,
        action: str,
        expected_revision: str,
        device_request_id: str,
        target: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Submit a guarded lifecycle control."""
        safe_id = urllib.parse.quote(project_id.strip(), safe="")
        body = {
            "action": action,
            "expected_revision": expected_revision,
            "device_request_id": device_request_id,
            "target": target or {},
        }
        return self._request("POST", f"/api/v1/mobile/v1/projects/{safe_id}/controls", body=body)

    def command_status(self, command_id: str) -> dict[str, Any]:
        """Poll status of a submitted command."""
        safe_id = urllib.parse.quote(command_id.strip(), safe="")
        return self._request("GET", f"/api/v1/mobile/v1/commands/{safe_id}")

    def alert_policy(self) -> dict[str, Any]:
        """Fetch alert policy."""
        return self._request("GET", "/api/v1/mobile/v1/alert-policy")

    def update_alert_policy(self, policy: dict[str, Any]) -> dict[str, Any]:
        """Update alert policy."""
        return self._request("PUT", "/api/v1/mobile/v1/alert-policy", body=policy)

    def ack_alert(self, alert_key: str) -> dict[str, Any]:
        """Acknowledge an alert."""
        safe_key = urllib.parse.quote(alert_key.strip(), safe="")
        return self._request("POST", f"/api/v1/mobile/v1/alerts/{safe_key}/ack")

    def snooze_alert(self, alert_key: str, duration_seconds: int = 3600) -> dict[str, Any]:
        """Snooze an alert."""
        safe_key = urllib.parse.quote(alert_key.strip(), safe="")
        body = {"duration_seconds": int(duration_seconds)}
        return self._request("POST", f"/api/v1/mobile/v1/alerts/{safe_key}/snooze", body=body)

    def poll_events(self, cursor: Optional[str] = None) -> dict[str, Any]:
        """Long-poll events."""
        query = f"?cursor={urllib.parse.quote(cursor)}" if cursor else ""
        return self._request("GET", f"/api/v1/mobile/v1/events/poll{query}")

    def read_stream_events(
        self,
        cursor: Optional[str] = None,
        max_events: int = 5,
        timeout_seconds: float = 5.0,
    ) -> List[dict[str, Any]]:
        """Connect to SSE stream and read up to max_events."""
        parsed = urllib.parse.urlsplit(self.base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        query = f"?cursor={urllib.parse.quote(cursor)}" if cursor else ""

        headers = {"Accept": "text/event-stream"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        conn = http.client.HTTPConnection(host, port, timeout=timeout_seconds)
        conn.request("GET", f"/api/v1/mobile/v1/events{query}", headers=headers)
        res = conn.getresponse()
        if res.status != 200:
            raw = res.read()
            conn.close()
            raise MobileContractClientError(f"stream failed status {res.status}", res.status, raw)

        events = []
        cur_event: dict[str, Any] = {}
        try:
            while len(events) < max_events:
                line = res.fp.readline()
                if not line:
                    break
                decoded = line.decode("utf-8").rstrip("\r\n")
                if not decoded:
                    if cur_event:
                        events.append(cur_event)
                        cur_event = {}
                    continue
                if decoded.startswith(":"):
                    # SSE comment / ping
                    continue
                k, sep, v = decoded.partition(":")
                val = v.lstrip(" ")
                if k == "event":
                    cur_event["event"] = val
                elif k == "id":
                    cur_event["id"] = val
                elif k == "data":
                    try:
                        cur_event["data"] = json.loads(val)
                    except Exception:
                        cur_event["data"] = val
        finally:
            conn.close()

        return events
