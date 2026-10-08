"""Shared exponential-backoff math for the crawl/download HTTP paths (PRD §37)."""

from __future__ import annotations

#: Never sleep longer than this between retries, whatever the server says.
MAX_BACKOFF_SECONDS: float = 60.0


def backoff_delay(base_seconds: float, attempt: int) -> float:
    """Seconds to wait before retry number ``attempt`` (0-based): ``base * 2**attempt``, capped.

    ``base_seconds`` is the configured page delay (default ≥ 1s, AGENTS.md §9),
    so backoff grows 1s, 2s, 4s, … and never exceeds :data:`MAX_BACKOFF_SECONDS`.
    """
    if attempt < 0:
        raise ValueError("attempt must be >= 0")
    delay = max(0.0, base_seconds) * (2**attempt)
    return min(delay, MAX_BACKOFF_SECONDS)


def clamp_retry_after(retry_after: float | None, computed: float) -> float:
    """Combine a server ``Retry-After`` hint with the computed backoff: take the
    larger of the two, still capped at :data:`MAX_BACKOFF_SECONDS`."""
    if retry_after is None:
        return min(computed, MAX_BACKOFF_SECONDS)
    return min(max(computed, max(0.0, retry_after)), MAX_BACKOFF_SECONDS)


def parse_retry_after(raw: str | None) -> float | None:
    """Parse a ``Retry-After`` header in delta-seconds form; HTTP-date form is ignored (None)."""
    if raw is None:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        return None
