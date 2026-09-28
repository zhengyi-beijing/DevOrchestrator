"""Native execution transport layer for DevOrchestrator machine operations."""

from .contracts import (
    FileReadRequest,
    FileResult,
    FileWriteRequest,
    HostCapabilities,
    MachineOperation,
    MachineOperationResult,
    MachineTransport,
    StatResult,
    StagedWriteContent,
    TransportAmbiguousError,
    TransportError,
    TransportRejectedError,
    TransportSelection,
    TransportUnavailableError,
    WriteContentUpload,
)
from .hosts import (
    HostCapabilityCache,
    TransportHostProfile,
    TransportHostsConfig,
    discover_local_capabilities,
    get_transport_for_host,
    load_transport_hosts_config,
)
from .local import LocalMachineTransport
from .rdc import RDCTransport
from .selector import select_transport
from .ssh import SSHMachineTransport

__all__ = [
    "FileReadRequest",
    "FileResult",
    "FileWriteRequest",
    "HostCapabilities",
    "HostCapabilityCache",
    "LocalMachineTransport",
    "MachineOperation",
    "MachineOperationResult",
    "MachineTransport",
    "RDCTransport",
    "SSHMachineTransport",
    "StatResult",
    "StagedWriteContent",
    "TransportAmbiguousError",
    "TransportError",
    "TransportHostProfile",
    "TransportHostsConfig",
    "TransportRejectedError",
    "TransportSelection",
    "TransportUnavailableError",
    "WriteContentUpload",
    "discover_local_capabilities",
    "get_transport_for_host",
    "load_transport_hosts_config",
    "select_transport",
]
