"""Security hardening helpers (booby-trap guards) for hostile crawl targets.

* :mod:`backend.security.text` — sanitize untrusted text before storage/prompts.
* :mod:`backend.security.urls` — block dangerous schemes, userinfo URLs and
  private/metadata addresses at every network choke point.
* :mod:`backend.security.prompt` — wrap untrusted text placed into AI prompts.
* :mod:`backend.security.fetch` — guarded HTTP GET with validated redirects.
"""

from backend.security.fetch import guarded_get
from backend.security.prompt import wrap_untrusted_text
from backend.security.text import (
    MAX_UNTRUSTED_TEXT_CHARS,
    sanitize_optional_text,
    sanitize_text,
)
from backend.security.urls import (
    ALLOWED_SCHEMES,
    MAX_REDIRECTS,
    UnsafeURLError,
    ensure_safe_url,
    reject_url,
    validate_url,
    validate_url_with_dns,
)

__all__ = [
    "ALLOWED_SCHEMES",
    "MAX_REDIRECTS",
    "MAX_UNTRUSTED_TEXT_CHARS",
    "UnsafeURLError",
    "ensure_safe_url",
    "guarded_get",
    "reject_url",
    "sanitize_optional_text",
    "sanitize_text",
    "validate_url",
    "validate_url_with_dns",
    "wrap_untrusted_text",
]
