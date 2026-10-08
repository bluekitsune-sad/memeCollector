"""M2.3 duplicate flag lifecycle tests — PRD §12.1 / AGENTS.md §7.

Covers the binding rules: first-collected copy retained, idempotent scan (the
7-day clock never restarts), user ``unflag`` is permanent, the daily purge
removes expired ``dup`` items but never the last copy of a SHA-256, and file
deletion is confined to the configured storage directories.

Everything is offline: items are ingested through the real STORE path and the
purge clock is injected (``now``).
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.config import Settings, load_settings
from backend.jobs.dup_job import run_dup_job
from backend.media.duplicates import (
    DUP_RETENTION_DAYS,
    dup_expires_at,
    purge_expired_dups,
    scan_and_flag,
    unflag,
)
from backend.media.library import ingest_download, remove_media
from tests.test_library import CDN, _comment, _make_image, _result

NOW = datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def dup_settings(tmp_path: Path) -> Settings:
    """Settings with every storage path inside the test's tmp dir."""
    base = load_settings()
    storage = replace(
        base.storage,
        database_path=tmp_path / "dups.sqlite",
        media_directory=tmp_path / "media",
        thumbnail_directory=tmp_path / "thumbnails",
        preview_directory=tmp_path / "previews",
    )
    return replace(base, storage=storage)


def _ingest_unique(db, settings: Settings, tmp_path: Path, name: str) -> int:
    """Store a byte-unique image; returns its media id."""
    shade = (sum(ord(char) for char in name) % 200) + 20
    source = _make_image(tmp_path / "dl" / f"{name}.png", color=(shade, 40, 60))
    media_id = ingest_download(
        db, _result(source, url=f"{CDN}/{name}.png"), _comment(), settings=settings
    )
    assert media_id is not None
    return media_id


def _ingest_copy(db, settings: Settings, tmp_path: Path, name: str, of: int) -> None:
    """Store byte-identical content under a new URL → a Level-2 ``dup`` row."""
    original = settings.storage.media_directory / f"{of:08d}.png"
    duplicate = tmp_path / "dl" / f"{name}.png"
    duplicate.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(original, duplicate)
    assert (
        ingest_download(
            db,
            _result(duplicate, url=f"{CDN}/{name}.png"),
            _comment(comment_id="800001"),
            settings=settings,
        )
        is None
    )


def _age_flag(db, media_id: int, days: int) -> None:
    """Backdate ``dup_flagged_at`` so the 7-day clock has elapsed."""
    moment = NOW - timedelta(days=days)
    db.execute(
        "UPDATE media SET dup_flagged_at = ? WHERE id = ?",
        (moment.strftime("%Y-%m-%d %H:%M:%S"), media_id),
    )
    db.commit()


def _row(db, media_id: int):
    row = db.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
    assert row is not None, f"media {media_id} vanished"
    return row


# ---------------------------------------------------------------------------
# scan_and_flag (rules 1–2)
# ---------------------------------------------------------------------------


def test_scan_flags_later_copy_and_retains_first(db, dup_settings, tmp_path) -> None:
    retained = _ingest_unique(db, dup_settings, tmp_path, "alpha")
    _ingest_copy(db, dup_settings, tmp_path, "alpha-again", of=retained)

    assert scan_and_flag(db, retained) == "nondup"
    assert scan_and_flag(db, 2) == "dup"

    kept, copy = _row(db, retained), _row(db, 2)
    assert kept["dup_status"] == "nondup"
    assert kept["dup_flagged_at"] is None
    assert copy["dup_status"] == "dup"
    assert copy["dup_of_media_id"] == retained
    assert copy["dup_flagged_at"] is not None


