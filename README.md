# MemeCollector — Meme Comment Archive

Chief Meme Officer (CMO) or Meme Specialist

A local-first application that collects image/GIF/video media from the comment
sections of supported comic sites, deduplicates it, generates thumbnails,
analyzes it with AI (descriptions/tags), and makes it searchable via keyword
(SQLite FTS5) and semantic (vector embedding) search.

Requirements docs (read in this order): `PRD.md` → `AGENTS.md` → `TODO.md`.

## Features

- **Comment-only collection** — image/GIF/video attachments from the comment sections of four supported sites (AsuraScans, MangaDex, MangaPark, Comix); panels, logos, nav icons and avatars are never downloaded (PRD §6).
- **Site-wide backfill** — the Jobs page has a dedicated **Site Backfill** section (above the AI status card): start a catalog crawl (default `asurascans.com/comics`), then pause / resume / cancel / **retry failed** / **run again**, with a progress bar, live counts, the current comic and an expandable per-comic list (PRD §35–36).
- **Auto-resume after restart** — interrupted crawls and a stranded backfill relaunch themselves on the next boot "when they can" (background task at startup; zombie rows are failed, never wedged — PRD §36).
- **Resilient AI** — per-item retry with backoff, then an automatic **revive** of retryable failures after `ai.revive_after_seconds` (capped by `ai.max_item_revives`); permanent causes (video, bad key) are never retried; manual Reanalyze resets the slate (PRD §18/§36).
- **Duplicate-flag lifecycle** — every item flagged `dup`/`nondup`, `dup` auto-deleted after 7 days unless manually unflagged; gallery filter + header badges, and the badges stay **faceted** (full `dup`/`nondup`/`unflagged` and processing-status breakdown) while the gallery itself is filtered (PRD §12.1, §19).
- **Watch system** — a passive supervisor re-scans comics you've registered on an interval; add/remove/enable/disable via `POST /api/watch` (PRD §35/§39).
- **Hybrid search** — keyword (SQLite FTS5) + semantic (FAISS/NumPy embeddings) + tag/metadata weights, with filters down to dup status (PRD §21–22, §24).
- **Dark-theme-only UI** — a single dark palette (no light theme) with pixel-art flourishes: dot grid, stepped hard shadows, pixel focus ring.
- **Local-first & private** — binds `127.0.0.1` only; API keys live in the environment, never in YAML, the DB, or logs (PRD §41–42).

## Setup

Prerequisites: **Python 3.14**, **Node 24** (frontend).

```powershell
# 1. Virtual environment
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate

# 2. Backend dependencies
pip install -r requirements.txt

# 3. Browser for the crawler (Milestone 1+)
playwright install chromium

# 4. Environment secrets
copy .env.example .env.local     # then add your OPENROUTER_API_KEY (gitignored)
```

No API key? Set `MEME_AI_PROVIDER=mock` (or `ai.provider: mock` in
`config/config.yaml`) — analysis, embeddings and semantic search then run fully
offline with the deterministic mock provider.

## Run

```powershell
# Backend (from the project root; binds 127.0.0.1:8000)
uvicorn backend.main:app --host 127.0.0.1 --port 8000
# equivalent: python -m backend.main

# Frontend (Next.js dev server; it proxies /api → 127.0.0.1:8000)
cd frontend
npm install
npm run dev -- -p 3100   # port 3000 is the `next dev` default; this project runs on 3100
```

The backend binds to `127.0.0.1` only (PRD §41).

## API

All endpoints are JSON, local-only; interactive docs at
`http://127.0.0.1:8000/docs`. `type` = `image|gif|video`,
`dup_status` = `dup|nondup|unflagged`.

