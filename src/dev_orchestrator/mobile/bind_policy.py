"""Tailscale bind policy and interface address validation for MobileGateway.

Enforces that MobileGateway binds exclusively to verified local Tailscale interface
addresses. Wildcard (0.0.0.0, ::), loopback (127.0.0.1, ::1), RFC1918 private LAN,
and unassigned or public addresses are strictly refused.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Tuple

TAILSCALE_IPV4_NETWORK = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_IPV6_NETWORK = ipaddress.ip_network("fd7a:115c:a1e0::/48")

RFC1918_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def verify_tailscale_bind_address(address: str) -> Tuple[bool, str]:
    """Verify that an address is a valid, locally-assigned Tailscale interface address.

    Returns:
        (ok: bool, reason: str)
    """
    if not address or not isinstance(address, str):
        return False, "bind address must be a nonblank string"

    cleaned = address.strip().strip("[]")
    try:
        ip = ipaddress.ip_address(cleaned)
    except ValueError:
        return False, f"invalid IP address: {address!r}"

    # 1. Refuse wildcard addresses
    if ip.is_unspecified:
        return False, f"wildcard address {address!r} is strictly forbidden for MobileGateway"

    # 2. Refuse loopback addresses
    if ip.is_loopback:
        return False, f"loopback substitution {address!r} is not permitted for MobileGateway"

    # 3. Refuse RFC1918 private IPv4 addresses
    if isinstance(ip, ipaddress.IPv4Address):
        for net in RFC1918_NETWORKS:
            if ip in net:
                return False, f"RFC1918 private LAN address {address!r} is not permitted for MobileGateway"

    # 4. Check if in Tailscale CGNAT IPv4 or Tailscale ULA IPv6
    is_tailscale_v4 = isinstance(ip, ipaddress.IPv4Address) and ip in TAILSCALE_IPV4_NETWORK
    is_tailscale_v6 = isinstance(ip, ipaddress.IPv6Address) and ip in TAILSCALE_IPV6_NETWORK
    if not (is_tailscale_v4 or is_tailscale_v6):
        return False, (
            f"address {address!r} is not within Tailscale networks "
            f"({TAILSCALE_IPV4_NETWORK} or {TAILSCALE_IPV6_NETWORK})"
        )

    # 5. Verify that the address is actually assigned locally by attempting a test bind
    family = socket.AF_INET if isinstance(ip, ipaddress.IPv4Address) else socket.AF_INET6
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            # Bind to port 0 to test local address assignment
            sock.bind((str(ip), 0))
        finally:
            sock.close()
    except OSError as exc:
        return False, f"address {address!r} is not assigned to a local interface: {exc}"

    return True, "address verified as local Tailscale interface"
