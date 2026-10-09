"""Media persistence — the STORE stage of the pipeline (PRD §11, §20, §31, §57).

One cohesive module owns writing/reading library rows:

* :func:`ingest_download` — turn a successful :class:`DownloadResult` into a
  ``media`` row + ``source`` provenance row (PRD §31) under the id-based
  filename scheme ``00000001.jpg`` (PRD §5.3), then run the duplicate flag
  scan (M2.3) so every stored item is born ``dup``/``nondup``.
* :func:`get_media` / :func:`list_media` / :func:`count_media` /
  :func:`count_by_dup_status` / :func:`count_by_processing_status` /
  :func:`filter_ids` — read side for the gallery and search APIs, always
  paginated (PRD §50: never load the whole library); :func:`shape_record` is
  the shared row → API-record mapping both routes use.
* :func:`remove_media` — delete one item's files + rows (used by
  ``DELETE /api/media/{id}``; the 7-day dup purge lives in
  :mod:`backend.media.duplicates`).

Ingest atomicity and failure handling (PRD §11, §36):

1. SHA-256 and image metadata are read *before* any DB write — an unreadable
   file raises here and leaves no rows behind.
2. ``media`` + ``source`` are inserted in **one transaction**; the id is
   computed first so ``file_path`` is already the final
   ``<storage.media_directory>/<id><ext>`` name.
3. Only after that transaction commits is the downloaded file moved into place
   (``os.replace``, falling back to copy+unlink across filesystems). If the
   move fails, the just-inserted rows are deleted again (compensation) and the
   error is re-raised — **a row never points at a missing file**. The
   downloaded file itself stays at the downloader's path for inspection.
4. Finally :func:`~backend.media.duplicates.scan_and_flag` flags the row; a
   failure there is repaired by the next dup job (the scan is idempotent).

Level-2 exact duplicates (same SHA-256 as an existing row) are **recorded, not
dropped**: the new copy gets its own row + file, is immediately flagged ``dup``
(``dup_of_media_id`` → retained copy) and returns ``None`` from
:func:`ingest_download` — ``None`` means "not a new unique item", while the row
itself powers the PRD §12.1 lifecycle (pending-deletion UI, unflag, 7-day
purge). The *first-collected* row is always the retained, never-flagged copy.

Conventions: ``media.extension`` is lowercase without a dot (``png``);
``source.site`` is the page hostname (matching M0's seed convention); derived
``dup_expires_at`` is attached to list/detail records for the UI.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from backend.config import Settings
from backend.database.database import transaction
from backend.media.duplicates import dup_expires_at, scan_and_flag, unlink_media_files
from backend.media.hashing import ImageMeta, compute_image_meta, sha256_file
from backend.scraper.adapters.base import CommentMeta
from backend.scraper.downloader import DownloadResult

logger = logging.getLogger(__name__)

#: ``media.type`` filter → matching lowercase extensions (no dot).
_TYPE_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "image": ("jpg", "jpeg", "png", "webp"),
    "gif": ("gif",),
    "video": ("mp4", "webm"),
}

#: Accepted values for the ``dup_status`` filter (schema CHECK constraint).
DUP_STATUSES: frozenset[str] = frozenset({"dup", "nondup", "unflagged"})

#: Accepted values for the ``processing_status`` filter (PRD §19).
PROCESSING_STATUSES: frozenset[str] = frozenset(
    {"DOWNLOADED", "ANALYZING", "ANALYZED", "EMBEDDING", "READY", "FAILED"}
)

_EXTENSION_BY_CONTENT_TYPE: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}

_MIME_BY_EXTENSION: dict[str, str] = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
    "mp4": "video/mp4",
    "webm": "video/webm",
}

#: File naming per PRD §5.3: ``00000001.jpg`` — eight digits, extension with dot.
_ID_NAME_DIGITS = 8

#: How often ingest retries when another writer took the id it computed.
_ID_RETRY_ATTEMPTS = 3


@dataclass(frozen=True)
class MediaFilters:
    """Where-clause for gallery queries (PRD §24); every field defaults to no filter."""

    type: str | None = None            # image | gif | video (by extension)
    site: str | None = None            # exact source.site (page hostname)
    format: str | None = None          # extension, case/dot-insensitive (jpg, png, gif…)
    chapter: str | None = None         # exact source.chapter
    date_from: str | None = None       # inclusive lower bound on created_at
    date_to: str | None = None         # inclusive upper bound on created_at (whole day)
    processing_status: str | None = None
    dup_status: str | None = None
    is_favorite: bool | None = None
    emotion: str | None = None         # case-insensitive ai_metadata.emotions member (json1)


def ingest_download(
    conn: sqlite3.Connection,
    result: DownloadResult,
    comment: CommentMeta,
    *,
    settings: Settings,
) -> int | None:
    """Store one successful download: ``media`` + ``source`` rows + id-named file.

    ``comment`` is the item's
    :class:`~backend.scraper.adapters.base.CommentMeta` provenance (page URL,
    chapter, page number, comment id, author).

    Returns the new ``media.id`` for a unique item, or ``None`` when the same
    SHA-256 already exists — in that case the copy is still recorded and
    flagged ``dup`` per PRD §12.1 (see the module docstring), and ``None``
    tells the caller "not new".

    Raises ``ValueError`` when ``result`` is not a successful download,
    ``OSError``/``sqlite3.Error`` on storage failures (the caller records a
    failed item — PRD §36).
    """
    if not result.ok or result.path is None:
        raise ValueError("ingest_download requires a successful DownloadResult with a path")
    source_path = Path(result.path)
    sha256 = result.sha256 or sha256_file(source_path)
    meta = compute_image_meta(source_path)
    extension = _extension_for(result, source_path)
    mime_type = result.content_type or _MIME_BY_EXTENSION.get(extension, "application/octet-stream")
    file_size = result.size or source_path.stat().st_size
    site = _site_for(comment.page_url) if comment.page_url else _site_for(result.url)

    media_dir = settings.storage.media_directory
    media_dir.mkdir(parents=True, exist_ok=True)
    media_id, final_path = _insert_rows(
        conn,
        result=result,
        comment=comment,
        settings=settings,
        site=site,
        sha256=sha256,
        meta=meta,
        extension=extension,
        mime_type=mime_type,
        file_size=file_size,
    )
    try:
        _move_into_place(source_path, final_path)
    except OSError:
        logger.exception("storing file failed — rolling back rows media_id=%d path=%s",
                         media_id, final_path)
        _delete_rows(conn, media_id)
        raise
    status = scan_and_flag(conn, media_id)
    if status == "dup":
        logger.info("ingested as duplicate media_id=%d sha256=%s", media_id, sha256[:12])
        return None
    logger.info("ingested media_id=%d path=%s size=%d", media_id, final_path, file_size)
    return media_id


def get_media(conn: sqlite3.Connection, media_id: int) -> dict[str, Any] | None:
    """Full record for the detail page: media fields + ``sources`` + ``ai_metadata``.

    ``None`` when the id does not exist. Includes the derived ``site``
    (first source's hostname) and ``dup_expires_at``.
    """
    row = conn.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
    if row is None:
        return None
    sources = [
        dict(item)
        for item in conn.execute(
            "SELECT * FROM source WHERE media_id = ? ORDER BY id", (media_id,)
        )
    ]
    ai_row = conn.execute(
        "SELECT * FROM ai_metadata WHERE media_id = ?", (media_id,)
    ).fetchone()
    record = shape_record(row)
    record["site"] = sources[0]["site"] if sources else None
    record["sources"] = sources
    record["ai_metadata"] = dict(ai_row) if ai_row is not None else None
    return record


def list_media(
    conn: sqlite3.Connection,
    filters: MediaFilters | None = None,
    *,
    page: int = 1,
    page_size: int = 50,
) -> list[dict[str, Any]]:
    """One filtered, newest-first page of gallery items (PRD §50 pagination)."""
    active = filters if filters is not None else MediaFilters()
    clause, params = _where(active)
    offset = (max(1, page) - 1) * max(1, page_size)
    rows = conn.execute(
        "SELECT m.*, "
        "(SELECT s.site FROM source s WHERE s.media_id = m.id ORDER BY s.id LIMIT 1) AS site "
        f"FROM media m {clause} "
        "ORDER BY m.created_at DESC, m.id DESC LIMIT ? OFFSET ?",
        (*params, page_size, offset),
    ).fetchall()
    return [shape_record(row) for row in rows]


def count_media(conn: sqlite3.Connection, filters: MediaFilters | None = None) -> int:
    """Total rows matching ``filters`` (the gallery's ``total``)."""
    clause, params = _where(filters if filters is not None else MediaFilters())
    return int(
        conn.execute(f"SELECT COUNT(*) FROM media m {clause}", params).fetchone()[0]
    )


def count_by_dup_status(
    conn: sqlite3.Connection, filters: MediaFilters | None = None
) -> dict[str, int]:
    """Dup breakdown of the filtered set — always all three PRD §12.1 keys.

    Faceted counting: the ``dup_status`` filter itself is ignored, so clicking
    one Dup badge never zeroes the other badges (the strip keeps showing the
    full breakdown of everything else that matches the remaining filters).
    """
    active = filters if filters is not None else MediaFilters()
    if active.dup_status is not None:
        active = replace(active, dup_status=None)
    clause, params = _where(active)
    rows = conn.execute(
        f"SELECT m.dup_status, COUNT(*) AS n FROM media m {clause} GROUP BY m.dup_status",
        params,
    ).fetchall()
    found = {str(row["dup_status"]): int(row["n"]) for row in rows}
    return {status: found.get(status, 0) for status in DUP_STATUSES}


def count_by_processing_status(
    conn: sqlite3.Connection, filters: MediaFilters | None = None
) -> dict[str, int]:
    """PRD §19 header breakdown: ``ready`` / ``processing`` / ``failed``.

    ``processing`` is everything still moving through the queue
    (``DOWNLOADED``, ``ANALYZING``, ``ANALYZED``, ``EMBEDDING``) so the three
    buckets always sum to the filtered total. Faceted counting: the
    ``processing_status`` filter itself is ignored so the three buckets never
    collapse to a single one while a status filter is applied.
    """
    active = filters if filters is not None else MediaFilters()
    if active.processing_status is not None:
        active = replace(active, processing_status=None)
    clause, params = _where(active)
    rows = conn.execute(
        f"SELECT m.processing_status, COUNT(*) AS n FROM media m {clause} "
        "GROUP BY m.processing_status",
        params,
    ).fetchall()
    counts = {"ready": 0, "processing": 0, "failed": 0}
    for row in rows:
        status = str(row["processing_status"])
        bucket = "ready" if status == "READY" else "failed" if status == "FAILED" else "processing"
        counts[bucket] += int(row["n"])
    return counts


def filter_ids(
    conn: sqlite3.Connection,
    filters: MediaFilters | None = None,
    *,
    newest_first: bool = True,
) -> list[int]:
    """Ids matching ``filters`` — newest first by default (``created_at DESC, id DESC``).

    Used by the hybrid searcher: once for the allowed set (any order) and, for
    an empty query, as the ``filters_only`` result order (identical to
    :func:`list_media`'s ordering so paging behaves the same way).
    """
    clause, params = _where(filters if filters is not None else MediaFilters())
    order = "ORDER BY m.created_at DESC, m.id DESC" if newest_first else ""
    rows = conn.execute(
        f"SELECT m.id FROM media m {clause} {order}", params
    ).fetchall()
    return [int(row["id"]) for row in rows]


def remove_media(conn: sqlite3.Connection, media_id: int, *, settings: Settings) -> bool:
    """Delete one item: ``media_fts`` row, ``media`` row (children cascade), files.

    Returns ``False`` when the id does not exist. Files are removed best effort
    *after* the rows commit — a failed unlink leaves an orphan file (logged),
    never a row pointing at a deleted file. Child rows (``source``,
    ``ai_metadata``, ``embeddings``, ``collection_items``) cascade via FK.
    """
    row = conn.execute(
        "SELECT file_path, thumbnail_path, preview_path FROM media WHERE id = ?",
        (media_id,),
    ).fetchone()
    if row is None:
        return False
    with transaction(conn):
        conn.execute(
            "DELETE FROM media_fts WHERE rowid = ? OR media_id = ?", (media_id, media_id)
        )
        conn.execute("DELETE FROM media WHERE id = ?", (media_id,))
    unlink_media_files(
        (row["file_path"], row["thumbnail_path"], row["preview_path"]), settings
    )
    logger.info("media removed media_id=%d", media_id)
    return True


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _insert_rows(
    conn: sqlite3.Connection,
    *,
    result: DownloadResult,
    comment: CommentMeta,
    settings: Settings,
    site: str,
    sha256: str,
    meta: ImageMeta,
    extension: str,
    mime_type: str,
    file_size: int,
) -> tuple[int, Path]:
    """Insert ``media`` + ``source`` in one transaction; returns ``(id, final_path)``.

    Retries a few times on id collision (another writer computed the same next
    id); the ``IntegrityError`` rolls the whole transaction back, so no partial
    rows survive.
    """
    for attempt in range(_ID_RETRY_ATTEMPTS):
        media_id = _next_media_id(conn)
        final_path = settings.storage.media_directory / f"{media_id:0{_ID_NAME_DIGITS}d}{extension}"
        try:
            with transaction(conn):
                conn.execute(
                    "INSERT INTO media (id, file_path, original_filename, mime_type, "
                    "extension, file_size, width, height, duration, sha256, "
                    "processing_status, dup_status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'DOWNLOADED', 'nondup')",
                    (
                        media_id,
                        str(final_path),
                        Path(result.path).name if result.path is not None else None,
                        mime_type,
                        extension.lstrip("."),
                        file_size,
                        meta.width,
                        meta.height,
                        meta.duration,
                        sha256,
                    ),
                )
                conn.execute(
                    "INSERT INTO source (media_id, site, page_url, chapter, page_number, "
                    "comment_id, media_url, author_name) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        media_id,
                        site,
                        comment.page_url,
                        comment.chapter,
                        comment.page_number,
                        comment.comment_id,
                        result.url,
                        comment.author_name,
                    ),
                )
            return media_id, final_path
        except sqlite3.IntegrityError:
            if attempt + 1 == _ID_RETRY_ATTEMPTS:
                raise
            logger.warning("media id collision id=%d — retrying", media_id)


def _next_media_id(conn: sqlite3.Connection) -> int:
    """Next free ``media.id``: max of live rows and the AUTOINCREMENT sequence.

    Consulting both matters after purges: ``sqlite_sequence`` is what stops a
    deleted id from being handed out again (PRD §5.3's names must stay stable).
    """
    max_id = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM media").fetchone()[0])
    seq_row = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'media'"
    ).fetchone()
    sequence = int(seq_row[0]) if seq_row is not None else 0
    return max(max_id, sequence) + 1