def test_scan_is_idempotent_and_never_restarts_the_clock(db, dup_settings, tmp_path) -> None:
    retained = _ingest_unique(db, dup_settings, tmp_path, "beta")
    _ingest_copy(db, dup_settings, tmp_path, "beta-again", of=retained)
    _age_flag(db, 2, days=3)
    before = _row(db, 2)["dup_flagged_at"]

    for _ in range(3):
        assert scan_and_flag(db, 2) == "dup"
    assert _row(db, 2)["dup_flagged_at"] == before, "the 7-day clock must not restart"
    assert scan_and_flag(db, retained) == "nondup"


def test_scan_never_reflags_a_user_unflagged_item(db, dup_settings, tmp_path) -> None:
    retained = _ingest_unique(db, dup_settings, tmp_path, "gamma")
    _ingest_copy(db, dup_settings, tmp_path, "gamma-again", of=retained)
    assert unflag(db, 2) is True

    assert scan_and_flag(db, 2) == "unflagged"
    assert _row(db, 2)["dup_status"] == "unflagged"


def test_scan_repairs_flag_when_retained_copy_is_deleted(db, dup_settings, tmp_path) -> None:
    retained = _ingest_unique(db, dup_settings, tmp_path, "delta")
    _ingest_copy(db, dup_settings, tmp_path, "delta-again", of=retained)
    assert remove_media(db, retained, settings=dup_settings) is True

    assert scan_and_flag(db, 2) == "nondup", "no shared copy remains → repair (rule 2)"
    row = _row(db, 2)
    assert row["dup_status"] == "nondup"
    assert row["dup_flagged_at"] is None
    assert row["dup_of_media_id"] is None


def test_scan_unknown_id_raises(db) -> None:
    with pytest.raises(ValueError, match="no media row"):
        scan_and_flag(db, 4242)


# ---------------------------------------------------------------------------
# unflag (rule 3)
# ---------------------------------------------------------------------------


def test_unflag_moves_dup_to_unflagged_and_keeps_provenance(
    db, dup_settings, tmp_path
) -> None:
    retained = _ingest_unique(db, dup_settings, tmp_path, "epsilon")
    _ingest_copy(db, dup_settings, tmp_path, "epsilon-again", of=retained)
    _age_flag(db, 2, days=DUP_RETENTION_DAYS + 1)
    flagged_at = _row(db, 2)["dup_flagged_at"]

    assert unflag(db, 2) is True
    row = _row(db, 2)
    assert row["dup_status"] == "unflagged"
    assert row["dup_flagged_at"] == flagged_at, "kept as provenance for the UI"
    assert row["dup_of_media_id"] == retained

    assert unflag(db, 2) is False, "already unflagged → idempotent"


def test_unflag_refuses_nondup_and_unknown_rows(db, dup_settings, tmp_path) -> None:
    plain = _ingest_unique(db, dup_settings, tmp_path, "zeta")
    assert unflag(db, plain) is False
    with pytest.raises(ValueError, match="no media row"):
        unflag(db, 999)


# ---------------------------------------------------------------------------
# purge_expired_dups (rules 4–5)
# ---------------------------------------------------------------------------


def test_purge_deletes_expired_dup_rows_fts_and_files(
    db, dup_settings, tmp_path
) -> None:
    retained = _ingest_unique(db, dup_settings, tmp_path, "eta")
    _ingest_copy(db, dup_settings, tmp_path, "eta-again", of=retained)
    _age_flag(db, 2, days=DUP_RETENTION_DAYS + 1)
    # A sibling item flagged only yesterday must survive this run.
    theta = _ingest_unique(db, dup_settings, tmp_path, "theta")
    _ingest_copy(db, dup_settings, tmp_path, "theta-again", of=theta)
    _age_flag(db, 4, days=1)
    # A keyword-index row (standalone FTS, rowid == media.id) must go too.
    db.execute(
        "INSERT INTO media_fts (rowid, media_id, description, tags, source_text) "
        "VALUES (2, 2, 'expired meme', 'cat', 'hehe')"
    )
    db.commit()

    purged = purge_expired_dups(db, dup_settings, NOW)

    assert purged == [2]
    assert db.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 3
    assert db.execute("SELECT COUNT(*) FROM media WHERE id = 2").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM source WHERE media_id = 2").fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM media_fts WHERE rowid = 2"
    ).fetchone()[0] == 0, "purge must clear the keyword index (PRD §12.1)"
    assert not (dup_settings.storage.media_directory / "00000002.png").exists()
    assert _row(db, retained)["dup_status"] == "nondup"
    assert _row(db, 4)["dup_status"] == "dup", "recently flagged copies wait their turn"
    assert (dup_settings.storage.media_directory / "00000004.png").exists()


