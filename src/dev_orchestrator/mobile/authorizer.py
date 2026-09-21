"""Mobile device authorization contracts and principal data structures.

Enforces zero-bearer-propagation: bearer tokens are authenticated at gateway ingress
and mapped to non-secret MobileDevicePrincipal instances. Subsystems (loopback
Control API admission, ControlCommandCoordinator, projection services) operate
solely on device_id and MobileDevicePrincipal without access to credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class MobileDevicePrincipal:
    """Non-secret mobile device identity propagated after authentication."""

    device_id: str
    scope: str  # must be 'mobile_device'
    device_label: str
    created_at: str
    expires_at: Optional[str]
    revoked: bool


@runtime_checkable
class MobileDeviceAuthorizer(Protocol):
    """Authorizer interface backed by canonical ControlSecurity store."""

    def validate_mobile_bearer(
        self, header_or_token: Optional[str]
    ) -> tuple[bool, str, Optional[MobileDevicePrincipal]]:
        """Validate an incoming bearer token and return the non-secret principal."""
        ...

    def lookup_mobile_device(
        self, device_id: str
    ) -> tuple[bool, str, Optional[MobileDevicePrincipal]]:
        """Re-read canonical state and validate device_id without needing a token."""
        ...

    def mobile_revocation_generation(self) -> int:
        """Return the monotonic mobile revocation generation counter."""
        ...