| Method & path | Purpose |
|---|---|
| `GET /api/media` | Paginated gallery. Query: `type`, `site`, `format`, `chapter`, `date_from`, `date_to`, `processing_status`, `dup_status`, `is_favorite`, `page`, `page_size` → `{items, page, page_size, total, dup_counts, status_counts}` |
| `GET /api/media/random` | One random item; `scope=everything\|favorites\|gifs` (404 when nothing matches) |
| `GET /api/media/{id}` | Detail: media fields, `sources[]` provenance, `ai_metadata`, dup info |
| `PATCH /api/media/{id}` | Manual overrides: `title`, `user_description`, `user_tags[]`, `is_favorite` |
| `DELETE /api/media/{id}` | Remove rows + files → `{id, deleted: true}` |
| `POST /api/media/{id}/unflag-dup` | `dup` → `unflagged` (409 if not flagged) |
| `POST /api/media/{id}/reanalyze` | Retry AI analysis for one `FAILED`/`READY` item (404 unknown, 400 ineligible, 503 no provider/key) |
| `GET /api/media/{id}/file` | Original bytes (verified MIME type) |
| `GET /api/media/{id}/thumbnail` | 256 px WebP (404 until generated) |
| `GET /api/media/{id}/preview` | 1024 px WebP (404 until generated) |
| `GET /api/search` | Hybrid search. Query: `q` + every gallery filter + `page`, `page_size` → `{items, page, page_size, total, mode, weights}`; each item is a gallery card plus `description`, `tags`, `score` |
| `POST /api/scrape` | Start a crawl → `202 {job_id, status}`; body `{url, scope, force_rescan, urls[]}`; unsupported sites → `400 "Site not supported: …"` |
| `GET /api/scrape/{job_id}` | Crawl job status/progress/message |
| `GET /api/jobs` | Job history, newest first; `status`, `limit` |
| `POST /api/jobs/{id}/pause\|resume\|cancel` | Control a running crawl → `{job, applied}` (no-op-safe, `applied: false`; `cancel` also clears a **stale** `running` row with no live process, past a 5 s registration grace) |
| `POST /api/backfill/start` | Start a site-wide backfill → `202 {id, …}`; body `{url?}` defaults to `https://asurascans.com/comics`; `400` when `backfill.enabled=false` or the site exposes no `discover_series`; `409` when a run is already running or stranded `running` |
| `GET /api/backfill` | Backfill runs, newest first; `limit` → `{items}` |
| `GET /api/backfill/{id}` | One run + per-status comic counts (`counts: pending/running/done/failed`); `404` unknown |
| `GET /api/backfill/{id}/items` | The run's comic list → `{items, total}`; `status` (`pending\|running\|done\|failed`), `limit`, `offset` |
| `POST /api/backfill/{id}/pause` | Hold before the next comic → `{backfill, applied}` |
| `POST /api/backfill/{id}/resume` | Continue a paused run — or relaunch one stranded `running` by a crash → `{backfill, applied}` |
| `POST /api/backfill/{id}/cancel` | Stop the run cooperatively → `{backfill, applied}` (stale-stranded rows are cleared like jobs) |
| `POST /api/backfill/{id}/retry-failed` | Re-queue the run's failed comics; relaunches immediately when no run is live → `{backfill, applied}` |
| `GET /api/watch` | Every watched comic, oldest first → `{items}` |
| `POST /api/watch` | Register a comic `{url, title?}` → `201` (`200` if already watched; `400` unsupported site) |
| `PATCH /api/watch/{id}` | Toggle `{enabled}` for the passive scanner (404 unknown) |
| `DELETE /api/watch/{id}` | Stop watching; media and crawl history stay → `{id, deleted}` |
| `POST /api/watch/{id}/scan` | Run one watch pass for this comic now → `202 {job_id, status}` |
| `GET /api/ai/status` | Live background-AI state → `state` (`processing\|on_hold\|idle\|unavailable\|stopped`), `reason`, `provider`, `model`, `embedding_model`, `key_present` (never the key), `retry_in_seconds`, `next_retry_at`, `last_error`, `job` (`{id, done, total, ready, failed, deferred}`, `null` before any run), `updated_at` |
| `GET /api/settings` | Settings document: `server`, `storage`, `crawler`, `ai` (`key_present` only — never the key), `search` weights, `notices` |
| `PATCH /api/settings` | Save partial `crawler`/`ai`/`search` changes → validated, persisted to `config/config.yaml`, applied immediately (unknown fields/sections → 422) |

