#!/usr/bin/env python3
"""DevOrchestrator Self-Host Operational Acceptance Utility.

Read-only utility that verifies the canonical main deployment through the 8770
unified Control API and the 8875 AIBroker diagnostic API without mutating
lifecycle state.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def sanitize_url(url: str) -> str:
    """Remove userinfo / credentials from URL for safe diagnostics."""
    if not url or not isinstance(url, str):
        return ""
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
            hostname = parsed.hostname or ""
            port_part = f":{parsed.port}" if parsed.port is not None else ""
            return urllib.parse.urlunsplit((
                parsed.scheme,
                f"{hostname}{port_part}",
                parsed.path,
                parsed.query,
                parsed.fragment,
            ))
    except Exception:
        pass
    return url


def validate_loopback_url(url: str, param_name: str) -> str:
    """Validate that url is an HTTP loopback endpoint without credentials and return normalized base URL."""
    if not url or not isinstance(url, str):
        raise ValueError(f"{param_name} must be a non-empty string")
    stripped = url.strip()
    parsed = urllib.parse.urlsplit(stripped)
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError(f"{param_name} must not contain credentials")
    if parsed.scheme.lower() != "http":
        raise ValueError(f"{param_name} must use 'http' scheme: {sanitize_url(url)!r}")
    if not parsed.hostname:
        raise ValueError(f"{param_name} missing hostname: {sanitize_url(url)!r}")
    hostname = parsed.hostname.lower()
    is_loop = False
    try:
        is_loop = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        is_loop = (hostname == "localhost")
    if not is_loop:
        raise ValueError(f"{param_name} must target loopback address (127.0.0.1 or localhost): {sanitize_url(url)!r}")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{param_name} must not contain query or fragment parameters")
    port_part = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme.lower()}://{hostname}{port_part}".rstrip("/")


def canonical_path(p: str | Path | None) -> str:
    """Resolve and normalize path for cross-platform comparison."""
    if not p:
        return ""
    try:
        resolved = Path(p).resolve()
        return os.path.normcase(os.path.realpath(str(resolved)))
    except Exception:
        return os.path.normcase(str(p))


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject HTTP redirects to prevent navigating outside allowlisted loopback endpoints."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None

    def http_error_301(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        raise urllib.error.HTTPError(
            req.full_url, code, f"HTTP redirect {code} not permitted", headers, fp
        )

    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301


_OPENER = urllib.request.build_opener(NoRedirectHandler)


def fetch_json(url: str, timeout: float) -> tuple[int | None, Any, str | None]:
    """Issue a GET request to a loopback URL without following redirects.

    Returns (status_code, parsed_json, error_diagnostic).
    Response bodies and secrets are excluded from diagnostics.
    """
    safe_url = sanitize_url(url)
    req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            status = resp.status
            raw_data = resp.read()
            try:
                data = json.loads(raw_data.decode("utf-8"))
                return status, data, None
            except (UnicodeDecodeError, json.JSONDecodeError):
                return status, None, f"endpoint {safe_url!r} returned malformed JSON"
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            return exc.code, None, f"endpoint {safe_url!r} returned HTTP redirect {exc.code} (redirects not permitted)"
        return exc.code, None, f"endpoint {safe_url!r} returned HTTP {exc.code}: {exc.reason}"
    except urllib.error.URLError as exc:
        return None, None, f"endpoint {safe_url!r} connection failed: {exc.reason}"
    except (TimeoutError, OSError) as exc:
        return None, None, f"endpoint {safe_url!r} request failed: {type(exc).__name__}"


def run_acceptance_checks(
    control_url: str = "http://127.0.0.1:8770",
    broker_url: str = "http://127.0.0.1:8875",
    project_id: str = "devorchestrator",
    expected_repo_path: str | Path | None = None,
    expected_branch: str = "main",
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Execute read-only acceptance checks against Control and AIBroker surfaces."""
    validated_control = validate_loopback_url(control_url, "control_url")
    validated_broker = validate_loopback_url(broker_url, "broker_url")

    bounded_timeout = max(0.1, min(float(timeout), 60.0))
    resolved_expected_repo = (
        Path(expected_repo_path).resolve()
        if expected_repo_path is not None
        else Path(__file__).resolve().parent.parent
    )

    overview_url = f"{validated_control}/api/v1/control/overview"
    code_ov, overview, err_ov = fetch_json(overview_url, bounded_timeout)

    daemon_diags: list[str] = []
    control_diags: list[str] = []
    freshness_diags: list[str] = []
    project_identity_diags: list[str] = []
    accounting_diags: list[str] = []
    unified_res_diags: list[str] = []
    unified_exec_diags: list[str] = []

    overview_warnings: list[str] = []
    monitor_state: dict[str, Any] | None = None
    control_health: dict[str, Any] | None = None
    control_enabled: bool | None = None
    target_project: dict[str, Any] | None = None
    actual_repo_path: str | None = None
    actual_branch: str | None = None
    accounting_available: bool = False
    unified_res_visible: bool = False
    unified_exec_visible: bool = False

    if err_ov:
        overview_fail_msg = f"control overview unavailable: {err_ov}"
        daemon_diags.append(overview_fail_msg)
        control_diags.append(overview_fail_msg)
        freshness_diags.append(overview_fail_msg)
        project_identity_diags.append(overview_fail_msg)
        accounting_diags.append(overview_fail_msg)
        unified_res_diags.append(overview_fail_msg)
        unified_exec_diags.append(overview_fail_msg)
    elif not isinstance(overview, dict):
        malformed_msg = "control overview response is not a JSON object"
        daemon_diags.append(malformed_msg)
        control_diags.append(malformed_msg)
        freshness_diags.append(malformed_msg)
        project_identity_diags.append(malformed_msg)
        accounting_diags.append(malformed_msg)
        unified_res_diags.append(malformed_msg)
        unified_exec_diags.append(malformed_msg)
    else:
        overview_warnings = [
            str(w) for w in overview.get("warnings", [])
            if isinstance(w, str) and w
        ]
        sources = overview.get("sources", [])
        sources_dict = {
            s.get("name"): s.get("availability")
            for s in sources if isinstance(s, dict) and s.get("name")
        }

        data = overview.get("data")
        if not isinstance(data, dict):
            missing_data_msg = "control overview missing 'data' object"
            daemon_diags.append(missing_data_msg)
            control_diags.append(missing_data_msg)
            freshness_diags.append(missing_data_msg)
            project_identity_diags.append(missing_data_msg)
            accounting_diags.append(missing_data_msg)
            unified_res_diags.append(missing_data_msg)
            unified_exec_diags.append(missing_data_msg)
        else:
            # 1. Daemon health and monitor freshness
            monitor = data.get("monitor")
            if not isinstance(monitor, dict):
                daemon_diags.append("overview missing or invalid 'monitor' section")
                freshness_diags.append("overview missing or invalid 'monitor' section")
            else:
                monitor_state = monitor
                state = monitor.get("state")
                if state != "running":
                    daemon_diags.append(f"daemon monitor state is {state!r}, expected 'running'")
                if monitor.get("process_alive") is not True:
                    daemon_diags.append("daemon process is not alive")
                pid = monitor.get("pid")
                if not isinstance(pid, int) or pid <= 0:
                    daemon_diags.append(f"daemon monitor pid is invalid: {pid!r}")
                last_error = monitor.get("last_error")
                if last_error is not None:
                    daemon_diags.append(f"daemon monitor reported error: {last_error}")

                if monitor.get("stale") is not False:
                    freshness_diags.append("daemon monitor heartbeat is stale")

            # 2. Control authority
            control_enabled = data.get("control_enabled")
            if control_enabled is not True:
                control_diags.append("control authority is not enabled")
            c_health = data.get("control_health")
            if not isinstance(c_health, dict):
                control_diags.append("overview missing or invalid 'control_health' section")
            else:
                control_health = c_health
                if c_health.get("degraded") is not False:
                    reason = c_health.get("degraded_reason") or "unspecified"
                    control_diags.append(f"control store is degraded: {reason}")
            c_avail = sources_dict.get("control")
            if c_avail in ("degraded", "unavailable"):
                control_diags.append(f"control source availability is {c_avail!r}")

            # 3. Project identity
            projects = data.get("projects")
            if not isinstance(projects, list):
                project_identity_diags.append("overview missing or invalid 'projects' list")
            else:
                matching = [
                    p for p in projects
                    if isinstance(p, dict) and (p.get("project_id") == project_id or p.get("id") == project_id)
                ]
                if not matching:
                    project_identity_diags.append(f"project {project_id!r} not found in control overview")
                else:
                    target_project = matching[0]
                    actual_repo_path = target_project.get("repo_path") or target_project.get("root")
                    if not actual_repo_path:
                        project_identity_diags.append(f"project {project_id!r} missing repository path")
                    elif canonical_path(actual_repo_path) != canonical_path(resolved_expected_repo):
                        project_identity_diags.append(
                            f"project {project_id!r} repo path mismatch: "
                            f"expected {str(resolved_expected_repo)!r}, got {str(actual_repo_path)!r}"
                        )

                    actual_branch = (
                        (target_project.get("git") or {}).get("branch")
                        or (target_project.get("control_identity") or {}).get("branch")
                        or target_project.get("branch")
                    )
                    if actual_branch != expected_branch:
                        project_identity_diags.append(
                            f"project {project_id!r} branch mismatch: "
                            f"expected {expected_branch!r}, got {actual_branch!r}"
                        )

            # 4. P11 Accounting availability
            acct = data.get("accounting")
            if not isinstance(acct, dict):
                accounting_diags.append("overview missing or invalid 'accounting' section")
            else:
                acct_stale = acct.get("stale") is True or acct.get("availability") == "stale" or acct.get("data_status") == "stale"
                if acct_stale:
                    accounting_diags.append("P11 accounting reported stale")
                elif acct.get("available") is not True or acct.get("data_status") == "unavailable" or acct.get("availability") == "unavailable":
                    err_msg = acct.get("error") or "accounting data unavailable"
                    accounting_diags.append(f"P11 accounting unavailable: {err_msg}")
                else:
                    accounting_available = True
            if sources_dict.get("accounting") == "stale" and "stale" not in " ".join(accounting_diags):
                accounting_available = False
                accounting_diags.append("accounting source reported stale")
            elif sources_dict.get("accounting") == "unavailable" and not accounting_diags:
                accounting_available = False
                accounting_diags.append("accounting source reported unavailable")

            # 5. Unified broker resources & executions
            res = data.get("resources")
            if not isinstance(res, dict):
                unified_res_diags.append("overview missing or invalid 'resources' section")
            else:
                res_avail = res.get("availability")
                res_stale = res.get("stale") is True or res_avail == "stale"
                res_unavail = res.get("available") is False or res_avail == "unavailable"
                if res_stale:
                    err_res = res.get("error") or "broker resources reported stale"
                    unified_res_diags.append(f"broker resources reported stale through unified surface: {err_res}")
                elif res_unavail or res.get("available") is not True:
                    err_res = res.get("error") or "resources unavailable"
                    unified_res_diags.append(f"broker resources unavailable through unified surface: {err_res}")
                else:
                    unified_res_visible = True

            src_res_avail = sources_dict.get("broker_resources")
            if src_res_avail == "stale":
                unified_res_visible = False
                msg = "broker_resources source reported stale"
                if msg not in unified_res_diags:
                    unified_res_diags.append(msg)
            elif src_res_avail == "unavailable":
                unified_res_visible = False
                msg = "broker_resources source reported unavailable"
                if msg not in unified_res_diags:
                    unified_res_diags.append(msg)
            elif src_res_avail is not None and src_res_avail != "available":
                unified_res_visible = False
                msg = f"broker_resources source reported unexpected availability: {src_res_avail!r}"
                if msg not in unified_res_diags:
                    unified_res_diags.append(msg)

            ex = data.get("executions")
            if not isinstance(ex, dict):
                unified_exec_diags.append("overview missing or invalid 'executions' section")
            else:
                ex_avail = ex.get("availability")
                ex_stale = ex.get("stale") is True or ex_avail == "stale"
                ex_unavail = ex.get("available") is False or ex_avail == "unavailable"
                if ex_stale:
                    err_ex = ex.get("error") or "broker executions reported stale"
                    unified_exec_diags.append(f"broker executions reported stale through unified surface: {err_ex}")
                elif ex_unavail or ex.get("available") is not True:
                    err_ex = ex.get("error") or "executions unavailable"
                    unified_exec_diags.append(f"broker executions unavailable through unified surface: {err_ex}")
                else:
                    unified_exec_visible = True

            src_exec_avail = sources_dict.get("broker_executions")
            if src_exec_avail == "stale":
                unified_exec_visible = False
                msg = "broker_executions source reported stale"
                if msg not in unified_exec_diags:
                    unified_exec_diags.append(msg)
            elif src_exec_avail == "unavailable":
                unified_exec_visible = False
                msg = "broker_executions source reported unavailable"
                if msg not in unified_exec_diags:
                    unified_exec_diags.append(msg)
            elif src_exec_avail is not None and src_exec_avail != "available":
                unified_exec_visible = False
                msg = f"broker_executions source reported unexpected availability: {src_exec_avail!r}"
                if msg not in unified_exec_diags:
                    unified_exec_diags.append(msg)

    # Direct broker checks
    direct_res_diags: list[str] = []
    direct_res_count: int | None = None
    direct_res_visible: bool = False
    code_res, broker_res, err_res = fetch_json(f"{validated_broker}/api/resources", bounded_timeout)
    if err_res:
        direct_res_diags.append(f"direct broker /api/resources failed: {err_res}")
    elif not isinstance(broker_res, dict):
        direct_res_diags.append("direct broker /api/resources response is not a JSON object")
    elif not isinstance(broker_res.get("resources"), list):
        direct_res_diags.append("direct broker /api/resources response missing 'resources' list")
    elif broker_res.get("stale") is True or broker_res.get("availability") == "stale":
        direct_res_diags.append("direct broker /api/resources reported stale")
    elif broker_res.get("available") is False or broker_res.get("availability") == "unavailable":
        direct_res_diags.append("direct broker /api/resources reported unavailable")
    else:
        direct_res_visible = True
        direct_res_count = len(broker_res["resources"])

    direct_exec_diags: list[str] = []
    direct_exec_count: int | None = None
    direct_exec_visible: bool = False
    code_exec, broker_exec, err_exec = fetch_json(f"{validated_broker}/api/executions", bounded_timeout)
    if err_exec:
        direct_exec_diags.append(f"direct broker /api/executions failed: {err_exec}")
    elif not isinstance(broker_exec, dict):
        direct_exec_diags.append("direct broker /api/executions response is not a JSON object")
    elif not isinstance(broker_exec.get("executions"), list):
        direct_exec_diags.append("direct broker /api/executions response missing 'executions' list")
    elif broker_exec.get("stale") is True or broker_exec.get("availability") == "stale":
        direct_exec_diags.append("direct broker /api/executions reported stale")
    elif broker_exec.get("available") is False or broker_exec.get("availability") == "unavailable":
        direct_exec_diags.append("direct broker /api/executions reported unavailable")
    else:
        direct_exec_visible = True
        direct_exec_count = len(broker_exec["executions"])

    broker_resources_diags = list(unified_res_diags + direct_res_diags)
    broker_executions_diags = list(unified_exec_diags + direct_exec_diags)

    daemon_control_diags = list(daemon_diags + control_diags + freshness_diags)

    daemon_control_section = {
        "status": "PASS" if not daemon_control_diags else "FAIL",
        "daemon_state": monitor_state.get("state") if monitor_state else None,
        "daemon_pid": monitor_state.get("pid") if monitor_state else None,
        "daemon_alive": monitor_state.get("process_alive") if monitor_state else None,
        "monitor_stale": monitor_state.get("stale") if monitor_state else None,
        "daemon_last_error": monitor_state.get("last_error") if monitor_state else None,
        "control_enabled": control_enabled,
        "control_degraded": control_health.get("degraded") if control_health else None,
        "diagnostics": daemon_control_diags,
    }

    project_identity_section = {
        "status": "PASS" if not project_identity_diags else "FAIL",
        "project_id": project_id,
        "repo_path": actual_repo_path,
        "expected_repo_path": str(resolved_expected_repo),
        "branch": actual_branch,
        "expected_branch": expected_branch,
        "diagnostics": project_identity_diags,
    }

    accounting_section = {
        "status": "PASS" if not accounting_diags else "FAIL",
        "available": accounting_available,
        "diagnostics": accounting_diags,
    }

    broker_resources_section = {
        "status": "PASS" if not broker_resources_diags else "FAIL",
        "unified_visible": unified_res_visible and not unified_res_diags,
        "direct_visible": direct_res_visible and not direct_res_diags,
        "resource_count": direct_res_count,
        "diagnostics": broker_resources_diags,
    }

    broker_executions_section = {
        "status": "PASS" if not broker_executions_diags else "FAIL",
        "unified_visible": unified_exec_visible and not unified_exec_diags,
        "direct_visible": direct_exec_visible and not direct_exec_diags,
        "execution_count": direct_exec_count,
        "diagnostics": broker_executions_diags,
    }

    checks = {
        "daemon_health": {
            "status": "PASS" if not daemon_diags else "FAIL",
            "diagnostics": daemon_diags,
        },
        "control_authority": {
            "status": "PASS" if not control_diags else "FAIL",
            "diagnostics": control_diags,
        },
        "monitor_freshness": {
            "status": "PASS" if not freshness_diags else "FAIL",
            "diagnostics": freshness_diags,
        },
        "project_identity": {
            "status": "PASS" if not project_identity_diags else "FAIL",
            "diagnostics": project_identity_diags,
        },
        "accounting": {
            "status": "PASS" if not accounting_diags else "FAIL",
            "diagnostics": accounting_diags,
        },
        "broker_resources": {
            "status": "PASS" if not broker_resources_diags else "FAIL",
            "diagnostics": broker_resources_diags,
        },
        "broker_executions": {
            "status": "PASS" if not broker_executions_diags else "FAIL",
            "diagnostics": broker_executions_diags,
        },
    }

    all_diagnostics: list[str] = []
    for check_info in checks.values():
        all_diagnostics.extend(check_info["diagnostics"])

    overall_status = "PASS" if not all_diagnostics else "FAIL"

    return {
        "status": overall_status,
        "overall_status": overall_status,
        "checks": checks,
        "daemon_control": daemon_control_section,
        "project_identity": project_identity_section,
        "accounting": accounting_section,
        "broker_resources": broker_resources_section,
        "broker_executions": broker_executions_section,
        "warnings": overview_warnings,
        "diagnostics": all_diagnostics,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify DevOrchestrator self-host operational acceptance."
    )
    parser.add_argument(
        "--control-url",
        default="http://127.0.0.1:8770",
        help="Base URL for 8770 Control API (default: http://127.0.0.1:8770)",
    )
    parser.add_argument(
        "--broker-url",
        default="http://127.0.0.1:8875",
        help="Base URL for 8875 AIBroker API (default: http://127.0.0.1:8875)",
    )
    parser.add_argument(
        "--project-id",
        default="devorchestrator",
        help="Expected project ID in control overview (default: devorchestrator)",
    )
    parser.add_argument(
        "--expected-repo-path",
        default=None,
        help="Expected canonical repository path (default: repo containing utility)",
    )
    parser.add_argument(
        "--expected-branch",
        default="main",
        help="Expected repository branch (default: main)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="Request timeout in seconds (default: 5.0)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = run_acceptance_checks(
            control_url=args.control_url,
            broker_url=args.broker_url,
            project_id=args.project_id,
            expected_repo_path=args.expected_repo_path,
            expected_branch=args.expected_branch,
            timeout=args.timeout,
        )
    except ValueError as exc:
        diag = sanitize_url(str(exc))
        result = {
            "status": "FAIL",
            "overall_status": "FAIL",
            "checks": {},
            "daemon_control": {"status": "FAIL", "diagnostics": [diag]},
            "project_identity": {"status": "FAIL", "diagnostics": []},
            "accounting": {"status": "FAIL", "diagnostics": []},
            "broker_resources": {"status": "FAIL", "diagnostics": []},
            "broker_executions": {"status": "FAIL", "diagnostics": []},
            "warnings": [],
            "diagnostics": [diag],
        }
        print(json.dumps(result, indent=2))
        return 2

    print(json.dumps(result, indent=2))
    return 0 if result.get("overall_status") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
