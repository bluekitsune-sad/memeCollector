"""WatchSupervisor tests — interval loop, event-based waits, lifespan wiring (PRD §39).

The loop mirrors the AI supervisor's structure (backend/ai/supervisor.py):
an immediate first pass, event-based interval waits that ``nudge()``/``stop()``
cut short, and a lifespan that cancels + awaits the task (backend/main.py).
Everything offline: watched comics point at a listing with no chapters, so a
pass discovers zero pages and never fetches anything.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.jobs.watch import WatchSupervisor
from backend.jobs.watched_comics import ensure_watched_comic
from backend.main import create_app
from tests.watch_support import (
    EMPTY_LISTING_HTML,
    ENTRY_URL,
    listing_adapter,
    registered,
    state_app,
    watch_settings,
)


async def await_until(
    predicate: Callable[[], bool], *, timeout: float = 5.0, interval: float = 0.01
) -> None:
    """Poll ``predicate`` on the test loop; fail the test on timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    pytest.fail("condition not reached within timeout")


def crawl_count(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT COUNT(*) FROM jobs WHERE job_type = 'crawl'").fetchone()
    return int(row[0])


# ---------------------------------------------------------------------------
# The loop itself
# ---------------------------------------------------------------------------


async def test_supervisor_runs_its_first_pass_immediately(
    db: sqlite3.Connection, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="backend.jobs.watch")
    settings = watch_settings(tmp_path)  # default interval: nothing waits on it
    app = state_app(db, settings)
    with registered(listing_adapter(EMPTY_LISTING_HTML)):
        ensure_watched_comic(db, ENTRY_URL, "fixture")
        supervisor = WatchSupervisor(app)
        task = asyncio.create_task(supervisor.run())
        try:
            await await_until(lambda: crawl_count(db) >= 1)
            await await_until(lambda: supervisor.state == "idle")
        finally:
            await supervisor.stop()
            await asyncio.wait_for(task, timeout=3)

        assert supervisor.state == "stopped"
        assert task.done() and task.exception() is None
        assert crawl_count(db) >= 1, "the first pass ran without waiting an interval"
        assert "watch supervisor state=" in caplog.text


async def test_nudge_short_circuits_the_interval_wait(db: sqlite3.Connection, tmp_path: Path) -> None:
    app = state_app(db, watch_settings(tmp_path, interval_minutes=60.0))
    with registered(listing_adapter(EMPTY_LISTING_HTML)):
        ensure_watched_comic(db, ENTRY_URL, "fixture")
        supervisor = WatchSupervisor(app)
        task = asyncio.create_task(supervisor.run())
        try:
            await await_until(lambda: crawl_count(db) >= 1)
            await await_until(lambda: supervisor.state == "idle")
            # The loop is now inside a 60-minute wait — only a nudge can wake it.
            supervisor.nudge()
            await await_until(lambda: crawl_count(db) >= 2, timeout=3.0)
        finally:
            await supervisor.stop()
            await asyncio.wait_for(task, timeout=3)
        assert supervisor.state == "stopped"


async def test_wake_event_channel_wakes_the_supervisor(db: sqlite3.Connection, tmp_path: Path) -> None:
    """``app.state.watch_nudge.set()`` (the exposed wake event) must wake the loop."""
    app = state_app(db, watch_settings(tmp_path, interval_minutes=60.0))
    with registered(listing_adapter(EMPTY_LISTING_HTML)):
        ensure_watched_comic(db, ENTRY_URL, "fixture")
        supervisor = WatchSupervisor(app)
        task = asyncio.create_task(supervisor.run())
        try:
            await await_until(lambda: crawl_count(db) >= 1)
            await await_until(lambda: supervisor.state == "idle")
            supervisor.wake_event.set()
            await await_until(lambda: crawl_count(db) >= 2, timeout=3.0)
        finally:
            await supervisor.stop()
            await asyncio.wait_for(task, timeout=3)
        assert supervisor.wake_event is not None


async def test_stop_interrupts_a_long_interval_wait_promptly(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    app = state_app(db, watch_settings(tmp_path, interval_minutes=60.0))
    supervisor = WatchSupervisor(app)  # no comics: the pass itself is a fast no-op
    task = asyncio.create_task(supervisor.run())
    await await_until(lambda: supervisor.state == "idle")

    started = time.monotonic()
    await supervisor.stop()
    await asyncio.wait_for(task, timeout=3)

    assert time.monotonic() - started < 2.0, "shutdown must not wait out the interval"
    assert task.done() and task.exception() is None
    assert supervisor.state == "stopped"


async def test_disabled_supervisor_never_starts_a_pass(db: sqlite3.Connection, tmp_path: Path) -> None:
    app = state_app(db, watch_settings(tmp_path, enabled=False))
    supervisor = WatchSupervisor(app)

    await asyncio.wait_for(supervisor.run(), timeout=3)

    assert supervisor.state == "disabled"
    assert crawl_count(db) == 0


# ---------------------------------------------------------------------------
# Lifespan wiring (backend/main.py)
# ---------------------------------------------------------------------------


def test_lifespan_spawns_and_awaits_the_watch_task(tmp_path: Path) -> None:
    settings = watch_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        supervisor = client.app.state.watch_supervisor
        task = client.app.state.watch_task
        assert isinstance(supervisor, WatchSupervisor)
        assert client.app.state.watch_nudge is supervisor.wake_event
        assert not task.done(), "the passive watcher keeps running while the app lives"

    assert task.done(), "lifespan shutdown cancels + awaits the task (like the AI supervisor)"
    assert supervisor.state == "stopped"
