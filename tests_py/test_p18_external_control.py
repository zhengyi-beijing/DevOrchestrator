from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from dev_orchestrator.control.adapter import (
    ControlAdapterAuthError,
    ControlAdapterClient,
    ControlAdapterError,
)
from dev_orchestrator.control.mcp_adapter import MCPAdapter
from dev_orchestrator.storage.json_store import write_json
from dev_orchestrator.web.server import make_server


ROOT = Path(__file__).resolve().parents[1]


class P18ExternalControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.runtime = self.base / "runtime"
        self.repo = self.base / "repo"
        self.repo.mkdir(parents=True)
        self.runtime.mkdir(parents=True)
        (self.runtime / "projects").mkdir()
        (self.repo / "inside.bin").write_bytes(b"inside\x00bytes")
        self.outside = self.base / "outside.bin"
        self.outside.write_bytes(b"outside")

        project = {
            "project_id": "p18", "id": "p18", "name": "P18",
            "repo_path": str(self.repo), "root": str(self.repo),
            "state": "READY_TO_RUN", "lifecycle_state": "COMPLETE",
            "git": {"branch": "main", "head": "abc123", "dirty": False, "changed_entries": 0},
            "telemetry": {"task_id": "P18"},
            "active_execution": None, "active_roles": [], "gate": None,
            "last_activity_at": "2026-09-29T00:00:00+00:00",
            "activity": {"active_role": None, "disposition": "terminal_success"},
            "authoritative_lifecycle": {
                "current_task_id": "P18", "lifecycle_state": "COMPLETE",
                "active_owner": None, "owner_gate": None,
                "invariants": [{"code": "NEXT_TASK_WITHOUT_HANDOFF", "holds": True}],
            },
        }
        write_json(self.runtime / "summary.json", {"projects": [project], "project_count": 1})
        write_json(self.runtime / "projects" / "p18.json", project)
        write_json(self.runtime / "daemon.json", {
            "state": "running", "pid": os.getpid(), "listen_address": "127.0.0.1",
            "port": 0, "last_tick_at": "2026-09-29T00:00:00+00:00", "last_error": None,
        })
        self.config = self.base / "projects.json"
        write_json(self.config, {"projects": [{"project_id": "p18", "repo_path": str(self.repo)}]})
        py = sys.executable
        write_json(self.runtime / "execution-jobs.json", {
            "enabled": True,
            "runtime_root": str(self.runtime),
            "max_concurrent_jobs": 2,
            "log_caps": {"max_line_bytes": 128, "max_job_bytes": 256, "head_lines": 10, "tail_lines": 10},
            "projects": {
                "p18": {
                    "repo_path": str(self.repo),
                    "file_roots": [str(self.repo)],
                    "commands": {
                        "success": {"argv": [py, "-c", "print('ok')"], "cwd": ".", "effect_class": "read_only"},
                        "fail": {"argv": [py, "-c", "import sys; print('bad', file=sys.stderr); sys.exit(7)"], "cwd": ".", "effect_class": "read_only"},
                        "timeout": {"argv": [py, "-c", "import time; time.sleep(2)"], "cwd": ".", "effect_class": "read_only", "max_runtime_seconds": 2},
                        "cwd": {"argv": [py, "-c", "import os; print(os.getcwd())"], "cwd": ".", "effect_class": "read_only"},
                        "large": {"argv": [py, "-c", "print('x'*5000)"], "cwd": ".", "effect_class": "read_only"},
                        "short_job": {"argv": [py, "-c", "print('job-ok')"], "cwd": ".", "effect_class": "idempotent", "max_runtime_seconds": 10},
                        "long_job": {"argv": [py, "-c", "import time; time.sleep(10)"], "cwd": ".", "effect_class": "idempotent", "max_runtime_seconds": 20},
                    },
                }
            },
        })
        self.server = make_server(
            "127.0.0.1", 0, self.runtime, ROOT / "web",
            enable_control=True, config_path=self.config,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.token = self.server.control_security.token()
        self.client = ControlAdapterClient(base_url=self.url, token=self.token)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def test_authenticated_status_is_authoritative_and_audited(self) -> None:
        endpoint = self.url + "/api/v1/control/external/status?project_id=p18"
        with self.assertRaises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(endpoint)
        self.assertEqual(missing.exception.code, 401)
        with self.assertRaises(ControlAdapterAuthError):
            ControlAdapterClient(base_url=self.url, token="invalid").external_status("p18")

        result = self.client.external_status("p18")
        self.assertTrue(result["request_id"].startswith("req-"))
        self.assertLess(len(json.dumps(result, ensure_ascii=False)), 65536)
        data = result["data"]
        self.assertTrue(data["daemon"]["process_alive"])
        self.assertEqual(data["authoritative_lifecycle"]["lifecycle_state"], "COMPLETE")
        self.assertIsNone(data["owner_gate"])
        self.assertIsNone(data["active_execution"])
        self.assertEqual(data["active_ai_roles"], [])
        audit = (self.runtime / "control" / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn('"event":"external_operation"', audit)
        self.assertIn(result["request_id"], audit)
        self.assertNotIn(self.token, audit)

    def test_exec_success_failure_timeout_cwd_and_output_bound(self) -> None:
        common = {"project_id": "p18", "host_id": "local"}
        ok = self.client.transport_exec(**common, command_ref="success", idempotency_key="exec-ok")
        self.assertEqual(ok["data"]["status"], "ok")
        self.assertEqual(ok["data"]["exit_code"], 0)
        self.assertEqual(ok["data"]["selected_transport"], "local")

        failed = self.client.transport_exec(**common, command_ref="fail", idempotency_key="exec-fail")
        self.assertEqual(failed["data"]["status"], "failed")
        self.assertEqual(failed["data"]["exit_code"], 7)
        self.assertIn("bad", failed["data"]["stderr"])

        timed = self.client.transport_exec(
            **common, command_ref="timeout", idempotency_key="exec-timeout", timeout_seconds=0.05,
        )
        self.assertEqual(timed["data"]["status"], "timeout")
        cwd = self.client.transport_exec(**common, command_ref="cwd", idempotency_key="exec-cwd")
        self.assertEqual(Path(cwd["data"]["stdout"].strip()), self.repo.resolve())
        large = self.client.transport_exec(**common, command_ref="large", idempotency_key="exec-large")
        self.assertTrue(large["data"]["raw_evidence"]["output_truncated"])
        self.assertLessEqual(len(large["data"]["stdout"].encode("utf-8")), 256)

    def test_durable_spawn_poll_completion_cancel_and_idempotency(self) -> None:
        first = self.client.transport_spawn(
            project_id="p18", command_ref="short_job", idempotency_key="spawn-same",
        )
        second = self.client.transport_spawn(
            project_id="p18", command_ref="short_job", idempotency_key="spawn-same",
        )
        job_id = first["data"]["job_id"]
        self.assertEqual(job_id, second["data"]["job_id"])
        terminal = None
        for _ in range(100):
            terminal = self.client.transport_poll(operation_id=job_id, project_id="p18")
            if terminal["data"]["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.05)
        self.assertEqual(terminal["data"]["status"], "completed")
        self.assertEqual(terminal["data"]["exit_code"], 0)

        long = self.client.transport_spawn(
            project_id="p18", command_ref="long_job", idempotency_key="spawn-cancel",
        )
        long_id = long["data"]["job_id"]
        cancelled = self.client.transport_cancel(operation_id=long_id, project_id="p18", reason="test")
        replay = self.client.transport_cancel(operation_id=long_id, project_id="p18", reason="test")
        self.assertEqual(cancelled["data"]["status"], "cancelled")
        self.assertEqual(replay["data"]["status"], "cancelled")
        index = json.loads((self.runtime / "jobs" / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["jobs"][long_id]["state"], "cancelled")
        with self.assertRaises(ControlAdapterAuthError):
            self.client.transport_cancel(operation_id=long_id, project_id="other")

    def test_scoped_file_read_stat_stage_and_cas_binary_roundtrip(self) -> None:
        read = self.client.transport_read_file(project_id="p18", path="inside.bin", max_bytes=999999999)
        self.assertEqual(read["data"]["status"], "ok")
        self.assertEqual(read["data"]["selected_transport"], "local")
        stat = self.client.transport_stat(project_id="p18", path="inside.bin")
        self.assertTrue(stat["data"]["is_file"])
        with self.assertRaises(ControlAdapterError):
            self.client.transport_read_file(project_id="p18", path="../outside.bin")
        with self.assertRaises(ControlAdapterError):
            self.client.transport_read_file(project_id="p18", path=str(self.outside))

        first_bytes = b"\x00P18\xffexternal\r\n"
        first_digest = "sha256:" + hashlib.sha256(first_bytes).hexdigest()
        staged = self.client.transport_stage_write(
            project_id="p18", content=first_bytes, content_sha256=first_digest,
        )["data"]
        created = self.client.transport_write_file(
            project_id="p18", target_path="roundtrip.bin", idempotency_key="write-create",
            content_ref=staged["content_ref"], content_sha256=first_digest,
            decoded_size_bytes=len(first_bytes), if_absent=True,
        )["data"]
        self.assertEqual(created["post_digest"], first_digest)

        second_bytes = b"updated\x00binary"
        second_digest = "sha256:" + hashlib.sha256(second_bytes).hexdigest()
        staged2 = self.client.transport_stage_write(
            project_id="p18", content=second_bytes, content_sha256=second_digest,
        )["data"]
        updated = self.client.transport_write_file(
            project_id="p18", target_path="roundtrip.bin", idempotency_key="write-update",
            content_ref=staged2["content_ref"], content_sha256=second_digest,
            decoded_size_bytes=len(second_bytes), expected_sha256=first_digest,
        )["data"]
        self.assertEqual(updated["post_digest"], second_digest)
        with self.assertRaises(ControlAdapterError):
            self.client.transport_write_file(
                project_id="p18", target_path="roundtrip.bin", idempotency_key="write-conflict",
                content_ref=staged2["content_ref"], content_sha256=second_digest,
                decoded_size_bytes=len(second_bytes), expected_sha256=first_digest,
            )
        readback = self.client.transport_read_file(project_id="p18", path="roundtrip.bin")
        self.assertEqual(readback["data"]["content_sha256"], second_digest)

    def test_max_concurrent_jobs_fails_closed(self) -> None:
        one = self.client.transport_spawn(
            project_id="p18", command_ref="long_job", idempotency_key="cap-one",
        )["data"]["job_id"]
        two = self.client.transport_spawn(
            project_id="p18", command_ref="long_job", idempotency_key="cap-two",
        )["data"]["job_id"]
        try:
            with self.assertRaises(ControlAdapterError) as ctx:
                self.client.transport_spawn(
                    project_id="p18", command_ref="long_job", idempotency_key="cap-three",
                )
            self.assertIn("maximum concurrent jobs reached", str(ctx.exception))
        finally:
            self.client.transport_cancel(operation_id=one, project_id="p18", reason="test cleanup")
            self.client.transport_cancel(operation_id=two, project_id="p18", reason="test cleanup")

    def test_mcp_privileged_tools_are_opt_in_closed_and_require_cas(self) -> None:
        default_names = {row["name"] for row in MCPAdapter(self.client).tools}
        self.assertNotIn("devorch_exec", default_names)
        adapter = MCPAdapter(self.client, enable_transport_tools=True)
        names = {row["name"] for row in adapter.tools}
        for name in ("devorch_exec", "devorch_spawn", "devorch_poll", "devorch_cancel", "devorch_read_file", "devorch_stat", "devorch_stage_write", "devorch_write"):
            self.assertIn(name, names)
        unsafe = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "devorch_write", "arguments": {
                "project_id": "p18", "target_path": "x", "idempotency_key": "x",
                "content_ref": "x", "content_sha256": "sha256:" + "0" * 64,
                "decoded_size_bytes": 0,
            }},
        }
        response = json.loads(adapter.handle_message(json.dumps(unsafe)))
        self.assertTrue(response["result"]["isError"])
        self.assertIn("if_absent=true or expected_sha256", response["result"]["content"][0]["text"])

    def test_external_api_has_no_direct_lifecycle_ledger_route(self) -> None:
        req = urllib.request.Request(
            self.url + "/api/v1/control/external/lifecycle",
            data=b"{}",
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 404)
        authority = json.loads((self.runtime / "summary.json").read_text(encoding="utf-8"))["projects"][0]["authoritative_lifecycle"]
        self.assertEqual(authority["lifecycle_state"], "COMPLETE")
        self.assertTrue(authority["invariants"][0]["holds"])


if __name__ == "__main__":
    unittest.main()
