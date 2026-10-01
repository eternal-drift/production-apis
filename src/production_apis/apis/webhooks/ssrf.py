"""SSRF protection for tenant-supplied webhook URLs.

Tenants choose where we send HTTP requests, so an attacker could aim us at cloud metadata
(169.254.169.254), localhost admin ports or internal services. We validate the URL at
registration *and* re-resolve immediately before every delivery (DNS can change).
Defence in depth in production: also route deliveries through an egress proxy / network policy.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from ...core.errors import AppError, ErrorCode


def _is_forbidden(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def assert_public_url(url: str, *, allow_private: bool) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        raise AppError(ErrorCode.VALIDATION, "Webhook URL must be an absolute http(s) URL")
    if parts.username or parts.password:
        raise AppError(ErrorCode.VALIDATION, "Webhook URL must not embed credentials")
    if allow_private:  # development/test only
        return
    if parts.scheme != "https":
        raise AppError(ErrorCode.VALIDATION, "Webhook URL must use https")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            parts.hostname, parts.port or 443, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise AppError(ErrorCode.VALIDATION, "Webhook host does not resolve") from exc
    for info in infos:
        if _is_forbidden(ipaddress.ip_address(info[4][0])):
            raise AppError(ErrorCode.VALIDATION, "Webhook URL resolves to a non-public address")
