import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any
import unittest

from ops.self_host_acceptance import (
    canonical_path,
    run_acceptance_checks,
    validate_loopback_url,
)


class EphemeralMockServer:
    def __init__(self, routes: dict[str, Any] | None = None):
        self.routes = dict(routes or {})
        self.recorded_requests: list[tuple[str, str]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass  # Suppress stderr logging

            def do_GET(self):
                outer.recorded_requests.append(("GET", self.path))
                if self.path in outer.routes:
                    entry = outer.routes[self.path]
                    if len(entry) == 4:
                        status, body, content_type, headers = entry
                    else:
                        status, body, content_type = entry
                        headers = {}
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(body)))
                    for h_name, h_val in (headers or {}).items():
                        self.send_header(h_name, h_val)
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                outer.recorded_requests.append(("POST", self.path))
                self.send_response(405)
                self.end_headers()

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def set_route(
        self,
        path: str,
        status: int,
        data: bytes | str | dict,
        content_type: str = "application/json",
        headers: dict[str, str] | None = None,
    ):
        if isinstance(data, dict):
            body = json.dumps(data).encode("utf-8")
        elif isinstance(data, str):
            body = data.encode("utf-8")
        else:
            body = data
        self.routes[path] = (status, body, content_type, headers or {})

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def make_healthy_overview(
    repo_path: str,
    branch: str = "main",
    project_id: str = "devorchestrator",
    warnings: list[str] | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "generated_at": "2026-09-16T08:00:00+00:00",
        "data": {
            "control_enabled": True,
            "monitor": {
                "state": "running",
                "pid": 12345,
                "process_alive": True,
                "interval_seconds": 5,
                "started_at": "2026-09-16T07:00:00+00:00",
                "last_tick_at": "2026-09-16T08:00:00+00:00",
                "heartbeat_age_seconds": 1.2,
                "stale": False,
                "last_error": None,
            },
            "control_health": {
                "degraded": False,
                "degraded_reason": None,
            },
            "projects": [
                {
                    "project_id": project_id,
                    "repo_path": repo_path,
                    "git": {"branch": branch, "head": "abc1234", "dirty": False},
                    "control_identity": {"project_id": project_id, "repo_path": repo_path, "branch": branch},
                }
            ],
            "accounting": {
                "available": True,
                "data_status": "available",
            },
            "resources": {
                "available": True,
                "availability": "available",
            },
            "executions": {
                "available": True,
                "availability": "available",
            },
        },
        "warnings": list(warnings or []),
        "sources": [
            {"name": "monitor", "availability": "available"},
            {"name": "control", "availability": "available"},
            {"name": "accounting", "availability": "available"},
            {"name": "broker_resources", "availability": "available"},
            {"name": "broker_executions", "availability": "available"},
        ],
    }


def make_healthy_resources() -> dict:
    return {
        "resources": [
            {"resource_id": "r1", "provider": "anthropic", "model": "claude-opus-5"},
            {"resource_id": "r2", "provider": "agy", "model": "gemini-3.8-flash"},
        ]
    }


def make_healthy_executions() -> dict:
    return {
        "executions": [
            {"execution_id": "e1", "resource_id": "r1", "status": "succeeded"},
        ]
    }


class SelfHostAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.control_server = EphemeralMockServer()
        self.broker_server = EphemeralMockServer()
        self.repo_dir = str(Path(__file__).resolve().parent.parent)
        self.script_path = Path(self.repo_dir) / "ops" / "self_host_acceptance.py"

    def tearDown(self):
        self.control_server.close()
        self.broker_server.close()

    def _setup_healthy_endpoints(self, warnings: list[str] | None = None) -> dict:
        overview = make_healthy_overview(self.repo_dir, warnings=warnings)
        self.control_server.set_route("/api/v1/control/overview", 200, overview)
        self.broker_server.set_route("/api/resources", 200, make_healthy_resources())
        self.broker_server.set_route("/api/executions", 200, make_healthy_executions())
        return overview

    def test_healthy_fixture_direct_and_subprocess(self):
        self._setup_healthy_endpoints()

        # 1. Direct function call
        res = run_acceptance_checks(
            control_url=self.control_server.url,
            broker_url=self.broker_server.url,
            project_id="devorchestrator",
            expected_repo_path=self.repo_dir,
            expected_branch="main",
        )
        self.assertEqual(res["status"], "PASS")
        self.assertEqual(res["overall_status"], "PASS")
        self.assertEqual(res["diagnostics"], [])
        self.assertEqual(res["daemon_control"]["status"], "PASS")
        self.assertEqual(res["daemon_control"]["daemon_state"], "running")
        self.assertEqual(res["project_identity"]["status"], "PASS")
        self.assertEqual(res["accounting"]["status"], "PASS")
        self.assertEqual(res["broker_resources"]["status"], "PASS")
        self.assertEqual(res["broker_resources"]["resource_count"], 2)
        self.assertEqual(res["broker_executions"]["status"], "PASS")
        self.assertEqual(res["broker_executions"]["execution_count"], 1)

        # 2. Subprocess invocation
        proc = subprocess.run(
            [
                sys.executable,
                str(self.script_path),
                "--control-url", self.control_server.url,
                "--broker-url", self.broker_server.url,
                "--project-id", "devorchestrator",
                "--expected-repo-path", self.repo_dir,
                "--expected-branch", "main",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        parsed = json.loads(proc.stdout)
        self.assertEqual(parsed["status"], "PASS")
        self.assertEqual(parsed["overall_status"], "PASS")

    def test_exact_get_only_request_paths(self):
        self._setup_healthy_endpoints()
        run_acceptance_checks(
            control_url=self.control_server.url,
            broker_url=self.broker_server.url,
            project_id="devorchestrator",
            expected_repo_path=self.repo_dir,
            expected_branch="main",
        )

        all_reqs = self.control_server.recorded_requests + self.broker_server.recorded_requests
        for method, path in all_reqs:
            self.assertEqual(method, "GET", f"Expected GET request, got {method} for {path}")

        control_paths = [path for _, path in self.control_server.recorded_requests]
        broker_paths = [path for _, path in self.broker_server.recorded_requests]
        self.assertEqual(control_paths, ["/api/v1/control/overview"])
        self.assertEqual(sorted(broker_paths), ["/api/executions", "/api/resources"])

    def test_warning_preservation_does_not_fail_acceptance(self):
        warnings = ["review queue backlog high", "cold cache warning"]
        self._setup_healthy_endpoints(warnings=warnings)

        res = run_acceptance_checks(
            control_url=self.control_server.url,
            broker_url=self.broker_server.url,
            project_id="devorchestrator",
            expected_repo_path=self.repo_dir,
            expected_branch="main",
        )
        self.assertEqual(res["status"], "PASS")
        self.assertEqual(res["warnings"], warnings)
        self.assertEqual(res["diagnostics"], [])

    def test_unreachable_control_endpoint(self):
        # Point to unused port
        bad_control_url = "http://127.0.0.1:1"
        self.broker_server.set_route("/api/resources", 200, make_healthy_resources())
        self.broker_server.set_route("/api/executions", 200, make_healthy_executions())

        res = run_acceptance_checks(
            control_url=bad_control_url,
            broker_url=self.broker_server.url,
            expected_repo_path=self.repo_dir,
        )
        self.assertEqual(res["status"], "FAIL")
        self.assertTrue(len(res["diagnostics"]) > 0)
        self.assertTrue(any("control overview unavailable" in d for d in res["diagnostics"]))

        proc = subprocess.run(
            [
                sys.executable,
                str(self.script_path),
                "--control-url", bad_control_url,
                "--broker-url", self.broker_server.url,
                "--expected-repo-path", self.repo_dir,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        parsed = json.loads(proc.stdout)
        self.assertEqual(parsed["status"], "FAIL")

    def test_unreachable_broker_endpoint(self):
        overview = make_healthy_overview(self.repo_dir)
        self.control_server.set_route("/api/v1/control/overview", 200, overview)
        bad_broker_url = "http://127.0.0.1:1"

        res = run_acceptance_checks(
            control_url=self.control_server.url,
            broker_url=bad_broker_url,
            expected_repo_path=self.repo_dir,
        )
        self.assertEqual(res["status"], "FAIL")
        self.assertEqual(res["broker_resources"]["status"], "FAIL")
        self.assertEqual(res["broker_executions"]["status"], "FAIL")
        self.assertTrue(any("direct broker /api/resources failed" in d for d in res["diagnostics"]))

    def test_malformed_json_responses(self):
        # Control returns invalid JSON
        self.control_server.set_route("/api/v1/control/overview", 200, "not json at all")
        self.broker_server.set_route("/api/resources", 200, make_healthy_resources())
        self.broker_server.set_route("/api/executions", 200, make_healthy_executions())

        res = run_acceptance_checks(
            control_url=self.control_server.url,
            broker_url=self.broker_server.url,
            expected_repo_path=self.repo_dir,
        )
        self.assertEqual(res["status"], "FAIL")
        self.assertTrue(any("returned malformed JSON" in d for d in res["diagnostics"]))
        # Verify no response body leaked into diagnostics
        for diag in res["diagnostics"]:
            self.assertNotIn("not json at all", diag)

        # Broker returns invalid JSON
        self.control_server.set_route("/api/v1/control/overview", 200, make_healthy_overview(self.repo_dir))
        self.broker_server.set_route("/api/resources", 200, "{broken json")

        res2 = run_acceptance_checks(
            control_url=self.control_server.url,
            broker_url=self.broker_server.url,
            expected_repo_path=self.repo_dir,
        )
        self.assertEqual(res2["status"], "FAIL")
        self.assertEqual(res2["broker_resources"]["status"], "FAIL")
        for diag in res2["diagnostics"]:
            self.assertNotIn("{broken json", diag)

    def test_missing_or_invalid_fields_in_broker_endpoints(self):
        self.control_server.set_route("/api/v1/control/overview", 200, make_healthy_overview(self.repo_dir))
        # Resources endpoint returns dict without 'resources' list
        self.broker_server.set_route("/api/resources", 200, {"unexpected": []})
        self.broker_server.set_route("/api/executions", 200, make_healthy_executions())

        res = run_acceptance_checks(
            control_url=self.control_server.url,
            broker_url=self.broker_server.url,
            expected_repo_path=self.repo_dir,
        )
        self.assertEqual(res["status"], "FAIL")
        self.assertEqual(res["broker_resources"]["status"], "FAIL")
        self.assertTrue(any("missing 'resources' list" in d for d in res["diagnostics"]))

    def test_stale_monitor_state(self):
        overview = make_healthy_overview(self.repo_dir)
        overview["data"]["monitor"]["stale"] = True
        self.control_server.set_route("/api/v1/control/overview", 200, overview)
        self.broker_server.set_route("/api/resources", 200, make_healthy_resources())
        self.broker_server.set_route("/api/executions", 200, make_healthy_executions())

        res = run_acceptance_checks(
            control_url=self.control_server.url,
            broker_url=self.broker_server.url,
            expected_repo_path=self.repo_dir,
        )
        self.assertEqual(res["status"], "FAIL")
        self.assertEqual(res["checks"]["monitor_freshness"]["status"], "FAIL")
        self.assertTrue(any("heartbeat is stale" in d for d in res["diagnostics"]))

    def test_degraded_control_state(self):
        overview = make_healthy_overview(self.repo_dir)
        overview["data"]["control_health"]["degraded"] = True
        overview["data"]["control_health"]["degraded_reason"] = "corrupt command store"
        self.control_server.set_route("/api/v1/control/overview", 200, overview)
        self.broker_server.set_route("/api/resources", 200, make_healthy_resources())
        self.broker_server.set_route("/api/executions", 200, make_healthy_executions())

        res = run_acceptance_checks(
            control_url=self.control_server.url,
            broker_url=self.broker_server.url,
            expected_repo_path=self.repo_dir,
        )
        self.assertEqual(res["status"], "FAIL")
        self.assertEqual(res["checks"]["control_authority"]["status"], "FAIL")
        self.assertTrue(any("control store is degraded" in d for d in res["diagnostics"]))

    def test_control_authority_disabled(self):
        overview = make_healthy_overview(self.repo_dir)
        overview["data"]["control_enabled"] = False
        self.control_server.set_route("/api/v1/control/overview", 200, overview)
        self.broker_server.set_route("/api/resources", 200, make_healthy_resources())
        self.broker_server.set_route("/api/executions", 200, make_healthy_executions())

        res = run_acceptance_checks(
            control_url=self.control_server.url,
            broker_url=self.broker_server.url,
            expected_repo_path=self.repo_dir,
        )
        self.assertEqual(res["status"], "FAIL")
        self.assertEqual(res["checks"]["control_authority"]["status"], "FAIL")
        self.assertTrue(any("control authority is not enabled" in d for d in res["diagnostics"]))

    def test_accounting_unavailable(self):
        overview = make_healthy_overview(self.repo_dir)
        overview["data"]["accounting"] = {"available": False, "data_status": "unavailable", "error": "ledger locked"}
        self.control_server.set_route("/api/v1/control/overview", 200, overview)
        self.broker_server.set_route("/api/resources", 200, make_healthy_resources())
        self.broker_server.set_route("/api/executions", 200, make_healthy_executions())

        res = run_acceptance_checks(
            control_url=self.control_server.url,
            broker_url=self.broker_server.url,
            expected_repo_path=self.repo_dir,
        )
        self.assertEqual(res["status"], "FAIL")
        self.assertEqual(res["checks"]["accounting"]["status"], "FAIL")
        self.assertTrue(any("accounting unavailable" in d for d in res["diagnostics"]))

    def test_project_id_mismatch(self):
        self._setup_healthy_endpoints()
        res = run_acceptance_checks(
            control_url=self.control_server.url,
            broker_url=self.broker_server.url,
            project_id="non_existent_project",
            expected_repo_path=self.repo_dir,
        )
        self.assertEqual(res["status"], "FAIL")
        self.assertEqual(res["checks"]["project_identity"]["status"], "FAIL")
        self.assertTrue(any("non_existent_project" in d for d in res["diagnostics"]))

    def test_project_repo_path_mismatch(self):
        self._setup_healthy_endpoints()
        res = run_acceptance_checks(
            control_url=self.control_server.url,
            broker_url=self.broker_server.url,
            project_id="devorchestrator",
            expected_repo_path="C:/some/other/repo/path",
        )
        self.assertEqual(res["status"], "FAIL")
        self.assertEqual(res["checks"]["project_identity"]["status"], "FAIL")
        self.assertTrue(any("repo path mismatch" in d for d in res["diagnostics"]))

    def test_project_branch_mismatch(self):
        self._setup_healthy_endpoints()
        res = run_acceptance_checks(
            control_url=self.control_server.url,
            broker_url=self.broker_server.url,
            project_id="devorchestrator",
            expected_repo_path=self.repo_dir,
            expected_branch="feature/unexpected",
        )
        self.assertEqual(res["status"], "FAIL")
        self.assertEqual(res["checks"]["project_identity"]["status"], "FAIL")
        self.assertTrue(any("branch mismatch" in d for d in res["diagnostics"]))

    def test_non_loopback_url_rejection(self):
        with self.assertRaises(ValueError):
            validate_loopback_url("http://example.com:8770", "control_url")
        with self.assertRaises(ValueError):
            validate_loopback_url("https://127.0.0.1:8770", "control_url")
        with self.assertRaises(ValueError):
            validate_loopback_url("http://192.168.1.100:8770", "control_url")
        with self.assertRaises(ValueError):
            validate_loopback_url("http://127.0.0.1:8770/overview?foo=1", "control_url")

        # Subprocess check
        proc = subprocess.run(
            [
                sys.executable,
                str(self.script_path),
                "--control-url", "http://example.com:8770",
                "--broker-url", self.broker_server.url,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        parsed = json.loads(proc.stdout)
        self.assertEqual(parsed["status"], "FAIL")
        self.assertTrue(any("must target loopback address" in d for d in parsed["diagnostics"]))

    def test_path_normalization_handles_casing_and_slashes(self):
        path1 = canonical_path("C:/work/github/DevOrchestrator-dev")
        path2 = canonical_path("c:\\work\\github\\devorchestrator-dev")
        self.assertEqual(path1, path2)

    def test_userinfo_credentials_rejection_and_no_leak_in_diagnostics(self):
        with self.assertRaises(ValueError) as ctx:
            validate_loopback_url("http://user:secret123@127.0.0.1:8770", "control_url")
        self.assertIn("must not contain credentials", str(ctx.exception))
        self.assertNotIn("secret123", str(ctx.exception))

        with self.assertRaises(ValueError):
            validate_loopback_url("http://user@127.0.0.1:8770", "control_url")
        with self.assertRaises(ValueError):
            validate_loopback_url("http://:secret123@localhost:8770", "control_url")

        # Subprocess execution must reject credentials without leaking them
        proc = subprocess.run(
            [
                sys.executable,
                str(self.script_path),
                "--control-url", "http://admin:super_secret_token@127.0.0.1:8770",
                "--broker-url", self.broker_server.url,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("super_secret_token", proc.stdout)
        self.assertNotIn("super_secret_token", proc.stderr)
        parsed = json.loads(proc.stdout)
        self.assertEqual(parsed["status"], "FAIL")
        self.assertTrue(any("must not contain credentials" in d for d in parsed["diagnostics"]))

    def test_http_redirects_rejected_without_following(self):
        # Point overview to a 302 redirect targeting a secondary path
        self._setup_healthy_endpoints()
        self.control_server.set_route(
            "/api/v1/control/overview",
            302,
            b"Redirecting",
            "text/plain",
            headers={"Location": f"{self.control_server.url}/evil_redirect_target"},
        )
        self.control_server.set_route(
            "/evil_redirect_target",
            200,
            make_healthy_overview(self.repo_dir),
        )

        res = run_acceptance_checks(
            control_url=self.control_server.url,
            broker_url=self.broker_server.url,
            expected_repo_path=self.repo_dir,
        )
        self.assertEqual(res["status"], "FAIL")
        self.assertEqual(res["checks"]["daemon_health"]["status"], "FAIL")
        self.assertTrue(any("redirect" in d.lower() for d in res["diagnostics"]))

        # Confirm the redirect target was NEVER fetched
        control_paths = [path for _, path in self.control_server.recorded_requests]
        self.assertNotIn("/evil_redirect_target", control_paths)

    def test_stale_unified_broker_resources_fails_acceptance(self):
        # 1. Stale availability in data.resources
        overview = make_healthy_overview(self.repo_dir)
        overview["data"]["resources"]["availability"] = "stale"
        overview["sources"] = [
            {"name": "monitor", "availability": "available"},
            {"name": "control", "availability": "available"},
            {"name": "accounting", "availability": "available"},
            {"name": "broker_resources", "availability": "stale"},
            {"name": "broker_executions", "availability": "available"},
        ]
        self.control_server.set_route("/api/v1/control/overview", 200, overview)
        self.broker_server.set_route("/api/resources", 200, make_healthy_resources())
        self.broker_server.set_route("/api/executions", 200, make_healthy_executions())

        res = run_acceptance_checks(
            control_url=self.control_server.url,
            broker_url=self.broker_server.url,
            expected_repo_path=self.repo_dir,
        )
        self.assertEqual(res["status"], "FAIL")
        self.assertEqual(res["overall_status"], "FAIL")
        self.assertEqual(res["checks"]["broker_resources"]["status"], "FAIL")
        self.assertEqual(res["broker_resources"]["status"], "FAIL")
        self.assertFalse(res["broker_resources"]["unified_visible"])
        self.assertTrue(any("stale" in d.lower() for d in res["diagnostics"]))

        # Subprocess must return non-zero
        proc = subprocess.run(
            [
                sys.executable,
                str(self.script_path),
                "--control-url", self.control_server.url,
                "--broker-url", self.broker_server.url,
                "--expected-repo-path", self.repo_dir,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        parsed = json.loads(proc.stdout)
        self.assertEqual(parsed["status"], "FAIL")
        self.assertEqual(parsed["checks"]["broker_resources"]["status"], "FAIL")

        # 2. stale=True flag in data.resources
        overview2 = make_healthy_overview(self.repo_dir)
        overview2["data"]["resources"]["stale"] = True
        self.control_server.set_route("/api/v1/control/overview", 200, overview2)
        res2 = run_acceptance_checks(
            control_url=self.control_server.url,
            broker_url=self.broker_server.url,
            expected_repo_path=self.repo_dir,
        )
        self.assertEqual(res2["status"], "FAIL")
        self.assertEqual(res2["checks"]["broker_resources"]["status"], "FAIL")

    def test_stale_unified_broker_executions_fails_acceptance(self):
        # 1. Stale availability in data.executions
        overview = make_healthy_overview(self.repo_dir)
        overview["data"]["executions"]["availability"] = "stale"
        overview["sources"] = [
            {"name": "monitor", "availability": "available"},
            {"name": "control", "availability": "available"},
            {"name": "accounting", "availability": "available"},
            {"name": "broker_resources", "availability": "available"},
            {"name": "broker_executions", "availability": "stale"},
        ]
        self.control_server.set_route("/api/v1/control/overview", 200, overview)
        self.broker_server.set_route("/api/resources", 200, make_healthy_resources())
        self.broker_server.set_route("/api/executions", 200, make_healthy_executions())

        res = run_acceptance_checks(
            control_url=self.control_server.url,
            broker_url=self.broker_server.url,
            expected_repo_path=self.repo_dir,
        )
        self.assertEqual(res["status"], "FAIL")
        self.assertEqual(res["overall_status"], "FAIL")
        self.assertEqual(res["checks"]["broker_executions"]["status"], "FAIL")
        self.assertEqual(res["broker_executions"]["status"], "FAIL")
        self.assertFalse(res["broker_executions"]["unified_visible"])
        self.assertTrue(any("stale" in d.lower() for d in res["diagnostics"]))

        # Subprocess must return non-zero
        proc = subprocess.run(
            [
                sys.executable,
                str(self.script_path),
                "--control-url", self.control_server.url,
                "--broker-url", self.broker_server.url,
                "--expected-repo-path", self.repo_dir,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        parsed = json.loads(proc.stdout)
        self.assertEqual(parsed["status"], "FAIL")
        self.assertEqual(parsed["checks"]["broker_executions"]["status"], "FAIL")

        # 2. stale=True flag in data.executions
        overview2 = make_healthy_overview(self.repo_dir)
        overview2["data"]["executions"]["stale"] = True
        self.control_server.set_route("/api/v1/control/overview", 200, overview2)
        res2 = run_acceptance_checks(
            control_url=self.control_server.url,
            broker_url=self.broker_server.url,
            expected_repo_path=self.repo_dir,
        )
        self.assertEqual(res2["status"], "FAIL")
        self.assertEqual(res2["checks"]["broker_executions"]["status"], "FAIL")

    def test_stale_direct_broker_endpoints_fail_acceptance(self):
        # Direct resources reports stale
        overview = make_healthy_overview(self.repo_dir)
        self.control_server.set_route("/api/v1/control/overview", 200, overview)
        stale_res = make_healthy_resources()
        stale_res["availability"] = "stale"
        self.broker_server.set_route("/api/resources", 200, stale_res)
        self.broker_server.set_route("/api/executions", 200, make_healthy_executions())

        res = run_acceptance_checks(
            control_url=self.control_server.url,
            broker_url=self.broker_server.url,
            expected_repo_path=self.repo_dir,
        )
        self.assertEqual(res["status"], "FAIL")
        self.assertEqual(res["checks"]["broker_resources"]["status"], "FAIL")
        self.assertEqual(res["broker_resources"]["status"], "FAIL")

        # Direct executions reports stale
        self.broker_server.set_route("/api/resources", 200, make_healthy_resources())
        stale_exec = make_healthy_executions()
        stale_exec["availability"] = "stale"
        self.broker_server.set_route("/api/executions", 200, stale_exec)

        res2 = run_acceptance_checks(
            control_url=self.control_server.url,
            broker_url=self.broker_server.url,
            expected_repo_path=self.repo_dir,
        )
        self.assertEqual(res2["status"], "FAIL")
        self.assertEqual(res2["checks"]["broker_executions"]["status"], "FAIL")
        self.assertEqual(res2["broker_executions"]["status"], "FAIL")


    def test_ipv6_loopback_url_preserves_brackets(self):
        self.assertEqual(validate_loopback_url("http://[::1]:8770", "control_url"), "http://[::1]:8770")
        with self.assertRaises(ValueError):
            validate_loopback_url("http://[2001:db8::1]:8770", "control_url")

    def test_malformed_overview_collections_fail_closed_without_traceback(self):
        self.broker_server.set_route("/api/resources", 200, make_healthy_resources())
        self.broker_server.set_route("/api/executions", 200, make_healthy_executions())
        for field in ("sources", "warnings"):
            with self.subTest(field=field):
                overview = make_healthy_overview(self.repo_dir)
                overview[field] = None
                self.control_server.set_route("/api/v1/control/overview", 200, overview)
                res = run_acceptance_checks(
                    control_url=self.control_server.url,
                    broker_url=self.broker_server.url,
                    expected_repo_path=self.repo_dir,
                )
                self.assertEqual(res["overall_status"], "FAIL")
                self.assertTrue(any(field in d for d in res["diagnostics"]))
                proc = subprocess.run(
                    [sys.executable, str(self.script_path), "--control-url", self.control_server.url,
                     "--broker-url", self.broker_server.url, "--expected-repo-path", self.repo_dir],
                    capture_output=True, text=True, check=False,
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertEqual(json.loads(proc.stdout)["overall_status"], "FAIL")

    def test_invalid_nested_project_identity_objects_fail_closed(self):
        self.broker_server.set_route("/api/resources", 200, make_healthy_resources())
        self.broker_server.set_route("/api/executions", 200, make_healthy_executions())
        for field in ("git", "control_identity"):
            with self.subTest(field=field):
                overview = make_healthy_overview(self.repo_dir)
                overview["data"]["projects"][0][field] = "invalid"
                self.control_server.set_route("/api/v1/control/overview", 200, overview)
                res = run_acceptance_checks(
                    control_url=self.control_server.url,
                    broker_url=self.broker_server.url,
                    expected_repo_path=self.repo_dir,
                )
                self.assertEqual(res["overall_status"], "FAIL")
                self.assertTrue(any(field in d for d in res["diagnostics"]))


if __name__ == "__main__":
    unittest.main()
