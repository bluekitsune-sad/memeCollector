# AGENTS.md — Project Instructions for AI Engineers

This file contains binding instructions for anyone (human or AI) working on this codebase. Read it together with `PRD.md` (requirements) and `TODO.md` (execution tracker).

**Priority of truth:** `PRD.md` > this file > `TODO.md`. If the TODO conflicts with the PRD, the PRD wins and the TODO must be corrected.

---

## 1. Project Overview

Meme Comment Archive ("MemeCollector") — a local-first application that collects image/GIF/video media from the comment sections of supported comic sites, deduplicates it, generates thumbnails, analyzes it with AI (descriptions/tags), and makes it searchable via keyword (SQLite FTS5) and semantic (vector embedding) search.

Architecture principle (PRD §57): **separate COLLECT → STORE → PROCESS → INDEX → SEARCH.** Never merge these stages into one script.

## 2. Tech Stack (decided — do not change without updating the PRD)

| Layer | Choice |
|---|---|
| Backend | Python 3.14, FastAPI, uvicorn |
| Browser automation | Playwright (Chromium, headless default, debug flag for headed) |
| HTML parsing | BeautifulSoup4 |
| Database | SQLite (WAL mode) + FTS5 for keyword search |
| Vector search | FAISS if wheels install on Python 3.14; otherwise a pure-NumPy cosine-similarity fallback behind the same interface (document the swap in TODO.md) |
| Images | Pillow |
| AI | OpenRouter (OpenAI-compatible), behind a `VisionProvider` interface + deterministic mock provider for tests |
| Frontend | Next.js (React + TypeScript), App Router |
| Tests | pytest (backend), vitest or Playwright tests (frontend where practical) |

## 3. Repository Layout

```
meme/
├── backend/
│   ├── main.py                # FastAPI app entry (lifespan: startup auto-resume, graceful shutdown)
│   ├── logging_config.py      # structured console + rotating file logs (secret redaction)
│   ├── api/                   # routes_media, routes_search, routes_scraper, routes_watch,
│   │                          # routes_jobs, routes_backfill, routes_ai, routes_settings (+ schemas.py)
│   ├── config/                # loader.py — typed settings incl. watch: and backfill: sections
│   ├── database/              # database.py, migrations.py, migrations/001–006_*.sql
│   ├── scraper/
│   │   ├── crawler.py
│   │   ├── downloader.py
│   │   ├── backoff.py
│   │   └── adapters/          # base.py + one module per site
│   ├── media/                 # library, hashing, thumbnails, duplicates (dup flags)
│   ├── security/              # hostile-page isolation: URL/text sanitization, guarded fetch, prompt wrapping
│   ├── ai/                    # provider, openrouter, mock, frames, queue, supervisor, runner
│   ├── search/                # keyword (FTS5), semantic, hybrid
│   └── jobs/                  # crawl_job, pipeline, backfill, resume, recovery,
│                              # watch, watched_comics, thumbnail_job, dup_job, index_job, ai_job
├── frontend/                  # Next.js app (App Router: src/app, src/components, src/lib)
├── data/                      # runtime artifacts (gitignored): media/, thumbnails/, previews/, database.sqlite, browser-profile/
├── logs/                      # structured logs (gitignored)
├── tests/                     # pytest suite + watch_support.py, fixtures/ (HTML pages, fake_adapter.py)
├── config/                    # config.yaml (storage, crawler, ai, search, watch, backfill)
├── .env.example
├── requirements.txt
├── README.md
├── PRD.md
├── AGENTS.md                  # this file
└── TODO.md
```

## 4. Coding Conventions

- **Python:** type hints on all public functions; `from __future__ import annotations` where useful; no unbounded `except:` — catch specific exceptions and log with context.
- **Naming:** modules lowercase `snake_case`; classes `PascalCase`; functions/variables `snake_case`; constants `UPPER_SNAKE`.
- **Async:** the crawler/downloader use `async` Playwright + `httpx`; CPU-bound work (hashing, thumbnails) runs in a thread executor. Do not block the event loop.
- **Config:** all tunables live in config (env vars / YAML), never hard-coded. Secrets only in `.env` (gitignored); `.env.example` documents them without values.
- **Logging:** structured logs (`logging` with key=value extras or structlog-style), one logger per module. No `print()` in backend code. No leftover debug statements.
- **Errors:** every failure (download, AI call, parse) is recorded in the DB `jobs`/error fields per PRD §36 and must never abort an entire crawl.
- **Frontend:** TypeScript strict mode; components small and single-purpose; API calls go through one typed API client module; no business logic in components that belongs on the server.
- **No dead code, no unused imports, no TODO comments left in source** — put unfinished work in `TODO.md`.

## 5. Adapter Contract (PRD §7)

