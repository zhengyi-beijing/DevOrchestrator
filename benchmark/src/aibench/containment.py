"""Windows containment protocol, SID verification, and queue dispatch."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from .contracts import (
    ContainmentAuditResult,
    ContainmentConfig,
    sha256_bytes,
    utc_now_iso,
)


def get_current_user_sid() -> tuple[str, str]:
    """Return (user_name, sid_string) using whoami /user."""
    try:
        proc = subprocess.run(
            ["whoami", "/user"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10.0,
        )
        if proc.returncode == 0:
            lines = proc.stdout.splitlines()
            for line in lines:
                parts = line.strip().split()
                if len(parts) >= 2 and parts[-1].startswith("S-1-"):
                    return parts[0], parts[-1]
    except Exception:
        pass
    return os.environ.get("USERNAME", "unknown_user"), ""


def check_directory_write_denied(dir_path: Path) -> bool:
    """Check if write/delete access to directory is denied without creating or modifying any file.

    Opens a directory handle requesting FILE_WRITE_DATA | FILE_ADD_FILE | DELETE.
    Returns True if access was denied (PermissionError / access denied), False if granted.
    """
    if not dir_path.exists():
        return False

    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        CreateFileW = kernel32.CreateFileW
        CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        CreateFileW.restype = wintypes.HANDLE
        CloseHandle = kernel32.CloseHandle
        CloseHandle.argtypes = [wintypes.HANDLE]
        CloseHandle.restype = wintypes.BOOL

        FILE_WRITE_DATA = 0x0002
        FILE_ADD_FILE = 0x0002
        DELETE = 0x00010000
        desired_access = FILE_WRITE_DATA | FILE_ADD_FILE | DELETE
        share_mode = 1 | 2 | 4  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
        creation_disposition = 3  # OPEN_EXISTING
        flags = 0x02000000  # FILE_FLAG_BACKUP_SEMANTICS (required to open directories)

        handle = CreateFileW(
            str(dir_path.resolve()),
            desired_access,
            share_mode,
            None,
            creation_disposition,
            flags,
            None,
        )
        invalid_val = wintypes.HANDLE(-1).value
        if handle in (-1, invalid_val, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
            return True
        else:
            CloseHandle(handle)
            return False
    else:
        return not os.access(dir_path, os.W_OK)


def _is_path_disjoint(protected_root: Path, target_dir: Path) -> bool:
    """Verify target_dir is strictly outside protected_root."""
    try:
        p_resolved = protected_root.resolve()
        t_resolved = target_dir.resolve()
        # Check if t_resolved starts with or is within p_resolved
        if t_resolved == p_resolved or p_resolved in t_resolved.parents:
            return False
        return True
    except Exception:
        return False


def audit_containment(
    config: ContainmentConfig,
    allow_mock_sid: bool = False,
    allow_unrestricted_dev_roots: bool = False,
) -> ContainmentAuditResult:
    """Audit containment boundary: dedicated SID, protected roots isolation, and scratch access."""
    details: dict[str, Any] = {}

    # 1. Identity Check
    user_name, current_sid = get_current_user_sid()
    details["current_user"] = user_name
    details["current_sid"] = current_sid
    details["expected_sid"] = config.dedicated_sid

    if allow_mock_sid:
        sid_match = True
    elif not config.dedicated_sid:
        sid_match = False
    else:
        sid_match = bool(current_sid and current_sid.lower() == config.dedicated_sid.lower())

    # 2. Scratch and Queue Root Isolation Check
    scratch_p = Path(config.scratch_root)
    queue_p = Path(config.queue_root)
    isolation_errors: list[str] = []

    for prot_str in config.protected_roots:
        prot_p = Path(prot_str)
        if prot_p.exists():
            if not _is_path_disjoint(prot_p, scratch_p):
                isolation_errors.append(f"scratch_root {scratch_p} is inside protected root {prot_p}")
            if not _is_path_disjoint(prot_p, queue_p):
                isolation_errors.append(f"queue_root {queue_p} is inside protected root {prot_p}")

    details["isolation_errors"] = isolation_errors

    # 3. Protected Roots Write/Delete Denial Check (without creating or modifying any file)
    acl_verified = True
    acl_details: list[dict[str, Any]] = []

    for prot_str in config.protected_roots:
        prot_p = Path(prot_str)
        if not prot_p.exists():
            continue
        denied = check_directory_write_denied(prot_p)
        acl_details.append({"root": prot_str, "write_denied": denied})
        if not denied and not allow_unrestricted_dev_roots:
            acl_verified = False

    details["acl_checks"] = acl_details

    # 4. Scratch Mutation Canary Check
    scratch_verified = False
    canary_file = scratch_p / f"_aibench_scratch_canary_{uuid4().hex[:6]}.tmp"
    renamed_file = scratch_p / f"_aibench_scratch_canary_{uuid4().hex[:6]}_renamed.tmp"
    try:
        scratch_p.mkdir(parents=True, exist_ok=True)
        canary_file.write_text("scratch_test_data", encoding="utf-8")
        read_back = canary_file.read_text(encoding="utf-8")
        canary_file.rename(renamed_file)
        renamed_file.unlink()
        scratch_verified = (read_back == "scratch_test_data")
    except Exception as exc:
        details["scratch_error"] = str(exc)
        scratch_verified = False

    # 5. Registry Verification Check
    registry_p = Path(config.broker_config_path)
    registry_verified = registry_p.is_file() and os.access(registry_p, os.R_OK)
    details["registry_exists"] = registry_verified

    passed = sid_match and (len(isolation_errors) == 0) and acl_verified and scratch_verified and registry_verified

    return ContainmentAuditResult(
        sid_match=sid_match,
        deny_acl_verified=acl_verified and len(isolation_errors) == 0,
        scratch_verified=scratch_verified,
        registry_verified=registry_verified,
        passed=passed,
        details=details,
    )


def submit_queue_request(queue_dir: Path, plan_path: Path, plan_hash: str) -> str:
    """Submit a trial execution request to the external worker queue."""
    queue_dir.mkdir(parents=True, exist_ok=True)
    nonce = uuid4().hex
    request_data = {
        "nonce": nonce,
        "plan_path": str(plan_path.resolve()),
        "plan_hash": plan_hash,
        "submitted_at": utc_now_iso(),
    }
    request_file = queue_dir / f"request-{nonce}.json"
    temp_file = queue_dir / f"request-{nonce}.tmp"
    temp_file.write_text(json.dumps(request_data, indent=2), encoding="utf-8")
    temp_file.rename(request_file)
    return nonce


def poll_queue_result(queue_dir: Path, nonce: str, timeout_seconds: float = 30.0) -> dict[str, Any]:
    """Poll for worker result corresponding to nonce."""
    result_file = queue_dir / f"result-{nonce}.json"
    start_time = time.monotonic()
    while time.monotonic() - start_time < timeout_seconds:
        if result_file.exists():
            try:
                data = json.loads(result_file.read_text(encoding="utf-8"))
                return data
            except Exception:
                pass
        time.sleep(0.5)
    raise TimeoutError(f"timed out waiting for worker result {nonce}")