def test_purge_never_deletes_the_last_copy_of_a_sha(db, dup_settings, tmp_path) -> None:
    """The retained copy was deleted by hand: the expired dup becomes the last copy."""
    retained = _ingest_unique(db, dup_settings, tmp_path, "iota")
    _ingest_copy(db, dup_settings, tmp_path, "iota-again", of=retained)
    _age_flag(db, 2, days=DUP_RETENTION_DAYS + 1)
    assert remove_media(db, retained, settings=dup_settings) is True

    assert purge_expired_dups(db, dup_settings, NOW) == []
    assert db.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 1
    assert (dup_settings.storage.media_directory / "00000002.png").exists()


def test_purge_keeps_earliest_when_a_whole_group_expired(
    db, dup_settings, tmp_path
) -> None:
    """Two expired copies of the same sha256 → the earliest one is retained."""
    retained = _ingest_unique(db, dup_settings, tmp_path, "kappa")
    _ingest_copy(db, dup_settings, tmp_path, "kappa-b", of=retained)
    _ingest_copy(db, dup_settings, tmp_path, "kappa-c", of=retained)
    _age_flag(db, 2, days=DUP_RETENTION_DAYS + 2)
    _age_flag(db, 3, days=DUP_RETENTION_DAYS + 1)
    sha256 = _row(db, 2)["sha256"]
    # Simulate the retained copy having been removed: only the two dups remain.
    assert remove_media(db, retained, settings=dup_settings) is True

    purged = purge_expired_dups(db, dup_settings, NOW)

    assert purged == [3], "the earliest expired copy (id 2) is kept as the last copy"
    assert _row(db, 2)["sha256"] == sha256
    assert (dup_settings.storage.media_directory / "00000002.png").exists()
    assert not (dup_settings.storage.media_directory / "00000003.png").exists()


def test_purge_skips_paths_outside_the_storage_directories(
    db, dup_settings, tmp_path
) -> None:
    """A corrupted row must never turn the purge into an arbitrary-file delete (§41)."""
    retained = _ingest_unique(db, dup_settings, tmp_path, "lambda")
    _ingest_copy(db, dup_settings, tmp_path, "lambda-again", of=retained)
    _age_flag(db, 2, days=DUP_RETENTION_DAYS + 1)
    outside = tmp_path / "outside-target.txt"
    outside.write_text("must survive", encoding="utf-8")
    db.execute("UPDATE media SET file_path = ? WHERE id = 2", (str(outside),))
    db.commit()

    assert purge_expired_dups(db, dup_settings, NOW) == [2]
    assert db.execute("SELECT COUNT(*) FROM media WHERE id = 2").fetchone()[0] == 0
    assert outside.exists(), "refusing to unlink a path outside storage"


def test_purge_ignores_unflagged_and_nondup_items(db, dup_settings, tmp_path) -> None:
    retained = _ingest_unique(db, dup_settings, tmp_path, "mu")
    _ingest_copy(db, dup_settings, tmp_path, "mu-again", of=retained)
    _age_flag(db, 2, days=DUP_RETENTION_DAYS + 5)
    assert unflag(db, 2) is True
    plain = _ingest_unique(db, dup_settings, tmp_path, "nu")

    assert purge_expired_dups(db, dup_settings, NOW) == []
    assert _row(db, 2)["dup_status"] == "unflagged"
    assert (dup_settings.storage.media_directory / "00000002.png").exists()
    assert _row(db, plain)["dup_status"] == "nondup"
    assert _row(db, retained)["dup_status"] == "nondup"


