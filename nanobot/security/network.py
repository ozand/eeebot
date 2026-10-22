"""Network security utilities — SSRF protection and internal URL detection."""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

_URL_RE = re.compile(r"https?://[^\s\"'`;|<>]+", re.IGNORECASE)


def _is_private(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(addr in net for net in _BLOCKED_NETWORKS)


def resolve_public_addresses(hostname: str, port: int) -> tuple[tuple[str, ...], str]:
    """Resolve one immutable public address snapshot, rejecting every malformed or blocked answer."""
    try:
        infos = socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except (OSError, OverflowError, TypeError):
        return (), f"Cannot resolve hostname: {hostname}"
    if not infos:
        return (), f"No addresses resolved for hostname: {hostname}"
    addresses: list[str] = []
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        except (IndexError, TypeError, ValueError):
            return (), f"Malformed DNS result for hostname: {hostname}"
        if _is_private(address):
            return (), f"Blocked: {hostname} resolves to private/internal address {address}"
        normalized = str(address)
        if normalized not in addresses:
            addresses.append(normalized)
    if not addresses:
        return (), f"No valid addresses resolved for hostname: {hostname}"
    return tuple(addresses), ""


def validate_url_target(url: str) -> tuple[bool, str]:
    """Validate URL syntax and DNS answers without weakening failures."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False, f"Only http/https allowed, got '{parsed.scheme or 'none'}'"
        if not parsed.netloc or not parsed.hostname:
            return False, "Missing domain or hostname"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        _, error = resolve_public_addresses(parsed.hostname, port)
        return not error, error
    except (TypeError, ValueError) as exc:
        return False, str(exc)


def validate_resolved_url(url: str) -> tuple[bool, str]:
    """Validate an already-fetched URL (e.g. after redirect)."""
    try:
        parsed = urlparse(url)
    except Exception:
        return True, ""
    hostname = parsed.hostname
    if not hostname:
        return True, ""
    try:
        address = ipaddress.ip_address(hostname)
        if _is_private(address):
            return False, f"Redirect target is a private address: {address}"
    except ValueError:
        try:
            infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            return True, ""
        for info in infos:
            try:
                address = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if _is_private(address):
                return False, f"Redirect target {hostname} resolves to private address {address}"
    return True, ""


def contains_internal_url(command: str) -> bool:
    """Return True when a command contains an internal/private target URL."""
    for match in _URL_RE.finditer(command):
        ok, _ = validate_url_target(match.group(0))
        if not ok:
            return True
    return False