One scrape chains five job rows (`crawl` → `thumbnail` → `dup_scan` →
`ai_analysis` → `index_rebuild`), each visible in `GET /api/jobs`. Every stage
is failure-isolated: without an AI provider/key the `ai_analysis` stage is
skipped with a warning and the rest of the pipeline still runs (PRD §36).

All of it resumes itself: on restart, crawls started via `POST /api/scrape`
that were interrupted by a shutdown are relaunched in the background from
their stored params, a stranded backfill continues where it stopped
(`POST /api/backfill/{id}/resume` does the same manually), and
terminally-failed AI items marked *retryable* get a second try after
`ai.revive_after_seconds` — each governed by the rules in AGENTS.md §8
(PRD §36).

`mode` in a search response reports how results were ranked: `hybrid`
(keyword + semantic + tag + metadata), `keyword_only` (no provider/key/
embeddings — the semantic weight is renormalized over the rest) or
`filters_only` (empty `q`, ordered newest first).

## Configuration

Defaults live in `config/config.yaml` (storage paths, crawler limits, AI model
names, hybrid search weights — PRD §22/§40). Notable sections:

| Section | Keys | Meaning |
|---|---|---|
| `backfill` | `enabled`, `delay_seconds` | Site-wide backfill master switch and the pause between comics (on top of `crawler.delay_seconds`). `enabled: false` blocks new runs and the startup resume of an interrupted one |
| `watch` | `enabled`, `interval_minutes`, `max_pages_per_run` | Passive re-scan supervisor cadence and page cap |
| `ai` | `revive_after_seconds` (default 1800), `max_item_revives` (default 3) | Second-try schedule for retryable AI failures, and how many revives each item gets before it stays `FAILED` |

The default AI models are
**free OpenRouter models** (the `:free` tier): vision
`dots-studio/dots-3-note-preview:free`, embeddings
`nvidia/nemotron-3-embed-1b:free` — swap either via `AI_MODEL` /
`EMBEDDING_MODEL` or by editing the YAML. Resolution order:

1. Environment variables — generic `MEME_<SECTION>_<KEY>` (e.g.
   `MEME_CRAWLER_DELAY_SECONDS`, `MEME_AI_PROVIDER`) plus aliases
   `OPENROUTER_API_KEY`, `AI_MODEL`, `EMBEDDING_MODEL`, `MEME_HOST`,
   `MEME_PORT`, `MEME_CONFIG_PATH`, `MEME_LOG_LEVEL`
2. `config/config.yaml` (or the file pointed at by `MEME_CONFIG_PATH`)
3. Built-in defaults in `backend/config/loader.py`

Relative paths resolve against the project root. The AI API key is read from
the environment only — it is never stored in YAML or the database, and the
settings API exposes it as a boolean (`key_present`) alone.

## Logging

Structured `key=value` logs (PRD §53) from every backend module go to the
console and to a rotating `logs/memecollector.log` (2 MiB × 4 files, gitignored).
The level defaults to `INFO`; override it with `MEME_LOG_LEVEL=DEBUG`.

## Tests

```powershell
python -m pytest
```

The suite runs offline with no API key: scraper tests use local fixture HTML,
HTTP is mocked with `httpx.MockTransport`, and AI tests use a deterministic
mock provider. The Playwright browser-launch test is guarded: it asserts the
clean `BrowserNotInstalledError` when Chromium has not been downloaded yet
(run `playwright install chromium` once, before crawling a live site) and
skips only on machines where Chromium is already present.

## Project layout

See AGENTS.md §3. Runtime artifacts (`data/`, `logs/`) and `.env` are
gitignored. Milestone tracking lives in `TODO.md`.




# in short

![itsMine](/hehe.webp)
