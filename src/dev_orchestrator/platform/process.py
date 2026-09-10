"""Cross-platform process liveness/lifecycle helpers.

Business logic never branches on the OS; all OS-specific behavior lives here
(detached-process launch and PID liveness/termination). Liveness checks never
create or attach to processes; termination only ever targets an explicitly
recorded DevOrchestrator-owned PID.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

if os.name == "nt":  # pragma: no cover - exercised on Windows hosts
    import ctypes

    _kernel32 = ctypes.windll.kernel32
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PROCESS_QUERY_INFORMATION = 0x0400
    _SYNCHRONIZE = 0x00100000
    _STILL_ACTIVE = 259

    def _windows_pid_alive(pid: int) -> bool:
        for access in (
            _SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION,
            _PROCESS_QUERY_LIMITED_INFORMATION,
            _PROCESS_QUERY_INFORMATION,
        ):
            handle = _kernel32.OpenProcess(access, False, pid)
            if not handle:
                continue
            try:
                code = ctypes.c_ulong()
                if _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == _STILL_ACTIVE
                return True  # cannot read exit code but the process exists
            finally:
                _kernel32.CloseHandle(handle)
        return False

    def _windows_terminate(pid: int) -> bool:
        # ``os.kill`` is blocked by some sandboxed hosts; a direct
        # OpenProcess(PROCESS_TERMINATE) + TerminateProcess targets only ``pid``.
        _PROCESS_TERMINATE = 0x0001
        handle = _kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
        if not handle:
            return False
        try:
            return bool(_kernel32.TerminateProcess(handle, 15))
        finally:
            _kernel32.CloseHandle(handle)

else:  # pragma: no cover - exercised on POSIX hosts

    def _posix_pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True


def is_pid_alive(pid: Any) -> bool:
    """Return whether ``pid`` refers to a live process.

    Non-numeric, non-positive and unknown pids report ``False``. On Windows a
    query-limited handle is used; the process is never signaled.
    """
    if pid is None:
        return False
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - Windows
        return _windows_pid_alive(pid_int)
    return _posix_pid_alive(pid_int)  # pragma: no cover - POSIX


def terminate_pid(pid: Any) -> bool:
    """Terminate only the process with the recorded ``pid``.

    Never walks a process tree and never targets unrelated processes.
    """
    if pid is None:
        return False
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - Windows
        return _windows_terminate(pid_int)
    try:
        os.kill(pid_int, signal.SIGTERM)
        return True
    except OSError:
        return False


def terminate_process_tree(pid: Any) -> bool:
    """Terminate a recorded DevOrchestrator process and its descendants."""
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    if os.name == "nt":
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid_int), "/T", "/F"],
            capture_output=True, text=True, check=False, **hidden_subprocess_kwargs(),
        )
        return completed.returncode == 0 or not is_pid_alive(pid_int)
    try:
        os.killpg(os.getpgid(pid_int), signal.SIGTERM)
        return True
    except OSError:
        return False


def hidden_subprocess_kwargs() -> dict[str, int]:
    """Return Popen kwargs that suppress console windows on Windows.

    Background monitor/backend child processes must never create a visible
    console or steal focus from the interactive desktop. POSIX needs no extra
    flags, so callers can safely splat the returned mapping into run/Popen.
    """
    if os.name != "nt":
        return {}
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return {"creationflags": int(flag)}


def spawn_detached(
    argv: Sequence[str],
    *,
    cwd: Optional[PathLike] = None,
    env: Optional[dict] = None,
) -> subprocess.Popen:
    """Launch ``argv`` fully detached from the current console/session.

    On Windows the child uses ``DETACHED_PROCESS`` + ``CREATE_NEW_PROCESS_GROUP``;
    on POSIX a new session. The child outlives this process and must be stopped
    through its recorded PID file.
    """
    kwargs: dict = {
        "cwd": str(cwd) if cwd is not None else None,
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":  # pragma: no cover - Windows
        flags = 0x00000008  # DETACHED_PROCESS
        flags |= 0x00000200  # CREATE_NEW_PROCESS_GROUP
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        kwargs["creationflags"] = flags
    else:  # pragma: no cover - POSIX
        kwargs["start_new_session"] = True
    return subprocess.Popen([str(argument) for argument in argv], **kwargs)


PathLike = os.PathLike


def executable_path() -> str:
    """Interpreter path used to relaunch the module (never bare ``python``)."""
    return sys.executable
