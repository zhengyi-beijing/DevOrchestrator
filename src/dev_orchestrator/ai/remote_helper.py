"""Fixed remote DevOrchestrator helper process.

Executed remotely via `python -m dev_orchestrator.ai.remote_helper` over OpenSSH.
Accepts structured JSON requests on stdin, dispatches local broker operations,
and returns structured JSON on stdout with correlated request_id and host_identity.
Prohibits arbitrary shell commands or path operations.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4


from dev_orchestrator.ai.aibroker_subprocess import (
    _SERVICE_OPENER,
    sanitize_url,
    validate_loopback_url,
)


def _build_env(broker_repo: str | None) -> dict[str, str]:
    env = dict(os.environ)
    if broker_repo:
        broker_src = str(Path(broker_repo) / "src")
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = broker_src + (os.pathsep + existing if existing else "")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _service_call(
    service_url: str,
    endpoint: str,
    body: dict[str, Any] | None,
    token: str | None,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    base = validate_loopback_url(service_url)
    url = base + endpoint
    safe_target = sanitize_url(url)
    req_headers = {"Accept": "application/json"}
    if token:
        req_headers["X-AIResourceBroker-Token"] = token
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    if data is not None:
        req_headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=req_headers, method="POST" if body is not None else "GET")
    try:
        with _SERVICE_OPENER.open(req, timeout=timeout_seconds) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            try:
                raw = exc.read().decode("utf-8")
                parsed_404 = json.loads(raw)
                if isinstance(parsed_404, dict) and parsed_404.get("status") == "not_found":
                    return parsed_404
            except Exception:
                pass
            return {"status": "not_found"}
        if 300 <= exc.code < 400:
            raise RuntimeError(f"broker service redirect not permitted ({exc.code})") from exc
        err_body = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(err_body)
        except Exception:
            err_msg = f"broker service error {exc.code} for {safe_target}: {err_body}"
            if token and token in err_msg:
                err_msg = err_msg.replace(token, "[REDACTED]")
            raise RuntimeError(err_msg) from exc
    except (OSError, urllib.error.URLError) as exc:
        err_msg = str(exc)
        if token and token in err_msg:
            err_msg = err_msg.replace(token, "[REDACTED]")
        raise RuntimeError(f"broker service invocation failed for {safe_target}: {err_msg}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"broker service returned invalid JSON for {safe_target}") from exc


def _resolve_project_target_path(jobs_cfg: Any, project_id: str, path_str: str) -> tuple[Path, Path]:
    proj = jobs_cfg.projects.get(project_id)
    if proj is None:
        raise ValueError(f"unknown project_id: {project_id!r}")
    from dev_orchestrator.jobs.config import is_path_contained
    target = Path(path_str)
    allowed_roots = [proj.repo_path] + list(proj.file_roots)
    if target.is_absolute():
        for root in allowed_roots:
            if is_path_contained(root, target):
                return target, root
    else:
        cand = (proj.repo_path / target).resolve()
        for root in allowed_roots:
            if is_path_contained(root, cand):
                return cand, root
    raise ValueError(f"path {path_str!r} escapes project containment")


def _retry_chain_job_id(
    project_id: str,
    idempotency_key: str,
    *,
    job_id_for: Any,
    retry_successor_id: Any,
) -> str:
    """Derive a chained retry job id by folding every retry request segment."""
    parts = idempotency_key.split(":retry:")
    job_id = job_id_for({"project_id": project_id, "idempotency_key": parts[0]})
    for retry_request_id in parts[1:]:
        job_id = retry_successor_id(job_id, retry_request_id)
    return job_id


def execute_request(req: dict[str, Any]) -> dict[str, Any]:
    operation = req.get("operation")
    req_id = req.get("request_id")
    if not operation or not req_id:
        raise ValueError("operation and request_id are required")

    _MACHINE_OPERATIONS = frozenset({
        "op_resolve",
        "op_exec",
        "op_spawn",
        "op_poll",
        "op_cancel",
        "op_read_file",
        "op_stage_write",
        "op_write_file",
        "op_stat",
        "op_capabilities",
    })
    _JOB_OPERATIONS = frozenset({"job_start", "job_status", "job_logs", "job_cancel", "job_artifact"})

    if operation in _MACHINE_OPERATIONS or operation in _JOB_OPERATIONS:
        FORBIDDEN_WIRE_KEYS = frozenset({"argv", "cwd", "env", "shell", "effect_class", "file_roots"})
        forbidden = sorted(FORBIDDEN_WIRE_KEYS & set(req.keys()))
        if forbidden:
            raise ValueError(f"forbidden wire authority parameter in request: {forbidden}")

    if operation in _MACHINE_OPERATIONS:
        _ALLOWED_MACHINE_OP_FIELDS = {
            "op_resolve": frozenset({"operation", "request_id", "project_id", "command_ref", "parameters", "expected_working_directory"}),
            "op_exec": frozenset({"operation", "request_id", "project_id", "command_ref", "parameters", "expected_working_directory", "timeout_seconds"}),
            "op_spawn": frozenset({"operation", "request_id", "project_id", "command_ref", "parameters", "expected_working_directory", "idempotency_key", "job_id", "input_payload", "input_digest", "job_spec", "controller_spec_hash", "retry_of", "retry_request_id"}),
            "op_poll": frozenset({"operation", "request_id", "job_id"}),
            "op_cancel": frozenset({"operation", "request_id", "job_id", "reason"}),
            "op_read_file": frozenset({"operation", "request_id", "project_id", "path", "max_bytes", "offset_bytes"}),
            "op_stage_write": frozenset({"operation", "request_id", "project_id", "content_base64", "decoded_size_bytes", "content_sha256"}),
            "op_write_file": frozenset({"operation", "request_id", "project_id", "target_path", "idempotency_key", "content_ref", "content_sha256", "decoded_size_bytes", "if_absent", "expected_sha256", "expected_file_policy_digest"}),
            "op_stat": frozenset({"operation", "request_id", "path", "project_id"}),
            "op_capabilities": frozenset({"operation", "request_id"}),
        }
        unknown = sorted(set(req.keys()) - _ALLOWED_MACHINE_OP_FIELDS[operation])
        if unknown:
            raise ValueError(f"unknown fields in remote operation {operation}: {unknown}")

        from dev_orchestrator.jobs.config import (
            is_path_contained,
            load_jobs_config,
            resolve_execution_policy,
            resolve_remote_jobs_config_path,
        )
        from dev_orchestrator.jobs.models import JobRecord, JobSpec, job_id_for, retry_successor_id, spec_hash
        from dev_orchestrator.jobs.store import ExecutionJobStore
        from dev_orchestrator.jobs.transport import LocalJobTransport
        from dev_orchestrator.storage.json_store import utc_now_iso

        cfg_path = resolve_remote_jobs_config_path()
        jobs_cfg = load_jobs_config(cfg_path)
        if jobs_cfg is None and operation != "op_capabilities":
            raise RuntimeError(f"host-local jobs configuration absent or disabled at {cfg_path}")

        if operation == "op_capabilities":
            from dev_orchestrator.transport.hosts import discover_local_capabilities
            caps = discover_local_capabilities(jobs_cfg)
            return {
                "status": "ok",
                "capabilities": {
                    "host_id": caps.host_id,
                    "os_family": caps.os_family,
                    "path_style": caps.path_style,
                    "helper_version": caps.helper_version,
                    "jobs_config_valid": caps.jobs_config_valid,
                    "approved_policy_pins": caps.approved_policy_pins,
                    "response_limits": caps.response_limits,
                    "supported_operations": caps.supported_operations,
                    "probed_at": caps.probed_at,
                },
            }

        assert jobs_cfg is not None

        if operation == "op_resolve":
            proj_id = req.get("project_id")
            cmd_ref = req.get("command_ref")
            if not proj_id or not cmd_ref:
                raise ValueError("project_id and command_ref are required for op_resolve")
            ok, failure_kind, resolved = resolve_execution_policy(
                jobs_cfg,
                proj_id,
                cmd_ref,
                parameters=req.get("parameters"),
                expected_working_directory=req.get("expected_working_directory"),
            )
            if not ok:
                if failure_kind and "hardware" in failure_kind:
                    raise ValueError("hardware_execution_not_supported_in_p18")
                raise ValueError(f"execution resolution failed: {failure_kind}")
            assert resolved is not None
            if resolved.effect_class == "hardware":
                raise ValueError("hardware_execution_not_supported_in_p18")
            return {
                "status": "ok",
                "project_id": proj_id,
                "command_ref": cmd_ref,
                "effect_class": resolved.effect_class,
                "duration_class": resolved.duration_class,
                "max_runtime_seconds": resolved.max_runtime_seconds,
                "heartbeat_interval_seconds": resolved.heartbeat_interval_seconds,
                "resolved_cwd": str(resolved.resolved_cwd),
                "resolved_argv": resolved.resolved_argv,
                "parameters_digest": resolved.parameters_digest,
                "execution_policy_digest": resolved.execution_policy_digest,
                "resolution_digest": resolved.resolution_digest,
            }

        if operation == "op_exec":
            proj_id = req.get("project_id")
            cmd_ref = req.get("command_ref")
            if not proj_id or not cmd_ref:
                raise ValueError("project_id and command_ref are required for op_exec")
            ok, failure_kind, resolved = resolve_execution_policy(
                jobs_cfg,
                proj_id,
                cmd_ref,
                parameters=req.get("parameters"),
                expected_working_directory=req.get("expected_working_directory"),
            )
            if not ok:
                if failure_kind and "hardware" in failure_kind:
                    raise ValueError("hardware_execution_not_supported_in_p18")
                raise ValueError(f"execution resolution failed: {failure_kind}")
            assert resolved is not None
            if resolved.effect_class == "hardware":
                raise ValueError("hardware_execution_not_supported_in_p18")
            if resolved.effect_class != "read_only":
                raise ValueError(f"op_exec only supports read_only commands, found effect_class: {resolved.effect_class!r}")

            timeout = float(req.get("timeout_seconds") or resolved.max_runtime_seconds)
            try:
                completed = subprocess.run(
                    resolved.resolved_argv,
                    cwd=resolved.resolved_cwd,
                    timeout=timeout,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                return {
                    "status": "ok" if completed.returncode == 0 else "failed",
                    "exit_code": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                    "parameters_digest": resolved.parameters_digest,
                    "execution_policy_digest": resolved.execution_policy_digest,
                    "resolution_digest": resolved.resolution_digest,
                }
            except subprocess.TimeoutExpired as exc:
                return {
                    "status": "timeout",
                    "exit_code": -1,
                    "stdout": exc.stdout if isinstance(exc.stdout, str) else "",
                    "stderr": exc.stderr if isinstance(exc.stderr, str) else "",
                    "error": f"command timed out after {timeout} seconds",
                    "parameters_digest": resolved.parameters_digest,
                    "execution_policy_digest": resolved.execution_policy_digest,
                    "resolution_digest": resolved.resolution_digest,
                }

        if operation == "op_spawn":
            incoming_spec = req.get("job_spec")
            if isinstance(incoming_spec, dict):
                spec = JobSpec.from_dict(incoming_spec)
                if incoming_spec != spec.to_canonical_dict():
                    raise ValueError("job_spec must match canonical dictionary byte-for-byte")
                computed_shash = spec_hash(spec)
                c_spec_hash = req.get("controller_spec_hash") or req.get("spec_hash")
                if c_spec_hash and c_spec_hash != computed_shash:
                    raise ValueError(f"controller_spec_hash mismatch ({c_spec_hash} != {computed_shash})")
                proj_id = spec.project_id
                cmd_ref = spec.command_ref
                idem_key = spec.idempotency_key
                expected_cwd = spec.expected_working_directory
            else:
                proj_id = req.get("project_id")
                cmd_ref = req.get("command_ref")
                if not proj_id or not cmd_ref:
                    raise ValueError("project_id and command_ref are required for op_spawn")
                expected_cwd = req.get("expected_working_directory")
                idem_key = req.get("idempotency_key") or req.get("job_id")
                if not idem_key:
                    raise ValueError("job_id or idempotency_key is required")
                spec = None

            params = req.get("parameters")
            ok, failure_kind, resolved = resolve_execution_policy(
                jobs_cfg,
                proj_id,
                cmd_ref,
                parameters=params,
                expected_working_directory=expected_cwd,
            )
            if not ok:
                if failure_kind and "hardware" in failure_kind:
                    raise ValueError("hardware_execution_not_supported_in_p18")
                raise ValueError(f"execution resolution failed: {failure_kind}")
            assert resolved is not None
            if resolved.effect_class == "hardware":
                raise ValueError("hardware_execution_not_supported_in_p18")

            if spec is not None:
                if (params or req.get("parameters")) and not spec.parameters_digest:
                    raise ValueError("supplied parameters but job_spec missing parameters_digest")
                if (
                    spec.parameters_digest and spec.parameters_digest != resolved.parameters_digest
                    or spec.execution_policy_digest and spec.execution_policy_digest != resolved.execution_policy_digest
                    or spec.resolution_digest and spec.resolution_digest != resolved.resolution_digest
                ):
                    raise ValueError("tampered job_spec digest mismatch with host-local policy")
            else:
                input_digest = req.get("input_digest")
                spec = JobSpec(
                    project_id=proj_id,
                    command_ref=cmd_ref,
                    idempotency_key=str(idem_key),
                    expected_working_directory=expected_cwd,
                    input_digest=str(input_digest) if input_digest else None,
                    parameters_digest=resolved.parameters_digest,
                    execution_policy_digest=resolved.execution_policy_digest,
                    resolution_digest=resolved.resolution_digest,
                )

            is_retry = bool(req.get("retry_of") and req.get("retry_request_id"))
            if is_retry:
                expected_jid = retry_successor_id(str(req["retry_of"]), str(req["retry_request_id"]))
            elif spec is not None and ":retry:" in (spec.idempotency_key or ""):
                expected_jid = _retry_chain_job_id(
                    spec.project_id,
                    spec.idempotency_key,
                    job_id_for=job_id_for,
                    retry_successor_id=retry_successor_id,
                )
            elif idem_key and ":retry:" in str(idem_key):
                expected_jid = _retry_chain_job_id(
                    str(proj_id),
                    str(idem_key),
                    job_id_for=job_id_for,
                    retry_successor_id=retry_successor_id,
                )
            else:
                expected_jid = job_id_for(spec) if spec is not None else job_id_for({"project_id": proj_id, "idempotency_key": str(idem_key)})
            wire_jid = req.get("job_id")
            if wire_jid and wire_jid != expected_jid:
                raise ValueError(f"unauthorized job_id {wire_jid!r}, expected {expected_jid!r}")
            target_job_id = expected_jid

            store = ExecutionJobStore(jobs_cfg.runtime_root)
            local_transport = LocalJobTransport()

            def _factory(jid: str, shash: str) -> JobRecord:
                now = utc_now_iso()
                jdir = store._job_dir(jid)
                resolved_argv = [arg.replace("{job_dir}", str(jdir)) for arg in resolved.resolved_argv]
                return JobRecord(
                    job_id=jid,
                    idempotency_key=spec.idempotency_key,
                    spec_hash=shash,
                    kind=spec.kind,
                    project_id=spec.project_id,
                    command_ref=spec.command_ref,
                    resolved_argv=resolved_argv,
                    working_directory=str(resolved.resolved_cwd),
                    transport=spec.transport,
                    host_identity=socket.gethostname(),
                    duration_class=resolved.duration_class,
                    max_runtime_seconds=resolved.max_runtime_seconds,
                    heartbeat_interval_seconds=resolved.heartbeat_interval_seconds,
                    log_caps=dict(jobs_cfg.log_caps),
                    input_digest=spec.input_digest,
                    submission_spec=spec.to_canonical_dict(),
                    parameters=resolved.canonical_parameters,
                    parameters_digest=resolved.parameters_digest,
                    execution_policy_digest=resolved.execution_policy_digest,
                    resolution_digest=resolved.resolution_digest,
                    state="queued",
                    task_id=spec.task_id,
                    stage_run_id=spec.stage_run_id,
                    role_run_id=spec.role_run_id,
                    source_request_id=spec.source_request_id,
                    broker_request_id=spec.broker_request_id,
                    timestamps={"created_at": now, "queued_at": now, "updated_at": now},
                )

            record, is_new = store.claim_or_get(spec, _factory, target_job_id=target_job_id)
            if is_new:
                input_payload = req.get("input_payload")
                if input_payload is not None:
                    store.save_input_artifact(record.job_id, input_payload)
                start_res = local_transport.job_start(spec, store._job_dir(record.job_id))
                sup_pid = start_res.get("supervisor_pid") if isinstance(start_res, dict) else None
                if isinstance(sup_pid, int) and sup_pid > 0:
                    def _record_pid(rec: JobRecord) -> None:
                        rec.supervisor["pid"] = sup_pid
                    store.update(record.job_id, _record_pid)

            return {
                "status": "ok",
                "job_id": record.job_id,
                "already_exists": not is_new,
                "parameters_digest": resolved.parameters_digest,
                "execution_policy_digest": resolved.execution_policy_digest,
                "resolution_digest": resolved.resolution_digest,
            }

        if operation == "op_poll":
            job_id = req.get("job_id")
            if not job_id:
                raise ValueError("job_id is required for op_poll")
            store = ExecutionJobStore(jobs_cfg.runtime_root)
            local_transport = LocalJobTransport()
            record = store.get(job_id)
            status_res = local_transport.job_status(job_id, store._job_dir(job_id))
            return {
                "status": "ok",
                "job_id": job_id,
                "state": record.state if record else status_res.get("status", "unknown"),
                "exit_code": record.exit_code if record else None,
                "parameters_digest": record.parameters_digest if record else None,
                "execution_policy_digest": record.execution_policy_digest if record else None,
                "resolution_digest": record.resolution_digest if record else None,
                "details": status_res,
            }

        if operation == "op_cancel":
            job_id = req.get("job_id")
            if not job_id:
                raise ValueError("job_id is required for op_cancel")
            reason = str(req.get("reason") or "cancelled")
            store = ExecutionJobStore(jobs_cfg.runtime_root)
            local_transport = LocalJobTransport()
            cancel_res = local_transport.job_cancel(job_id, store._job_dir(job_id), reason=reason)
            return {
                "status": "ok",
                "job_id": job_id,
                "reason": reason,
                "details": cancel_res,
            }

        if operation == "op_read_file":
            proj_id = req.get("project_id")
            path_str = req.get("path")
            if not proj_id or not path_str:
                raise ValueError("project_id and path are required for op_read_file")
            resolved, _ = _resolve_project_target_path(jobs_cfg, proj_id, path_str)
            if not resolved.is_file():
                return {"status": "failed", "path": path_str, "error": f"file not found: {path_str}"}
            max_bytes = int(req.get("max_bytes") or 10 * 1024 * 1024)
            offset_bytes = int(req.get("offset_bytes") or 0)
            with open(resolved, "rb") as f:
                if offset_bytes:
                    f.seek(offset_bytes)
                data = f.read(max_bytes)
            sha = "sha256:" + hashlib.sha256(data).hexdigest()
            return {
                "status": "ok",
                "path": str(resolved),
                "size_bytes": len(data),
                "sha256": sha,
                "content_base64": base64.b64encode(data).decode("ascii"),
            }

        if operation == "op_stage_write":
            proj_id = req.get("project_id")
            b64_str = req.get("content_base64")
            sha = req.get("content_sha256")
            size_b = int(req.get("decoded_size_bytes") or 0)
            if not proj_id or not b64_str or not sha:
                raise ValueError("project_id, content_base64, and content_sha256 are required for op_stage_write")
            from dev_orchestrator.transport.contracts import WriteContentUpload
            upload = WriteContentUpload(
                project_id=proj_id,
                host_id=req.get("host_id") or socket.gethostname(),
                content_base64=b64_str,
                decoded_size_bytes=size_b,
                content_sha256=sha,
            )
            store = ExecutionJobStore(jobs_cfg.runtime_root)
            proj = jobs_cfg.projects.get(proj_id)
            max_bytes = proj.max_file_write_bytes if proj else 8 * 1024 * 1024
            staged = store.stage_write_content(upload, max_bytes=max_bytes)
            return {
                "status": "ok",
                "content_ref": staged.content_ref,
                "content_sha256": staged.content_sha256,
                "decoded_size_bytes": staged.decoded_size_bytes,
                "project_id": staged.project_id,
                "host_id": socket.gethostname(),
                "staged_at": staged.staged_at,
                "expires_at": staged.expires_at,
            }

        if operation == "op_write_file":
            proj_id = req.get("project_id")
            target_p = req.get("target_path")
            content_ref = req.get("content_ref")
            if not proj_id or not target_p or not content_ref:
                raise ValueError("project_id, target_path, and content_ref are required for op_write_file")
            resolved, root = _resolve_project_target_path(jobs_cfg, proj_id, target_p)
            proj = jobs_cfg.projects.get(proj_id)
            allowed_roots = ([proj.repo_path] + list(proj.file_roots)) if proj else [root]
            from dev_orchestrator.transport.contracts import FileWriteRequest
            import dataclasses
            write_req = FileWriteRequest(
                project_id=proj_id,
                host_id=req.get("host_id") or socket.gethostname(),
                target_path=str(resolved),
                idempotency_key=str(req.get("idempotency_key") or uuid4()),
                content_ref=content_ref,
                content_sha256=req.get("content_sha256") or "",
                decoded_size_bytes=int(req.get("decoded_size_bytes") or 0),
                if_absent=req.get("if_absent"),
                expected_sha256=req.get("expected_sha256"),
                expected_file_policy_digest=req.get("expected_file_policy_digest"),
            )
            store = ExecutionJobStore(jobs_cfg.runtime_root)
            f_res = store.apply_file_write(
                write_req,
                [str(r) for r in allowed_roots],
                host_identity=socket.gethostname(),
            )
            res_dict = dataclasses.asdict(f_res)
            if "content_bytes" in res_dict:
                del res_dict["content_bytes"]
            res_dict["target_path"] = f_res.path
            return res_dict

        if operation == "op_stat":
            path_str = req.get("path")
            if not path_str:
                raise ValueError("path is required for op_stat")
            proj_id = req.get("project_id")
            allowed_roots = []
            if proj_id:
                proj = jobs_cfg.projects.get(proj_id)
                if proj is None:
                    raise ValueError(f"unknown project_id: {proj_id!r}")
                allowed_roots = [proj.repo_path] + list(proj.file_roots)
            else:
                for p in jobs_cfg.projects.values():
                    allowed_roots.append(p.repo_path)
                    allowed_roots.extend(p.file_roots)

            cand = Path(path_str)
            resolved_cand = None
            if cand.is_absolute():
                for root in allowed_roots:
                    if is_path_contained(root, cand):
                        resolved_cand = cand
                        break
            else:
                for root in allowed_roots:
                    c = (root / cand).resolve()
                    if is_path_contained(root, c):
                        resolved_cand = c
                        break

            if resolved_cand is None:
                raise ValueError(f"path {path_str!r} escapes allowed roots containment")

            if not resolved_cand.exists():
                return {"status": "ok", "path": path_str, "exists": False}

            is_f = resolved_cand.is_file()
            is_d = resolved_cand.is_dir()
            st = resolved_cand.stat()
            mtime_iso = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()
            sha_val = None
            if is_f and st.st_size <= 10 * 1024 * 1024:
                sha_val = "sha256:" + hashlib.sha256(resolved_cand.read_bytes()).hexdigest()

            return {
                "status": "ok",
                "path": str(resolved_cand),
                "exists": True,
                "is_file": is_f,
                "is_dir": is_d,
                "size_bytes": st.st_size if is_f else None,
                "modified_at": mtime_iso,
                "sha256": sha_val,
            }

    _JOB_OPERATIONS = frozenset({"job_start", "job_status", "job_logs", "job_cancel", "job_artifact"})
    if operation in _JOB_OPERATIONS:
        _ALLOWED_JOB_FIELDS = frozenset({
            "operation",
            "request_id",
            "job_id",
            "project_id",
            "command_ref",
            "idempotency_key",
            "cursor",
            "limit",
            "reason",
            "expected_working_directory",
            "artifact_name",
            "input_payload",
            "input_digest",
            "parameters",
            "job_spec",
            "spec_hash",
            "controller_spec_hash",
            "retry_of",
            "retry_request_id",
            "parameters_digest",
            "execution_policy_digest",
            "resolution_digest",
        })
        unknown = sorted(set(req.keys()) - _ALLOWED_JOB_FIELDS)
        if unknown:
            raise ValueError(f"unknown fields in remote job request: {unknown}")

        from dev_orchestrator.jobs.config import (
            load_jobs_config,
            resolve_execution_policy,
            resolve_remote_jobs_config_path,
            validate_and_resolve_execution,
        )
        from dev_orchestrator.jobs.models import (
            JobRecord,
            JobSpec,
            job_id_for,
            retry_successor_id,
            spec_hash,
        )
        from dev_orchestrator.jobs.store import ExecutionJobStore
        from dev_orchestrator.jobs.transport import LocalJobTransport
        from dev_orchestrator.storage.json_store import utc_now_iso

        cfg_path = resolve_remote_jobs_config_path()
        jobs_cfg = load_jobs_config(cfg_path)
        if jobs_cfg is None:
            raise RuntimeError(f"host-local jobs configuration absent or disabled at {cfg_path}")

        store = ExecutionJobStore(jobs_cfg.runtime_root)
        local_transport = LocalJobTransport()

        if operation == "job_start":
            proj_id = req.get("project_id")
            cmd_ref = req.get("command_ref")
            idem_key = req.get("idempotency_key") or req.get("job_id")
            incoming_spec = req.get("job_spec")
            controller_hash = req.get("controller_spec_hash")
            spec = None
            if incoming_spec is not None:
                if not isinstance(incoming_spec, dict):
                    raise ValueError("job_spec must be a JSON object")
                spec = JobSpec.from_dict(incoming_spec)
                if incoming_spec != spec.to_canonical_dict():
                    raise ValueError("job_spec was modified in transit or not in canonical form")
                calc_hash = spec_hash(spec)
                if controller_hash and controller_hash != calc_hash:
                    raise ValueError(f"controller_spec_hash mismatch: expected {calc_hash}, got {controller_hash}")
                proj_id = spec.project_id
                cmd_ref = spec.command_ref
                idem_key = spec.idempotency_key
                expected_cwd = spec.expected_working_directory
                params = spec.metadata.get("parameters") or req.get("parameters")
            else:
                if not proj_id or not cmd_ref:
                    raise ValueError("project_id and command_ref are required for job_start")
                expected_cwd = req.get("expected_working_directory")
                params = req.get("parameters")

            ok, failure_kind, resolved = resolve_execution_policy(
                jobs_cfg, proj_id, cmd_ref, parameters=params, expected_working_directory=expected_cwd
            )
            if not ok:
                if failure_kind and "hardware" in failure_kind:
                    raise ValueError("hardware_execution_not_supported_in_p18")
                raise ValueError(f"execution validation failed: {failure_kind}")
            assert resolved is not None
            if resolved.effect_class == "hardware":
                raise ValueError("hardware_execution_not_supported_in_p18")

            if spec is not None:
                if (params or req.get("parameters")) and not spec.parameters_digest:
                    raise ValueError("supplied parameters but job_spec missing parameters_digest")
                if (
                    spec.parameters_digest and spec.parameters_digest != resolved.parameters_digest
                    or spec.execution_policy_digest and spec.execution_policy_digest != resolved.execution_policy_digest
                    or spec.resolution_digest and spec.resolution_digest != resolved.resolution_digest
                ):
                    raise ValueError("tampered job_spec digest mismatch with host-local policy")
            else:
                if not idem_key:
                    raise ValueError("job_id or idempotency_key is required")
                input_digest = req.get("input_digest")
                spec = JobSpec(
                    project_id=proj_id,
                    command_ref=cmd_ref,
                    idempotency_key=str(idem_key),
                    expected_working_directory=expected_cwd,
                    input_digest=str(input_digest) if input_digest else None,
                    parameters_digest=resolved.parameters_digest,
                    execution_policy_digest=resolved.execution_policy_digest,
                    resolution_digest=resolved.resolution_digest,
                )

            is_retry = bool(req.get("retry_of") and req.get("retry_request_id"))
            if is_retry:
                expected_jid = retry_successor_id(str(req["retry_of"]), str(req["retry_request_id"]))
            elif spec is not None and ":retry:" in (spec.idempotency_key or ""):
                expected_jid = _retry_chain_job_id(
                    spec.project_id,
                    spec.idempotency_key,
                    job_id_for=job_id_for,
                    retry_successor_id=retry_successor_id,
                )
            elif idem_key and ":retry:" in str(idem_key):
                expected_jid = _retry_chain_job_id(
                    str(proj_id),
                    str(idem_key),
                    job_id_for=job_id_for,
                    retry_successor_id=retry_successor_id,
                )
            else:
                expected_jid = job_id_for(spec) if spec is not None else job_id_for({"project_id": proj_id, "idempotency_key": str(idem_key)})
            wire_jid = req.get("job_id")
            if wire_jid and wire_jid != expected_jid:
                raise ValueError(f"unauthorized job_id {wire_jid!r}, expected {expected_jid!r}")
            target_job_id = expected_jid

            def _factory(jid: str, shash: str) -> JobRecord:
                now = utc_now_iso()
                jdir = store._job_dir(jid)
                resolved_argv = [arg.replace("{job_dir}", str(jdir)) for arg in resolved.resolved_argv]
                return JobRecord(
                    job_id=jid,
                    idempotency_key=spec.idempotency_key,
                    spec_hash=shash,
                    kind=spec.kind,
                    project_id=spec.project_id,
                    command_ref=spec.command_ref,
                    resolved_argv=resolved_argv,
                    working_directory=str(resolved.resolved_cwd),
                    transport=spec.transport,
                    host_identity=socket.gethostname(),
                    duration_class=resolved.duration_class,
                    max_runtime_seconds=resolved.max_runtime_seconds,
                    heartbeat_interval_seconds=resolved.heartbeat_interval_seconds,
                    log_caps=dict(jobs_cfg.log_caps),
                    input_digest=spec.input_digest,
                    submission_spec=spec.to_canonical_dict(),
                    parameters=resolved.canonical_parameters,
                    parameters_digest=resolved.parameters_digest,
                    execution_policy_digest=resolved.execution_policy_digest,
                    resolution_digest=resolved.resolution_digest,
                    state="queued",
                    task_id=spec.task_id,
                    stage_run_id=spec.stage_run_id,
                    role_run_id=spec.role_run_id,
                    source_request_id=spec.source_request_id,
                    broker_request_id=spec.broker_request_id,
                    timestamps={"created_at": now, "queued_at": now, "updated_at": now},
                )

            record, is_new = store.claim_or_get(spec, _factory, target_job_id=target_job_id)
            if is_new:
                input_payload = req.get("input_payload")
                if input_payload is not None:
                    store.save_input_artifact(record.job_id, input_payload)
                start_res = local_transport.job_start(spec, store._job_dir(record.job_id))
                sup_pid = start_res.get("supervisor_pid") if isinstance(start_res, dict) else None
                if isinstance(sup_pid, int) and sup_pid > 0:
                    def _record_pid(rec: JobRecord) -> None:
                        rec.supervisor["pid"] = sup_pid
                    store.update(record.job_id, _record_pid)
                start_res["job_id"] = record.job_id
                return start_res
            return {
                "job_id": record.job_id,
                "status": record.state,
                "already_exists": True,
            }

        if operation == "job_status":
            job_id = req.get("job_id")
            if not job_id:
                raise ValueError("job_id is required for job_status")
            return local_transport.job_status(job_id, store._job_dir(job_id))

        if operation == "job_logs":
            job_id = req.get("job_id")
            if not job_id:
                raise ValueError("job_id is required for job_logs")
            cursor = int(req.get("cursor") or 0)
            limit = int(req.get("limit") or 100)
            return local_transport.job_logs(job_id, store._job_dir(job_id), cursor=cursor, limit=limit)

        if operation == "job_cancel":
            job_id = req.get("job_id")
            if not job_id:
                raise ValueError("job_id is required for job_cancel")
            reason = str(req.get("reason") or "cancelled")
            return local_transport.job_cancel(job_id, store._job_dir(job_id), reason=reason)

        if operation == "job_artifact":
            job_id = req.get("job_id")
            art_name = req.get("artifact_name")
            if not job_id or not art_name:
                raise ValueError("job_id and artifact_name are required for job_artifact")
            art = store.get_output_artifact(job_id, art_name)
            if art is None:
                return {
                    "job_id": job_id,
                    "status": "not_found",
                    "error": f"artifact {art_name} not found for job {job_id}",
                    "host_identity": socket.gethostname(),
                }
            return {
                "job_id": job_id,
                "status": "ok",
                "artifact": {
                    "name": art["name"],
                    "sha256": art["sha256"],
                    "size_bytes": art["size_bytes"],
                },
                "content": art["content"],
                "raw_text": art["raw_bytes"].decode("utf-8", errors="replace"),
                "host_identity": socket.gethostname(),
            }

    broker_repo = req.get("broker_repo")
    config_path = req.get("config_path")
    database_path = req.get("database_path")
    service_url = req.get("service_url")
    service_token = req.get("service_token")

    if operation == "dispatch":
        role_req = req.get("request") or {}
        if not isinstance(role_req, dict):
            raise ValueError("dispatch request must be a dictionary")
        if not role_req.get("request_id") and req_id:
            role_req["request_id"] = req_id

        if service_url:
            timeout_sec = 60.0
            if role_req.get("timeout_seconds") is not None:
                try:
                    timeout_sec = max(60.0, float(role_req["timeout_seconds"]) + 60.0)
                except (TypeError, ValueError):
                    pass
            return _service_call(service_url, "/api/dispatch", role_req, service_token, timeout_seconds=timeout_sec)

        # CLI subprocess execution
        env = _build_env(broker_repo)

        with tempfile.TemporaryDirectory(prefix="devorch-remote-") as temp_dir:
            prompt_file = Path(temp_dir) / "prompt.txt"
            prompt_file.write_text(role_req.get("prompt", ""), encoding="utf-8")
            argv = [
                sys.executable, "-m", "ai_resource_broker.cli",
                "--config", str(config_path),
            ]
            if database_path:
                argv += ["--database", str(database_path)]
            argv += [
                "dispatch",
                "--role", str(role_req.get("role", "")),
                "--quality", str(role_req.get("quality", "standard")),
                "--independence", str(role_req.get("independence", "isolated")),
                "--prompt-file", str(prompt_file),
                "--request-id", str(role_req.get("request_id", req_id)),
                "--cwd", str(role_req.get("working_directory") or role_req.get("cwd") or "."),
            ]
            timeout = role_req.get("timeout_seconds") or role_req.get("timeout")
            if timeout is not None:
                argv += ["--timeout", str(timeout)]
            if req.get("probe") or role_req.get("probe"):
                argv.append("--probe")
            for res_id in role_req.get("excluded_resource_ids", []):
                argv += ["--excluded-resource-id", str(res_id)]

            prev_context = role_req.get("previous_resource_context")
            if isinstance(prev_context, dict):
                for flag, key in (
                    ("--previous-resource-id", "resource_id"),
                    ("--previous-provider", "provider"),
                    ("--previous-account", "account"),
                    ("--previous-model", "model"),
                ):
                    val = prev_context.get(key)
                    if val is not None:
                        argv += [flag, str(val)]

            completed = subprocess.run(
                argv,
                cwd=str(broker_repo) if broker_repo else None,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )

        if completed.returncode not in (0, 1):
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"remote broker dispatch rejected (exit {completed.returncode}): {detail}")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"remote broker dispatch returned invalid JSON (exit {completed.returncode}): {detail}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("remote broker dispatch payload must be a JSON object")
        return payload

    if operation == "status":
        if service_url:
            return _service_call(
                service_url,
                "/api/dispatches/" + urllib.parse.quote(str(req_id), safe=""),
                None,
                service_token,
            )
        env = _build_env(broker_repo)
        argv = [sys.executable, "-m", "ai_resource_broker.cli", "--config", str(config_path)]
        if database_path:
            argv += ["--database", str(database_path)]
        argv += ["dispatch-status", str(req_id)]
        completed = subprocess.run(
            argv,
            cwd=str(broker_repo) if broker_repo else None,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode not in (0, 1):
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"remote broker status rejected (exit {completed.returncode}): {detail}")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"remote broker status returned invalid JSON (exit {completed.returncode}): {detail}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("remote broker status payload must be a JSON object")
        return payload

    if operation == "interrupt":
        reason = req.get("reason", "DevOrchestrator managed stop")
        if service_url:
            return _service_call(
                service_url,
                "/api/dispatches/" + urllib.parse.quote(str(req_id), safe="") + "/interrupt",
                {"reason": reason},
                service_token,
            )
        env = _build_env(broker_repo)
        argv = [sys.executable, "-m", "ai_resource_broker.cli", "--config", str(config_path)]
        if database_path:
            argv += ["--database", str(database_path)]
        argv += ["interrupt-dispatch", str(req_id), "--reason", reason]
        completed = subprocess.run(
            argv,
            cwd=str(broker_repo) if broker_repo else None,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode not in (0, 1):
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"remote broker interrupt rejected (exit {completed.returncode}): {detail}")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"remote broker interrupt returned invalid JSON (exit {completed.returncode}): {detail}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("remote broker interrupt payload must be a JSON object")
        return payload

    raise ValueError(f"unsupported remote helper operation: {operation!r}")


def handle_request(req: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = execute_request(req)
        return {
            "request_id": req.get("request_id") if isinstance(req, dict) else None,
            "host_identity": socket.gethostname(),
            "status": "success",
            "payload": payload,
        }
    except Exception as exc:
        return {
            "request_id": req.get("request_id") if isinstance(req, dict) else None,
            "host_identity": socket.gethostname(),
            "status": "error",
            "error": str(exc),
            "payload": {},
        }


def main() -> int:
    try:
        raw_in = sys.stdin.read()
        if not raw_in.strip():
            sys.stderr.write("remote helper received empty input on stdin\n")
            return 1
        req = json.loads(raw_in)
        response = handle_request(req)
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return 0 if response.get("status") == "success" else 1
    except Exception as exc:
        err_response = {
            "request_id": None,
            "host_identity": socket.gethostname(),
            "status": "error",
            "error": str(exc),
            "payload": {},
        }
        sys.stdout.write(json.dumps(err_response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return 1


if __name__ == "__main__":
    sys.exit(main())
