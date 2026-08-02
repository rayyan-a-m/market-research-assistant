"""SSRF guard.

Applied to every user-supplied URL and every discovery candidate before
it is ever fetched. The concrete risk on Azure: the Instance Metadata
Service is reachable at http://169.254.169.254/metadata/identity/... with
no auth from inside the container. A URL pointed at that address would
hand back the container's managed-identity token to whoever supplied it.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_BLOCKED_REASON = {
    "private": "private IP range (RFC 1918)",
    "link_local": "link-local range (includes cloud metadata services)",
    "loopback": "loopback address",
    "unspecified": "unspecified address",
    "reserved": "reserved address range",
}


def resolve_hostname(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Resolve a hostname to an IP address.

    Uses `getaddrinfo` rather than `gethostbyname` deliberately —
    `gethostbyname` only resolves IPv4, so an IPv6 loopback/link-local
    literal (e.g. `https://[::1]/`) would silently come back as "did not
    resolve" instead of being correctly flagged as unsafe, and a
    legitimate public IPv6 host would be rejected outright.

    This is a blocking call. It's used from a synchronous Pydantic
    field_validator, which itself runs on the event loop thread during
    request validation — a real trade-off (adds ~10-50ms per URL to
    request validation) accepted because Pydantic validators cannot be
    async. If this guard moves to being called from async code directly,
    prefer `await asyncio.get_running_loop().getaddrinfo(...)` instead.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
        return ipaddress.ip_address(infos[0][4][0])
    except (socket.gaierror, ValueError, UnicodeError, IndexError):
        return None


def unsafe_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Return why an IP is unsafe to fetch, or None if it's fine."""
    if ip.is_loopback:
        return _BLOCKED_REASON["loopback"]
    if ip.is_link_local:
        return _BLOCKED_REASON["link_local"]
    if ip.is_private:
        return _BLOCKED_REASON["private"]
    if ip.is_unspecified:
        return _BLOCKED_REASON["unspecified"]
    if ip.is_reserved or ip.is_multicast:
        return _BLOCKED_REASON["reserved"]
    return None


def is_https_url(url: str) -> bool:
    return urlparse(url).scheme == "https"


def is_safe_url(url: str) -> tuple[bool, str | None]:
    """Returns (is_safe, reason_if_unsafe).

    Blocks: RFC 1918 private ranges, link-local (169.254/16 — Azure IMDS),
    loopback, unspecified, and reserved/multicast ranges, for both IPv4
    and IPv6. Does not itself enforce HTTPS — see `is_https_url`.

    Known limitation (documented, not silently ignored): this checks the
    IP at validation time. A DNS-rebinding attack could resolve to a safe
    IP here and a private IP at fetch time. The fetch layer (crawl4ai) is
    expected to reuse a fixed browser context rather than re-resolving,
    which mitigates but does not eliminate this — a defense-in-depth
    egress proxy/firewall rule is the complete fix and is out of scope
    for an application-layer guard.
    """
    hostname = urlparse(url).hostname
    if not hostname:
        return False, "no hostname in URL"

    ip = resolve_hostname(hostname)
    if ip is None:
        return False, "hostname did not resolve"

    reason = unsafe_reason(ip)
    if reason is not None:
        return False, reason

    return True, None
