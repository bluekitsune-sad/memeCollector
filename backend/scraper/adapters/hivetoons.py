"""HiveToons adapter for ``hivetoons.org`` (AGENTS.md §5, PRD §0/§6).

HiveToons runs the same "vortex" white-label reader platform as vortexscans.org —
its client build config literally carries ``SITE_SLUG: "vortexscans"`` (verified
2026-10-13) — so all selectors, URL shapes, and caveats documented in
``backend.scraper.adapters.vortexscans`` apply verbatim here: client-rendered
``div[data-comment-id]`` comments that lazy-mount on scroll, ``.comment-body``
attachments only (best-effort — the platform currently exposes no image-attach
affordance, so live yield is expected near-zero), server-rendered
``/series/{slug}`` chapter links, and the paginated ``/series?page=N`` catalog
(50/page on this domain — verified live).
"""

from __future__ import annotations

import logging

from backend.scraper.adapters.base import register
from backend.scraper.adapters.vortexscans import VortexPlatformAdapter

logger = logging.getLogger(__name__)


class HiveToonsAdapter(VortexPlatformAdapter):
    """Collect comment attachments from HiveToons chapter pages."""

    site = "hivetoons"
    _hosts = frozenset({"hivetoons.org", "www.hivetoons.org"})


register(HiveToonsAdapter())
