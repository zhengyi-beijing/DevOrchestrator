"""Real separate-process P18 external-control acceptance against daemon port 8770."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dev_orchestrator.control.adapter import ControlAdapterClient


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
    parser.add_argument("--output", default="docs/evidence/P18_EXTERNAL_CONTROL_ZERO_RDC_ACCEPTANCE.json")
    parser.add_argument("--operations-output", default="docs/evidence/P18_EXTERNAL_CONTROL_OPERATIONS.ndjson")
    parser.add_argument("--completion-command", default="transport_compileall")
    parser.add_argument("--cancel-command", default="external_control_sleep")
    args = parser.parse_args()

    runtime = Path(args.runtime_root).resolve()
    token = (runtime / "control" / "api-token").read_text(encoding="utf-8").strip()
    client = ControlAdapterClient(base_url=args.base_url, token=token, timeout_seconds=20)
    run_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rows: list[dict[str, Any]] = []

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

    status = record(
        "status",
        client.external_status(args.project_id),
        lifecycle_state=None,
    )
    rows[-1]["lifecycle_state"] = (status.get("authoritative_lifecycle") or {}).get("lifecycle_state")
    rows[-1]["active_execution"] = status.get("active_execution")
    rows[-1]["owner_gate"] = status.get("owner_gate")

    logs = client.logs(args.project_id, limit=5)
    rows.append({
        "timestamp": now_iso(), "operation": "log_read", "request_id": logs.get("request_id"),
        "selected_transport": None, "status": "ok", "item_count": len((logs.get("data") or {}).get("items") or []),
    })

    for command in ("git_log", "git_status"):
        response = client.transport_exec(
            project_id=args.project_id,
            command_ref=command,
            idempotency_key=f"p18-external-{run_tag}-{command}",
            request_id=f"p18-ext-{run_tag}-{command}",
        )
        body = record(command, response, exit_code=data(response).get("exit_code"))
        if body.get("status") != "ok":
            raise RuntimeError(f"{command} failed: {body.get('error')}")

    spawn = client.transport_spawn(
        project_id=args.project_id,
        command_ref=args.completion_command,
        idempotency_key=f"p18-external-{run_tag}-complete",
        request_id=f"p18-ext-{run_tag}-spawn-complete",
    )
    spawned = record("spawn", spawn, command_ref=args.completion_command)
    complete_job_id = str(spawned.get("job_id") or spawned.get("operation_id"))
    rows[-1]["job_id"] = complete_job_id
    deadline = time.monotonic() + 180
    completed = None
    poll_index = 0
    while time.monotonic() < deadline:
        poll_index += 1
        response = client.transport_poll(
            operation_id=complete_job_id, project_id=args.project_id,
            request_id=f"p18-ext-{run_tag}-poll-{poll_index}",
        )
        completed = record("poll", response, job_id=complete_job_id, exit_code=data(response).get("exit_code"))
        if completed.get("status") in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.2)
    if not completed or completed.get("status") != "completed" or completed.get("exit_code") != 0:
        raise RuntimeError(f"completion job did not succeed: {completed}")

    read = client.transport_read_file(
        project_id=args.project_id, path="README.md", max_bytes=4096,
        request_id=f"p18-ext-{run_tag}-read",
    )
    read_data = record("read_file", read, content_sha256=data(read).get("content_sha256"))
    stat = client.transport_stat(
        project_id=args.project_id, path="README.md",
        request_id=f"p18-ext-{run_tag}-stat",
    )
    record("stat", stat, sha256=data(stat).get("sha256"), size_bytes=data(stat).get("size_bytes"))

    target = f"runtime/p18-external-control/roundtrip-{run_tag}.bin"
    first = b"P18-external-control\x00\xff\r\n"
    first_hash = "sha256:" + hashlib.sha256(first).hexdigest()
    staged = client.transport_stage_write(
        project_id=args.project_id, content=first, content_sha256=first_hash,
        request_id=f"p18-ext-{run_tag}-stage-create",
    )
    staged_data = record("stage_write", staged, content_sha256=first_hash)
    created = client.transport_write_file(
        project_id=args.project_id, target_path=target,
        idempotency_key=f"p18-external-{run_tag}-create",
        content_ref=staged_data["content_ref"], content_sha256=first_hash,
        decoded_size_bytes=len(first), if_absent=True,
        request_id=f"p18-ext-{run_tag}-write-create",
    )
    record("write_create", created, pre_digest=data(created).get("pre_digest"), post_digest=data(created).get("post_digest"))

    second = b"P18-external-control-updated\x00\x10"
    second_hash = "sha256:" + hashlib.sha256(second).hexdigest()
    staged2 = client.transport_stage_write(
        project_id=args.project_id, content=second, content_sha256=second_hash,
        request_id=f"p18-ext-{run_tag}-stage-update",
    )
    staged2_data = record("stage_write_update", staged2, content_sha256=second_hash)
    updated = client.transport_write_file(
        project_id=args.project_id, target_path=target,
        idempotency_key=f"p18-external-{run_tag}-update",
        content_ref=staged2_data["content_ref"], content_sha256=second_hash,
        decoded_size_bytes=len(second), expected_sha256=first_hash,
        request_id=f"p18-ext-{run_tag}-write-update",
    )
    record("cas_update", updated, pre_digest=data(updated).get("pre_digest"), post_digest=data(updated).get("post_digest"))
    readback = client.transport_read_file(
        project_id=args.project_id, path=target,
        request_id=f"p18-ext-{run_tag}-readback",
    )
    record("readback", readback, content_sha256=data(readback).get("content_sha256"))
    if data(readback).get("content_sha256") != second_hash:
        raise RuntimeError("binary CAS readback hash mismatch")

    long_spawn = client.transport_spawn(
        project_id=args.project_id, command_ref=args.cancel_command,
        idempotency_key=f"p18-external-{run_tag}-cancel",
        request_id=f"p18-ext-{run_tag}-spawn-cancel",
    )
    long_data = record("spawn_cancel_target", long_spawn, command_ref=args.cancel_command)
    cancel_job_id = str(long_data.get("job_id") or long_data.get("operation_id"))
    rows[-1]["job_id"] = cancel_job_id
    cancel = client.transport_cancel(
        operation_id=cancel_job_id, project_id=args.project_id, reason="P18 harmless acceptance cancellation",
        request_id=f"p18-ext-{run_tag}-cancel",
    )
    record("cancel", cancel, job_id=cancel_job_id)

    native_rows = [row for row in rows if row.get("selected_transport")]
    rdc_rows = [row for row in native_rows if "rdc" in str(row.get("selected_transport") or "").lower()]
    bad_rows = [row for row in native_rows if row.get("selected_transport") not in {"local", "ssh"}]
    if rdc_rows or bad_rows:
        raise RuntimeError(f"non-native transport observed: {rdc_rows or bad_rows}")

    evidence = {
        "schema_version": 1,
        "acceptance": "P18 external control zero-RDC",
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
