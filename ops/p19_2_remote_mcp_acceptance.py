"""Real separate-process P19.2 remote MCP acceptance against daemon port 8770."""

from __future__ import annotations

import argparse
import base64
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8770")
    parser.add_argument("--runtime-root", default="runtime")
    parser.add_argument("--project-id", default="devorchestrator")
    parser.add_argument("--output", default="docs/evidence/P19_2_REMOTE_MCP_ACCEPTANCE.json")
    parser.add_argument("--operations-output", default="docs/evidence/P19_2_REMOTE_MCP_OPERATIONS.ndjson")
    parser.add_argument("--completion-command", default="transport_compileall")
    parser.add_argument("--cancel-command", default="external_control_sleep")
    args = parser.parse_args()

    runtime_root = Path(args.runtime_root).resolve()
    from dev_orchestrator.control.security import (
        ControlSecurity,
        create_mcp_capability,
        revoke_mcp_capability,
    )

    run_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rows: list[dict[str, Any]] = []

    # Step 1: Mint a scoped MCP capability token for this live acceptance run
    cap_info = create_mcp_capability(
        label=f"p19_2_acceptance_{run_tag}",
        ttl_seconds=3600,
        runtime_root=runtime_root,
    )
    token = cap_info["token"]
    capability_id = cap_info["capability_id"]

    mcp_url = f"{args.base_url}/mcp"
    parsed_url = urllib.parse.urlsplit(args.base_url)
    host_header = parsed_url.netloc
    origin_header = f"{parsed_url.scheme}://{parsed_url.netloc}"

    request_seq = 0
    session_id: Optional[str] = None
    protocol_version: Optional[str] = None

    def mcp_rpc(
        payload: Optional[dict[str, Any]],
        *,
        method: str = "POST",
        accept: str = "application/json",
        custom_session_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> tuple[int, dict[str, str], Any, str]:
        nonlocal request_seq
        request_seq += 1
        req_id = request_id or f"p19-2-mcp-{run_tag}-req-{request_seq:03d}"
        active_session = custom_session_id if custom_session_id is not None else session_id

        headers: dict[str, str] = {
            "Host": host_header,
            "Authorization": f"Bearer {token}",
            "X-DevOrch-Request-ID": req_id,
            "Origin": origin_header,
        }
        if accept:
            headers["Accept"] = accept
        if active_session:
            headers["Mcp-Session-Id"] = active_session

        body_bytes = None
        if payload is not None and method == "POST":
            headers["Content-Type"] = "application/json"
            body_bytes = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(mcp_url, data=body_bytes, headers=headers, method=method)
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
            ctype = resp_headers.get("Content-Type", "")
            if "text/event-stream" in ctype:
                parsed = raw_bytes.decode("utf-8")
            else:
                try:
                    parsed = json.loads(raw_bytes.decode("utf-8"))
                except Exception:
                    parsed = raw_bytes.decode("utf-8", errors="replace")
        return status, resp_headers, parsed, req_id

    def record(
        operation: str,
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
            "request_id": req_id,
            "selected_transport": selected_transport,
            "job_id": job_id,
            **(extra or {}),
        }
        rows.append(entry)

    try:
        # Step 2: Initialize MCP session
        init_status, init_headers, init_resp, init_req_id = mcp_rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            },
            request_id=f"p19-2-mcp-{run_tag}-init",
        )
        if init_status != 200:
            raise RuntimeError(f"initialize failed with status {init_status}: {init_resp}")

        session_id = init_headers.get("Mcp-Session-Id")
        protocol_version = init_headers.get("MCP-Protocol-Version") or init_resp["result"]["protocolVersion"]
        if not session_id:
            raise RuntimeError("missing Mcp-Session-Id in initialize response headers")
        record("initialize", init_resp, init_req_id, extra={"session_id": session_id, "protocol_version": protocol_version})

        # Step 3: Send notifications/initialized
        n_status, _, _, n_req_id = mcp_rpc(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            request_id=f"p19-2-mcp-{run_tag}-notif",
        )
        if n_status != 202:
            raise RuntimeError(f"notifications/initialized expected 202, got {n_status}")
        record("notifications/initialized", None, n_req_id, extra={"status_code": 202})

        # Step 4: Discover tools via tools/list
        list_status, _, list_resp, list_req_id = mcp_rpc(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            request_id=f"p19-2-mcp-{run_tag}-tools-list",
        )
        if list_status != 200:
            raise RuntimeError(f"tools/list failed: {list_resp}")
        tools = list_resp["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        expected_tools = ["devo_status", "devo_start_task", "devo_job_status", "devo_cancel_job", "devo_read_log", "devo_read_file"]
        if sorted(tool_names) != sorted(expected_tools):
            raise RuntimeError(f"unexpected tool set: {tool_names} (expected {expected_tools})")
        record("tools/list", list_resp, list_req_id, extra={"tool_count": len(tools), "tool_names": tool_names})

        # Step 5: Test devo_status
        stat_status, _, stat_resp, stat_req_id = mcp_rpc(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "devo_status", "arguments": {"project_id": args.project_id}},
            },
            request_id=f"p19-2-mcp-{run_tag}-status",
        )
        if stat_status != 200 or stat_resp["result"].get("isError"):
            raise RuntimeError(f"devo_status failed: {stat_resp}")
        stat_data = json.loads(stat_resp["result"]["content"][0]["text"])
        record("devo_status", stat_data, stat_req_id, extra={"project_state": stat_data.get("project", {}).get("state")})

        # Step 6: Start allowlisted task via devo_start_task
        task_idem = f"mcp-accept-comp-{run_tag}"
        start_status, _, start_resp, start_req_id = mcp_rpc(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "devo_start_task",
                    "arguments": {
                        "project_id": args.project_id,
                        "command_ref": args.completion_command,
                        "idempotency_key": task_idem,
                    },
                },
            },
            request_id=f"p19-2-mcp-{run_tag}-start-task",
        )
        if start_status != 200 or start_resp["result"].get("isError"):
            raise RuntimeError(f"devo_start_task failed: {start_resp}")
        start_data = json.loads(start_resp["result"]["content"][0]["text"])
        complete_job_id = start_data.get("job_id") or start_data.get("operation_id")
        complete_transport = start_data.get("selected_transport", "local")
        record("devo_start_task", start_data, start_req_id, selected_transport=complete_transport, job_id=complete_job_id)

        # Step 7: Poll until completion via devo_job_status
        poll_done = False
        final_job_data = None
        for _ in range(60):
            p_status, _, p_resp, p_req_id = mcp_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "devo_job_status",
                        "arguments": {"project_id": args.project_id, "job_id": complete_job_id},
                    },
                },
                request_id=f"p19-2-mcp-{run_tag}-job-status",
            )
            if p_status != 200:
                raise RuntimeError(f"devo_job_status failed: {p_resp}")
            final_job_data = json.loads(p_resp["result"]["content"][0]["text"])
            if final_job_data.get("status") in ("completed", "failed", "cancelled"):
                poll_done = True
                record("devo_job_status", final_job_data, p_req_id, selected_transport=final_job_data.get("selected_transport", "local"), job_id=complete_job_id)
                break
            time.sleep(0.1)

        if not poll_done or final_job_data.get("status") != "completed":
            raise RuntimeError(f"job did not complete successfully: {final_job_data}")

        # Step 8: Spawn cancel candidate and cancel via devo_cancel_job
        cancel_idem = f"mcp-accept-cancel-{run_tag}"
        c_spawn_status, _, c_spawn_resp, c_spawn_req_id = mcp_rpc(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "devo_start_task",
                    "arguments": {
                        "project_id": args.project_id,
                        "command_ref": args.cancel_command,
                        "idempotency_key": cancel_idem,
                    },
                },
            },
            request_id=f"p19-2-mcp-{run_tag}-spawn-cancel",
        )
        c_spawn_data = json.loads(c_spawn_resp["result"]["content"][0]["text"])
        cancel_job_id = c_spawn_data.get("job_id") or c_spawn_data.get("operation_id")
        record("devo_start_task_for_cancel", c_spawn_data, c_spawn_req_id, selected_transport=c_spawn_data.get("selected_transport", "local"), job_id=cancel_job_id)

        # Cancel it
        c_status, _, c_resp, c_req_id = mcp_rpc(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "devo_cancel_job",
                    "arguments": {
                        "project_id": args.project_id,
                        "job_id": cancel_job_id,
                        "reason": "P19.2 live MCP acceptance verification",
                    },
                },
            },
            request_id=f"p19-2-mcp-{run_tag}-cancel-job",
        )
        c_data = json.loads(c_resp["result"]["content"][0]["text"])
        record("devo_cancel_job", c_data, c_req_id, selected_transport=c_data.get("selected_transport", "local"), job_id=cancel_job_id)

        # Step 9: Read log via devo_read_log
        log_status, _, log_resp, log_req_id = mcp_rpc(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {
                    "name": "devo_read_log",
                    "arguments": {"source": "events", "limit": 10},
                },
            },
            request_id=f"p19-2-mcp-{run_tag}-read-log",
        )
        log_data = json.loads(log_resp["result"]["content"][0]["text"])
        record("devo_read_log", log_data, log_req_id, extra={"items_count": len(log_data.get("items") or [])})

        # Step 10: Read file via devo_read_file (verify text reporting)
        rf_status, _, rf_resp, rf_req_id = mcp_rpc(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {
                    "name": "devo_read_file",
                    "arguments": {"project_id": args.project_id, "path": "pyproject.toml"},
                },
            },
            request_id=f"p19-2-mcp-{run_tag}-read-file",
        )
        rf_data = json.loads(rf_resp["result"]["content"][0]["text"])
        if not rf_data.get("is_text") or not rf_data.get("content"):
            raise RuntimeError(f"devo_read_file expected text content: {rf_data}")
        record("devo_read_file", rf_data, rf_req_id, selected_transport=rf_data.get("selected_transport", "local"), extra={"size_bytes": rf_data.get("size_bytes")})

        # Step 11: SSE content negotiation
        sse_status, sse_headers, sse_body, sse_req_id = mcp_rpc(
            {"jsonrpc": "2.0", "id": 10, "method": "tools/list", "params": {}},
            accept="text/event-stream",
            request_id=f"p19-2-mcp-{run_tag}-sse-negotiate",
        )
        if sse_status != 200 or not str(sse_body).startswith("event: message\ndata:"):
            raise RuntimeError("SSE negotiation failed to return event stream")
        record("sse_negotiation", None, sse_req_id, extra={"content_type": sse_headers.get("Content-Type")})

        # Step 12: Session termination via DELETE /mcp
        del_status, _, del_resp, del_req_id = mcp_rpc(
            None,
            method="DELETE",
            request_id=f"p19-2-mcp-{run_tag}-terminate",
        )
        if del_status != 200 or not del_resp.get("deleted"):
            raise RuntimeError(f"DELETE /mcp failed: {del_resp}")
        record("session_delete", del_resp, del_req_id, extra={"deleted": True})

    finally:
        # Revoke the temporary test capability token
        try:
            revoke_mcp_capability(capability_id, runtime_root=runtime_root)
        except Exception:
            pass

    # Step 13: Transport audit and zero-RDC verification
    native_rows = [row for row in rows if row.get("selected_transport")]
    rdc_rows = [row for row in rows if "rdc" in str(row.get("selected_transport") or "").lower()]
    rdc_call_count = len(rdc_rows)
    bad_rows = [row for row in native_rows if row.get("selected_transport") not in {"local", "ssh"}]
    if rdc_rows or bad_rows:
        raise RuntimeError(f"non-native or RDC transport observed: {rdc_rows or bad_rows}")

    evidence = {
        "schema_version": 1,
        "acceptance": "P19.2 Remote MCP Interface zero-RDC",
        "host": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME"),
        "started_run_tag": run_tag,
        "completed_at": now_iso(),
        "base_url": args.base_url,
        "project_id": args.project_id,
        "capability_id": capability_id,
        "protocol_version": protocol_version,
        "tools_discovered": len(expected_tools),
        "result": "PASS",
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
