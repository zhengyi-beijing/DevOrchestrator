"""Host profiles, approved policy-digest pins, and capability discovery."""

from __future__ import annotations

import os
import platform
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from dev_orchestrator.jobs.config import (
    load_jobs_config,
    resolve_local_jobs_config_path,
)
from dev_orchestrator.storage.json_store import read_json, utc_now_iso
from dev_orchestrator.transport.contracts import HostCapabilities

DEFAULT_CAPABILITY_TTL_SECONDS = 300.0  # 5 minutes


@dataclass(frozen=True)
class TransportHostProfile:
    """Configured profile for a local or remote execution host."""

    host_id: str
    candidate_order: list[str] = field(default_factory=lambda: ["local", "ssh"])
    ssh: Optional[dict[str, Any]] = None
    expected_host_identity: Optional[str] = None
    response_limits: dict[str, int] = field(default_factory=lambda: {
        "max_response_bytes": 10 * 1024 * 1024,
        "max_file_write_bytes": 8 * 1024 * 1024,
    })
    approved_policy_pins: dict[str, str] = field(default_factory=dict)
    os_family: str = "windows" if os.name == "nt" else "linux"
    path_style: str = "windows" if os.name == "nt" else "posix"
    helper_version: str = "1.0.0"
    enabled: bool = True


@dataclass(frozen=True)
class TransportHostsConfig:
    """Configuration set of transport host profiles."""

    hosts: dict[str, TransportHostProfile] = field(default_factory=dict)
    default_host: str = "local"


def _detect_local_os_family() -> str:
    sys_name = platform.system().lower()
    if "windows" in sys_name:
        return "windows"
    if "darwin" in sys_name:
        return "darwin"
    return "linux"


def _default_local_profile() -> TransportHostProfile:
    os_fam = _detect_local_os_family()
    return TransportHostProfile(
        host_id="local",
        candidate_order=["local"],
        expected_host_identity=socket.gethostname(),
        os_family=os_fam,
        path_style="windows" if os_fam == "windows" else "posix",
        helper_version="1.0.0",
        enabled=True,
    )


def load_transport_hosts_config(
    runtime_root: Path | str | None = None,
    config_path: Path | str | None = None,
) -> TransportHostsConfig:
    """Load transport-hosts.json from runtime root or return default local profile."""
    path: Optional[Path] = None
    if config_path is not None:
        path = Path(config_path)
    elif runtime_root is not None:
        path = Path(runtime_root) / "transport-hosts.json"

    if path is None or not path.is_file():
        local_p = _default_local_profile()
        return TransportHostsConfig(
            hosts={"local": local_p},
            default_host="local",
        )

    raw = read_json(path, None)
    if not isinstance(raw, dict):
        local_p = _default_local_profile()
        return TransportHostsConfig(
            hosts={"local": local_p},
            default_host="local",
        )

    hosts_dict: dict[str, TransportHostProfile] = {}
    raw_hosts = raw.get("hosts", {})
    if isinstance(raw_hosts, dict):
        for hid, hdata in raw_hosts.items():
            if not isinstance(hdata, dict):
                continue
            cand_order = [str(x) for x in hdata.get("candidate_order", ["local", "ssh"])]
            pins = dict(hdata.get("approved_policy_pins", {}))
            ssh_dict = dict(hdata.get("ssh")) if isinstance(hdata.get("ssh"), dict) else None
            limits = dict(hdata.get("response_limits", {}))
            profile = TransportHostProfile(
                host_id=hid,
                candidate_order=cand_order,
                ssh=ssh_dict,
                expected_host_identity=hdata.get("expected_host_identity"),
                response_limits=limits or {
                    "max_response_bytes": 10 * 1024 * 1024,
                    "max_file_write_bytes": 8 * 1024 * 1024,
                },
                approved_policy_pins=pins,
                os_family=str(hdata.get("os_family") or _detect_local_os_family()),
                path_style=str(hdata.get("path_style") or ("windows" if os.name == "nt" else "posix")),
                helper_version=str(hdata.get("helper_version", "1.0.0")),
                enabled=bool(hdata.get("enabled", True)),
            )
            hosts_dict[hid] = profile

    if "local" not in hosts_dict:
        hosts_dict["local"] = _default_local_profile()

    default_host = str(raw.get("default_host", "local"))
    if default_host not in hosts_dict:
        default_host = "local"

    return TransportHostsConfig(
        hosts=hosts_dict,
        default_host=default_host,
    )


