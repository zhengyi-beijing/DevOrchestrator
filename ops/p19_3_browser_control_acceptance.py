"""Real separate-process P19.3 ChatGPT Plus browser control acceptance against bridge port 8765."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description="P19.3 Browser Control Acceptance Test")
    parser.add_argument("--bridge-url", default="http://127.0.0.1:8765")
    parser.add_argument("--daemon-url", default="http://127.0.0.1:8770")
    parser.add_argument("--runtime-root", default="runtime")
    parser.add_argument("--project-id", default="devorchestrator")
    parser.add_argument("--output", default="docs/evidence/P19_3_BROWSER_CONTROL_ACCEPTANCE.json")
    parser.add_argument("--operations-output", default="docs/evidence/P19_3_BROWSER_CONTROL_OPERATIONS.ndjson")
    parser.add_argument("--completion-command", default="transport_compileall")
    parser.add_argument("--cancel-command", default="external_control_sleep")
    args = parser.parse_args()

    runtime_root = Path(args.runtime_root).resolve()
    from dev_orchestrator.control.security import (
        create_browser_control_capability,
        revoke_browser_control_capability,
    )
    from dev_orchestrator.bridge.store import BrowserBridgeStore

    run_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rows: list[dict[str, Any]] = []

    def record(
        operation: str,
        status_code: int,
        resp_data: Any,
        req_id: str,
        *,
        selected_transport: Optional[str] = None,
        job_id: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        entry = {
            "timestamp": now_iso(),
            "operation": operation,
            "status_code": status_code,
            "request_id": req_id,
            "selected_transport": selected_transport,
            "job_id": job_id,
            **(extra or {}),
        }
        rows.append(entry)

    def http_request(
        url: str,
        *,
        method: str = "GET",
        headers: Optional[dict[str, str]] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> tuple[int, dict[str, str], Any]:
        req_headers = dict(headers or {})
        body_bytes = None
        if payload is not None:
            if "Content-Type" not in req_headers:
                req_headers["Content-Type"] = "application/json"
            body_bytes = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(url, data=body_bytes, headers=req_headers, method=method)
        resp_headers: dict[str, str] = {}
        try:
            with urllib.request.urlopen(req) as resp:
                status = resp.status
                for k, v in resp.headers.items():
                    resp_headers[k] = v
                raw_bytes = resp.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            for k, v in exc.headers.items():
                resp_headers[k] = v
            raw_bytes = exc.read()

        parsed: Any = None
        if raw_bytes:
            try:
                parsed = json.loads(raw_bytes.decode("utf-8"))
            except Exception:
                parsed = raw_bytes.decode("utf-8", errors="replace")
        return status, resp_headers, parsed

    # Step 1: Health check on bridge
    health_url = f"{args.bridge_url}/v1/control/health"
    h_status, h_hdrs, h_data = http_request(health_url, method="GET")
    if h_status != 200 or not isinstance(h_data, dict) or h_data.get("status") != "ok":
        raise RuntimeError(f"Bridge health check failed: status={h_status}, body={h_data}")
    record("bridge_control_health", h_status, h_data, f"req-{run_tag}-health", extra={"service": h_data.get("service")})

    # Step 2: CORS Preflight check from ChatGPT origin
    action_url = f"{args.bridge_url}/v1/control/action"
    preflight_origin = "https://chatgpt.com"
    p_status, p_hdrs, _ = http_request(
        action_url,
        method="OPTIONS",
        headers={
            "Origin": preflight_origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization, Content-Type, X-DevOrch-Request-ID",
        },
    )
    if p_status not in (200, 204):
        raise RuntimeError(f"CORS preflight failed with status {p_status}")
    allow_origin = p_hdrs.get("Access-Control-Allow-Origin")
    allow_methods = p_hdrs.get("Access-Control-Allow-Methods", "")
    if allow_origin != preflight_origin:
        raise RuntimeError(f"CORS preflight origin mismatch: got {allow_origin}, expected {preflight_origin}")
    if "POST" not in allow_methods or "OPTIONS" not in allow_methods:
        raise RuntimeError(f"CORS preflight methods missing POST or OPTIONS: {allow_methods}")
    record(
        "cors_preflight",
        p_status,
        None,
        f"req-{run_tag}-cors",
        extra={"allow_origin": allow_origin, "allow_methods": allow_methods},
    )

    # Step 3: Unauthenticated & invalid token rejection
    no_auth_status, _, no_auth_data = http_request(
        action_url,
        method="POST",
        payload={"protocol": "DEVORCH_ACTION_V1", "action": "status", "project_id": args.project_id},
    )
    if no_auth_status != 401:
        raise RuntimeError(f"Expected 401 for unauthenticated request, got {no_auth_status}: {no_auth_data}")
    record("unauthorized_missing_token", no_auth_status, no_auth_data, f"req-{run_tag}-no-auth")

    bad_auth_status, _, bad_auth_data = http_request(
        action_url,
        method="POST",
        headers={"Authorization": "Bearer devorch_invalid_dummy_token_12345"},
        payload={"protocol": "DEVORCH_ACTION_V1", "action": "status", "project_id": args.project_id},
    )
    if bad_auth_status != 401:
        raise RuntimeError(f"Expected 401 for invalid token, got {bad_auth_status}: {bad_auth_data}")
    record("unauthorized_invalid_token", bad_auth_status, bad_auth_data, f"req-{run_tag}-bad-auth")

    # Step 4: Mint capability token for browser control
    cap_info = create_browser_control_capability(
        project_id=args.project_id,
        label=f"p19_3_acceptance_{run_tag}",
        ttl_seconds=3600,
        allowed_actions=None,
        runtime_root=runtime_root,
    )
    token = cap_info["token"]
    capability_id = cap_info["capability_id"]
    auth_header = f"Bearer {token}"

    request_seq = 0

    def control_rpc(
        action: str,
        params: Optional[dict[str, Any]] = None,
        *,
        request_id: Optional[str] = None,
        confirmed: bool = False,
        custom_token: Optional[str] = None,
        extra_body: Optional[dict[str, Any]] = None,
    ) -> tuple[int, dict[str, str], Any, str]:
        nonlocal request_seq
        request_seq += 1
        req_id = request_id or f"p19-3-acc-{run_tag}-{action}-{request_seq:04d}"
        payload: dict[str, Any] = {
            "protocol": "DEVORCH_ACTION_V1",
            "action": action,
            "request_id": req_id,
            "project_id": args.project_id,
            "parameters": params or {},
            "confirmed": confirmed,
        }
        if extra_body:
            payload.update(extra_body)

        headers = {
            "Origin": preflight_origin,
            "Authorization": f"Bearer {custom_token}" if custom_token is not None else auth_header,
            "X-DevOrch-Request-ID": req_id,
        }
        status, hdrs, body = http_request(action_url, method="POST", headers=headers, payload=payload)
        return status, hdrs, body, req_id

    complete_job_id: Optional[str] = None
    cancel_job_id: Optional[str] = None

    try:
        # Step 5: Read-only action: status
        stat_status, _, stat_data, stat_req_id = control_rpc("status")
        if stat_status != 200 or stat_data.get("status") != "success":
            raise RuntimeError(f"status action failed: status={stat_status}, data={stat_data}")
        proj_data = stat_data.get("data", {}).get("data", {}).get("project", {})
        proj_state = proj_data.get("state")
        record("action_status", stat_status, stat_data, stat_req_id, extra={"project_state": proj_state})

        # Step 6: Read-only action: commands_catalog & hardware fence verification
        cat_status, _, cat_data, cat_req_id = control_rpc("commands_catalog")
        if cat_status != 200 or cat_data.get("status") != "success":
            raise RuntimeError(f"commands_catalog action failed: status={cat_status}, data={cat_data}")
        cat_res = cat_data.get("data", {})
        commands = cat_res.get("commands", [])
        selectable = [c for c in commands if c.get("selectable")]
        hw_non_selectable = [c for c in commands if c.get("is_hardware")]
        record(
            "action_commands_catalog",
            cat_status,
            cat_data,
            cat_req_id,
            extra={
                "command_count": len(commands),
                "selectable_count": len(selectable),
                "hw_non_selectable_count": len(hw_non_selectable),
            },
        )

        # Step 7: Read-only action: read_file
        rf_status, _, rf_data, rf_req_id = control_rpc("read_file", {"path": "pyproject.toml"})
        if rf_status != 200 or rf_data.get("status") != "success":
            raise RuntimeError(f"read_file action failed: status={rf_status}, data={rf_data}")
        rf_res = rf_data.get("data", {})
        text = rf_res.get("content") or rf_res.get("content_text")
        if not text:
            raise RuntimeError(f"read_file expected text content: {rf_res}")
        record(
            "action_read_file",
            rf_status,
            rf_data,
            rf_req_id,
            selected_transport=rf_res.get("selected_transport", "local"),
            extra={"path": rf_res.get("path"), "size_bytes": rf_res.get("size_bytes")},
        )

        # Step 8: Path traversal fail-closed
        trav_status, _, trav_data, trav_req_id = control_rpc("read_file", {"path": "../../windows/win.ini"})
        if trav_status != 400:
            raise RuntimeError(f"Expected 400 for path traversal attempt, got {trav_status}: {trav_data}")
        record("action_read_file_traversal_fail_closed", trav_status, trav_data, trav_req_id)

        # Step 9: Effectful action confirmation gate check
        exec_req_id = f"p19-3-acc-{run_tag}-comp-task"
        comp_params = {
            "command_ref": args.completion_command,
            "idempotency_key": f"browser-acc-comp-{run_tag}",
        }
        gate_status, _, gate_data, gate_req_id = control_rpc(
            "start_task",
            comp_params,
            request_id=exec_req_id,
            confirmed=False,
        )
        if gate_status != 400 or not gate_data.get("confirmation_required"):
            raise RuntimeError(f"Expected confirmation_required (400), got {gate_status}: {gate_data}")
        record(
            "action_start_task_confirmation_gate",
            gate_status,
            gate_data,
            gate_req_id,
            extra={"confirmation_required": True, "action": gate_data.get("action")},
        )

        # Step 10: Effectful action with confirmation
        exec_status, _, exec_data, exec_req_id_actual = control_rpc(
            "start_task",
            comp_params,
            request_id=exec_req_id,
            confirmed=True,
        )
        if exec_status != 200 or exec_data.get("status") != "success":
            raise RuntimeError(f"start_task confirmed failed: status={exec_status}, data={exec_data}")
        exec_res = exec_data.get("data", {})
        complete_job_id = exec_res.get("job_id")
        comp_transport = exec_res.get("selected_transport", "local")
        if not complete_job_id:
            raise RuntimeError(f"start_task missing job_id: {exec_res}")
        record(
            "action_start_task_confirmed",
            exec_status,
            exec_data,
            exec_req_id_actual,
            selected_transport=comp_transport,
            job_id=complete_job_id,
        )

        # Step 11: Idempotency exact replay
        replay_status, _, replay_data, replay_req_id = control_rpc(
            "start_task",
            comp_params,
            request_id=exec_req_id,
            confirmed=True,
        )
        if replay_status != 200 or replay_data.get("data", {}).get("job_id") != complete_job_id:
            raise RuntimeError(f"Idempotency replay failed: status={replay_status}, data={replay_data}")
        record(
            "action_idempotency_exact_replay",
            replay_status,
            replay_data,
            replay_req_id,
            selected_transport=comp_transport,
            job_id=complete_job_id,
        )

        # Step 12: Idempotency conflict rejection (differing payload with same request_id)
        conflict_status, _, conflict_data, conflict_req_id = control_rpc(
            "start_task",
            {"command_ref": "different_command_ref"},
            request_id=exec_req_id,
            confirmed=True,
        )
        if conflict_status != 409:
            raise RuntimeError(f"Expected 409 Conflict for altered payload, got {conflict_status}: {conflict_data}")
        record(
            "action_idempotency_conflict_rejection",
            conflict_status,
            conflict_data,
            conflict_req_id,
        )

        # Step 13: Poll job_status until terminal
        poll_done = False
        final_job_data = None
        for _ in range(60):
            js_status, _, js_data, js_req_id = control_rpc("job_status", {"job_id": complete_job_id})
            if js_status != 200:
                raise RuntimeError(f"job_status poll failed: {js_data}")
            js_res = js_data.get("data", {})
            st = js_res.get("status")
            if st in ("completed", "failed", "cancelled"):
                poll_done = True
                final_job_data = js_res
                record(
                    "action_job_status_poll_terminal",
                    js_status,
                    js_data,
                    js_req_id,
                    selected_transport=js_res.get("selected_transport", "local"),
                    job_id=complete_job_id,
                    extra={"job_status": st},
                )
                break
            time.sleep(0.1)

        if not poll_done or final_job_data.get("status") != "completed":
            raise RuntimeError(f"job {complete_job_id} did not complete successfully: {final_job_data}")

        # Step 14: Cancellable job workflow (spawn -> cancel with confirmation gate)
        cancel_spawn_req = f"p19-3-acc-{run_tag}-spawn-cancel"
        cancel_spawn_params = {
            "command_ref": args.cancel_command,
            "idempotency_key": f"browser-acc-cancel-{run_tag}",
        }
        # First gate check for spawning cancellable job
        cg_status, _, cg_data, cg_req_id = control_rpc("start_task", cancel_spawn_params, request_id=cancel_spawn_req, confirmed=False)
        if cg_status != 400 or not cg_data.get("confirmation_required"):
            raise RuntimeError(f"Expected confirmation required on cancellable job spawn: {cg_data}")

        # Confirmed spawn of cancellable job
        cs_status, _, cs_data, cs_req_id = control_rpc(
            "start_task",
            cancel_spawn_params,
            request_id=cancel_spawn_req,
            confirmed=True,
        )
        if cs_status != 200 or cs_data.get("status") != "success":
            raise RuntimeError(f"Failed to spawn cancellable job: {cs_data}")
        cancel_job_id = cs_data.get("data", {})["job_id"]
        record(
            "action_start_task_for_cancel",
            cs_status,
            cs_data,
            cs_req_id,
            selected_transport=cs_data.get("data", {}).get("selected_transport", "local"),
            job_id=cancel_job_id,
        )

        # Cancel without confirmation -> verify 400 gate
        cancel_act_req = f"p19-3-acc-{run_tag}-cancel-act"
        cgate_status, _, cgate_data, cgate_req_id = control_rpc(
            "cancel_job",
            {"job_id": cancel_job_id, "reason": "P19.3 acceptance verification"},
            request_id=cancel_act_req,
            confirmed=False,
        )
        if cgate_status != 400 or not cgate_data.get("confirmation_required"):
            raise RuntimeError(f"Expected confirmation required on cancel_job: {cgate_data}")
        record("action_cancel_job_confirmation_gate", cgate_status, cgate_data, cgate_req_id)

        # Cancel with confirmation -> verify 200
        cact_status, _, cact_data, cact_req_id = control_rpc(
            "cancel_job",
            {"job_id": cancel_job_id, "reason": "P19.3 acceptance verification"},
            request_id=cancel_act_req,
            confirmed=True,
        )
        if cact_status != 200 or cact_data.get("status") != "success":
            raise RuntimeError(f"cancel_job failed: {cact_data}")
        record(
            "action_cancel_job_confirmed",
            cact_status,
            cact_data,
            cact_req_id,
            selected_transport=cact_data.get("data", {}).get("selected_transport", "local"),
            job_id=cancel_job_id,
        )

        # Step 15: Read log action
        log_status, _, log_data, log_req_id = control_rpc("read_log", {"source": "events", "limit": 10})
        if log_status != 200 or log_data.get("status") != "success":
            raise RuntimeError(f"read_log failed: {log_data}")
        items = log_data.get("data", {}).get("items", [])
        record("action_read_log", log_status, log_data, log_req_id, extra={"items_count": len(items)})

        # Step 16: Channel isolation verification (ensure zero Web Sol queue pollution)
        bridge_store = BrowserBridgeStore(runtime_root / "bridge")
        queue_files = bridge_store._queue_files()
        web_sol_records = []
        for qf in queue_files:
            try:
                data = json.loads(qf.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    web_sol_records.extend(data.values())
            except Exception:
                pass
        control_in_websol = [
            r for r in web_sol_records
            if isinstance(r, dict) and (r.get("protocol") == "DEVORCH_ACTION_V1" or r.get("action") in {"status", "start_task", "cancel_job"})
        ]
        if control_in_websol:
            raise RuntimeError(f"Channel isolation violation: found control actions in Web Sol queue: {control_in_websol}")
        record("channel_isolation_verified", 200, None, f"req-{run_tag}-isolation", extra={"control_in_websol_count": len(control_in_websol)})

    finally:
        # Step 17: Revocation test & cleanup
        revoke_browser_control_capability(capability_id, runtime_root=runtime_root)
        rev_status, _, rev_data, rev_req_id = control_rpc("status", custom_token=token)
        if rev_status != 401:
            raise RuntimeError(f"Expected 401 after capability revocation, got {rev_status}: {rev_data}")
        record("capability_revocation_verified", rev_status, rev_data, rev_req_id, extra={"capability_id": capability_id})

    # Step 18: Audit transport & assert zero RDC calls
    native_rows = [row for row in rows if row.get("selected_transport")]
    rdc_rows = [row for row in rows if "rdc" in str(row.get("selected_transport") or "").lower()]
    rdc_call_count = len(rdc_rows)
    bad_rows = [row for row in native_rows if row.get("selected_transport") not in {"local", "ssh"}]
    if rdc_rows or bad_rows:
        raise RuntimeError(f"non-native or RDC transport observed: {rdc_rows or bad_rows}")

    evidence = {
        "schema_version": 1,
        "acceptance": "P19.3 ChatGPT Plus Browser Control Bridge zero-RDC",
        "host": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME"),
        "started_run_tag": run_tag,
        "completed_at": now_iso(),
        "bridge_url": args.bridge_url,
        "project_id": args.project_id,
        "capability_id": capability_id,
        "protocol_version": "DEVORCH_ACTION_V1",
        "result": "PASS",
        "confirmation_gate_enforced": True,
        "channel_isolation_enforced": True,
        "native_transport_operation_count": len(native_rows),
        "selected_transports": sorted({str(row["selected_transport"]) for row in native_rows}),
        "rdc_call_count": rdc_call_count,
        "observed_rdc_selections": rdc_call_count,
        "complete_job_id": complete_job_id,
        "cancel_job_id": cancel_job_id,
        "operations": rows,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    op_path = Path(args.operations_output)
    op_path.parent.mkdir(parents=True, exist_ok=True)
    op_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