def _extension_for(result: DownloadResult, source_path: Path) -> str:
    """Final-file extension (with dot) from the verified content type.

    Falls back to the downloaded file's suffix — both are validated against the
    accepted set, so untrusted input never shapes the filename (PRD §41).
    """
    if result.content_type:
        known = _EXTENSION_BY_CONTENT_TYPE.get(result.content_type)
        if known:
            return known
    suffix = source_path.suffix.lower()
    if suffix.lstrip(".") in _MIME_BY_EXTENSION:
        return suffix
    logger.warning("unknown content type %r for %s — using .bin",
                   result.content_type, source_path)
    return ".bin"


def _site_for(page_url: str) -> str:
    """``source.site`` for a provenance URL: its hostname (M0 seed convention)."""
    hostname = urlparse(page_url).hostname
    if hostname:
        return hostname
    return "unknown"


def _move_into_place(source: Path, target: Path) -> None:
    """Move the downloaded file to its final id-based name (PRD §11).

    ``os.replace`` is atomic within a filesystem; across filesystems (EXDEV)
    fall back to copy + unlink. Raises ``OSError`` on failure so the caller can
    roll back the rows it inserted.
    """
    if source.resolve() == target.resolve():
        return
    try:
        os.replace(source, target)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    shutil.copy2(source, target)
    try:
        source.unlink()
    except OSError:
        # The copy is in place and the row is valid; an orphan in the
        # downloader's directory is logged, not fatal.
        logger.warning("copied into library but could not remove source path=%s", source)


