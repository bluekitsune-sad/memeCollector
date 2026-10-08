"""Keyword search over the standalone ``media_fts`` FTS5 index (PRD §20, §21B).

``media_fts`` is a *standalone* FTS5 table whose ``rowid == media.id``
(rationale in :mod:`backend.database.migrations`); this module is the indexer
that owns keeping it in sync:

* :func:`rebuild_fts_index` — full reindex via one JOIN over ``media`` +
  ``ai_metadata`` + ``source`` (description, tags, filename, title,
  user-edited text, site and chapter all land in the index);
* :func:`index_media` — incremental reindex of a single id (after a manual edit
  or a reanalysis);
* :func:`query` — ranked lookup returning ``(media_id, bm25_rank)`` pairs,
  best match first (``bm25()`` is negative: *lower is better*).

**Query sanitization** — FTS5's ``MATCH`` grammar raises ``OperationalError``
on stray parentheses/operators (``foo OR (bar``), so user input never reaches
SQL verbatim. :func:`tokenize_query` extracts the words, drops the boolean
operators ``AND``/``OR``/``NOT``/``NEAR`` (an operator can never change the
meaning of a search here), wraps every term in double quotes and appends the
prefix marker ``*`` to the trailing term so incremental typing matches
(``conf`` → ``"conf"*``). Text the user quoted stays a phrase
(``"big mood"`` → ``"big mood"``). The result is joined with ``AND``, so a
sanitized query is always a valid MATCH expression; :func:`query` additionally
swallows a defensive ``OperationalError`` rather than ever raising (PRD §52).

Column-restricted variants (:func:`column_query`) power the hybrid tag and
metadata scores — ``{tags} : "cat"`` searches only that column.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Sequence

from backend.database.database import transaction

logger = logging.getLogger(__name__)

#: Indexed ``media_fts`` content columns (schema in 001_initial_schema.sql).
FTS_COLUMNS: tuple[str, ...] = ("description", "tags", "filename", "source_text")

#: FTS5 boolean keywords a user may type literally — stripped, never passed through.
_OPERATORS: frozenset[str] = frozenset({"and", "or", "not", "near"})

#: Word runs (letters/digits/marks) — the only tokens ever quoted; no FTS5
#: metacharacter (``"`` ``*`` ``(`` ``{`` …) survives this pattern.
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)

#: Double-quoted spans the user typed are preserved as exact phrases.
_PHRASE_RE = re.compile(r'"([^"]*)"')

#: Columns every row is built from, in :data:`FTS_COLUMNS` order.
_ROW_SELECT = """
SELECT m.id AS rowid,
       m.id AS media_id,
       TRIM(COALESCE(a.description, '') || ' ' || COALESCE(m.user_description, '')
            || ' ' || COALESCE(m.title, '')) AS description,
       TRIM(COALESCE(a.tags, '') || ' ' || COALESCE(m.user_tags, '')) AS tags,
       COALESCE(m.original_filename, '') AS filename,
       COALESCE(src.site_text, '') AS source_text
