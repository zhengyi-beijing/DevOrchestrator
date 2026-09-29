"""Real separate-process P19 web console acceptance against daemon port 8770."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def data(result: dict[str, Any]) -> dict[str, Any]:
    value = result.get("data")
    if not isinstance(value, dict):
        raise RuntimeError("Control API response is missing object data")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8770")
    parser.add_argument("--runtime-root", default="runtime")
    parser.add_argument("--project-id", default="devorchestrator")
    parser.add_argument("--output", default="docs/evidence/P19_WEB_CONSOLE_ACCEPTANCE.json")
    parser.add_argument("--operations-output", default="docs/evidence/P19_WEB_CONSOLE_OPERATIONS.ndjson")
    parser.add_argument("--completion-command", default="transport_compileall")
    parser.add_argument("--cancel-command", default="external_control_sleep")
    args = parser.parse_args()

    parsed_url = urllib.parse.urlsplit(args.base_url)
    host_header = parsed_url.netloc
    origin_header = f"{parsed_url.scheme}://{parsed_url.netloc}"
    run_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rows: list[dict[str, Any]] = []

    # Step 1: Establish browser session (same-origin, no master token)
    session_req = urllib.request.Request(
        f"{args.base_url}/api/v1/control/browser-sessions",
        data=b"{}",
        headers={
            "Host": host_header,
            "Origin": origin_header,
            "Sec-Fetch-Site": "same-origin",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(session_req) as resp:
        if resp.status != 201:
            raise RuntimeError(f"failed to establish browser session: status {resp.status}")
        set_cookie = resp.headers.get("Set-Cookie") or ""
        session_body = json.loads(resp.read().decode("utf-8"))
        cookie = set_cookie.split(";")[0].strip()
        csrf_token = session_body["data"]["csrf_token"]

    if not cookie.startswith("devorch_control=") or not csrf_token:
        raise RuntimeError(f"invalid session credentials: cookie={cookie}, csrf={csrf_token}")

    def browser_request(
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        allow_error: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        headers: dict[str, str] = {
            "Host": host_header,
            "Cookie": cookie,
            "X-DevOrch-CSRF": csrf_token,
            "Sec-Fetch-Site": "same-origin",
        }
        if method == "POST":
            headers["Origin"] = origin_header

        data_bytes = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data_bytes = json.dumps(body).encode("utf-8")

        req = urllib.request.Request(f"{args.base_url}{path}", data=data_bytes, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as r:
                r_bytes = r.read().decode("utf-8")
                return r.status, json.loads(r_bytes) if r_bytes else {}
        except urllib.error.HTTPError as exc:
            if not allow_error:
                raise RuntimeError(f"HTTP {exc.code} for {method} {path}: {exc.read().decode('utf-8')}") from exc
            err_bytes = exc.read().decode("utf-8")
            return exc.code, json.loads(err_bytes) if err_bytes else {}

    def record(name: str, response: dict[str, Any], **extra: Any) -> dict[str, Any]:
        body = data(response)
        row = {
            "timestamp": now_iso(),
            "operation": name,
            "request_id": response.get("request_id"),
            "selected_transport": body.get("selected_transport"),
            "status": body.get("status", "ok"),
            **extra,
        }
        rows.append(row)
        return body

    # 1. Authoritative status via browser session
    _, status_resp = browser_request(f"/api/v1/control/external/status?project_id={args.project_id}")
    status_body = record("status", status_resp)
    rows[-1]["lifecycle_state"] = (status_body.get("authoritative_lifecycle") or {}).get("lifecycle_state")
    rows[-1]["active_execution"] = status_body.get("active_execution")
    rows[-1]["owner_gate"] = status_body.get("owner_gate")

    # 2. Command catalog discovery via browser session
    _, cmds_resp = browser_request(f"/api/v1/control/transport/commands?project_id={args.project_id}&host_id=local")
    cmds_body = record("command_catalog", cmds_resp, command_count=len(data(cmds_resp).get("commands") or []))
    available_commands = {c["command_ref"] for c in cmds_body.get("commands") or []}
    if "git_status" not in available_commands:
        raise RuntimeError("git_status not found in discovered commands catalog")

    # 3. Synchronous exec via browser session
    _, exec_resp = browser_request(
        "/api/v1/control/transport/exec",
        method="POST",
        body={
            "project_id": args.project_id,
            "host_id": "local",
            "command_ref": "git_status",
            "idempotency_key": f"p19-web-{run_tag}-git-status",
        },
    )
    exec_body = record("git_status", exec_resp, exit_code=data(exec_resp).get("exit_code"))
    if exec_body.get("status") != "ok":
        raise RuntimeError(f"git_status failed: {exec_body.get('error')}")

    # 4. Asynchronous spawn via browser session
    _, spawn_resp = browser_request(
        "/api/v1/control/transport/spawn",
        method="POST",
        body={
            "project_id": args.project_id,
            "command_ref": args.completion_command,
            "idempotency_key": f"p19-web-{run_tag}-spawn-complete",
        },
    )
    spawned = record("spawn", spawn_resp, command_ref=args.completion_command)
    complete_job_id = str(spawned.get("job_id") or spawned.get("operation_id"))
    rows[-1]["job_id"] = complete_job_id

    # 5. Poll via browser session
    deadline = time.monotonic() + 180
    completed = None
    poll_index = 0
    while time.monotonic() < deadline:
        poll_index += 1
        _, poll_resp = browser_request(
            "/api/v1/control/transport/poll",
            method="POST",
            body={
                "project_id": args.project_id,
                "operation_id": complete_job_id,
            },
        )
        completed = record("poll", poll_resp, job_id=complete_job_id, exit_code=data(poll_resp).get("exit_code"))
        if completed.get("status") in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.2)
    if not completed or completed.get("status") != "completed" or completed.get("exit_code") != 0:
        raise RuntimeError(f"completion job did not succeed: {completed}")

    # 6. File read & stat via browser session
    _, read_resp = browser_request(f"/api/v1/control/transport/read?project_id={args.project_id}&path=README.md&max_bytes=4096")
    read_data = record("read_file", read_resp, content_sha256=data(read_resp).get("content_sha256"))

    _, stat_resp = browser_request(f"/api/v1/control/transport/stat?project_id={args.project_id}&path=README.md")
    record("stat", stat_resp, sha256=data(stat_resp).get("sha256"), size_bytes=data(stat_resp).get("size_bytes"))

    # 7. Binary staging, CAS create, CAS update, and CAS conflict via browser session
    target = f"runtime/p19-web-console/roundtrip-{run_tag}.bin"
    first = b"P19-web-console-binary\x00\xff\xfe\r\n"
    first_hash = "sha256:" + hashlib.sha256(first).hexdigest()
    first_b64 = base64.b64encode(first).decode("ascii")

    _, staged_resp = browser_request(
        "/api/v1/control/transport/stage-write",
        method="POST",
        body={
            "project_id": args.project_id,
            "content_base64": first_b64,
            "content_sha256": first_hash,
            "decoded_size_bytes": len(first),
        },
    )
    staged_data = record("stage_write", staged_resp, content_sha256=first_hash)

    _, created_resp = browser_request(
        "/api/v1/control/transport/write",
        method="POST",
        body={
            "project_id": args.project_id,
            "target_path": target,
            "idempotency_key": f"p19-web-{run_tag}-create",
            "content_ref": staged_data["content_ref"],
            "content_sha256": first_hash,
            "decoded_size_bytes": len(first),
            "if_absent": True,
        },
    )
    record("write_create", created_resp, pre_digest=data(created_resp).get("pre_digest"), post_digest=data(created_resp).get("post_digest"))

    second = b"P19-web-console-updated-payload\x00\x42"
    second_hash = "sha256:" + hashlib.sha256(second).hexdigest()
    second_b64 = base64.b64encode(second).decode("ascii")

    _, staged2_resp = browser_request(
        "/api/v1/control/transport/stage-write",
        method="POST",
        body={
            "project_id": args.project_id,
            "content_base64": second_b64,
            "content_sha256": second_hash,
            "decoded_size_bytes": len(second),
        },
    )
    staged2_data = record("stage_write_update", staged2_resp, content_sha256=second_hash)

    _, updated_resp = browser_request(
        "/api/v1/control/transport/write",
        method="POST",
        body={
            "project_id": args.project_id,
            "target_path": target,
            "idempotency_key": f"p19-web-{run_tag}-update",
            "content_ref": staged2_data["content_ref"],
            "content_sha256": second_hash,
            "decoded_size_bytes": len(second),
            "expected_sha256": first_hash,
        },
    )
    record("cas_update", updated_resp, pre_digest=data(updated_resp).get("pre_digest"), post_digest=data(updated_resp).get("post_digest"))

    # Test CAS conflict
    conflict_status, conflict_resp = browser_request(
        "/api/v1/control/transport/write",
        method="POST",
        body={
            "project_id": args.project_id,
            "target_path": target,
            "idempotency_key": f"p19-web-{run_tag}-conflict",
            "content_ref": staged2_data["content_ref"],
            "content_sha256": second_hash,
            "decoded_size_bytes": len(second),
            "expected_sha256": first_hash,  # stale hash, file is now second_hash
        },
        allow_error=True,
    )
    if conflict_status not in (400, 409):
        raise RuntimeError(f"expected CAS conflict (400/409), got status {conflict_status}")
    rows.append({
        "timestamp": now_iso(),
        "operation": "cas_conflict",
        "status": "conflict_rejected",
        "expected_code": conflict_status,
    })

    # Readback verified
    _, readback_resp = browser_request(f"/api/v1/control/transport/read?project_id={args.project_id}&path={target}")
    record("readback", readback_resp, content_sha256=data(readback_resp).get("content_sha256"))
    if data(readback_resp).get("content_sha256") != second_hash:
        raise RuntimeError("binary CAS readback hash mismatch")

    # 8. Spawn and Cancel via browser session
    _, long_spawn_resp = browser_request(
        "/api/v1/control/transport/spawn",
        method="POST",
        body={
            "project_id": args.project_id,
            "command_ref": args.cancel_command,
            "idempotency_key": f"p19-web-{run_tag}-spawn-cancel",
        },
    )
    long_data = record("spawn_cancel_target", long_spawn_resp, command_ref=args.cancel_command)
    cancel_job_id = str(long_data.get("job_id") or long_data.get("operation_id"))
    rows[-1]["job_id"] = cancel_job_id

    _, cancel_resp = browser_request(
        "/api/v1/control/transport/cancel",
        method="POST",
        body={
            "project_id": args.project_id,
            "operation_id": cancel_job_id,
            "reason": "P19 harmless web console acceptance cancellation",
        },
    )
    record("cancel", cancel_resp, job_id=cancel_job_id)

    # 9. Audit operations read
    _, ops_resp = browser_request("/api/v1/control/transport/operations?limit=10")
    record("operations_read", ops_resp, count=len(data(ops_resp).get("operations") or []))

    # Transport audit validation
    native_rows = [row for row in rows if row.get("selected_transport")]
    rdc_rows = [row for row in native_rows if "rdc" in str(row.get("selected_transport") or "").lower()]
    bad_rows = [row for row in native_rows if row.get("selected_transport") not in {"local", "ssh"}]
    if rdc_rows or bad_rows:
        raise RuntimeError(f"non-native transport observed: {rdc_rows or bad_rows}")

    evidence = {
        "schema_version": 1,
        "acceptance": "P19 Remote HTTP/HTTPS Gateway + Web UI zero-RDC",
        "host": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME"),
        "started_run_tag": run_tag,
        "completed_at": now_iso(),
        "base_url": args.base_url,
        "project_id": args.project_id,
        "result": "PASS",
        "native_transport_operation_count": len(native_rows),
        "selected_transports": sorted({str(row["selected_transport"]) for row in native_rows}),
        "rdc_call_count": 0,
        "complete_job_id": complete_job_id,
        "cancel_job_id": cancel_job_id,
        "binary_create_sha256": first_hash,
        "binary_final_sha256": second_hash,
        "read_probe_sha256": read_data.get("content_sha256"),
        "operations": rows,
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    op_output = Path(args.operations_output)
    op_output.parent.mkdir(parents=True, exist_ok=True)
    op_output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

