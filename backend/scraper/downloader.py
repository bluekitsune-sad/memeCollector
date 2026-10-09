"""Download manager — the first half of the STORE stage (PRD §11, §41; AGENTS.md §9).

Contract (the crawl job and Milestone 2 code against this):

* :meth:`Downloader.download` accepts a :class:`~backend.scraper.adapters.base.MediaRef`
  and returns a :class:`DownloadResult` — **it never raises for a failed
  download**; every failure is a ``status=FAILED`` result with an ``error``
  reason so one bad file never kills the job (PRD §36).
* Level-1 URL duplicate check runs first via the injected
  ``url_already_downloaded`` callback (``SELECT 1 FROM source WHERE media_url=?``
  in the crawl job) → ``status=DUPLICATE_URL`` without touching the network.
* Storage: files stream to a ``.part`` temp file **in the destination directory**,
  are validated (accepted content types + Pillow magic-byte verification for
  images, header magic for MP4/WebM — M1.9), hashed (SHA-256), then
  ``os.replace``-d to the final name — atomic, no partial files ever visible
  (PRD §11).
* Naming: caller passes ``target_name`` (Milestone 2 uses the id-based scheme
  ``00000001.jpg``); otherwise the media's ``original_filename`` or the URL
  basename is sanitized per PRD §41 (no traversal, no reserved characters).
  The final path is verified to stay inside ``destination_dir``.
* Limits from ``Settings.crawler``: ``max_file_size_mb`` (checked from
  ``Content-Length`` and again mid-stream), ``retry_attempts`` with
  exponential backoff on transport errors / 429 / 5xx, ``delay_seconds`` as
  the backoff base, ``request_timeout_seconds``.
* Navigation guard (PRD §41): the media URL and **every redirect hop** pass
  :func:`backend.security.urls.ensure_safe_url` before a request is issued, so
  a hostile link can never pivot the downloader onto a private/metadata
  address. A blocked URL is recorded as a normal ``status=FAILED`` result.

The downloader writes files only — it never creates DB rows (media/source rows
are Milestone 2).
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from PIL import Image

from backend.config import Settings
from backend.media.hashing import sha256_file
from backend.scraper.adapters import MediaKind, MediaRef
from backend.scraper.backoff import backoff_delay, clamp_retry_after, parse_retry_after
from backend.security.urls import (
    MAX_REDIRECTS,
    REDIRECT_STATUSES,
    UnsafeURLError,
    ensure_safe_url,
    reject_url,
)

logger = logging.getLogger(__name__)

#: Content types the collector accepts (PRD §5.3 — images/GIFs + MP4/WebM video).
ACCEPTED_CONTENT_TYPES: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/gif", "video/mp4", "video/webm"}
)

#: Declared types that carry no information — validation falls back to content sniffing.
_UNKNOWN_CONTENT_TYPES: frozenset[str] = frozenset({"application/octet-stream"})

_CONTENT_TYPE_ALIASES: dict[str, str] = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
}

_CONTENT_TYPE_BY_FORMAT: dict[str, str] = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}

_EXTENSION_BY_CONTENT_TYPE: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}

# Windows reserved device names — a file named e.g. "CON.png" is unopenable (PRD §41).
_WINDOWS_RESERVED: frozenset[str] = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

# Characters invalid in file names on Windows/POSIX plus all control characters.
_UNSAFE_CHARS: frozenset[str] = frozenset('<>:"|?*') | frozenset(chr(code) for code in range(32))

_MAX_STEM_LENGTH = 100
_MAX_EXT_LENGTH = 16

_WEBM_MAGIC = b"\x1a\x45\xdf\xa3"


class DownloadStatus(str, Enum):
    """Outcome of a download attempt."""

    OK = "ok"
    DUPLICATE_URL = "duplicate_url"
    FAILED = "failed"


@dataclass(frozen=True)
class DownloadResult:
    """Result of one :meth:`Downloader.download` call — success or recorded failure."""

    url: str
    status: DownloadStatus
    path: Path | None = None
    sha256: str | None = None
    size: int = 0
    content_type: str | None = None
    kind: MediaKind | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is DownloadStatus.OK


class _ContentError(Exception):
    """Payload failed content validation; message is the user-facing failure reason."""


def _normalize_content_type(raw: str | None) -> str | None:
    """Lower-cased media type without parameters; ``None`` when absent, empty, or a
    meaningless ``application/octet-stream`` (so validation can sniff instead)."""
    if raw is None:
        return None
    media_type = raw.split(";", 1)[0].strip().lower()
    if not media_type or media_type in _UNKNOWN_CONTENT_TYPES:
        return None
    return _CONTENT_TYPE_ALIASES.get(media_type, media_type)


def sanitize_filename(name: str, *, fallback: str = "download") -> str:
    """Reduce an untrusted filename to a safe basename (PRD §41).

    Strips directory components (both separators, percent-decoded first so
    ``..%2f`` traversal cannot survive), removes reserved/control characters,
    drops leading dots, guards Windows reserved device names, and bounds
    length. Empty results become ``fallback``.
    """
    decoded = unquote(name)
    base = decoded.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(
        "_" if char in _UNSAFE_CHARS else char for char in base
    ).strip()
    cleaned = cleaned.lstrip(".").strip()
    if not cleaned:
        return fallback
    stem, extension = os.path.splitext(cleaned)
    if not stem:
        return fallback
    if len(extension) > _MAX_EXT_LENGTH:
        stem, extension = cleaned, ""
    if stem.upper() in _WINDOWS_RESERVED:
        stem = f"_{stem}"
    if len(stem) > _MAX_STEM_LENGTH:
        stem = stem[:_MAX_STEM_LENGTH]
    return stem + extension


def _sniff_video_type(head: bytes) -> str | None:
    """Basic container magic: MP4 (``ftyp`` box) or WebM/Matroska (EBML)."""
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "video/mp4"
    if head[:4] == _WEBM_MAGIC:
        return "video/webm"
    return None


def _validate_media(path: Path, declared_content_type: str | None) -> tuple[MediaKind, str]:
    """Validate downloaded bytes; return ``(kind, content_type)`` or raise :class:`_ContentError`.

    Images are sniffed with Pillow (magic bytes + ``verify()``); MP4/WebM use
    header magic. A declared content type must match what the bytes say.
    """
    declared = _normalize_content_type(declared_content_type)
    if declared is not None and declared not in ACCEPTED_CONTENT_TYPES:
        raise _ContentError(f"unsupported content type: {declared}")
    with open(path, "rb") as handle:
        head = handle.read(16)
    sniffed_video = _sniff_video_type(head)
    if sniffed_video is not None:
        if declared is not None and declared != sniffed_video:
            raise _ContentError(f"content type mismatch: declared {declared}, content is {sniffed_video}")
        return "video", sniffed_video
    try:
        with Image.open(path) as image:
            image_format = image.format
            image.verify()
    except (OSError, SyntaxError, ValueError) as exc:
        raise _ContentError(f"content is not a valid supported image ({type(exc).__name__})") from exc
    content_type = _CONTENT_TYPE_BY_FORMAT.get(image_format or "")
    if content_type is None:
        raise _ContentError(f"unsupported image format: {image_format}")
    if declared is not None and declared != content_type:
        raise _ContentError(f"content type mismatch: declared {declared}, content is {content_type}")
    kind: MediaKind = "gif" if image_format == "GIF" else "image"
    return kind, content_type


@dataclass(frozen=True)
class _Transfer:
    """Outcome of streaming one attempt to a temp file (``temp_path`` set on success)."""

    temp_path: Path | None
    header_content_type: str | None
    size: int
    error: str | None
    transient: bool
    retry_after: float | None


class Downloader:
    """Async streaming downloader with validation, limits, retries, and atomic writes."""

    def __init__(
        self,
        *,
        destination_dir: Path,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        url_already_downloaded: Callable[[str], bool] | None = None,
    ) -> None:
        self._destination_dir = Path(destination_dir)
        self._settings = settings
        self._client = client
        self._owns_client = client is None
        self._url_already_downloaded = url_already_downloaded

    async def download(self, media: MediaRef, *, target_name: str | None = None) -> DownloadResult:
        """Download ``media`` into the destination directory; never raises for a failed download."""
        try:
            return await self._run(media, target_name=target_name)
        except Exception as exc:  # last-resort guard: a job must survive any single file
            logger.exception("download failed unexpectedly url=%s", media.url)
            return DownloadResult(url=media.url, status=DownloadStatus.FAILED,
                                  error=f"unexpected error: {type(exc).__name__}: {exc}")

    async def aclose(self) -> None:
        """Close the HTTP client only when this downloader created it."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    # -- internals ---------------------------------------------------------

    async def _run(self, media: MediaRef, *, target_name: str | None) -> DownloadResult:
        url = media.url
        if self._url_already_downloaded is not None and self._url_already_downloaded(url):
            logger.info("level-1 duplicate url skipped url=%s", url)
            return DownloadResult(url=url, status=DownloadStatus.DUPLICATE_URL)

        self._destination_dir.mkdir(parents=True, exist_ok=True)
        max_bytes = max(1, self._settings.crawler.max_file_size_mb) * 1024 * 1024
        attempts = max(1, self._settings.crawler.retry_attempts)
        base_delay = self._settings.crawler.delay_seconds
        reason = "unknown"
        retry_after: float | None = None
        for attempt in range(attempts):
            if attempt:
                wait = clamp_retry_after(retry_after, backoff_delay(base_delay, attempt - 1))
                logger.info("download backoff url=%s wait=%.1fs attempt=%d/%d",
                            url, wait, attempt + 1, attempts)
                await asyncio.sleep(wait)
            transfer = await self._fetch_to_temp(url, max_bytes)
            if transfer.error is not None and transfer.temp_path is None:
                if not transfer.transient:
                    logger.warning("download failed url=%s reason=%s", url, transfer.error)
                    return DownloadResult(url=url, status=DownloadStatus.FAILED, error=transfer.error)
                reason = transfer.error
                retry_after = transfer.retry_after
                logger.warning("transient download failure url=%s attempt=%d/%d reason=%s",
                               url, attempt + 1, attempts, reason)
                continue
            return await self._finalize(media, transfer, target_name=target_name)
        error = f"failed after {attempts} attempts: {reason}"
        logger.warning("download gave up url=%s attempts=%d reason=%s", url, attempts, reason)
        return DownloadResult(url=url, status=DownloadStatus.FAILED, error=error)

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._settings.crawler.request_timeout_seconds,
                follow_redirects=False,  # _fetch_to_temp follows hops itself, guarding each
            )
        return self._client

    async def _fetch_to_temp(self, url: str, max_bytes: int) -> _Transfer:
        """Stream ``url`` into a ``.part`` file beside the final path; cleans up on any failure.

        Redirects are followed manually, one guarded hop at a time; a blocked
        target is a non-transient failure (retrying cannot make it safe).
        """
        client = self._ensure_client()
        temp_path = self._destination_dir / f".download-{uuid.uuid4().hex}.part"
        completed = False
        try:
            current = url
            for _hop in range(MAX_REDIRECTS + 1):
                await ensure_safe_url(current)
                async with client.stream("GET", current, follow_redirects=False) as response:
                    status = response.status_code
                    redirect_to = (
                        response.headers.get("location") if status in REDIRECT_STATUSES else None
                    )
                    if redirect_to:
                        current = str(httpx.URL(current).join(redirect_to))
                        continue
                    header_content_type = response.headers.get("content-type")
                    if status == 429 or status >= 500:
                        return _Transfer(None, header_content_type, 0, f"HTTP {status}", True,
                                         parse_retry_after(response.headers.get("retry-after")))
                    if status >= 400:
                        return _Transfer(None, header_content_type, 0, f"HTTP {status}", False, None)
                    declared = _normalize_content_type(header_content_type)
                    if declared is not None and declared not in ACCEPTED_CONTENT_TYPES:
                        return _Transfer(None, header_content_type, 0,
                                         f"unsupported content type: {declared}", False, None)
                    content_length = _parse_content_length(response.headers.get("content-length"))
                    if content_length is not None and content_length > max_bytes:
                        return _Transfer(None, header_content_type, 0,
                                         f"file exceeds size limit ({content_length} > {max_bytes} bytes)",
                                         False, None)
                    written = 0
                    with open(temp_path, "wb") as handle:
                        async for chunk in response.aiter_bytes():
                            written += len(chunk)
                            if written > max_bytes:
                                return _Transfer(None, header_content_type, written,
                                                 f"file exceeds size limit ({max_bytes} bytes)", False, None)
                            handle.write(chunk)
                    completed = True
                    return _Transfer(temp_path, header_content_type, written, None, False, None)
            reject_url(url, "redirect limit exceeded")
        except UnsafeURLError as exc:
            return _Transfer(None, None, 0, f"unsafe url: {exc}", False, None)
        except httpx.HTTPError as exc:
            return _Transfer(None, None, 0, f"{type(exc).__name__}: {exc}", True, None)
        except OSError as exc:
            return _Transfer(None, None, 0, f"local file error: {type(exc).__name__}: {exc}", False, None)
        finally:
            if not completed:
                temp_path.unlink(missing_ok=True)

    async def _finalize(
        self,
        media: MediaRef,
        transfer: _Transfer,
        *,
        target_name: str | None,
    ) -> DownloadResult:
        """Validate → hash → atomically move the temp file to its final name (PRD §11)."""
        assert transfer.temp_path is not None
        temp_path = transfer.temp_path
        try:
            try:
                kind, content_type = _validate_media(temp_path, transfer.header_content_type)
            except _ContentError as exc:
                logger.warning("download rejected url=%s reason=%s", media.url, exc)
                return DownloadResult(url=media.url, status=DownloadStatus.FAILED, error=str(exc))
            digest = sha256_file(temp_path)
            filename = self._final_filename(media, target_name, content_type)
            final_path = self._safe_destination(filename)
            os.replace(temp_path, final_path)
        finally:
            temp_path.unlink(missing_ok=True)
        logger.info("downloaded url=%s path=%s size=%d kind=%s",
                    media.url, final_path, transfer.size, kind)
        return DownloadResult(
            url=media.url,
            status=DownloadStatus.OK,
            path=final_path,
            sha256=digest,
            size=transfer.size,
            content_type=content_type,
            kind=kind,
        )

    def _final_filename(self, media: MediaRef, target_name: str | None, content_type: str) -> str:
        """Resolve the final name: explicit target → original filename → URL basename (all sanitized)."""
        if target_name is not None:
            filename = sanitize_filename(target_name)
        elif media.original_filename:
            filename = sanitize_filename(media.original_filename)
        else:
            url_basename = unquote(urlparse(media.url).path.rsplit("/", 1)[-1])
            filename = sanitize_filename(url_basename)
        if not Path(filename).suffix:
            filename += _EXTENSION_BY_CONTENT_TYPE.get(content_type, "")
        return filename

    def _safe_destination(self, filename: str) -> Path:
        """Return ``destination_dir/filename``, refusing anything that escapes it (PRD §41)."""
        target = self._destination_dir / filename
        if target.resolve().parent != self._destination_dir.resolve():
            raise ValueError(f"resolved filename escapes destination directory: {filename!r}")
        return target


def _parse_content_length(raw: str | None) -> int | None:
    """Parse ``Content-Length``; malformed or negative values are ignored."""
    if raw is None:
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    return value if value >= 0 else None
