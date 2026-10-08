"""Search-stage tests — FTS5 keyword search, vector semantic search, hybrid ranking.

Covers PRD §20–§23 and the §52 search validation areas, fully offline: rows go
into a freshly migrated temp database, query embeddings come from the
deterministic mock provider (no key, no network), and both semantic backends
(FAISS and the pure-NumPy fallback) are exercised behind the same interface.

Ranking behavior under test: exact tag beats description-only matches, prefix
and phrase queries resolve, adversarial input never raises, an empty query is a
filters-only run ordered newest-first, weights always sum to 1.0 and
``keyword_only`` renormalizes after the semantic component drops out.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import sqlite3
from dataclasses import replace

import pytest

from backend.ai.mock import MockVisionProvider
from backend.ai.provider import AIUnavailableError
from backend.ai.queue import serialize_embedding
from backend.database.database import transaction
from backend.media.library import MediaFilters
from backend.search import hybrid, keyword
from backend.search.semantic import (
    FaissSemanticIndex,
    NumpySemanticIndex,
    create_semantic_index,
    faiss_available,
)

#: MockVisionProvider's default embedding model (``mock/384``).
MOCK_EMBEDDING_MODEL = "mock/384"

_seq = itertools.count(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed(
    conn: sqlite3.Connection,
    *,
    filename: str = "media.png",
    extension: str = "png",
    description: str = "",
    tags: tuple[str, ...] = (),
    emotions: tuple[str, ...] = (),
    user_tags: str = "",
    user_description: str = "",
    site: str = "asurascans",
    chapter: str | None = "chapter-42",
    dup_status: str = "nondup",
    processing_status: str = "READY",
    is_favorite: bool = False,
    created_at: str = "2026-01-01 00:00:00",
) -> int:
    """Insert ``media`` + ``source`` (+ ``ai_metadata``) rows directly; returns the id."""
    digest = hashlib.sha256(f"row-{next(_seq)}".encode()).hexdigest()
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO media (file_path, original_filename, extension, mime_type, sha256,"
            " dup_status, processing_status, is_favorite, user_tags, user_description, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"/tmp/media/{digest[:12]}.{extension}",
                filename,
                extension,
                "image/gif" if extension == "gif" else "image/png",
                digest,
                dup_status,
                processing_status,
                int(is_favorite),
                user_tags,
                user_description,
                created_at,
            ),
        )
        media_id = int(cursor.lastrowid)
        conn.execute(
            "INSERT INTO source (media_id, site, page_url, chapter, media_url)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                media_id,
                site,
                f"https://{site}/comic/{chapter or 'chapter-42'}?page=1",
                chapter,
                f"https://cdn.example.com/{digest[:12]}.{extension}",
            ),
        )
        if description or tags or emotions:
            conn.execute(
                "INSERT INTO ai_metadata (media_id, description, tags, emotions)"
                " VALUES (?, ?, ?, ?)",
                (
                    media_id,
                    description,
                    json.dumps(list(tags)),
                    json.dumps(list(emotions)),
                ),
            )
    return media_id


def _put_embedding(
    conn: sqlite3.Connection, media_id: int, vector: list[float], model: str = MOCK_EMBEDDING_MODEL
) -> None:
    """Store one embedding row in the documented little-endian float32 layout."""
    with transaction(conn):
        conn.execute(
            "INSERT OR REPLACE INTO embeddings (media_id, embedding, embedding_model)"
            " VALUES (?, ?, ?)",
            (media_id, serialize_embedding(vector), model),
        )


def _ids(results: hybrid.SearchResults) -> list[int]:
    return results.media_ids


class _BrokenProvider(MockVisionProvider):
    """Provider whose embedding call fails — must degrade, never raise."""

    async def generate_embedding(self, text: str) -> list[float]:
        raise AIUnavailableError("no api key configured")


# ---------------------------------------------------------------------------
# Keyword stage (backend.search.keyword)
# ---------------------------------------------------------------------------


def test_keyword_query_matches_description_and_prefix(db: sqlite3.Connection) -> None:
    confused = _seed(db, description="a confused cat stares blankly")
    _seed(db, description="a happy dog wags its tail")
    keyword.rebuild_fts_index(db)

    assert [row[0] for row in keyword.query(db, "conf")] == [confused]
    assert [row[0] for row in keyword.query(db, "happy")] != [confused]
    # bm25 is negative (lower = better) for every hit.
    assert all(rank <= 0 for _, rank in keyword.query(db, "conf"))


def test_keyword_phrase_query_matches_exact_phrase(db: sqlite3.Connection) -> None:
    phrase = _seed(db, description="honestly this is a big mood")
    _seed(db, description="big changes are coming to the mood ring")
    keyword.rebuild_fts_index(db)

    assert [row[0] for row in keyword.query(db, '"big mood"')] == [phrase]


def test_adversarial_query_is_sanitized_and_never_raises(db: sqlite3.Connection) -> None:
    _seed(db, description="innocent row")

    assert keyword.sanitize_query("foo OR (bar") == '"foo" AND "bar"*'
    assert keyword.sanitize_query("conf") == '"conf"*'
    assert keyword.sanitize_query('"big mood"') == '"big mood"'
    assert keyword.sanitize_query("AND OR NOT") == ""
    # Every sanitizer output form is a valid MATCH expression.
    for raw in ("foo OR (bar", '"; DROP TABLE media; --', "unclosed (paren", "*"):
        assert isinstance(keyword.query(db, raw), list)
    with pytest.raises(ValueError):
        keyword.column_query(db, "x", ("nope",))


async def test_tag_match_outranks_description_only(db: sqlite3.Connection, make_settings) -> None:
    tagged = _seed(db, description="stares into your soul", tags=("confused",))
    _seed(db, description="a confused commuter on a train", tags=("commute",))
    keyword.rebuild_fts_index(db)
    settings = make_settings()

    results = await hybrid.search(db, "confused", MediaFilters(), settings, provider=None)

    assert results.mode == hybrid.MODE_KEYWORD_ONLY
    assert _ids(results)[0] == tagged
    assert results.total == 2


# ---------------------------------------------------------------------------
# Semantic stage (backend.search.semantic)
# ---------------------------------------------------------------------------


async def test_semantic_ranks_identical_embedding_first(db: sqlite3.Connection, make_settings) -> None:
    provider = MockVisionProvider()
    query = "confused cat reaction"
    target = _seed(db, description="pixel art of a feline")
    other = _seed(db, description="spreadsheet memo about budgets")
    _put_embedding(db, target, await provider.generate_embedding(query))
    _put_embedding(db, other, await provider.generate_embedding("quarterly budget memo"))

    results = await hybrid.search(db, query, MediaFilters(), make_settings(), provider=provider)

    assert results.mode == hybrid.MODE_HYBRID
    assert _ids(results)[0] == target
    assert results.hits[0].score is not None and results.hits[1].score is not None
    assert results.hits[0].score > results.hits[1].score
    assert all(0.0 <= (hit.score or 0.0) <= 1.0 for hit in results.hits)


async def test_provider_failure_degrades_to_keyword_only(db: sqlite3.Connection, make_settings) -> None:
    matched = _seed(db, description="a startled banana")
    keyword.rebuild_fts_index(db)

    results = await hybrid.search(
        db, "startled", MediaFilters(), make_settings(), provider=_BrokenProvider()
    )

    assert results.mode == hybrid.MODE_KEYWORD_ONLY
    assert _ids(results) == [matched]
    assert results.weights["semantic"] == 0.0
    assert sum(results.weights.values()) == pytest.approx(1.0)


@pytest.mark.parametrize("index_cls", [NumpySemanticIndex, FaissSemanticIndex])
def test_semantic_index_excludes_other_models_and_malformed_blobs(
    db: sqlite3.Connection, index_cls
) -> None:
    if index_cls is FaissSemanticIndex and not faiss_available():
        pytest.skip("faiss-cpu is not installed on this interpreter")
    good = _seed(db)
    foreign = _seed(db)
    broken = _seed(db)
    _put_embedding(db, good, [1.0, 0.0, 0.0])
    _put_embedding(db, foreign, [0.0, 1.0, 0.0], model="other/embedding-v1")
    with transaction(db):
        db.execute(
            "INSERT OR REPLACE INTO embeddings (media_id, embedding, embedding_model)"
            " VALUES (?, ?, ?)",
            (broken, b"\x01\x02", MOCK_EMBEDDING_MODEL),  # 2 bytes → not a float32 multiple
        )

    scoped = index_cls.from_db(db, embedding_model=MOCK_EMBEDDING_MODEL)
    assert len(scoped) == 1
    scoped_hits = scoped.search([1.0, 0.0, 0.0], 10)
    assert scoped_hits[0][0] == good
    assert scoped_hits[0][1] == pytest.approx(1.0)

    unscoped = index_cls.from_db(db)
    assert len(unscoped) == 2  # malformed blob still excluded
    ranked = unscoped.search([1.0, 0.0, 0.0], 10)
    assert ranked[0][0] == good and foreign in [media_id for media_id, _ in ranked]

    # A query vector from another dimension finds nothing instead of nonsense.
    assert unscoped.search([1.0, 0.0, 0.0, 0.0], 10) == []


def test_create_semantic_index_uses_best_available_backend(db: sqlite3.Connection) -> None:
    index = create_semantic_index(db)
    expected = FaissSemanticIndex if faiss_available() else NumpySemanticIndex
    assert isinstance(index, expected)


# ---------------------------------------------------------------------------
# Hybrid ranking: modes, weights, scoring
# ---------------------------------------------------------------------------


async def test_empty_query_is_filters_only_ordered_newest_first(db: sqlite3.Connection, make_settings) -> None:
    oldest = _seed(db, created_at="2026-01-01 00:00:00")
    newest = _seed(db, created_at="2026-01-09 00:00:00")
    middle = _seed(db, created_at="2026-01-05 00:00:00")

    results = await hybrid.search(db, "   ", MediaFilters(), make_settings(), provider=None)

    assert results.mode == hybrid.MODE_FILTERS_ONLY
    assert _ids(results) == [newest, middle, oldest]
    assert all(hit.score is None for hit in results.hits)
    # An empty query must not create the provider at all.
    assert results.weights["semantic"] == pytest.approx(
        float(make_settings().search.semantic_weight)
    )


async def test_no_results_is_an_empty_page_not_an_error(db: sqlite3.Connection, make_settings) -> None:
    _seed(db, description="ordinary meme")
    keyword.rebuild_fts_index(db)

    results = await hybrid.search(db, "zzznotpresent", MediaFilters(), make_settings(), provider=None)

    assert results.hits == []
    assert results.total == 0
    assert results.mode == hybrid.MODE_KEYWORD_ONLY


async def test_perfect_match_scores_sum_of_configured_weights(
    db: sqlite3.Connection, make_settings
) -> None:
    provider = MockVisionProvider()
    query = "atom"
    row = _seed(db, filename="atom.png", description="an atom appears", tags=("atom",))
    _put_embedding(db, row, await provider.generate_embedding(query))
    keyword.rebuild_fts_index(db)
    settings = make_settings()

    results = await hybrid.search(db, query, MediaFilters(), settings, provider=provider)

    # Every component scores 1.0 (only row, exact text) → score == weight sum == 1.0.
    expected = sum(
        float(value)
        for value in (
            settings.search.keyword_weight,
            settings.search.semantic_weight,
            settings.search.tag_weight,
            settings.search.metadata_weight,
        )
    )
    assert results.mode == hybrid.MODE_HYBRID
    assert results.hits[0].score == pytest.approx(expected, abs=1e-6)
    assert sum(results.weights.values()) == pytest.approx(1.0)


async def test_keyword_only_weights_renormalize_over_remaining(
    db: sqlite3.Connection, make_settings
) -> None:
    row = _seed(db, description="a startled banana")
    keyword.rebuild_fts_index(db)
    settings = make_settings()

    results = await hybrid.search(db, "startled", MediaFilters(), settings, provider=None)

    total = (
        settings.search.keyword_weight + settings.search.tag_weight + settings.search.metadata_weight
    )
    assert results.weights["semantic"] == 0.0
    assert results.weights["keyword"] == pytest.approx(settings.search.keyword_weight / total)
    assert results.weights["tag"] == pytest.approx(settings.search.tag_weight / total)
    assert results.weights["metadata"] == pytest.approx(settings.search.metadata_weight / total)
    assert sum(results.weights.values()) == pytest.approx(1.0)
    assert _ids(results) == [row]


async def test_keyword_only_equal_split_when_only_semantic_was_configured(
    db: sqlite3.Connection, make_settings
) -> None:
    _seed(db, description="a startled banana")
    keyword.rebuild_fts_index(db)
    settings = make_settings()
    settings = replace(
        settings,
        search=replace(
            settings.search,
            keyword_weight=0.0,
            semantic_weight=1.0,
            tag_weight=0.0,
            metadata_weight=0.0,
        ),
    )

    results = await hybrid.search(db, "startled", MediaFilters(), settings, provider=None)

    assert results.weights["semantic"] == 0.0
    assert results.weights["keyword"] == pytest.approx(1.0 / 3.0)
    assert results.weights["tag"] == pytest.approx(1.0 / 3.0)
    assert results.weights["metadata"] == pytest.approx(1.0 / 3.0)
    assert sum(results.weights.values()) == pytest.approx(1.0)


async def test_tag_like_fallback_covers_a_stale_index(db: sqlite3.Connection, make_settings) -> None:
    row = _seed(db, description="innocent row")
    keyword.rebuild_fts_index(db)
    # Tags edited after the last index run: FTS knows nothing about them yet.
    with transaction(db):
        db.execute(
            "UPDATE ai_metadata SET tags = ? WHERE media_id = ?",
            (json.dumps(["unicorn"]), row),
        )

    results = await hybrid.search(db, "unicorn", MediaFilters(), make_settings(), provider=None)

    assert _ids(results) == [row]


# ---------------------------------------------------------------------------
# Filters (shared with the gallery where-clause)
# ---------------------------------------------------------------------------


async def test_ranked_and_filters_only_runs_honor_every_filter(
    db: sqlite3.Connection, make_settings
) -> None:
    flagged = _seed(
        db,
        description="shared meme text",
        dup_status="dup",
        emotions=("joy",),
        extension="png",
    )
    gif = _seed(
        db,
        description="shared meme text",
        dup_status="nondup",
        emotions=("sadness",),
        extension="gif",
        site="mangadex",
    )
    keyword.rebuild_fts_index(db)
    settings = make_settings()

    dup_only = await hybrid.search(db, "meme", MediaFilters(dup_status="dup"), settings, provider=None)
    assert _ids(dup_only) == [flagged]

    emotion_only = await hybrid.search(
        db, "meme", MediaFilters(emotion="JOY"), settings, provider=None
    )
    assert _ids(emotion_only) == [flagged]

    gifs = await hybrid.search(db, "meme", MediaFilters(type="gif"), settings, provider=None)
    assert _ids(gifs) == [gif]

    unfiltered = await hybrid.search(db, "meme", MediaFilters(), settings, provider=None)
    assert set(_ids(unfiltered)) == {flagged, gif}

    filters_only = await hybrid.search(
        db, "", MediaFilters(emotion="joy"), settings, provider=None
    )
    assert filters_only.mode == hybrid.MODE_FILTERS_ONLY
    assert _ids(filters_only) == [flagged]


# ---------------------------------------------------------------------------
# Index maintenance: self-heal + incremental reindex
# ---------------------------------------------------------------------------


async def test_search_self_heals_a_never_built_index(
    db: sqlite3.Connection, make_settings
) -> None:
    row = _seed(db, description="glorious meme collection")
    # Simulate a library whose INDEX stage never ran.
    with transaction(db):
        db.execute("DELETE FROM media_fts")

    results = await hybrid.search(db, "glorious", MediaFilters(), make_settings(), provider=None)

    assert _ids(results) == [row]
    assert int(db.execute("SELECT COUNT(*) FROM media_fts").fetchone()[0]) == 1


def test_index_media_reindexes_a_single_row(db: sqlite3.Connection) -> None:
    row = _seed(db, description="original caption alpha", user_description="first draft zebra")
    keyword.rebuild_fts_index(db)
    assert [hit[0] for hit in keyword.query(db, "zebra")] == [row]

    with transaction(db):
        db.execute("UPDATE media SET user_description = ? WHERE id = ?", ("renamed bravo", row))

    assert keyword.index_media(db, row) is True
    assert keyword.query(db, "zebra") == []  # replaced text dropped from the index
    assert [hit[0] for hit in keyword.query(db, "bravo")] == [row]
    assert [hit[0] for hit in keyword.query(db, "alpha")] == [row]  # untouched AI text stays
    assert keyword.index_media(db, 999_999) is False