def _delete_rows(conn: sqlite3.Connection, media_id: int) -> None:
    """Compensating rollback for a failed file move; never raises."""
    try:
        with transaction(conn):
            conn.execute("DELETE FROM media WHERE id = ?", (media_id,))
    except sqlite3.Error:
        logger.exception("compensating delete failed media_id=%d", media_id)


def shape_record(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    """Row → API record: add ``dup_expires_at`` (only meaningful while ``dup``).

    ``file_path`` (an absolute storage path) is dropped — records are shaped for
    clients; the serve/remove code paths read the column straight from the row.
    Shared by the gallery and search routes so both expose identical items.
    """
    record = dict(row)
    record.pop("file_path", None)
    flagged_at = record.get("dup_flagged_at")
    record["dup_expires_at"] = (
        dup_expires_at(flagged_at) if record.get("dup_status") == "dup" else None
    )
    return record


def _where(filters: MediaFilters) -> tuple[str, list[Any]]:
    """Build the shared ``WHERE`` clause for list/count queries (params bound)."""
    clauses: list[str] = []
    params: list[Any] = []
    if filters.type is not None:
        extensions = _TYPE_EXTENSIONS.get(filters.type)
        if extensions is None:
            raise ValueError(
                f"unknown media type {filters.type!r} — expected one of {sorted(_TYPE_EXTENSIONS)}"
            )
        clauses.append(f"m.extension IN ({', '.join('?' * len(extensions))})")
        params.extend(extensions)
    if filters.site is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM source s WHERE s.media_id = m.id AND s.site = ?)"
        )
        params.append(filters.site)
    if filters.format is not None:
        clauses.append("LOWER(m.extension) = ?")
        params.append(filters.format.lstrip(".").lower())
    if filters.chapter is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM source s WHERE s.media_id = m.id AND s.chapter = ?)"
        )
        params.append(filters.chapter)
    if filters.date_from is not None:
        clauses.append("m.created_at >= ?")
        params.append(filters.date_from)
    if filters.date_to is not None:
        clauses.append("m.created_at < date(?, '+1 day')")
        params.append(filters.date_to)
    if filters.processing_status is not None:
        if filters.processing_status not in PROCESSING_STATUSES:
            raise ValueError(
                f"unknown processing status {filters.processing_status!r} — "
                f"expected one of {sorted(PROCESSING_STATUSES)}"
            )
        clauses.append("m.processing_status = ?")
        params.append(filters.processing_status)
    if filters.dup_status is not None:
        if filters.dup_status not in DUP_STATUSES:
            raise ValueError(
                f"unknown dup status {filters.dup_status!r} — expected one of {sorted(DUP_STATUSES)}"
            )
        clauses.append("m.dup_status = ?")
        params.append(filters.dup_status)
    if filters.is_favorite is not None:
        clauses.append("m.is_favorite = ?")
        params.append(1 if filters.is_favorite else 0)
    if filters.emotion is not None:
        # json1 membership over ai_metadata.emotions (verified json_each/json_valid
        # on the bundled SQLite); malformed/null JSON matches nothing instead of erroring.
        clauses.append(
            "EXISTS (SELECT 1 FROM ai_metadata ae WHERE ae.media_id = m.id AND EXISTS ("
            "SELECT 1 FROM json_each(CASE WHEN json_valid(ae.emotions) THEN ae.emotions "
            "ELSE '[]' END) je WHERE lower(je.value) = lower(?)))"
        )
        params.append(filters.emotion)
    return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), params