FROM media m
LEFT JOIN ai_metadata a ON a.media_id = m.id
LEFT JOIN (
    SELECT media_id, GROUP_CONCAT(site || ' ' || COALESCE(chapter, ''), ' ') AS site_text
    FROM source GROUP BY media_id
) src ON src.media_id = m.id
"""


def plain_terms(query: str) -> list[str]:
    """Words of ``query`` with operators removed — used by the hybrid LIKE fallback."""
    return [
        word for word in _WORD_RE.findall(query) if word.lower() not in _OPERATORS
    ]


def tokenize_query(query: str) -> list[str]:
    """Split raw user input into safe FTS5 phrase atoms (never raises).

    Each atom is a fully quoted phrase: ``conf`` → ``["conf"*]``,
    ``foo OR (bar`` → ``["foo", "bar"*]``, ``"big mood"`` → ``["big mood"]``.
    The ``*`` prefix marker lands only on the last unquoted word, and never
    when the query ends inside a quoted phrase.
    """
    phrases = _PHRASE_RE.findall(query)
    remainder = _PHRASE_RE.sub(" ", query)
    words = plain_terms(remainder)
    prefix_last = bool(words) and not query.rstrip().endswith('"')
    atoms = [
        '"' + " ".join(words_of_phrase) + '"'
        for words_of_phrase in (_WORD_RE.findall(phrase) for phrase in phrases)
        if words_of_phrase
    ]
    for index, word in enumerate(words):
        atom = f'"{word}"'
        if prefix_last and index == len(words) - 1:
            atom += "*"
        atoms.append(atom)
    return atoms


def sanitize_query(query: str) -> str:
    """Complete injection-safe FTS5 ``MATCH`` expression; ``""`` when nothing is searchable."""
    return " AND ".join(tokenize_query(query))


def rebuild_fts_index(conn: sqlite3.Connection) -> int:
    """Drop and rebuild every ``media_fts`` row; returns the number of rows indexed.

    One transaction: the index is never observable half-built. Idempotent, so
    it is safe to call from the INDEX job, the search self-heal and tests alike.
    """
    with transaction(conn):
        conn.execute("DELETE FROM media_fts")
        conn.execute(
            "INSERT INTO media_fts (rowid, media_id, description, tags, filename, source_text) "
            + _ROW_SELECT
        )
        count = int(conn.execute("SELECT COUNT(*) FROM media_fts").fetchone()[0])
    logger.info("fts index rebuilt indexed=%d", count)
    return count


def index_media(conn: sqlite3.Connection, media_id: int) -> bool:
    """Reindex one media id (row replaced atomically); ``False`` if the id is gone.

    Incremental counterpart of :func:`rebuild_fts_index` — called after manual
    edits (PRD §27) and reanalysis so keyword search tracks changed text
    without a full reindex.
    """
    row = conn.execute(_ROW_SELECT + " WHERE m.id = ?", (media_id,)).fetchone()
    with transaction(conn):
        conn.execute("DELETE FROM media_fts WHERE rowid = ?", (media_id,))
        if row is None:
            return False
        conn.execute(
            "INSERT INTO media_fts (rowid, media_id, description, tags, filename, source_text) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                int(row["rowid"]),
                int(row["media_id"]),
                row["description"],
                row["tags"],
                row["filename"],
                row["source_text"],
            ),
        )
    return True


def query(
    conn: sqlite3.Connection, q: str, limit: int = 500
) -> list[tuple[int, float]]:
    """Ranked matches for ``q``: ``[(media_id, bm25_rank), …]`` best first.

    ``bm25_rank`` is SQLite's raw ``bm25()`` value (negative — lower is better);
    callers normalize it. Adversarial input is sanitized first and an empty
    result list is returned when no searchable term survives (PRD §52: "no
    results" is a valid outcome, an exception never is).
    """
    return _match(conn, sanitize_query(q), limit)


def column_query(
    conn: sqlite3.Connection,
    q: str,
    columns: Sequence[str],
    limit: int = 500,
) -> list[tuple[int, float]]:
    """Like :func:`query` but restricted to ``columns`` (``{tags} : "cat"``).

    Used for the hybrid tag score (tags only) and metadata score (filename +
    source text only). Unknown column names raise ``ValueError`` — they are
    always internal constants, never user input.
    """
    unknown = [column for column in columns if column not in FTS_COLUMNS]
    if unknown:
        raise ValueError(f"unknown media_fts columns: {unknown}")
    atoms = tokenize_query(q)
    if not atoms or not columns:
        return []
    colspec = "{" + " ".join(columns) + "}"
    expression = " AND ".join(f"{colspec} : {atom}" for atom in atoms)
    return _match(conn, expression, limit)


def _match(conn: sqlite3.Connection, expression: str, limit: int) -> list[tuple[int, float]]:
    """Run one sanitized ``MATCH`` expression; never raises for bad input."""
    if not expression:
        return []
    try:
        rows = conn.execute(
            "SELECT rowid, bm25(media_fts) AS rank FROM media_fts "
            "WHERE media_fts MATCH ? ORDER BY bm25(media_fts) LIMIT ?",
            (expression, limit),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        # The sanitizer makes this unreachable; kept so a future grammar change
        # degrades to "no keyword hits" instead of a 500 (PRD §52).
        logger.warning("fts match rejected expression=%s error=%s", expression, exc)
        return []
    return [(int(row["rowid"]), float(row["rank"])) for row in rows]
