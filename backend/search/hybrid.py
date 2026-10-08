"""Hybrid ranking — keyword + semantic + tag + metadata (PRD §21–§23, §52).

:func:`search` combines the four PRD §22 components with the configurable
``settings.search`` weights (defaults 0.25 / 0.50 / 0.20 / 0.05):

* **keyword** — FTS5 ``bm25()`` over the whole indexed row
  (:mod:`backend.search.keyword`), normalized so the best hit scores 1.0;
* **semantic** — cosine similarity from the vector index
  (:mod:`backend.search.semantic`), mapped from ``[-1, 1]`` to ``[0, 1]``;
* **tag** — FTS restricted to the ``tags`` column
  (``{tags} : "cat"``), with a **LIKE fallback** over the tags text
  (``ai_metadata.tags`` + ``media.user_tags``) when the restricted match finds
  nothing, so an exact tag still resolves while the index is stale;
* **metadata** — FTS restricted to ``{filename source_text}`` (original
  filename, site, chapter).

**Modes** (reported in the response so the UI can explain what happened):

``hybrid``
    Non-empty query, an embedding obtained from the provider **and** at least
    one usable embedding stored for that model — all four components scored
    with the configured weights.
``keyword_only``
    No provider, no API key, no embeddings, or the embedding call failed.
    The semantic component is dropped and its weight is **renormalized over
    the remaining components**: each remaining weight is divided by their sum,
    so the effective weights still sum to 1.0 and the keyword/tag/metadata
    proportions are preserved (``0.25/0.20/0.05`` → ``5/9, 4/9, 1/9``).
``filters_only``
    Empty query — filters only, ordered ``created_at DESC``, ``score`` is
    ``None`` (no text was searched, so inventing a score would mislead).

Component scores live in ``[0, 1]``; a candidate must score above zero in at
least one component *and* pass every filter, so "no results" is an ordinary
outcome rather than an error (PRD §52). Adversarial queries are sanitized by
:mod:`backend.search.keyword` and provider failures degrade to
``keyword_only`` — this function never raises on user input.

The search is ``async`` because PRD §16 providers expose ``generate_embedding``
as ``async``; everything else (FTS, vectors, filters) is plain SQLite/NumPy.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from backend.ai.provider import VisionProvider
from backend.config import Settings
from backend.media.library import MediaFilters, filter_ids
from backend.search import keyword
from backend.search.semantic import create_semantic_index

logger = logging.getLogger(__name__)

#: Candidates taken per component — enough for pagination at library scale (PRD §50).
CANDIDATE_LIMIT = 1000

#: Ranking modes reported to clients (PRD §21–§22).
MODE_HYBRID = "hybrid"
MODE_KEYWORD_ONLY = "keyword_only"
MODE_FILTERS_ONLY = "filters_only"

#: Weight keys in PRD §22 order.
WEIGHT_KEYS: tuple[str, ...] = ("keyword", "semantic", "tag", "metadata")


@dataclass(frozen=True)
class SearchHit:
    """One ranked result: the media id and its combined score (``None`` when unranked)."""

    media_id: int
    score: float | None = None


@dataclass(frozen=True)
class SearchResults:
    """Ranked page source for ``GET /api/search`` (route slices it for pagination)."""

    hits: list[SearchHit]
    total: int
    mode: str
    weights: dict[str, float]

    @property
    def media_ids(self) -> list[int]:
        return [hit.media_id for hit in self.hits]


async def search(
    conn: sqlite3.Connection,
    query: str,
    filters: MediaFilters,
    settings: Settings,
    provider: VisionProvider | None = None,
) -> SearchResults:
    """Rank everything matching ``filters`` for ``query``; best first.

    ``provider`` supplies the query embedding; ``None`` (or any provider
    failure) simply degrades the run to ``keyword_only`` — see the module
    docstring for the mode rules and the weight renormalization.
    """
    weights = _configured_weights(settings)
    text = (query or "").strip()
    if not text:
        ordered = filter_ids(conn, filters)
        return SearchResults(
            hits=[SearchHit(media_id) for media_id in ordered],
            total=len(ordered),
            mode=MODE_FILTERS_ONLY,
            weights=weights,
        )

    _ensure_index(conn)
    keyword_scores = _normalize(keyword.query(conn, text, CANDIDATE_LIMIT))
    tag_scores = _tag_scores(conn, text)
    metadata_scores = _normalize(
        keyword.column_query(conn, text, ("filename", "source_text"), CANDIDATE_LIMIT)
    )
    semantic_scores = await _semantic_scores(conn, text, provider)

    if semantic_scores is not None:
        mode = MODE_HYBRID
        effective = dict(weights)
    else:
        mode = MODE_KEYWORD_ONLY
        effective = _renormalize_without_semantic(weights)

    allowed = set(filter_ids(conn, filters))
    components = (keyword_scores, tag_scores, metadata_scores, semantic_scores or {})
    candidates = set().union(*(set(component) for component in components)) & allowed
    scored: list[SearchHit] = []
    for media_id in candidates:
        score = (
            effective["keyword"] * keyword_scores.get(media_id, 0.0)
            + effective["semantic"] * (semantic_scores or {}).get(media_id, 0.0)
            + effective["tag"] * tag_scores.get(media_id, 0.0)
            + effective["metadata"] * metadata_scores.get(media_id, 0.0)
        )
        if score > 0.0:
            scored.append(SearchHit(media_id, score))
    scored.sort(key=lambda hit: (-(hit.score or 0.0), hit.media_id))
    logger.debug(
        "search mode=%s hits=%d query=%r provider=%s",
        mode, len(scored), text, provider.name if provider is not None else None,
    )
    return SearchResults(hits=scored, total=len(scored), mode=mode, weights=effective)


def _configured_weights(settings: Settings) -> dict[str, float]:
    """PRD §22 weights as reported to clients."""
    return {
        "keyword": float(settings.search.keyword_weight),
        "semantic": float(settings.search.semantic_weight),
        "tag": float(settings.search.tag_weight),
        "metadata": float(settings.search.metadata_weight),
    }


def _renormalize_without_semantic(weights: dict[str, float]) -> dict[str, float]:
    """Drop the semantic weight and rescale the rest to sum to 1.0 (module docstring)."""
    remaining = {key: value for key, value in weights.items() if key != "semantic"}
    total = sum(remaining.values())
    if total <= 0:
        # Degenerate configuration (no positive weight to rescale): equal split.
        return {"semantic": 0.0, **{key: 1.0 / len(remaining) for key in remaining}}
    return {"semantic": 0.0, **{key: value / total for key, value in remaining.items()}}


def _normalize(pairs: list[tuple[int, float]]) -> dict[int, float]:
    """``bm25`` ranks (negative, lower = better) → ``[0, 1]`` with best = 1.0."""
    if not pairs:
        return {}
    raw = {media_id: -rank for media_id, rank in pairs}
    best = max(raw.values())
    if best <= 0:  # bm25 is negative for every hit; keep the fallback explicit
        return {media_id: 1.0 for media_id in raw}
    return {media_id: value / best for media_id, value in raw.items()}


def _tag_scores(conn: sqlite3.Connection, text: str) -> dict[int, float]:
    """Tag component: FTS restricted to the ``tags`` column, LIKE fallback if empty."""
    scores = _normalize(keyword.column_query(conn, text, ("tags",), CANDIDATE_LIMIT))
    if scores:
        return scores
    return _tag_like_scores(conn, text)


def _tag_like_scores(conn: sqlite3.Connection, text: str) -> dict[int, float]:
    """Case-insensitive LIKE fallback over the tags text (see the module docstring).

    Every plain term of the query must appear in ``ai_metadata.tags`` or the
    user's ``media.user_tags``. Terms are word-only (see
    :func:`backend.search.keyword.plain_terms`), so they never contain the
    ``%``/``_`` LIKE metacharacters.
    """
    terms = [term.lower() for term in keyword.plain_terms(text)]
    if not terms:
        return {}
    haystack = "LOWER(COALESCE(a.tags, '') || ' ' || COALESCE(m.user_tags, ''))"
    clauses = " AND ".join(f"{haystack} LIKE ?" for _ in terms)
    params = [f"%{term}%" for term in terms]
    rows = conn.execute(
        "SELECT m.id FROM media m LEFT JOIN ai_metadata a ON a.media_id = m.id "
        f"WHERE {clauses}",
        params,
    ).fetchall()
    return {int(row["id"]): 1.0 for row in rows}


async def _semantic_scores(
    conn: sqlite3.Connection, text: str, provider: VisionProvider | None
) -> dict[int, float] | None:
    """Embedding-based component, or ``None`` when the run must degrade (→ keyword_only)."""
    if provider is None:
        return None
    try:
        vector = await provider.generate_embedding(text)
    except Exception as exc:
        logger.warning("semantic component skipped: embedding failed error=%s", exc)
        return None
    if not vector:
        logger.warning("semantic component skipped: provider returned an empty embedding")
        return None
    index = create_semantic_index(conn, embedding_model=provider.embedding_model)
    if len(index) == 0:
        return None
    pairs = index.search(vector, CANDIDATE_LIMIT)
    if not pairs:
        # Embeddings exist but none shares the query's model/dimension — see
        # backend.search.semantic: scoring across models would be meaningless.
        return None
    return {
        media_id: min(1.0, max(0.0, (1.0 + similarity) / 2.0))
        for media_id, similarity in pairs
    }


def _ensure_index(conn: sqlite3.Connection) -> None:
    """Bootstrap/self-heal the FTS index when its row count diverges from ``media``.

    Covers a library that has never been indexed (first search after ingest)
    and rows added before the pipeline's INDEX stage ran; day-to-day freshness
    comes from that stage plus :func:`backend.search.keyword.index_media`.
    """
    media_count = int(conn.execute("SELECT COUNT(*) FROM media").fetchone()[0])
    index_count = int(conn.execute("SELECT COUNT(*) FROM media_fts").fetchone()[0])
    if media_count != index_count:
        indexed = keyword.rebuild_fts_index(conn)
        logger.info("search index self-heal media=%d indexed=%d", media_count, indexed)
