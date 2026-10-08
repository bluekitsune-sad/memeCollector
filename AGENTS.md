# AGENTS.md — Project Instructions for AI Engineers

This file contains binding instructions for anyone (human or AI) working on this codebase. Read it together with `PRD.md` (requirements) and `TODO.md` (execution tracker).

**Priority of truth:** `PRD.md` > this file > `TODO.md`. If the TODO conflicts with the PRD, the PRD wins and the TODO must be corrected.

---

## 1. Project Overview

Meme Comment Archive ("MemeVault") — a local-first application that collects image/GIF/video media from the comment sections of supported comic sites, deduplicates it, generates thumbnails, analyzes it with AI (descriptions/tags), and makes it searchable via keyword (SQLite FTS5) and semantic (vector embedding) search.

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
│   ├── main.py                # FastAPI app entry
│   ├── api/                   # route modules (media, search, scraper, jobs)
│   ├── database/              # models, connection, migrations
│   ├── scraper/
│   │   ├── crawler.py
│   │   ├── downloader.py
│   │   ├── detector.py
│   │   └── adapters/          # base.py + one module per site
│   ├── media/                 # hashing, thumbnails, gif processing, dup flags
│   ├── ai/                    # provider interface, openrouter, mock, queue
│   ├── search/                # keyword (FTS5), semantic, hybrid
│   └── jobs/                  # background worker/queue
├── frontend/                  # Next.js app
├── data/                      # runtime artifacts (gitignored): media/, thumbnails/, previews/, database.sqlite, browser-profile/
├── logs/                      # structured logs (gitignored)
├── tests/                     # pytest suite
├── config/                    # config files
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

## 8. Testing & Validation

- Tests live in `tests/` and run with `pytest`. Target: every core module has tests (PRD §52 coverage areas: scraper, downloader, media, AI, search).
- The AI provider must have a **mock** so the full test suite runs with no API key and no network.
- Scraper tests use **local fixture HTML** (no live-site hitting in CI/tests).
- Before marking any task complete: run `pytest`, and for frontend changes run the type check/build (`npm run build` or `tsc --noEmit`).
- Never declare a task done because "code was written" — it is done when it is **verified**.

## 9. Security & Compliance (PRD §41–43)

- Bind to `127.0.0.1` only. No public exposure.
- No website passwords stored, ever. Optional persistent Playwright profile for user-driven login.
- Sanitize filenames; block path traversal; enforce max file size; validate content types.
- Respect robots/crawl-rate limits: default delay ≥ 1s between pages, concurrency ≤ 2, handle HTTP 429 with backoff.
- API keys never in the database or logs.

## 10. Git / File Hygiene

- Not a git repo currently; if initialized, commit in small logical units.
- Do not commit `data/`, `logs/`, `.env`, `node_modules/`, `__pycache__/`.
- Update `README.md` when startup/usage changes; keep `TODO.md` synchronized with reality.
