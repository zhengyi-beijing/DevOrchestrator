from __future__ import annotations

import base64
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
from typing import Any

from dev_orchestrator.storage.json_store import write_json
from dev_orchestrator.web.server import make_server


ROOT = Path(__file__).resolve().parents[1]


class P19WebConsoleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.runtime = self.base / "runtime"
        self.repo = self.base / "repo"
        self.repo.mkdir(parents=True)
        self.runtime.mkdir(parents=True)
        (self.runtime / "projects").mkdir()
        (self.repo / "inside.bin").write_bytes(b"inside\x00data\xff")
        self.outside = self.base / "outside.bin"
        self.outside.write_bytes(b"outside")

        project = {
            "project_id": "p19", "id": "p19", "name": "P19",
            "repo_path": str(self.repo), "root": str(self.repo),
            "state": "READY_TO_RUN", "lifecycle_state": "COMPLETE",
            "git": {"branch": "main", "head": "abc123", "dirty": False, "changed_entries": 0},
            "telemetry": {"task_id": "P19"},
            "active_execution": None, "active_roles": [], "gate": None,
            "last_activity_at": "2026-09-29T00:00:00+00:00",
            "activity": {"active_role": None, "disposition": "terminal_success"},
            "authoritative_lifecycle": {
                "current_task_id": "P19", "lifecycle_state": "COMPLETE",
                "active_owner": None, "owner_gate": None,
                "invariants": [{"code": "NEXT_TASK_WITHOUT_HANDOFF", "holds": True}],
            },
        }
        write_json(self.runtime / "summary.json", {"projects": [project], "project_count": 1})
        write_json(self.runtime / "projects" / "p19.json", project)
        write_json(self.runtime / "daemon.json", {
            "state": "running", "pid": os.getpid(), "listen_address": "127.0.0.1",
            "port": 0, "last_tick_at": "2026-09-29T00:00:00+00:00", "last_error": None,
        })
        self.config = self.base / "projects.json"
        write_json(self.config, {"projects": [{"project_id": "p19", "repo_path": str(self.repo)}]})
        py = sys.executable
        write_json(self.runtime / "execution-jobs.json", {
            "enabled": True,
            "runtime_root": str(self.runtime),
            "max_concurrent_jobs": 2,
            "log_caps": {"max_line_bytes": 128, "max_job_bytes": 256, "head_lines": 10, "tail_lines": 10},
            "projects": {
                "p19": {
                    "repo_path": str(self.repo),
                    "file_roots": [str(self.repo)],
                    "commands": {
                        "success": {"argv": [py, "-c", "print('web-console-ok')"], "cwd": ".", "effect_class": "read_only"},
                        "fail": {"argv": [py, "-c", "import sys; sys.exit(3)"], "cwd": ".", "effect_class": "read_only"},
                        "short_job": {"argv": [py, "-c", "print('job-finished')"], "cwd": ".", "effect_class": "idempotent", "max_runtime_seconds": 10},
                        "long_job": {"argv": [py, "-c", "import time; time.sleep(10)"], "cwd": ".", "effect_class": "idempotent", "max_runtime_seconds": 20},
                        "hw_cmd": {"argv": [py, "-c", "print('hw')"], "cwd": ".", "effect_class": "hardware"},
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
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.token = self.server.control_security.token()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def _create_session(self) -> tuple[str, str]:
        req = urllib.request.Request(
            f"{self.url}/api/v1/control/browser-sessions",
            data=b"{}",
            headers={
                "Host": f"127.0.0.1:{self.port}",
                "Origin": f"http://127.0.0.1:{self.port}",
                "Sec-Fetch-Site": "same-origin",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 201)
            cookie_header = resp.headers.get("Set-Cookie") or ""
            data = json.loads(resp.read().decode("utf-8"))
            csrf = data["data"]["csrf_token"]
            # extract cookie key=val
            cookie_part = cookie_header.split(";")[0].strip()
            return cookie_part, csrf

    def _browser_request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        cookie: str | None = None,
        csrf: str | None = None,
        origin: str | None = None,
        sec_fetch_site: str | None = "same-origin",
        auth_header: str | None = None,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        headers: dict[str, str] = {
            "Host": f"127.0.0.1:{self.port}",
        }
        if cookie is not None:
            headers["Cookie"] = cookie
        if csrf is not None:
            headers["X-DevOrch-CSRF"] = csrf
        if origin is not None:
            headers["Origin"] = origin
        if sec_fetch_site is not None:
            headers["Sec-Fetch-Site"] = sec_fetch_site
        if auth_header is not None:
            headers["Authorization"] = auth_header

        data_bytes = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data_bytes = json.dumps(body).encode("utf-8")

        req = urllib.request.Request(f"{self.url}{path}", data=data_bytes, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                resp_body = resp.read().decode("utf-8")
                parsed = json.loads(resp_body) if resp_body else {}
                return resp.status, parsed, dict(resp.headers)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8")
            parsed = json.loads(err_body) if err_body else {}
            return exc.code, parsed, dict(exc.headers)

    def test_browser_session_creation(self) -> None:
        cookie, csrf = self._create_session()
        self.assertTrue(cookie.startswith("devorch_control="))
        self.assertTrue(len(csrf) >= 24)

        # Missing Origin fails 403
        status, _, _ = self._browser_request("/api/v1/control/browser-sessions", method="POST", body={}, origin=None)
        self.assertEqual(status, 403)

        # Cross origin fails 403
        status, _, _ = self._browser_request("/api/v1/control/browser-sessions", method="POST", body={}, origin="http://evil.com")
        self.assertEqual(status, 403)

        # Sec-Fetch-Site cross-site fails 403
        status, _, _ = self._browser_request(
            "/api/v1/control/browser-sessions",
            method="POST",
            body={},
            origin=f"http://127.0.0.1:{self.port}",
            sec_fetch_site="cross-site",
        )
        self.assertEqual(status, 403)

    def test_browser_authorized_across_all_transport_operations(self) -> None:
        cookie, csrf = self._create_session()
        same_origin = f"http://127.0.0.1:{self.port}"

        # 1. status (GET - no origin sent by browser)
        status, res, _ = self._browser_request(
            "/api/v1/control/external/status?project_id=p19",
            method="GET",
            cookie=cookie,
            csrf=csrf,
            origin=None,
            sec_fetch_site="same-origin",
        )
        self.assertEqual(status, 200)
        self.assertTrue(res["data"]["daemon"]["process_alive"])
        self.assertEqual(res["data"]["authoritative_lifecycle"]["lifecycle_state"], "COMPLETE")

        # 2. commands (GET - no origin sent by browser)
        status, res, _ = self._browser_request(
            "/api/v1/control/transport/commands?project_id=p19&host_id=local",
            method="GET",
            cookie=cookie,
            csrf=csrf,
            origin=None,
            sec_fetch_site="same-origin",
        )
        self.assertEqual(status, 200)
        self.assertTrue(res["data"]["host_local"])
        crefs = [c["command_ref"] for c in res["data"]["commands"]]
        self.assertIn("success", crefs)
        self.assertIn("fail", crefs)

        # 3. exec (POST - browser sends Origin)
        status, res, _ = self._browser_request(
            "/api/v1/control/transport/exec",
            method="POST",
            body={"project_id": "p19", "host_id": "local", "command_ref": "success", "idempotency_key": "web-exec-1"},
            cookie=cookie,
            csrf=csrf,
            origin=same_origin,
            sec_fetch_site="same-origin",
        )
        self.assertEqual(status, 200)
        self.assertEqual(res["data"]["status"], "ok")
        self.assertEqual(res["data"]["exit_code"], 0)
        self.assertIn("web-console-ok", res["data"]["stdout"])

        # 4. spawn (POST - browser sends Origin)
        status, res, _ = self._browser_request(
            "/api/v1/control/transport/spawn",
            method="POST",
            body={"project_id": "p19", "command_ref": "short_job", "idempotency_key": "web-spawn-1"},
            cookie=cookie,
            csrf=csrf,
            origin=same_origin,
            sec_fetch_site="same-origin",
        )
        self.assertIn(status, (200, 202))
        job_id = res["data"]["job_id"]
        self.assertTrue(job_id)

        # 5. poll (POST - browser sends Origin)
        poll_done = False
        for _ in range(50):
            status, res, _ = self._browser_request(
                "/api/v1/control/transport/poll",
                method="POST",
                body={"project_id": "p19", "operation_id": job_id},
                cookie=cookie,
                csrf=csrf,
                origin=same_origin,
                sec_fetch_site="same-origin",
            )
            self.assertEqual(status, 200)
            if res["data"]["status"] in ("completed", "failed", "cancelled"):
                poll_done = True
                self.assertEqual(res["data"]["status"], "completed")
                break
            time.sleep(0.05)
        self.assertTrue(poll_done)

        # 6. cancel (POST - browser sends Origin)
        status, res, _ = self._browser_request(
            "/api/v1/control/transport/spawn",
            method="POST",
            body={"project_id": "p19", "command_ref": "long_job", "idempotency_key": "web-spawn-cancel"},
            cookie=cookie,
            csrf=csrf,
            origin=same_origin,
            sec_fetch_site="same-origin",
        )
        cancel_id = res["data"]["job_id"]
        status, res, _ = self._browser_request(
            "/api/v1/control/transport/cancel",
            method="POST",
            body={"project_id": "p19", "operation_id": cancel_id, "reason": "web user test"},
            cookie=cookie,
            csrf=csrf,
            origin=same_origin,
            sec_fetch_site="same-origin",
        )
        self.assertEqual(status, 200)
        self.assertEqual(res["data"]["status"], "cancelled")

        # 7. stat (GET - no origin sent by browser)
        status, res, _ = self._browser_request(
            "/api/v1/control/transport/stat?project_id=p19&path=inside.bin",
            method="GET",
            cookie=cookie,
            csrf=csrf,
            origin=None,
            sec_fetch_site="same-origin",
        )
        self.assertEqual(status, 200)
        self.assertTrue(res["data"]["exists"])
        self.assertTrue(res["data"]["is_file"])

        # 8. read (GET - no origin sent by browser)
        status, res, _ = self._browser_request(
            "/api/v1/control/transport/read?project_id=p19&path=inside.bin",
            method="GET",
            cookie=cookie,
            csrf=csrf,
            origin=None,
            sec_fetch_site="same-origin",
        )
        self.assertEqual(status, 200)
        self.assertEqual(res["data"]["status"], "ok")
        content_bytes = base64.b64decode(res["data"]["content_base64"])
        self.assertEqual(content_bytes, b"inside\x00data\xff")

        # 9. stage-write (POST - browser sends Origin)
        test_content = b"browser_uploaded_binary\x00\x01\x02"
        test_sha256 = "sha256:" + hashlib.sha256(test_content).hexdigest()
        status, res, _ = self._browser_request(
            "/api/v1/control/transport/stage-write",
            method="POST",
            body={
                "project_id": "p19",
                "content_base64": base64.b64encode(test_content).decode("ascii"),
                "decoded_size_bytes": len(test_content),
                "content_sha256": test_sha256,
            },
            cookie=cookie,
            csrf=csrf,
            origin=same_origin,
            sec_fetch_site="same-origin",
        )
        self.assertEqual(status, 201)
        content_ref = res["data"]["content_ref"]
        self.assertTrue(content_ref)

        # 10. write (POST - browser sends Origin)
        status, res, _ = self._browser_request(
            "/api/v1/control/transport/write",
            method="POST",
            body={
                "project_id": "p19",
                "target_path": "uploaded.bin",
                "content_ref": content_ref,
                "content_sha256": test_sha256,
                "decoded_size_bytes": len(test_content),
                "if_absent": True,
                "idempotency_key": "web-write-1",
            },
            cookie=cookie,
            csrf=csrf,
            origin=same_origin,
            sec_fetch_site="same-origin",
        )
        self.assertEqual(status, 200)
        self.assertEqual(res["data"]["post_digest"], test_sha256)

        # 11. operations (GET - no origin sent by browser)
        status, res, _ = self._browser_request(
            "/api/v1/control/transport/operations?limit=20",
            method="GET",
            cookie=cookie,
            csrf=csrf,
            origin=None,
            sec_fetch_site="same-origin",
        )
        self.assertEqual(status, 200)
        self.assertGreater(res["data"]["count"], 0)

    def test_rejections_and_invalid_credentials(self) -> None:
        cookie, csrf = self._create_session()
        same_origin = f"http://127.0.0.1:{self.port}"

        # Missing cookie (CSRF only) -> 401
        status, _, _ = self._browser_request(
            "/api/v1/control/external/status?project_id=p19",
            method="GET",
            cookie=None,
            csrf=csrf,
            sec_fetch_site="same-origin",
        )
        self.assertEqual(status, 401)

        # Missing CSRF (Cookie only) -> 401
        status, _, _ = self._browser_request(
            "/api/v1/control/external/status?project_id=p19",
            method="GET",
            cookie=cookie,
            csrf=None,
            sec_fetch_site="same-origin",
        )
        self.assertEqual(status, 401)

        # Wrong CSRF -> 401
        status, _, _ = self._browser_request(
            "/api/v1/control/external/status?project_id=p19",
            method="GET",
            cookie=cookie,
            csrf="invalid-csrf-token",
            sec_fetch_site="same-origin",
        )
        self.assertEqual(status, 401)

        # Expired session -> 401
        session_id = cookie.split("=")[1].strip()
        with self.server.control_security._lock:
            self.server.control_security._sessions[session_id]["expires"] = 0
        status, _, _ = self._browser_request(
            "/api/v1/control/external/status?project_id=p19",
            method="GET",
            cookie=cookie,
            csrf=csrf,
            sec_fetch_site="same-origin",
        )
        self.assertEqual(status, 401)

        # New valid session for cross-origin tests
        c2, cs2 = self._create_session()

        # Cross-origin on POST -> 401/403
        status, _, _ = self._browser_request(
            "/api/v1/control/transport/exec",
            method="POST",
            body={"project_id": "p19", "command_ref": "success"},
            cookie=c2,
            csrf=cs2,
            origin="http://attacker.com",
            sec_fetch_site="same-origin",
        )
        self.assertIn(status, (401, 403))

        # Sec-Fetch-Site cross-site on GET -> 401/403
        status, _, _ = self._browser_request(
            "/api/v1/control/external/status?project_id=p19",
            method="GET",
            cookie=c2,
            csrf=cs2,
            origin=None,
            sec_fetch_site="cross-site",
        )
        self.assertIn(status, (401, 403))

        # Origin absent and Sec-Fetch-Site absent on GET -> 401
        status, _, _ = self._browser_request(
            "/api/v1/control/external/status?project_id=p19",
            method="GET",
            cookie=c2,
            csrf=cs2,
            origin=None,
            sec_fetch_site=None,
        )
        self.assertEqual(status, 401)

    def test_boundary_origin_less_mutation_fails_closed_while_read_succeeds(self) -> None:
        cookie, csrf = self._create_session()

        # Mutating POST routes MUST fail closed when Origin is omitted, even with valid cookie + CSRF + Sec-Fetch-Site
        post_endpoints = [
            ("/api/v1/control/transport/exec", {"project_id": "p19", "command_ref": "success"}),
            ("/api/v1/control/transport/spawn", {"project_id": "p19", "command_ref": "short_job"}),
            ("/api/v1/control/transport/poll", {"project_id": "p19", "operation_id": "test-op"}),
            ("/api/v1/control/transport/cancel", {"project_id": "p19", "operation_id": "test-op"}),
            ("/api/v1/control/transport/stage-write", {"project_id": "p19", "content_base64": "AA==", "decoded_size_bytes": 1, "content_sha256": "sha256:" + "0"*64}),
            ("/api/v1/control/transport/write", {"project_id": "p19", "target_path": "x.bin", "content_ref": "x", "content_sha256": "sha256:" + "0"*64, "decoded_size_bytes": 0}),
            ("/api/v1/control/browser-sessions", {}),
            ("/api/v1/control/adapter-pairings", {}),
        ]
        for path, payload in post_endpoints:
            status, _, _ = self._browser_request(
                path,
                method="POST",
                body=payload,
                cookie=cookie,
                csrf=csrf,
                origin=None,
                sec_fetch_site="same-origin",
            )
            self.assertIn(status, (401, 403), f"Mutating POST {path} without Origin must fail closed")

        # In contrast, GET read routes SUCCEED with valid cookie + CSRF + Sec-Fetch-Site and NO Origin
        get_endpoints = [
            "/api/v1/control/transport/commands?project_id=p19&host_id=local",
            "/api/v1/control/external/status?project_id=p19",
            "/api/v1/control/transport/read?project_id=p19&path=inside.bin",
            "/api/v1/control/transport/stat?project_id=p19&path=inside.bin",
        ]
        for path in get_endpoints:
            status, _, _ = self._browser_request(
                path,
                method="GET",
                cookie=cookie,
                csrf=csrf,
                origin=None,
                sec_fetch_site="same-origin",
            )
            self.assertEqual(status, 200, f"GET {path} without Origin must succeed for browser session")

    def test_boundary_scoped_capability_rejected_by_commands_discovery(self) -> None:
        sec = self.server.control_security
        scoped_cap = sec.create_transport_capability(
            project_id="p19",
            host_id="local",
            allowed_operations=["stat", "read_file", "exec"],
            allowed_commands=["success"],
            ttl_seconds=3600,
            label="scoped-test",
        )
        cap_token = scoped_cap["token"]

        # 1. Scoped capability to GET /transport/commands -> MUST be rejected (401)
        status, _, _ = self._browser_request(
            "/api/v1/control/transport/commands?project_id=p19&host_id=local",
            method="GET",
            auth_header=f"Bearer {cap_token}",
        )
        self.assertEqual(status, 401, "Scoped transport capability must be rejected by owner-only command discovery")

        # 2. Master bearer token to GET /transport/commands -> 200 OK
        status, res, _ = self._browser_request(
            "/api/v1/control/transport/commands?project_id=p19&host_id=local",
            method="GET",
            auth_header=f"Bearer {self.token}",
        )
        self.assertEqual(status, 200, "Master bearer token must be accepted by command discovery")
        self.assertGreater(len(res["data"]["commands"]), 0)

        # 3. Browser session to GET /transport/commands -> 200 OK
        cookie, csrf = self._create_session()
        status, res, _ = self._browser_request(
            "/api/v1/control/transport/commands?project_id=p19&host_id=local",
            method="GET",
            cookie=cookie,
            csrf=csrf,
            origin=None,
            sec_fetch_site="same-origin",
        )
        self.assertEqual(status, 200, "Browser session must be accepted by command discovery")

        # 4. Scoped capability can still read/stat file -> 200 OK (normal capability access intact)
        status, stat_res, _ = self._browser_request(
            "/api/v1/control/transport/stat?project_id=p19&path=inside.bin",
            method="GET",
            auth_header=f"Bearer {cap_token}",
        )
        self.assertEqual(status, 200, "Scoped capability should still be able to stat allowed files")
        self.assertTrue(stat_res["data"]["exists"])

    def test_command_catalog_content_and_safety(self) -> None:
        cookie, csrf = self._create_session()

        # Local host commands
        status, res, _ = self._browser_request(
            "/api/v1/control/transport/commands?project_id=p19&host_id=local",
            method="GET",
            cookie=cookie,
            csrf=csrf,
            origin=None,
            sec_fetch_site="same-origin",
        )
        self.assertEqual(status, 200)
        data = res["data"]
        self.assertEqual(data["project_id"], "p19")
        self.assertEqual(data["host_id"], "local")
        self.assertTrue(data["host_local"])

        cmd_map = {c["command_ref"]: c for c in data["commands"]}
        self.assertIn("success", cmd_map)
        self.assertIn("hw_cmd", cmd_map)

        # Check safety: NO argv, cwd, env, or executable leakage
        for cmd in data["commands"]:
            self.assertNotIn("argv", cmd)
            self.assertNotIn("cwd", cmd)
            self.assertNotIn("env", cmd)
            self.assertNotIn("executable", cmd)
            self.assertIn("command_ref", cmd)
            self.assertIn("effect_class", cmd)
            self.assertIn("selectable", cmd)

        # Hardware command must have selectable=False
        self.assertFalse(cmd_map["hw_cmd"]["selectable"])
        # Normal command has selectable=True
        self.assertTrue(cmd_map["success"]["selectable"])

        # Remote host yields empty list
        status, res_remote, _ = self._browser_request(
            "/api/v1/control/transport/commands?project_id=p19&host_id=remote-ssh",
            method="GET",
            cookie=cookie,
            csrf=csrf,
            origin=None,
            sec_fetch_site="same-origin",
        )
        self.assertEqual(status, 200)
        self.assertFalse(res_remote["data"]["host_local"])
        self.assertEqual(res_remote["data"]["commands"], [])


if __name__ == "__main__":
    unittest.main()

