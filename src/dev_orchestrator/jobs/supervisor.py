"""Detached supervisor process executing allowlisted jobs and emitting durable heartbeats."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from dev_orchestrator.platform.process import (
    hidden_subprocess_kwargs,
    is_pid_alive,
    terminate_process_tree,
)
from dev_orchestrator.storage.json_store import (
    read_json,
    utc_now_iso,
    write_json,
)

from .logs import BoundedNDJSONLog
from .models import JobRecord
from .store import ExecutionJobStore


def run_supervisor(job_dir_path: Path | str) -> int:
    job_dir = Path(job_dir_path).resolve()
    job_file = job_dir / "job.json"
    result_file = job_dir / "result.json"
    heartbeat_file = job_dir / "heartbeat.json"
    cancel_file = job_dir / "cancel.json"

    if not job_file.is_file():
        sys.stderr.write(f"supervisor error: {job_file} does not exist\n")
        return 1

    # Check terminal result
    if result_file.is_file():
        sys.stderr.write(f"supervisor refusal: {result_file} already exists\n")
        return 0

    job_id = job_dir.name
    store = ExecutionJobStore(job_dir.parent.parent)

    # Load existing job record
    rec = store.get(job_id)
    if rec is None:
        sys.stderr.write(f"supervisor error: {job_file} is not valid or cannot be read\n")
        return 1

    # Check if a supervisor is already alive
    existing_sup = rec.supervisor or {}
    if isinstance(existing_sup, dict):
        live_pid = existing_sup.get("pid")
        if live_pid and live_pid != os.getpid() and is_pid_alive(live_pid):
            sys.stderr.write(f"supervisor refusal: PID {live_pid} is already alive\n")
            return 2

    # Check state is not terminal
    if rec.state in ("completed", "failed", "cancelled"):
        sys.stderr.write(f"supervisor refusal: job is already in terminal state {rec.state!r}\n")
        return 0

    # Setup supervisor identity under lock
    my_pid = os.getpid()
    start_token = uuid4().hex
    started_at = utc_now_iso()

    def _start_sup(r: JobRecord) -> None:
        if r.state in ("completed", "failed", "cancelled"):
            return
        r.transition_to("running", reason=None, failure_kind=None, timestamp=started_at)
        r.supervisor["pid"] = my_pid
        r.supervisor["start_token"] = start_token
        r.supervisor["started_at"] = started_at
        r.timestamps["started_at"] = started_at
        r.timestamps["updated_at"] = started_at

    store.update(job_id, _start_sup)
    rec = store.get(job_id) or rec

    # Prepare command execution
    argv = rec.resolved_argv
    if not isinstance(argv, list) or not argv:
        err_res = {
            "job_id": job_id,
            "exit_code": -1,
            "outcome": "failed",
            "error": "resolved_argv is empty or missing",
            "finished_at": utc_now_iso(),
        }
        store.save_result(job_id, err_res)

        def _fail_empty(r: JobRecord) -> None:
            r.transition_to("failed", reason="resolved_argv is empty or missing", failure_kind="empty_argv", timestamp=utc_now_iso())
            r.exit_code = -1
            r.terminal = err_res

        store.update(job_id, _fail_empty)
        return 1

    cwd = rec.working_directory or "."
    max_runtime = float(rec.max_runtime_seconds or 300.0)
    hb_interval = float(rec.heartbeat_interval_seconds or 5.0)

    log_caps = rec.log_caps or {}
    max_line_bytes = int(log_caps.get("max_line_bytes") or 4096)
    max_job_bytes = int(log_caps.get("max_job_bytes") or 2 * 1024 * 1024)
    head_lines = int(log_caps.get("head_lines") or 1000)
    tail_lines = int(log_caps.get("tail_lines") or 1000)

    log_writer = BoundedNDJSONLog(
        job_dir / "log.ndjson",
        max_line_bytes=max_line_bytes,
        max_job_bytes=max_job_bytes,
        head_lines=head_lines,
        tail_lines=tail_lines,
    )
    log_writer.append(f"supervisor started PID={my_pid} start_token={start_token}", stream="system")

    # Start child process
    try:
        child = subprocess.Popen(
            [str(x) for x in argv],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
            shell=False,
            **hidden_subprocess_kwargs(),
        )
    except Exception as exc:
        spawn_err_res = {
            "job_id": job_id,
            "exit_code": -1,
            "outcome": "failed",
            "error": f"spawn failed: {exc}",
            "finished_at": utc_now_iso(),
        }
        store.save_result(job_id, spawn_err_res)

        def _fail_spawn(r: JobRecord) -> None:
            r.transition_to("failed", reason=str(exc), failure_kind="spawn_error", timestamp=utc_now_iso())
            r.exit_code = -1
            r.terminal = spawn_err_res
            r.timestamps["finished_at"] = utc_now_iso()

        store.update(job_id, _fail_spawn)
        log_writer.append(f"spawn failed: {exc}", stream="system")
        return 1

    # Thread to stream stdout lines to bounded log
    def _stream_output():
        try:
            if child.stdout:
                for line in child.stdout:
                    log_writer.append(line.rstrip("\r\n"), stream="stdout")
        except Exception:
            pass

    stream_thread = threading.Thread(target=_stream_output, name=f"job-log-{job_id}", daemon=True)
    stream_thread.start()

    # Heartbeat and monitoring loop
    seq = 1
    start_mono = time.monotonic()
    timed_out = False
    cancelled = False
    exit_code: Optional[int] = None

    while True:
        # Check cancel file
        if cancel_file.is_file():
            cancelled = True
            log_writer.append("cancellation requested", stream="system")
            terminate_process_tree(child.pid)
            break

        # Check timeout
        if time.monotonic() - start_mono > max_runtime:
            timed_out = True
            log_writer.append(f"job exceeded max_runtime of {max_runtime}s", stream="system")
            terminate_process_tree(child.pid)
            break

        # Emit heartbeat
        hb_data = {
            "sequence": seq,
            "heartbeat_sequence": seq,
            "reported_at": utc_now_iso(),
            "pid": my_pid,
            "child_pid": child.pid,
            "start_token": start_token,
            "progress": "running",
        }
        write_json(heartbeat_file, hb_data, indent=2)
        seq += 1

        # Check child exit
        ret = child.poll()
        if ret is not None:
            exit_code = ret
            break

        time.sleep(min(1.0, hb_interval))

    if timed_out or cancelled:
        try:
            exit_code = child.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            terminate_process_tree(child.pid)
            exit_code = -15

    stream_thread.join(timeout=2.0)
    finished_at = utc_now_iso()

    # Determine terminal state
    if cancelled:
        final_state = "cancelled"
        failure_kind = "cancelled"
        outcome = "cancelled"
        error_msg = "job was cancelled"
    elif timed_out:
        final_state = "failed"
        failure_kind = "timeout"
        outcome = "failed"
        error_msg = f"job timed out after {max_runtime}s"
    elif exit_code == 0:
        final_state = "completed"
        failure_kind = None
        outcome = "success"
        error_msg = None
    else:
        final_state = "failed"
        failure_kind = "non_zero_exit"
        outcome = "failed"
        error_msg = f"process exited with return code {exit_code}"

    log_writer.append(f"supervisor finished state={final_state} exit_code={exit_code}", stream="system")

    # Read log compaction stats
    paginated_log = log_writer.read_paginated(0, 1)
    trunc_lines = paginated_log.get("truncated_lines", 0)
    trunc_bytes = paginated_log.get("truncated_bytes", 0)

    # Write terminal result write-once
    term_result = {
        "job_id": job_id,
        "exit_code": exit_code,
        "outcome": outcome,
        "error": error_msg,
        "finished_at": finished_at,
        "truncated_lines": trunc_lines,
        "truncated_bytes": trunc_bytes,
    }
    store.save_result(job_id, term_result)

    def _finish_sup(r: JobRecord) -> None:
        if r.state in ("completed", "failed", "cancelled"):
            return
        r.transition_to(final_state, reason=error_msg, failure_kind=failure_kind, timestamp=finished_at)
        r.exit_code = exit_code
        r.terminal = term_result
        r.timestamps["finished_at"] = finished_at
        r.timestamps["updated_at"] = finished_at

    store.update(job_id, _finish_sup)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="DevOrchestrator job supervisor")
    parser.add_argument("--job-dir", required=True, help="path to job directory")
    args = parser.parse_args()
    return run_supervisor(args.job_dir)


if __name__ == "__main__":
    sys.exit(main())