Every site adapter implements `SiteAdapter` in `backend/scraper/adapters/base.py`:

- `can_handle(url) -> bool`
- `discover_pages(url, scope) -> list[PageRef]`
- `find_comments(page) -> list[Comment]`
- `find_comment_media(comment) -> list[MediaRef]`
- `get_comment_metadata(comment) -> CommentMeta`
- `discover_series(url) -> list[SeriesRef]` — **optional** (base default returns `[]`); expands a catalog URL (e.g. `asurascans.com/comics`) into series links. Used **only** by the site-wide backfill (`backend/jobs/backfill.py`); currently implemented by `asurascans` alone.

Registry resolves a URL to an adapter; **if none matches, raise `UnsupportedSiteError`** which the API surfaces as a clear user-facing message ("Site not supported: add an adapter for …"). This is required behavior (PRD §0).

MVP adapters: `asurascans`, `mangadex`, `mangapark`, `comix`.

## 6. Media Identification Rule (PRD §6 — critical)

Only download media **attached to comments**. Never download comic panels, logos, navigation icons, ads, or avatars. Every adapter must have tests proving both: comment attachments are collected, page chrome is ignored.

## 7. Duplicate Flag Lifecycle (PRD §12.1 — required)

- After download, every item is scanned and flagged `dup` or `nondup`.
- `dup` items get `dup_flagged_at`; a daily job deletes them 7 days later.
- User can unflag → `unflagged` (never auto-deleted).
- Library UI has a Dup status filter (`dup` / `nondup` / `unflagged`).
- First-collected copy is retained; auto-delete never removes the last copy.
- Counts are **faceted**: `count_by_dup_status` / `count_by_processing_status` in `backend/media/library.py` ignore their own filter, so header badges always show the full breakdown while the gallery is filtered (PRD §19, §12.1).

## 8. Startup Behavior & Job Resilience (PRD §35–36 — required)

Long-running work must survive restarts and never wedge. This is enforced behavior, not optional polish:

- **Crawl auto-resume:** on boot, `backend/jobs/recovery.py` fails zombie `running` rows and returns the interrupted crawls; `backend/main.py`'s lifespan spawns a background task (never blocks startup, never raises) that `backend/jobs/resume.py` relaunches sequentially from each row's params JSON (`url`/`scope`/`force_rescan`/`urls`), annotating the old row "— resumed as crawl job N".
- **Backfill self-resume:** backfill-owned crawls (`params.owner == "backfill"`) are skipped by the generic resume — `backend/jobs/backfill.py:resume_interrupted_backfill` continues the stranded run itself. On shutdown the backfill stops cooperatively and its row stays `running` for the next boot.
- **AI revive (second try):** `backend/ai/queue.py` — a *retryable* failure schedules `ai_next_retry_at = now + ai.revive_after_seconds` while the item has revives left; permanent causes (video, bad API key, unknown model) never schedule. `revive_due_failures` (called every AI-supervisor tick) flips due `FAILED` rows back into the pipeline, capped by `ai.max_item_revives`. Manual Reanalyze resets the whole slate.
- **Stale-job cancel:** cancelling a `running` crawl/backfill row with no live handle marks it `cancelled` once past a short registration grace (`_STALE_GRACE_SECONDS = 5` in `backend/api/routes_jobs.py`); pause/resume stay no-ops there. Freshly-created rows are protected by the grace window.
- Every failure is recorded on the row and never aborts an unrelated job (PRD §36).

## 9. Testing & Validation

- Tests live in `tests/` and run with `pytest`. Target: every core module has tests (PRD §52 coverage areas: scraper, downloader, media, AI, search).
- The AI provider must have a **mock** so the full test suite runs with no API key and no network.
- Scraper tests use **local fixture HTML** (no live-site hitting in CI/tests).
- Before marking any task complete: run `pytest`, and for frontend changes run the type check/build (`npm run build` or `tsc --noEmit`).
- Never declare a task done because "code was written" — it is done when it is **verified**.

## 10. Security & Compliance (PRD §41–43)

- Bind to `127.0.0.1` only. No public exposure.
- No website passwords stored, ever. Optional persistent Playwright profile for user-driven login.
- Sanitize filenames; block path traversal; enforce max file size; validate content types.
- Respect robots/crawl-rate limits: default delay ≥ 1s between pages, concurrency ≤ 2, handle HTTP 429 with backoff.
- API keys never in the database or logs.

## 11. Git / File Hygiene

- Git repo is initialized and pushes to `origin` = `https://github.com/bluekitsune-sad/memeCollector`; commit in small logical units (message = what changed and why, not the milestone name alone).
- Do not commit `data/`, `logs/`, `.env`, `node_modules/`, `__pycache__/`.
- Update `README.md` when startup/usage changes; keep `TODO.md` synchronized with reality.
