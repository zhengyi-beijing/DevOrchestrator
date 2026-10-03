"""P19.3 read-only browser-control transport acceptance for ports 8765 -> 8770.

This script exercises the same structured request accepted from the Tampermonkey
adapter. It intentionally does not claim a browser UI round trip; the real
ChatGPT conversation proof is recorded separately after live-browser testing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def http_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, str], Any]:
    request_headers = dict(headers or {})
    body = None
    if payload is not None:
        request_headers.setdefault("Content-Type", "application/json")
        body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        response = urllib.request.urlopen(request, timeout=15)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        status = response.status
        response_headers = {key.lower(): value for key, value in response.headers.items()}
        raw = response.read()
    if not raw:
        return status, response_headers, None
    try:
        parsed: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = raw.decode("utf-8", errors="replace")
    return status, response_headers, parsed


def websol_control_records(runtime_root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in (runtime_root / "bridge" / "queues").glob("*/*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        candidates = value.values() if isinstance(value, dict) else value if isinstance(value, list) else []
        records.extend(
            row for row in candidates
            if isinstance(row, dict) and row.get("protocol") == "DEVORCH_ACTION_V1"
        )
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description="P19.3 read-only Browser Control acceptance")
    parser.add_argument("--bridge-url", default="http://127.0.0.1:8765")
    parser.add_argument("--runtime-root", default="runtime")
    parser.add_argument("--project-id", default="devorchestrator")
    parser.add_argument("--output", default="docs/evidence/P19_3_BROWSER_CONTROL_ACCEPTANCE.json")
    parser.add_argument("--operations-output", default="docs/evidence/P19_3_BROWSER_CONTROL_OPERATIONS.ndjson")
    args = parser.parse_args()

    from dev_orchestrator.control.security import ControlSecurity
    from dev_orchestrator.control.store import ConversationControlStore

    runtime_root = Path(args.runtime_root).resolve()
    bridge_url = args.bridge_url.rstrip("/")
    action_url = bridge_url + "/v1/control/action"
    run_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    operations: list[dict[str, Any]] = []

    def record(operation: str, status_code: int, request_id: str, **details: Any) -> None:
        operations.append({
            "timestamp": now_iso(),
            "operation": operation,
            "status_code": status_code,
            "request_id": request_id,
            **details,
        })

    conversations = ConversationControlStore(runtime_root)
    binding = conversations.binding_for_project(args.project_id)
    if not isinstance(binding, dict):
        raise RuntimeError(f"project {args.project_id!r} has no runtime conversation binding")
    adapter = str(binding.get("adapter") or "")
    binding_id = str(binding.get("binding_id") or "")
    if adapter != "chatgpt_web" or not binding_id:
        raise RuntimeError(f"project binding is not a ChatGPT Web conversation: {binding}")
    session = conversations.session_status(adapter, binding_id)
    if session.get("state") != "live":
        raise RuntimeError(f"bound conversation is not live: {session.get('state')}")

    health_status, _, health = http_request(bridge_url + "/v1/control/health")
    if health_status != 200 or not isinstance(health, dict) or health.get("service") != "browser_control_bridge":
        raise RuntimeError(f"bridge health failed: {health_status} {health}")
    record("bridge_health", health_status, f"p19-3-{run_tag}-health")

    origin = "https://chatgpt.com"
    cors_status, cors_headers, _ = http_request(
        action_url,
        method="OPTIONS",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization, Content-Type",
        },
    )
    if cors_status != 204 or cors_headers.get("access-control-allow-origin") != origin:
        raise RuntimeError(f"ChatGPT CORS preflight failed: {cors_status} {cors_headers}")
    record("cors_chatgpt_allowed", cors_status, f"p19-3-{run_tag}-cors")

    spoof_status, spoof_headers, _ = http_request(
        action_url,
        method="OPTIONS",
        headers={"Origin": "https://chatgpt.com.attacker.example", "Access-Control-Request-Method": "POST"},
    )
    if spoof_status != 403 or "access-control-allow-origin" in spoof_headers:
        raise RuntimeError("suffix-spoofed ChatGPT origin was not rejected")
    record("cors_suffix_spoof_rejected", spoof_status, f"p19-3-{run_tag}-cors-spoof")

    def action_payload(request_id: str, **changes: Any) -> dict[str, Any]:
        payload = {
            "protocol": "DEVORCH_ACTION_V1",
            "request_id": request_id,
            "action": "get_project_status",
            "project_id": args.project_id,
            "parameters": {},
            "binding": {"adapter": adapter, "binding_id": binding_id},
            "issued_at": now_iso(),
        }
        payload.update(changes)
        return payload

    unauth_request = action_payload(f"p19-3-{run_tag}-unauth")
    unauth_status, _, unauth_body = http_request(
        action_url, method="POST", headers={"Origin": origin}, payload=unauth_request,
    )
    if unauth_status != 401:
        raise RuntimeError(f"missing capability was not rejected: {unauth_status} {unauth_body}")
    record("missing_capability_rejected", unauth_status, unauth_request["request_id"])

    security = ControlSecurity(runtime_root)
    capability = security.create_web_bridge_capability(
        args.project_id,
        binding_id,
        adapter=adapter,
        expires_in_seconds=3600,
    )
    capability_id = str(capability["capability_id"])
    token = str(capability["token"])
    headers = {"Origin": origin, "Authorization": f"Bearer {token}"}

    before_pollution = len(websol_control_records(runtime_root))
    revoked = False
    try:
        status_request = action_payload(f"p19-3-{run_tag}-status")
        status_code, response_headers, status_body = http_request(
            action_url, method="POST", headers=headers, payload=status_request,
        )
        if status_code != 200 or not isinstance(status_body, dict):
            raise RuntimeError(f"get_project_status failed: {status_code} {status_body}")
        status_data = status_body.get("data")
        if not isinstance(status_data, dict):
            raise RuntimeError(f"status response omitted bounded data: {status_body}")
        required = {
            "project_id", "git", "current_task", "lifecycle_state", "paused",
            "readiness", "active_execution", "active_ai_roles", "active_executions",
            "active_resources", "watchdog", "conversation", "control_identity",
        }
        missing = sorted(required - set(status_data))
        if missing:
            raise RuntimeError(f"status response missing fields: {missing}")
        if status_data.get("project_id") != args.project_id:
            raise RuntimeError(f"status response project mismatch: {status_data.get('project_id')!r}")
        if (status_data.get("conversation") or {}).get("state") != "live":
            raise RuntimeError(f"status response did not preserve live conversation state: {status_data}")
        if response_headers.get("access-control-allow-origin") != origin:
            raise RuntimeError("action response omitted exact ChatGPT CORS origin")
        record(
            "get_project_status",
            status_code,
            status_request["request_id"],
            lifecycle_state=status_data.get("lifecycle_state"),
            current_task=status_data.get("current_task"),
            conversation_state=(status_data.get("conversation") or {}).get("state"),
        )

        replay_code, _, replay_body = http_request(
            action_url, method="POST", headers=headers, payload=status_request,
        )
        if replay_code != 200 or replay_body != status_body:
            raise RuntimeError(f"exact replay was not idempotent: {replay_code} {replay_body}")
        record("idempotent_exact_replay", replay_code, status_request["request_id"])

        changed = dict(status_request)
        changed["issued_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        conflict_code, _, conflict_body = http_request(
            action_url, method="POST", headers=headers, payload=changed,
        )
        if conflict_code != 409:
            raise RuntimeError(f"altered replay was not rejected: {conflict_code} {conflict_body}")
        record("idempotency_conflict_rejected", conflict_code, status_request["request_id"])

        forbidden_requests = [
            action_payload(f"p19-3-{run_tag}-start", action="start_task", parameters={"command_ref": "anything"}),
            action_payload(f"p19-3-{run_tag}-cancel", action="cancel_job", parameters={"job_id": "anything"}),
            action_payload(f"p19-3-{run_tag}-file", action="read_file", parameters={"path": "C:/Windows/win.ini"}),
            action_payload(f"p19-3-{run_tag}-shell", shell="powershell.exe -Command whoami"),
        ]
        for request in forbidden_requests:
            code, _, body = http_request(action_url, method="POST", headers=headers, payload=request)
            if code != 400:
                raise RuntimeError(f"unsupported or mutating request was not rejected: {code} {body}")
            record("unsupported_action_rejected", code, request["request_id"], action=request.get("action"))

        wrong_binding = action_payload(
            f"p19-3-{run_tag}-wrong-binding",
            binding={"adapter": adapter, "binding_id": binding_id + "-other"},
        )
        wrong_code, _, wrong_body = http_request(
            action_url, method="POST", headers=headers, payload=wrong_binding,
        )
        if wrong_code != 403:
            raise RuntimeError(f"wrong conversation was not rejected: {wrong_code} {wrong_body}")
        record("wrong_conversation_rejected", wrong_code, wrong_binding["request_id"])

        wrong_project = action_payload(
            f"p19-3-{run_tag}-wrong-project", project_id="__p19_3_cross_project__",
        )
        project_code, _, project_body = http_request(
            action_url, method="POST", headers=headers, payload=wrong_project,
        )
        if project_code != 403:
            raise RuntimeError(f"cross-project request was not rejected: {project_code} {project_body}")
        record("cross_project_rejected", project_code, wrong_project["request_id"])

        security.revoke_capability(capability_id)
        revoked = True
        revoked_request = action_payload(f"p19-3-{run_tag}-revoked")
        revoked_code, _, revoked_body = http_request(
            action_url, method="POST", headers=headers, payload=revoked_request,
        )
        if revoked_code != 401:
            raise RuntimeError(f"revoked capability was accepted: {revoked_code} {revoked_body}")
        record("revoked_capability_rejected", revoked_code, revoked_request["request_id"])
    finally:
        if not revoked:
            security.revoke_capability(capability_id)

    after_pollution = len(websol_control_records(runtime_root))
    if after_pollution != before_pollution:
        raise RuntimeError("browser-control request polluted the Web Sol queue")
    record("websol_channel_isolation", 200, f"p19-3-{run_tag}-isolation", control_records_added=0)

    evidence = {
        "schema_version": 2,
        "acceptance": "P19.3 bounded read-only Browser Control transport",
        "host": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME"),
        "completed_at": now_iso(),
        "bridge_url": bridge_url,
        "project_id": args.project_id,
        "binding": {"adapter": adapter, "binding_id": binding_id},
        "capability_id": capability_id,
        "protocol_version": "DEVORCH_ACTION_V1",
        "result": "PASS",
        "read_only": True,
        "allowed_actions": ["get_project_status"],
        "mutating_actions_exposed": [],
        "arbitrary_filesystem_exposed": False,
        "arbitrary_shell_exposed": False,
        "websol_channel_isolation_enforced": True,
        "rdc_call_count": 0,
        "native_chatgpt_mcp_call_count": 0,
        "openai_api_call_count": 0,
        "browser_ui_round_trip": "not_run_by_this_transport_script",
        "operations": operations,
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    operations_output = Path(args.operations_output)
    operations_output.parent.mkdir(parents=True, exist_ok=True)
    operations_output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in operations),
        encoding="utf-8",
    )
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
