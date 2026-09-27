"""Canonical runtime root and state-root ownership lock resolution.

Ensures explicit, fail-closed resolution of runtime and state roots,
preventing ambient derivation from code location and preventing split-brain
controller ownership of the state root.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping


def resolve_runtime_root(explicit_root: Path | str | None) -> Path:
    """Fail-closed resolution of runtime root.

    Target architecture requires an explicit, existing directory.
    Deriving runtime root implicitly from arbitrary code locations is forbidden.
    """
    if explicit_root is None:
        raise RuntimeError("FAIL_CLOSED: Runtime root cannot be derived from code location")
    path = Path(explicit_root)
    if not path.exists():
        raise RuntimeError("FAIL_CLOSED: Runtime root cannot be derived from code location")
    return path


def acquire_state_root_lock(
    state_root: Path,
    controller_root: Path,
    pid: int,
    host: str = "localhost",
) -> Mapping[str, Any] | bool:
    """Acquire or verify state-root ownership lock.

    Fails closed if the lock file is owned by another controller.
    """
    lock_file = state_root / "state_root_owner.lock"
    now_iso = datetime.now(timezone.utc).isoformat()
    if lock_file.exists():
        current = json.loads(lock_file.read_text(encoding="utf-8"))
        if current.get("controller_root") != str(controller_root):
            raise RuntimeError(
                f"LOCKED: State root is already owned by controller {current.get('controller_root')}"
            )
        return current

    payload = {
        "host": host,
        "pid": pid,
        "controller_root": str(controller_root),
        "acquired_at": now_iso,
    }
    lock_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return True