class HostCapabilityCache:
    """In-memory cache for discovered host capabilities with TTL bounding."""

    def __init__(
        self,
        runtime_root: Path | str | None = None,
        *,
        hosts_config: Optional[TransportHostsConfig] = None,
        ttl_seconds: float = DEFAULT_CAPABILITY_TTL_SECONDS,
    ) -> None:
        if isinstance(runtime_root, (int, float)):
            self.ttl_seconds = float(runtime_root)
            self.runtime_root = None
            self.hosts_config = hosts_config
        else:
            self.runtime_root = Path(runtime_root) if runtime_root else None
            self.hosts_config = hosts_config
            self.ttl_seconds = ttl_seconds
        self._cache: dict[str, tuple[float, HostCapabilities]] = {}

    def get(self, host_id: str) -> Optional[HostCapabilities]:
        if host_id in self._cache:
            ts, cap = self._cache[host_id]
            if time.time() - ts <= self.ttl_seconds:
                return cap
            del self._cache[host_id]
        return None

    def put(self, host_id: str, capabilities: HostCapabilities) -> None:
        self._cache[host_id] = (time.time(), capabilities)

    def invalidate(self, host_id: Optional[str] = None) -> None:
        if host_id is not None:
            self._cache.pop(host_id, None)
        else:
            self._cache.clear()

    def get_capabilities(self, host_id: str) -> HostCapabilities:
        cached = self.get(host_id)
        if cached is not None:
            return cached
        h_cfg = self.hosts_config or load_transport_hosts_config(self.runtime_root)
        profile = h_cfg.hosts.get(host_id)
        if host_id == "local":
            caps = discover_local_capabilities(self.runtime_root or ".", profile=profile, host_id=host_id)
        else:
            if profile is None:
                raise ValueError(f"unknown host_id: {host_id}")
            from dev_orchestrator.transport.ssh import SSHMachineTransport
            transport = SSHMachineTransport(profile)
            caps = transport.capabilities(host_id)
        self.put(host_id, caps)
        return caps


def discover_local_capabilities(
    runtime_root: Path | str | Any,
    profile: Optional[TransportHostProfile] = None,
    *,
    jobs_config: Optional[Any] = None,
    host_id: str = "local",
) -> HostCapabilities:
    """Discover host-local execution capabilities and jobs config validity."""
    from dev_orchestrator.jobs.config import JobsConfig
    if isinstance(runtime_root, JobsConfig):
        rt = Path(runtime_root.runtime_root)
        valid_cfg = bool(runtime_root.enabled)
    else:
        rt = Path(runtime_root)
        if jobs_config is not None:
            valid_cfg = bool(getattr(jobs_config, "enabled", True))
        else:
            cfg_p = resolve_local_jobs_config_path(rt)
            loaded_cfg = load_jobs_config(cfg_p)
            valid_cfg = loaded_cfg is not None

    pins = dict(profile.approved_policy_pins) if profile else {}
    limits = dict(profile.response_limits) if profile else {
        "max_response_bytes": 10 * 1024 * 1024,
        "max_file_write_bytes": 8 * 1024 * 1024,
    }
    os_fam = profile.os_family if profile else _detect_local_os_family()
    path_style = profile.path_style if profile else ("windows" if os.name == "nt" else "posix")

    supported_ops = [
        "exec",
        "spawn",
        "poll",
        "cancel",
        "read_file",
        "stage_write_content",
        "write_file",
        "stat",
        "capabilities",
    ]

    return HostCapabilities(
        host_id=host_id,
        os_family=os_fam,
        path_style=path_style,
        helper_version="1.0.0",
        jobs_config_valid=valid_cfg,
        approved_policy_pins=pins,
        response_limits=limits,
        supported_operations=supported_ops,
        probed_at=utc_now_iso(),
    )


def get_transport_for_host(
    runtime_root: Path | str | None = None,
    host_id: Optional[str] = None,
    config_path: Path | str | None = None,
    *,
    operation: str = "read_file",
    command_ref: Optional[str] = None,
    effect_class: str = "read_only",
    policy_digest: Optional[str] = None,
    capability_cache: Optional[HostCapabilityCache] = None,
):
    """Retrieve an initialized MachineTransport instance for the specified host after selection."""
    h_id = host_id or "local"
    cfg = load_transport_hosts_config(runtime_root, config_path)
    from dev_orchestrator.transport.selector import select_transport
    from dev_orchestrator.transport.contracts import TransportRejectedError
    cache = capability_cache or HostCapabilityCache(runtime_root, hosts_config=cfg)
    caps = None
    capability_discovery_error = None
    try:
        caps = cache.get_capabilities(h_id)
    except Exception as exc:
        capability_discovery_error = f"{type(exc).__name__}: {exc}"
    selection = select_transport(
        operation,
        command_ref=command_ref,
        effect_class=effect_class,
        target_host_id=h_id,
        hosts_config=cfg,
        policy_digest=policy_digest,
        capabilities=caps,
        capability_discovery_error=capability_discovery_error,
    )
    if selection.selected_transport in ("rejected", "rdc_fallback_required", "ambiguous"):
        raise TransportRejectedError(
            f"transport selection rejected ({selection.selected_transport}): "
            f"{selection.reason_code}; evidence={selection.evidence_source}; "
            f"candidate_rejections={selection.candidate_rejections}"
        )
    if selection.selected_transport == "local":
        from dev_orchestrator.transport.local import LocalMachineTransport
        rt = Path(runtime_root) if runtime_root else None
        cfg_p = (rt / "execution-jobs.json") if rt else None
        t = LocalMachineTransport(jobs_config_path=cfg_p, host_id="local")
        t.last_selection = selection
        return t
    profile = cfg.hosts.get(h_id)
    if profile is None:
        raise ValueError(f"unknown transport host: {h_id}")
    from dev_orchestrator.transport.ssh import SSHMachineTransport
    t = SSHMachineTransport(profile)
    t.last_selection = selection
    return t
