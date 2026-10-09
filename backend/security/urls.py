"""URL navigation guard for hostile crawler targets.

Blocks dangerous schemes, credential-bearing URLs and addresses that are not
globally routable (loopback, private, link-local, metadata, multicast, ...).
Run at every real network choke point (page.goto, httpx requests, downloader)
before bytes leave the machine (PRD §41).

DNS resolution is best-effort: an unresolvable hostname fails **open** so that
fixture/offline hosts keep working; blocked IP literals fail **closed**.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
import unicodedata
from typing import NoReturn
from urllib.parse import urlsplit

from backend.security.text import sanitize_text

logger = logging.getLogger("memecollector.security.urls")

ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})
MAX_REDIRECTS = 10
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
DNS_CACHE_TTL_SECONDS = 60.0
DNS_CACHE_MAX_ENTRIES = 512

# DNS cache: hostname -> (epoch seconds when entries were stored, ok).
_DNS_CACHE: dict[str, tuple[float, bool]] = {}


class UnsafeURLError(ValueError):
    """Raised when a URL must not be fetched (scheme, host or IP policy)."""

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"unsafe URL ({reason}): {sanitize_text(url)}")
        self.url = url
        self.reason = reason


def reject_url(url: str, reason: str) -> NoReturn:
    """Log a blocked URL as structured key=value and raise :class:`UnsafeURLError`."""
    logger.warning("url blocked reason=%s url=%s", reason, sanitize_text(url))
    raise UnsafeURLError(url, reason)


def _is_blocked_address(host: str) -> str | None:
    """Return a block reason if *host* is an IP literal that must not be fetched."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_global and not address.is_private:
        return None
    for name in (
        "is_private",
        "is_loopback",
        "is_link_local",
        "is_multicast",
        "is_reserved",
        "is_unspecified",
        "is_site_local",
    ):
        if getattr(address, name, False):
            return f"blocked ip address ({name.removeprefix('is_')})"
    return "blocked ip address (not global)"


def _validated_hostname(url: str) -> str:
    """Validate *url* without I/O and return its lower-cased hostname."""
    if not url or not url.strip():
        reject_url(url, "empty url")
    for char in url:
        if unicodedata.category(char) in {"Cc", "Cf", "Cs"}:
            reject_url(url, "control or format character in url")
    try:
        parts = urlsplit(url)
    except ValueError:
        reject_url(url, "malformed url")
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        reject_url(url, f"blocked scheme {scheme or '(none)'}")
    if parts.username is not None or parts.password is not None:
        reject_url(url, "credentials in url (userinfo)")
    hostname = parts.hostname
    if not hostname:
        reject_url(url, "missing host")
    blocked = _is_blocked_address(hostname)
    if blocked is not None:
        reject_url(url, blocked)
    return hostname.lower()


def validate_url(url: str) -> str:
    """Validate *url* syntactically (no DNS) and return it unchanged if safe.

    Raises :class:`UnsafeURLError` otherwise.
    """
    _validated_hostname(url)
    return url


def _resolve(hostname: str) -> bool:
    """Best-effort A/AAAA lookup. ``True`` = resolved or could not be checked."""
    now = time.monotonic()
    if len(_DNS_CACHE) > DNS_CACHE_MAX_ENTRIES:
        _DNS_CACHE.clear()
    cached = _DNS_CACHE.get(hostname)
    if cached is not None and now - cached[0] < DNS_CACHE_TTL_SECONDS:
        return cached[1]
    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        logger.debug("dns lookup failed host=%s error=%s", hostname, exc)
        _DNS_CACHE[hostname] = (now, True)
        return True
    ok = True
    for info in infos:
        address = info[4][0]
        blocked = _is_blocked_address(address)
        if blocked is not None:
            ok = False
            logger.warning(
                "dns answer blocked host=%s ip=%s reason=%s",
                hostname,
                sanitize_text(address),
                blocked,
            )
            break
    _DNS_CACHE[hostname] = (now, ok)
    return ok


def validate_url_with_dns(url: str) -> str:
    """Validate *url* and resolve its host; raise if any answer is a blocked IP."""
    validate_url(url)
    hostname = urlsplit(url).hostname or ""  # cannot fail: validate_url just passed
    if not _resolve(hostname.lower()):
        reject_url(url, "host resolves to blocked address")
    return url


async def ensure_safe_url(url: str) -> str:
    """Async wrapper around :func:`validate_url_with_dns` (DNS off the event loop)."""
    return await asyncio.to_thread(validate_url_with_dns, url)