def test_purge_with_nothing_expired_is_a_no_op(db, dup_settings, tmp_path) -> None:
    retained = _ingest_unique(db, dup_settings, tmp_path, "xi")
    _ingest_copy(db, dup_settings, tmp_path, "xi-again", of=retained)

    assert purge_expired_dups(db, dup_settings, NOW) == []
    assert db.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 2


# ---------------------------------------------------------------------------
# dup_expires_at
# ---------------------------------------------------------------------------


def test_dup_expires_at_adds_retention_days() -> None:
    assert dup_expires_at("2026-03-01 00:00:00") == "2026-03-08 00:00:00"
    assert dup_expires_at(None) is None
    assert dup_expires_at("not-a-date") is None


# ---------------------------------------------------------------------------
# run_dup_job (scan + purge under one jobs row)
# ---------------------------------------------------------------------------


async def test_run_dup_job_scans_flags_and_purges(
    db, dup_settings, tmp_path
) -> None:
    retained = _ingest_unique(db, dup_settings, tmp_path, "omicron")
    _ingest_copy(db, dup_settings, tmp_path, "omicron-old", of=retained)
    _age_flag(db, 2, days=DUP_RETENTION_DAYS + 1)
    pi = _ingest_unique(db, dup_settings, tmp_path, "pi")
    _ingest_copy(db, dup_settings, tmp_path, "pi-recent", of=pi)
    # A row inserted outside the ingest path (never scanned), same bytes as rho:
    rho = _ingest_unique(db, dup_settings, tmp_path, "rho")
    db.execute(
        "INSERT INTO media (id, file_path, original_filename, mime_type, extension, "
        "file_size, sha256, processing_status, dup_status) "
        "SELECT 99, file_path || '-copy', original_filename, mime_type, extension, "
        "file_size, sha256, 'DOWNLOADED', 'nondup' FROM media WHERE id = ?",
        (rho,),
    )
    db.commit()

    summary = await run_dup_job(db=db, settings=dup_settings, now=NOW)

    # 6 rows scanned; ids 2 and 4 stay flagged, raw-inserted 99 gets flagged.
    assert summary.scanned == 6
    assert summary.flagged == 3
    assert summary.purged == (2,), "only the 8-day-old flag is past retention"
    assert db.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 5
    assert db.execute("SELECT dup_status FROM media WHERE id = 99").fetchone()[0] == "dup"
    assert not (dup_settings.storage.media_directory / "00000002.png").exists()
    assert (dup_settings.storage.media_directory / "00000004.png").exists()

    job = db.execute("SELECT * FROM jobs WHERE id = ?", (summary.job_id,)).fetchone()
    assert job["job_type"] == "dup_scan"
    assert job["status"] == "completed"
    assert job["progress"] == 1.0
    assert job["message"] == "scanned=6 flagged=3 purged=1"
    assert job["completed_at"] is not None


async def test_run_dup_job_never_reflags_unflagged_items(
    db, dup_settings, tmp_path
) -> None:
    retained = _ingest_unique(db, dup_settings, tmp_path, "sigma")
    _ingest_copy(db, dup_settings, tmp_path, "sigma-again", of=retained)
    assert unflag(db, 2) is True
    _age_flag(db, 2, days=DUP_RETENTION_DAYS * 2)

    summary = await run_dup_job(db=db, settings=dup_settings, now=NOW)

    assert summary.purged == (), "an unflagged item is never auto-deleted (rule 3)"
    assert summary.flagged == 0
    row = _row(db, 2)
    assert row["dup_status"] == "unflagged"
    assert (dup_settings.storage.media_directory / "00000002.png").exists()
    assert db.execute(
        "SELECT status FROM jobs WHERE id = ?", (summary.job_id,)
    ).fetchone()["status"] == "completed"
