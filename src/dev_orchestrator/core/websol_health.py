"""Web Sol availability signals, evaluation and persistent health store."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from dev_orchestrator.accounting.events import InterProcessFileLock
from dev_orchestrator.bridge.store import BrowserBridgeStore
from dev_orchestrator.control.security import CapabilityVerdict, ControlSecurity
from dev_orchestrator.control.store import ConversationControlStore
from dev_orchestrator.storage.json_store import parse_utc, read_json, utc_now, utc_now_iso, write_json

WEBSOL_HEALTH_FILE = "websol-health.json"
HEALTH_SCHEMA_VERSION = 1
DEFAULT_HEALTH_TTL_SECONDS = 60
DEFAULT_PROBE_INITIAL_BACKOFF_SECONDS = 5.0
DEFAULT_PROBE_BACKOFF_MULTIPLIER = 2.0
DEFAULT_PROBE_MAX_BACKOFF_SECONDS = 60.0
DEFAULT_PROBE_MAX_ATTEMPTS = 3
DEFAULT_PROBE_FAILURE_RESET_SECONDS = 900.0


class WebSolAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    OFFLINE = "OFFLINE"
    PAIRING_REQUIRED = "PAIRING_REQUIRED"
    PROBE_FAILED = "PROBE_FAILED"


@dataclass(frozen=True)
class WebSolSignal:
    name: str  # bridge_listener, browser_claim_presence, control_heartbeat, binding_identity, capability, probe
    status: str  # healthy, degraded, offline, failed, unavailable, unknown, unverified
    reason: str
    observed_at: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WebSolHealth:
    project_id: str
    adapter: str
    binding_id: str
    availability: str
    evaluated_at: str
    valid_until: str
    reason: str
    signals: dict[str, Any]
    probe_generation: int = 1
    failover_summary: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        sigs: dict[str, Any] = {}
        if isinstance(self.signals, dict):
            for k, v in self.signals.items():
                if hasattr(v, "__dataclass_fields__") or hasattr(v, "__dict__"):
                    sigs[k] = asdict(v)
                elif isinstance(v, dict):
                    sigs[k] = dict(v)
                else:
                    sigs[k] = v
        else:
            sigs = self.signals
        return {
            "project_id": self.project_id,
            "adapter": self.adapter,
            "binding_id": self.binding_id,
            "availability": self.availability,
            "evaluated_at": self.evaluated_at,
            "valid_until": self.valid_until,
            "reason": self.reason,
            "signals": sigs,
            "probe_generation": self.probe_generation,
            "failover_summary": self.failover_summary,
        }


def health_key(project_id: str, adapter: str, binding_id: str) -> str:
    return f"{project_id.strip()}:{adapter.strip()}:{binding_id.strip()}"


def _as_utc(moment: Optional[datetime]) -> datetime:
    val = moment or utc_now()
    if val.tzinfo is None:
        val = val.replace(tzinfo=timezone.utc)
    return val.astimezone(timezone.utc)


def probe_bridge_listener(
    runtime_root: Path | str,
    *,
    timeout_seconds: float = 1.0,
    http_client: Optional[Callable[[str, float], tuple[int, str]]] = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Perform a loopback HTTP GET /v1/health against the address in bridge.json."""
    runtime = Path(runtime_root)
    bridge_json = runtime / "bridge.json"
    if not bridge_json.exists():
        return False, "bridge_heartbeat_missing", {}
    data = read_json(bridge_json, None)
    if not isinstance(data, dict):
        return False, "bridge_heartbeat_unreadable", {}

    listen = str(data.get("listen") or data.get("listen_address") or "127.0.0.1")
    port = data.get("port")
    if not port:
        return False, "bridge_port_missing", {}

    url = f"http://{listen}:{port}/v1/health"
    if http_client is not None:
        try:
            status, text = http_client(url, timeout_seconds)
            if status == 200:
                return True, "bridge_listener_healthy", {"status": status, "url": url}
            return False, f"bridge_http_{status}", {"status": status, "url": url, "text": text}
        except Exception as exc:
            return False, "bridge_unreachable", {"error": str(exc), "url": url}

    req = urllib.request.Request(url, headers={"User-Agent": "DevOrchestrator-WebSolHealth"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            status = resp.status
            body = resp.read().decode("utf-8")
            if status == 200:
                try:
                    payload = json.loads(body)
                    if payload.get("status") == "ok":
                        return True, "bridge_listener_healthy", {"url": url, "status": 200}
                except Exception:
                    pass
                return True, "bridge_listener_healthy", {"url": url, "status": 200}
            return False, f"bridge_http_{status}", {"url": url, "status": status}
    except Exception as exc:
        return False, "bridge_unreachable", {"error": str(exc), "url": url}


def collect_websol_signals(
    runtime_root: Path | str,
    project_id: str,
    adapter: str,
    binding_id: str,
    *,
    configured_binding: Optional[dict[str, Any]],
    bridge_store: BrowserBridgeStore,
    conversation_store: ConversationControlStore,
    security: ControlSecurity,
    health_store: Optional[WebSolHealthStore] = None,
    listener_prober: Optional[Callable[[], tuple[bool, str, dict[str, Any]]]] = None,
    now: Optional[datetime] = None,
) -> dict[str, WebSolSignal]:
    """Collect the six independent signals required for Web Sol availability."""
    moment = _as_utc(now)
    observed_iso = moment.isoformat()
    signals: dict[str, WebSolSignal] = {}

    # 1. bridge_listener
    if listener_prober is not None:
        l_ok, l_reason, l_details = listener_prober()
    else:
        l_ok, l_reason, l_details = probe_bridge_listener(runtime_root)
    signals["bridge_listener"] = WebSolSignal(
        name="bridge_listener",
        status="healthy" if l_ok else "offline",
        reason=l_reason,
        observed_at=observed_iso,
        details=l_details,
    )

    # 2. browser_claim_presence
    try:
        b_status = bridge_store.binding_status(adapter, binding_id, now=moment)
        b_bound = b_status.get("state") == "bound"
        signals["browser_claim_presence"] = WebSolSignal(
            name="browser_claim_presence",
            status="healthy" if b_bound else "offline",
            reason="active_presence" if b_bound else "no_browser_presence",
            observed_at=observed_iso,
            details=b_status,
        )
    except Exception as exc:
        signals["browser_claim_presence"] = WebSolSignal(
            name="browser_claim_presence",
            status="offline",
            reason="presence_error",
            observed_at=observed_iso,
            details={"error": str(exc)},
        )

    # 3. control_heartbeat
    try:
        s_status = conversation_store.session_status(adapter, binding_id, now=moment)
        s_state = s_status.get("state")
        active_tabs = int(s_status.get("active_tab_count") or 0)
        tabs = s_status.get("tabs") or {}

        if active_tabs > 1:
            h_status = "degraded"
            h_reason = "duplicate_tabs"
        elif s_state == "live":
            h_status = "healthy"
            h_reason = "heartbeat_fresh"
        elif s_state == "stale":
            h_status = "degraded"
            h_reason = "control_heartbeat_stale"
        else:
            h_status = "offline"
            h_reason = "control_heartbeat_missing"

        signals["control_heartbeat"] = WebSolSignal(
            name="control_heartbeat",
            status=h_status,
            reason=h_reason,
            observed_at=observed_iso,
            details={"state": s_state, "active_tab_count": active_tabs, "tab_count": len(tabs)},
        )
    except Exception as exc:
        signals["control_heartbeat"] = WebSolSignal(
            name="control_heartbeat",
            status="offline",
            reason="heartbeat_inspection_failed",
            observed_at=observed_iso,
            details={"error": str(exc)},
        )

    # 4. binding_identity
    try:
        runtime_binding = conversation_store.binding_for_project(project_id)
        if not runtime_binding:
            bi_status = "degraded"
            bi_reason = "unbound_project"
        elif (
            str(runtime_binding.get("adapter") or "") != adapter
            or str(runtime_binding.get("binding_id") or "") != binding_id
        ):
            bi_status = "degraded"
            bi_reason = "binding_mismatch"
        else:
            bi_status = "healthy"
            bi_reason = "binding_matches"

        signals["binding_identity"] = WebSolSignal(
            name="binding_identity",
            status=bi_status,
            reason=bi_reason,
            observed_at=observed_iso,
            details={
                "configured": configured_binding,
                "runtime": runtime_binding,
            },
        )
    except Exception as exc:
        signals["binding_identity"] = WebSolSignal(
            name="binding_identity",
            status="degraded",
            reason="binding_lookup_failed",
            observed_at=observed_iso,
            details={"error": str(exc)},
        )

    # 5. capability
    try:
        s_status = conversation_store.session_status(adapter, binding_id, now=moment)
        active_tabs = s_status.get("active_tab_count", 0)
        tabs = s_status.get("tabs") or {}

        live_tab_records = []
        for tab_id, tab_info in tabs.items():
            if isinstance(tab_info, dict):
                seen = parse_utc(tab_info.get("last_seen_at"))
                if seen and (moment - seen).total_seconds() < conversation_store.session_presence_seconds:
                    live_tab_records.append((tab_id, tab_info))

        if not live_tab_records:
            signals["capability"] = WebSolSignal(
                name="capability",
                status="no_active_tabs",
                reason="no_active_tabs",
                observed_at=observed_iso,
                details={},
            )
        else:
            # Check capability verdicts for live tabs
            verdicts = []
            for tab_id, tab_info in live_tab_records:
                cap_id = tab_info.get("capability_pairing_id")
                cap_src = tab_info.get("capability_source")
                if cap_src == "master":
                    verdicts.append((tab_id, CapabilityVerdict.VALID, "master"))
                elif not cap_id:
                    verdicts.append((tab_id, None, "unverified"))
                else:
                    verdict = security.capability_state(cap_id)
                    verdicts.append((tab_id, verdict, cap_id))

            # Resolution precedence: revoked/unknown > unavailable > unverified > valid
            has_revoked = any(v[1] == CapabilityVerdict.REVOKED for v in verdicts)
            has_unknown = any(v[1] == CapabilityVerdict.UNKNOWN for v in verdicts)
            has_unavailable = any(v[1] == CapabilityVerdict.UNAVAILABLE for v in verdicts)
            has_unverified = any(v[1] is None for v in verdicts)

            if has_revoked:
                c_status = "revoked"
                c_reason = "capability_revoked"
            elif has_unknown:
                c_status = "unknown"
                c_reason = "capability_unknown"
            elif has_unavailable:
                c_status = "unavailable"
                c_reason = "capability_store_unavailable"
            elif has_unverified:
                c_status = "unverified"
                c_reason = "capability_unverified"
            else:
                c_status = "healthy"
                c_reason = "valid"

            signals["capability"] = WebSolSignal(
                name="capability",
                status=c_status,
                reason=c_reason,
                observed_at=observed_iso,
                details={"verdicts": [v[1].value if hasattr(v[1], "value") else str(v[1]) for v in verdicts]},
            )
    except Exception as exc:
        signals["capability"] = WebSolSignal(
            name="capability",
            status="unavailable",
            reason="capability_store_unavailable",
            observed_at=observed_iso,
            details={"error": str(exc)},
        )

    # 6. probe
    probe_status = "pending"
    probe_reason = "probe_required"
    probe_details = {}
    if health_store is not None:
        p_info = health_store.get_probe_info(project_id, adapter, binding_id)
        if p_info:
            probe_details = p_info
            current_gen = health_store.current_generation()
            rec_gen = int(p_info.get("generation", 0))
            if rec_gen == current_gen:
                if p_info.get("success") is True:
                    probe_status = "healthy"
                    probe_reason = "probe_passed"
                elif p_info.get("success") is False:
                    probe_status = "failed"
                    probe_reason = str(p_info.get("reason") or "probe_failed")
            else:
                probe_status = "pending"
                probe_reason = "probe_generation_invalidated"
    signals["probe"] = WebSolSignal(
        name="probe",
        status=probe_status,
        reason=probe_reason,
        observed_at=observed_iso,
        details=probe_details,
    )

    return signals


def evaluate_websol_availability(
    signals: dict[str, WebSolSignal | dict[str, Any]],
    *,
    probe_generation: Optional[int] = None,
    current_probe_generation: Optional[int] = None,
    probe_passed: Optional[bool] = None,
    now: Optional[datetime] = None,
) -> tuple[WebSolAvailability, str]:
    """Pure deterministic fail-closed availability evaluation.

    Precedence:
    1. Authoritative capability revoked or unknown -> PAIRING_REQUIRED
    2. Bridge listener offline or claim presence absent/offline -> OFFLINE
    3. Failed current probe -> PROBE_FAILED
    4. Duplicate tabs, stale heartbeat, mismatched binding, legacy unverified capability,
       retryable capability-store unavailability, or missing current probe -> DEGRADED
    5. All signals healthy and current-generation probe passed -> AVAILABLE
    """
    def _sig(name: str) -> tuple[str, str]:
        obj = signals.get(name)
        if obj is None:
            return "missing", f"{name}_missing"
        if isinstance(obj, WebSolSignal):
            return obj.status, obj.reason
        if isinstance(obj, dict):
            status = str(obj.get("status") or "missing")
            reason = str(obj.get("reason") or f"{name}_missing")
            return status, reason
        return "missing", f"{name}_missing"

    cap_status, cap_reason = _sig("capability")
    bl_status, bl_reason = _sig("bridge_listener")
    cp_status, cp_reason = _sig("browser_claim_presence")
    ch_status, ch_reason = _sig("control_heartbeat")
    bi_status, bi_reason = _sig("binding_identity")
    pr_status, pr_reason = _sig("probe")

    # 1. Authoritative capability failures (revoked or unknown credential)
    if (cap_status in ("revoked", "unknown") or cap_reason in ("capability_revoked", "capability_unknown")) and cap_reason not in (
        "no_active_tabs", "no_active_tabs_missing", "capability_missing"
    ) and cap_status not in ("missing", "absent", "no_active_tabs"):
        return WebSolAvailability.PAIRING_REQUIRED, cap_reason or "capability_revoked_or_unknown"

    # 2. Bridge listener or browser claim presence offline
    if bl_status != "healthy":
        return WebSolAvailability.OFFLINE, bl_reason or "bridge_unreachable"
    if cp_status != "healthy":
        return WebSolAvailability.OFFLINE, cp_reason or "no_browser_presence"

    # 3. Probe failed in current generation
    if pr_status == "failed" or pr_reason == "probe_failed":
        return WebSolAvailability.PROBE_FAILED, pr_reason or "probe_failed"

    # 4. Degradations
    if ch_reason == "duplicate_tabs" or ch_status == "degraded":
        return WebSolAvailability.DEGRADED, ch_reason or "control_heartbeat_degraded"
    if ch_status == "offline":
        return WebSolAvailability.DEGRADED, ch_reason or "control_heartbeat_missing"
    if bi_status != "healthy":
        return WebSolAvailability.DEGRADED, bi_reason or "binding_mismatch"
    if cap_status == "unavailable":
        return WebSolAvailability.DEGRADED, "capability_store_unavailable"
    if cap_status in ("unverified", "degraded") or cap_reason == "capability_unverified":
        return WebSolAvailability.DEGRADED, "capability_unverified"
    if cap_status in ("no_active_tabs", "absent", "missing") or cap_status != "healthy":
        return WebSolAvailability.DEGRADED, cap_reason or "no_active_tabs"

    # 5. Probe required (generation mismatch or not yet passed)
    if probe_passed is None:
        pr_sig = signals.get("probe")
        pr_details = getattr(pr_sig, "details", {}) if pr_sig else {}
        if isinstance(pr_details, dict):
            probe_passed = bool(pr_details.get("success") is True)
            if probe_generation is None:
                probe_generation = int(pr_details.get("generation", 0))
        else:
            probe_passed = (pr_status == "healthy")
    if probe_generation is None:
        probe_generation = 1
    if current_probe_generation is None:
        current_probe_generation = 1

    if pr_status != "healthy" or not probe_passed or probe_generation != current_probe_generation:
        return WebSolAvailability.DEGRADED, "probe_required"

    # 6. All healthy
    return WebSolAvailability.AVAILABLE, "all_signals_healthy"


class WebSolHealthStore:
    """Thread- and process-safe persistent store for Web Sol health and probes."""

    def __init__(self, runtime_root: Path | str, *, default_ttl_seconds: int = DEFAULT_HEALTH_TTL_SECONDS) -> None:
        self.runtime_root = Path(runtime_root)
        self.health_path = self.runtime_root / WEBSOL_HEALTH_FILE
        self.lock_path = self.runtime_root / "websol-health.lock"
        self.default_ttl_seconds = max(10, int(default_ttl_seconds))
        self._lock = threading.RLock()
        self._in_flight_probes: set[str] = set()

    def is_probe_in_flight(self, project_id: str, adapter: str, binding_id: str) -> bool:
        key = health_key(project_id, adapter, binding_id)
        with self._lock:
            return key in self._in_flight_probes

    def mark_probe_in_flight(self, project_id: str, adapter: str, binding_id: str) -> bool:
        key = health_key(project_id, adapter, binding_id)
        with self._lock:
            if key in self._in_flight_probes:
                return False
            self._in_flight_probes.add(key)
            return True

    def clear_probe_in_flight(self, project_id: str, adapter: str, binding_id: str) -> None:
        key = health_key(project_id, adapter, binding_id)
        with self._lock:
            self._in_flight_probes.discard(key)

    def _empty_payload(self) -> dict[str, Any]:
        return {
            "version": HEALTH_SCHEMA_VERSION,
            "generation": 1,
            "entries": {},
            "probes": {},
            "probe_backoff": {},
        }

    def _load_data(self) -> dict[str, Any]:
        if not self.health_path.exists():
            return self._empty_payload()
        try:
            val = read_json(self.health_path, None)
            if not isinstance(val, dict) or val.get("version") != HEALTH_SCHEMA_VERSION:
                return self._empty_payload()
            val.setdefault("generation", 1)
            val.setdefault("entries", {})
            val.setdefault("probes", {})
            val.setdefault("probe_backoff", {})
            return val
        except Exception:
            return self._empty_payload()

    def current_generation(self) -> int:
        with self._lock:
            data = self._load_data()
            return int(data.get("generation", 1))

    def invalidate_generation(self, reason: str = "restart") -> int:
        """Bump probe generation, invalidating past probe successes."""
        with InterProcessFileLock(self.lock_path):
            with self._lock:
                self._in_flight_probes.clear()
            data = self._load_data()
            new_gen = int(data.get("generation", 1)) + 1
            data["generation"] = new_gen
            data["generation_invalidated_at"] = utc_now_iso()
            data["generation_invalidation_reason"] = reason
            data["probe_backoff"] = {}
            write_json(self.health_path, data, indent=2)
            return new_gen

    def sync_bindings(self, current_bindings: dict[str, str]) -> bool:
        """Track active bindings; bump generation if any project's binding changed."""
        with InterProcessFileLock(self.lock_path):
            data = self._load_data()
            known = data.setdefault("known_bindings", {})
            changed = False
            for p_id, b_sig in current_bindings.items():
                if p_id in known and known[p_id] != b_sig:
                    changed = True
                    break
            if not changed:
                for p_id in list(known.keys()):
                    if p_id not in current_bindings:
                        changed = True
                        break
            if changed:
                with self._lock:
                    self._in_flight_probes.clear()
                new_gen = int(data.get("generation", 1)) + 1
                data["generation"] = new_gen
                data["generation_invalidated_at"] = utc_now_iso()
                data["generation_invalidation_reason"] = "binding_changed"
                data["probe_backoff"] = {}
            data["known_bindings"] = dict(current_bindings)
            write_json(self.health_path, data, indent=2)
            return changed

    def get(
        self, project_id: str, adapter: str, binding_id: str, *, now: Optional[datetime] = None
    ) -> Optional[WebSolHealth]:
        """Get evaluated health for the exact binding. Expired or unreadable data is non-AVAILABLE."""
        key = health_key(project_id, adapter, binding_id)
        moment = _as_utc(now)
        with self._lock:
            data = self._load_data()
            entries = data.get("entries") or {}
            raw = entries.get(key)
            if not isinstance(raw, dict):
                return None

            valid_until = parse_utc(raw.get("valid_until"))
            availability = raw.get("availability")
            reason = raw.get("reason", "")
            if valid_until is not None and moment > valid_until:
                # Expired data is strictly non-AVAILABLE
                availability = WebSolAvailability.DEGRADED.value
                reason = "health_snapshot_expired"

            return WebSolHealth(
                project_id=str(raw.get("project_id")),
                adapter=str(raw.get("adapter")),
                binding_id=str(raw.get("binding_id")),
                availability=availability,
                evaluated_at=str(raw.get("evaluated_at")),
                valid_until=str(raw.get("valid_until")),
                reason=reason,
                signals=raw.get("signals") or {},
                probe_generation=int(raw.get("probe_generation", 1)),
                failover_summary=raw.get("failover_summary"),
            )

    def put(self, health: WebSolHealth) -> None:
        """Atomically persist an evaluated health record under the inter-process lock."""
        key = health_key(health.project_id, health.adapter, health.binding_id)
        with InterProcessFileLock(self.lock_path):
            data = self._load_data()
            entries = data.setdefault("entries", {})

            # Convert signals to dicts if they are dataclasses
            serialized_signals = {}
            for s_name, s_val in health.signals.items():
                if hasattr(s_val, "__dataclass_fields__"):
                    serialized_signals[s_name] = asdict(s_val)
                elif isinstance(s_val, dict):
                    serialized_signals[s_name] = s_val
                else:
                    serialized_signals[s_name] = str(s_val)

            entries[key] = {
                "project_id": health.project_id,
                "adapter": health.adapter,
                "binding_id": health.binding_id,
                "availability": health.availability,
                "evaluated_at": health.evaluated_at,
                "valid_until": health.valid_until,
                "reason": health.reason,
                "signals": serialized_signals,
                "probe_generation": health.probe_generation,
                "failover_summary": health.failover_summary,
            }
            write_json(self.health_path, data, indent=2)

    def list_all(self, *, now: Optional[datetime] = None) -> list[WebSolHealth]:
        """List all health entries, demoting expired entries to DEGRADED."""
        moment = _as_utc(now)
        with self._lock:
            data = self._load_data()
            entries = data.get("entries") or {}
            results = []
            for raw in entries.values():
                if not isinstance(raw, dict):
                    continue
                valid_until = parse_utc(raw.get("valid_until"))
                availability = raw.get("availability")
                reason = raw.get("reason", "")
                if valid_until is not None and moment > valid_until:
                    availability = WebSolAvailability.DEGRADED.value
                    reason = "health_snapshot_expired"

                results.append(
                    WebSolHealth(
                        project_id=str(raw.get("project_id")),
                        adapter=str(raw.get("adapter")),
                        binding_id=str(raw.get("binding_id")),
                        availability=availability,
                        evaluated_at=str(raw.get("evaluated_at")),
                        valid_until=str(raw.get("valid_until")),
                        reason=reason,
                        signals=raw.get("signals") or {},
                        probe_generation=int(raw.get("probe_generation", 1)),
                        failover_summary=raw.get("failover_summary"),
                    )
                )
            return sorted(results, key=lambda x: (x.project_id, x.binding_id))

    def get_probe_info(self, project_id: str, adapter: str, binding_id: str) -> Optional[dict[str, Any]]:
        key = health_key(project_id, adapter, binding_id)
        with self._lock:
            data = self._load_data()
            probes = data.get("probes") or {}
            raw = probes.get(key)
            return dict(raw) if isinstance(raw, dict) else None

    def record_probe(
        self,
        project_id: str,
        adapter: str,
        binding_id: str,
        *,
        generation: int,
        success: bool,
        reason: str,
        duration_seconds: float = 0.0,
        now: Optional[datetime] = None,
    ) -> None:
        key = health_key(project_id, adapter, binding_id)
        moment = _as_utc(now)
        with InterProcessFileLock(self.lock_path):
            data = self._load_data()
            probes = data.setdefault("probes", {})
            probes[key] = {
                "project_id": project_id,
                "adapter": adapter,
                "binding_id": binding_id,
                "generation": generation,
                "success": bool(success),
                "reason": reason,
                "duration_seconds": duration_seconds,
                "recorded_at": moment.isoformat(),
            }
            # Update backoff
            backoff_map = data.setdefault("probe_backoff", {})
            b_info = backoff_map.setdefault(key, {
                "attempts": 0,
                "next_allowed_at": None,
                "consecutive_failures": 0,
            })
            if success:
                b_info["consecutive_failures"] = 0
                b_info["attempts"] = 0
                b_info["next_allowed_at"] = None
            else:
                failures = int(b_info.get("consecutive_failures", 0)) + 1
                b_info["consecutive_failures"] = failures
                b_info["attempts"] = int(b_info.get("attempts", 0)) + 1
                delay = min(
                    DEFAULT_PROBE_MAX_BACKOFF_SECONDS,
                    DEFAULT_PROBE_INITIAL_BACKOFF_SECONDS * (DEFAULT_PROBE_BACKOFF_MULTIPLIER ** (failures - 1)),
                )
                b_info["next_allowed_at"] = (moment + timedelta(seconds=delay)).isoformat()
                b_info["last_failure_at"] = moment.isoformat()
            write_json(self.health_path, data, indent=2)

    def can_probe(self, project_id: str, adapter: str, binding_id: str, *, now: Optional[datetime] = None) -> bool:
        key = health_key(project_id, adapter, binding_id)
        moment = _as_utc(now)
        with self._lock:
            data = self._load_data()
            backoff_map = data.get("probe_backoff") or {}
            b_info = backoff_map.get(key)
            if not isinstance(b_info, dict):
                return True
            next_allowed = parse_utc(b_info.get("next_allowed_at"))
            if next_allowed is not None and moment < next_allowed:
                return False
            return True

    def probe_consecutive_failures(
        self,
        project_id: str,
        adapter: str,
        binding_id: str,
        *,
        now: Optional[datetime] = None,
        reset_seconds: float = DEFAULT_PROBE_FAILURE_RESET_SECONDS,
    ) -> int:
        key = health_key(project_id, adapter, binding_id)
        moment = _as_utc(now)
        with self._lock:
            data = self._load_data()
            backoff_map = data.get("probe_backoff") or {}
            b_info = backoff_map.get(key)
            if isinstance(b_info, dict):
                last_failure = parse_utc(b_info.get("last_failure_at"))
                if (
                    last_failure is not None
                    and reset_seconds > 0
                    and (moment - last_failure).total_seconds() >= reset_seconds
                ):
                    b_info["consecutive_failures"] = 0
                    write_json(self.health_path, data, indent=2)
                    return 0
                return int(b_info.get("consecutive_failures", 0))
            return 0

    def should_probe(
        self,
        project_id: str,
        adapter: str,
        binding_id: str,
        *,
        interval_seconds: int = 300,
        reset_seconds: float = DEFAULT_PROBE_FAILURE_RESET_SECONDS,
        max_attempts: int = DEFAULT_PROBE_MAX_ATTEMPTS,
        bridge_store: Optional[Any] = None,
        now: Optional[datetime] = None,
    ) -> bool:
        """Return True if prerequisites permit running an inference probe."""
        if self.is_probe_in_flight(project_id, adapter, binding_id):
            return False
        if (
            self.probe_consecutive_failures(
                project_id, adapter, binding_id, now=now, reset_seconds=reset_seconds
            )
            >= max_attempts
        ):
            return False
        if not self.can_probe(project_id, adapter, binding_id, now=now):
            return False
        if bridge_store is not None:
            try:
                st = bridge_store.binding_status(adapter, binding_id, now=now)
                if st.get("state") != "bound":
                    return False
            except Exception:
                return False
        p_info = self.get_probe_info(project_id, adapter, binding_id)
        if not p_info:
            return True
        moment = _as_utc(now)
        recorded_at = parse_utc(p_info.get("recorded_at"))
        if recorded_at is None:
            return True
        if int(p_info.get("generation", 0)) != self.current_generation():
            return True
        return (moment - recorded_at).total_seconds() >= interval_seconds

    def record_probe_result(
        self,
        project_id: str,
        adapter: str,
        binding_id: str,
        *,
        ok: bool,
        error: Optional[str] = None,
        duration_seconds: float = 0.0,
        now: Optional[datetime] = None,
    ) -> None:
        self.record_probe(
            project_id=project_id,
            adapter=adapter,
            binding_id=binding_id,
            generation=self.current_generation(),
            success=ok,
            reason=error or ("probe_ok" if ok else "probe_failed"),
            duration_seconds=duration_seconds,
            now=now,
        )

