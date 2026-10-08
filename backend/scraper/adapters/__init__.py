"""Site adapters implementing the SiteAdapter contract (AGENTS.md §5).

Importing this package registers every site adapter: each site module calls
:func:`backend.scraper.adapters.base.register` at import time, and the
``_SITE_MODULES`` tuple below lists the modules to import. **Adding a site =
one line in ``_SITE_MODULES`` plus the new module file.**

Modules still being built (Milestone 1 adapters arrive per site) are skipped
only when the file itself is missing — an import error *inside* an existing
adapter module still fails loudly.
"""

from __future__ import annotations

import importlib
import logging

from backend.scraper.adapters.base import (
    Comment,
    CommentMeta,
    CrawlScope,
    MediaKind,
    MediaRef,
    PageRef,
    ScopeKind,
    SiteAdapter,
    UnsupportedSiteError,
    get_adapter,
    list_sites,
    register,
)

logger = logging.getLogger(__name__)

# One line per site module (MVP: asurascans, mangadex, mangapark, comix — PRD §0).
_SITE_MODULES: tuple[str, ...] = (
    "asurascans",
    "mangadex",
    "mangapark",
    "comix",
)


def _import_site_modules() -> None:
    for name in _SITE_MODULES:
        module_name = f"{__name__}.{name}"
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name == module_name:
                logger.debug("site module not present yet module=%s", name)
                continue
            raise


_import_site_modules()

__all__ = [
    "Comment",
    "CommentMeta",
    "CrawlScope",
    "MediaKind",
    "MediaRef",
    "PageRef",
    "ScopeKind",
    "SiteAdapter",
    "UnsupportedSiteError",
    "get_adapter",
    "list_sites",
    "register",
]
