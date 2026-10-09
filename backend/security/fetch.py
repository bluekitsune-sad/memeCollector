"""Guarded HTTP GET with manual, per-hop validated redirects.

Adapter default fetch helpers are synchronous (they run inside
``asyncio.to_thread``), so this guard is synchronous too: the initial URL and
every redirect target pass :func:`backend.security.urls.validate_url_with_dns`
before a request is issued (PRD §41). Injected test fetchers bypass this seam
deliberately — only the default helpers use it.
"""

from __future__ import annotations

import httpx

from backend.security.urls import (
    MAX_REDIRECTS,
    REDIRECT_STATUSES,
    reject_url,
    validate_url_with_dns,
)

DEFAULT_TIMEOUT_SECONDS = 30.0


def guarded_get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> httpx.Response:
    """GET *url*, validating it and every redirect hop; never follows automatically.

    Raises :class:`backend.security.urls.UnsafeURLError` if the URL or a
    redirect target is blocked, or if the redirect limit is exceeded.
    """
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        validate_url_with_dns(current)
        response = httpx.get(
            current,
            headers=headers,
            timeout=timeout,
            follow_redirects=False,
        )
        location = (
            response.headers.get("location")
            if response.status_code in REDIRECT_STATUSES
            else None
        )
        if not location:
            return response
        current = str(httpx.URL(current).join(location))
    reject_url(current, "redirect limit exceeded")
