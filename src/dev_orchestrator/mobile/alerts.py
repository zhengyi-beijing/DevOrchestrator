"""Mobile notification evaluator and alert policy engine.

Enforces strict presentation-only boundary:
- Notification thresholds and rules are presentation policy only.
- Alerts derive from authoritative progress and transport observations; they cannot
  independently classify lifecycle state or promote running/unknown to stalled.
- Progress and transport alert families are disjoint.
- Absent, stale, or unavailable progress observations produce zero progress alerts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, List, Mapping, Optional

from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json

DEFAULT_ALERT_POLICY = {
    "schema_version": 1,
    "enabled": True,
    "sound_enabled": True,
    "vibration_enabled": True,
    "stall_threshold_seconds": 600,
    "quiet_hours": {
        "enabled": False,
        "start": "22:00",
        "end": "08:00",
    },
    "projects": {},  # project_id -> {stall_threshold_seconds, quiet_hours, ...}
}


@dataclass(frozen=True)
class NotificationItem:
    """Individual alert notification item."""

    alert_key: str
    family: str  # 'progress' or 'transport'
    alert_type: str  # 'stall', 'owner_gate', 'disconnected', 'degraded'
    project_id: Optional[str]
    title: str
    message: str
    severity: str  # 'info', 'warn', 'critical'
    sound: bool
    vibrate: bool
    dedup_key: str
    occurred_at: str


@dataclass(frozen=True)
class NotificationDecision:
    """Outcome of notification evaluation."""

    should_notify: bool
    reason: str
    notifications: List[NotificationItem]


def _in_quiet_hours(now_time: dtime, start_str: str, end_str: str) -> bool:
    try:
        sh, sm = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))
        start = dtime(sh, sm)
        end = dtime(eh, em)
    except (ValueError, TypeError):
        return False

    if start <= end:
        return start <= now_time < end
    # Crosses midnight (e.g. 22:00 to 08:00)
    return now_time >= start or now_time < end


def evaluate_notification(
    progress_observation: Optional[Mapping[str, Any]],
    transport_observation: Mapping[str, Any],
    policy: Mapping[str, Any],
    now: datetime,
    ack_state: Mapping[str, Any],
) -> NotificationDecision:
    """Pure two-input evaluator for mobile alerts.

    Args:
        progress_observation: Optional authoritative progress observation from daemon.
        transport_observation: Connectivity / gateway health observation.
        policy: Alert policy mapping.
        now: Current evaluation timestamp.
        ack_state: Mapping of dedup_key -> ack/snooze state.

    Returns:
        NotificationDecision with list of actionable notifications.
    """
    if not policy.get("enabled", True):
        return NotificationDecision(should_notify=False, reason="alert policy disabled", notifications=[])

    items: List[NotificationItem] = []
    now_iso = now.isoformat()
    now_time = now.time()

    # 1. Transport-family alerts (evaluable even when progress is absent)
    transport_connected = transport_observation.get("connected", True)
    transport_degraded = transport_observation.get("degraded", False)

    if not transport_connected:
        dedup = "transport:disconnected"
        items.append(
            NotificationItem(
                alert_key=dedup,
                family="transport",
                alert_type="disconnected",
                project_id=None,
                title="DevOrchestrator Disconnected",
                message=str(transport_observation.get("reason") or "Tailscale gateway connection lost"),
                severity="critical",
                sound=bool(policy.get("sound_enabled", True)),
                vibrate=bool(policy.get("vibration_enabled", True)),
                dedup_key=dedup,
                occurred_at=now_iso,
            )
        )
    elif transport_degraded:
        dedup = "transport:degraded"
        items.append(
            NotificationItem(
                alert_key=dedup,
                family="transport",
                alert_type="degraded",
                project_id=None,
                title="DevOrchestrator Transport Degraded",
                message=str(transport_observation.get("reason") or "Gateway health degraded"),
                severity="warn",
                sound=bool(policy.get("sound_enabled", True)),
                vibrate=bool(policy.get("vibration_enabled", True)),
                dedup_key=dedup,
                occurred_at=now_iso,
            )
        )

    # 2. Progress-family alerts (ONLY from authoritative progress observation)
    if progress_observation is not None:
        obs_state = progress_observation.get("progress_observation_state")
        if obs_state == "authoritative":
            project_id = str(progress_observation.get("project_id") or "")
            watchdog_state = progress_observation.get("watchdog_state")
            owner_gate = progress_observation.get("owner_gate")

            # Progress stall alert: MUST be watchdog agent_stalled
            if watchdog_state == "agent_stalled":
                dedup = f"progress:stall:{project_id}"
                items.append(
                    NotificationItem(
                        alert_key=dedup,
                        family="progress",
                        alert_type="stall",
                        project_id=project_id,
                        title=f"Project {project_id} Stalled",
                        message=str(progress_observation.get("watchdog_reason") or "Watchdog confirmed agent stall"),
                        severity="critical",
                        sound=bool(policy.get("sound_enabled", True)),
                        vibrate=bool(policy.get("vibration_enabled", True)),
                        dedup_key=dedup,
                        occurred_at=now_iso,
                    )
                )

            # Owner gate alert
            if isinstance(owner_gate, dict) and owner_gate.get("state") == "owner_gate":
                gate_id = owner_gate.get("gate_id") or owner_gate.get("request_id")
                dedup = f"progress:owner_gate:{project_id}:{gate_id}"
                items.append(
                    NotificationItem(
                        alert_key=dedup,
                        family="progress",
                        alert_type="owner_gate",
                        project_id=project_id,
                        title=f"Owner Gate Required: {project_id}",
                        message=str(owner_gate.get("reason") or "Human owner approval required"),
                        severity="warn",
                        sound=bool(policy.get("sound_enabled", True)),
                        vibrate=bool(policy.get("vibration_enabled", True)),
                        dedup_key=dedup,
                        occurred_at=now_iso,
                    )
                )

    # 3. Apply quiet hours, snoozing, and acknowledgement filters
    quiet_cfg = policy.get("quiet_hours") or {}
    is_quiet = quiet_cfg.get("enabled") and _in_quiet_hours(
        now_time, quiet_cfg.get("start", "22:00"), quiet_cfg.get("end", "08:00")
    )

    filtered_items: List[NotificationItem] = []
    for item in items:
        ack_info = ack_state.get(item.dedup_key)
        if isinstance(ack_info, dict):
            if ack_info.get("acknowledged"):
                continue
            snooze_until = ack_info.get("snoozed_until")
            if snooze_until:
                try:
                    snooze_dt = datetime.fromisoformat(snooze_until)
                    if now < snooze_dt:
                        continue
                except (ValueError, TypeError):
                    pass

        if is_quiet and item.severity != "critical":
            continue

        filtered_items.append(item)

    return NotificationDecision(
        should_notify=bool(filtered_items),
        reason=f"{len(filtered_items)} notifications to dispatch" if filtered_items else "no active notifications",
        notifications=filtered_items,
    )


def load_alert_policy(runtime_root: Path | str) -> dict[str, Any]:
    """Load mobile alert policy from runtime/mobile/alert-policy.json."""
    path = Path(runtime_root) / "mobile" / "alert-policy.json"
    data = read_json(path, None)
    if isinstance(data, dict) and data.get("schema_version") == 1:
        return data
    return dict(DEFAULT_ALERT_POLICY)


def save_alert_policy(runtime_root: Path | str, policy: dict[str, Any]) -> None:
    """Save mobile alert policy to runtime/mobile/alert-policy.json."""
    path = Path(runtime_root) / "mobile" / "alert-policy.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, policy, indent=2)
