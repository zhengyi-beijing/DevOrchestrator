"""ProjectAdapter boundary contract.

Adapters own the project-specific read-only projection of an observed
repository: the project snapshot, the Worker/task projection, and
project-specific task/stage hints. Core stays adapter-neutral and selects an
adapter purely from project configuration (daemon lifecycle, project registry,
repository truth, event/decision policy, storage, Web UI and Agent routing are
Core responsibilities).

``agent_files`` is the initial/default adapter; it understands the existing
``agent/CURRENT.md``, ``agent/next.md``, ``agent/result.md`` contract and the
configured Worker runtime. Additional adapters can be registered without
changing Core.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

DEFAULT_ADAPTER_ID = "agent_files"


class ProjectAdapter(ABC):
    """Contract implemented by every project adapter.

    A snapshot is one read-only projection of a configured project. Adapters
    never mutate the observed repository, never start Workers, and never
    advance tasks/stages; Core owns every mutation authority.
    """

    @property
    @abstractmethod
    def adapter_id(self) -> str:
        """Stable, registry-unique adapter identifier (e.g. ``agent_files``)."""

    @abstractmethod
    def snapshot(
        self,
        project: dict[str, Any],
        runs_path: Path | str,
        *,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Return one read-only projection dict for ``project``.

        ``project`` is already canonically normalized (``project_id`` /
        ``repo_path`` present). ``runs_path`` points at the DevOrchestrator
        terminal-run history used for telemetry/ETA policy.

        Adapters may optionally emit a top-level ``activity`` block containing
        ``watchdog_safe`` projection metadata. This block is required for
        project participation in the progress watchdog; ``MONITOR_ERROR``,
        ``UNAVAILABLE``, and adapters omitting the block are treated as
        activity-evidence-unavailable, suppressing automated watchdog attempts.
        """
