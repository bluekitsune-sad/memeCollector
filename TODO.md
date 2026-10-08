# TODO.md — Implementation Tracker

Sync with reality: mark complete **only after verification** (tests/build run). Priority of truth: PRD.md > AGENTS.md > this file.

Legend: `[ ]` pending · `[~]` in progress · `[x]` done & verified · `[!]` blocked/needs decision

---

## Milestone 0 — Project foundation

- [x] M0.1 Repository skeleton per AGENTS.md §3 (backend/, frontend/, tests/, config/, data/, logs/, .gitignore, .env.example, requirements.txt, README.md)
- [x] M0.2 Config module (YAML + env override): storage paths, crawler limits, AI settings, search weights
- [x] M0.3 SQLite schema + migration runner: `media`, `source`, `ai_metadata`, `embeddings`, `jobs`, `favorites` (media.is_favorite), `collections`, `crawl_history`, dup-flag columns (`dup_status`, `dup_flagged_at`, `dup_of_media_id`), FTS5 table `media_fts` — standalone table populated by explicit index code in M4.1, not sync triggers (rationale in backend/database/migrations.py docstring)
- [x] M0.4 Test harness: pytest config, fixtures (tmp DB, sample images/GIFs, fixture HTML pages), mock AI provider fixture
- [x] M0.5 Verify: `pytest` runs (39 passed, green), backend imports cleanly

## Milestone 1 — Collector (crawl + download)

- [x] M1.1 `SiteAdapter` base interface + adapter registry + `UnsupportedSiteError` (AGENTS.md §5) — `scraper/adapters/base.py` (5 methods, HTML-text `find_comments`, `CrawlScope`/`ScopeKind`, provenance backfill contract); `adapters/__init__.py` registers site modules from `_SITE_MODULES` (adding a site = one line)
- [x] M1.2 AsuraScans adapter (asurascans.com/comics entry URL → chapter/page discovery → comment extraction → attachment media) — `adapters/asurascans.py` (render=True); comment root `div[id=comment-{digits}]`, `img[alt="Comment media"]` attachments verified against live CommentsSection.js
- [x] M1.3 MangaDex adapter (mangadex.org comment sections) — `adapters/mangadex.py` (render=False); SPA has no comment DOM → maps title/chapter URLs to forums.mangadex.org XenForo threads via statistics API
- [x] M1.4 MangaPark adapter (mangapark.net comment sections) — `adapters/mangapark.py` (render=True); `div[data-name=comment-item]` verified against production bundle
- [x] M1.5 Comix adapter (comix site comment sections) — `adapters/comix.py` → **comix.to** (domain chosen after evaluating comick.io/globalcomix/comixship — see module docstring); `li.cm-item[id=cm-{id}]`
- [x] M1.6 Fixture HTML test pages for each adapter proving: comment attachments collected; panels/logos/avatars/ads ignored — 7 fixtures + 70 tests; each asserts media set EQUALS expected attachments and DISJOINT from named chrome set
- [x] M1.7 Crawler core (async Playwright, headless + `--debug` headed mode, scope = page/chapter/multi/custom URL list, max pages, delay, 429 backoff, crawl_history skip) — `scraper/crawler.py`: `HttpxFetcher`/`PlaywrightFetcher` behind `PageFetcher` (adapter `render` flag picks), delay + concurrency + exponential 429/5xx backoff, crawl_history skip unless `force_rescan` + upsert, pause/cancel controller, progress callback; browser launch guarded by `BrowserNotInstalledError`
- [x] M1.8 Downloader (streaming httpx, redirect follow, content-type sniff + Pillow validation, size limit, retry/backoff, atomic temp→validate→hash→move, filename sanitization, path traversal guard) — `scraper/downloader.py`: never raises (status/`error` result), accepted image types only, temp file in the destination dir removed on every failure path
- [x] M1.9 Video support (MP4/WebM comment attachments: download + store, no thumbnail frame required for MVP) — MP4 (`ftyp`) / WebM (EBML) header checks → `kind="video"`, stored alongside images
- [x] M1.10 Duplicate detection: Level 1 URL skip, Level 2 SHA-256 exact dup — `media/hashing.py` (`sha256_file`, exception-safe `compute_image_meta`); Level 1 via injected `source`-table callback in the crawl job; phash (Level 3) deferred to a later milestone per scope
- [x] M1.11 Crawl job with progress reporting (pages/comments/media/new/dup counters, pause/cancel) — `jobs/crawl_job.py`: jobs row `running→completed|cancelled|failed`, 60/40 crawl/download progress split, `key=value` message counters, cooperative pause/resume/cancel, `download_limit`, counter summary
- [x] M1.12 Verify: pytest suite for crawler/downloader/adapters green — `python -m pytest`: **107 passed** (39 pre-existing + 68 new), zero skips on this machine; all offline (fixture HTML, `httpx.MockTransport`, guarded Playwright launch)

## Milestone 2 — Library (store + browse)

- [x] M2.1 Media persistence layer (rows for media/source, id-based filenames `00000001.jpg`, original filename preserved) — `media/library.py` `ingest_download()` (single transaction; Level-2 dup → returns None, row still flagged)
- [x] M2.2 Thumbnails + previews (Pillow → webp; GIF → first-frame thumbnail; originals never modified) — `media/thumbnails.py` (256/1024 q80, atomic, video poster tile) + `jobs/thumbnail_job.py`
- [x] M2.3 Dup flag lifecycle (PRD §12.1): scan → flag `dup`/`nondup`, first-collected retained, unflag → `unflagged`, 7-day auto-delete job, "pending deletion" indicator — `media/duplicates.py` + `jobs/dup_job.py` (injectable clock, never purge last copy)
- [x] M2.4 FastAPI routes: `/api/media` (list w/ filters incl. **dup status**, pagination), `/api/media/{id}` detail, file serving for media/thumbnail/preview, favorites toggle, delete, edit (description/tags/emotions) — plus `/api/media/random`, unflag-dup, `/api/scrape`, `/api/jobs` (+pause/resume/cancel); migration `002_media_dup_lifecycle.sql` (sha256 UNIQ removed, title added)
- [x] M2.5 Next.js scaffold: app router, typed API client, gallery grid (lazy/paginated), media detail page (source provenance per PRD §31, Open Source button), favorites page — `frontend/src` (47 files): Gallery/MediaCard/FiltersBar/Pagination/StatusStrip, detail with TagEditor/DupBadge/SourceBlock, favorites, add, jobs, search, settings pages; single typed client `lib/api.ts`; Next 16.4
- [x] M2.6 Verify: pytest + `npm run build` (or tsc) green; manual smoke of gallery via dev servers — pytest 359 passed + 1 skip; `tsc --noEmit` 0 errors; `npm run build` exit 0; live E2E: gallery serves the 27 real collected items over the /api proxy

## Milestone 3 — AI pipeline

- [x] M3.1 `VisionProvider` interface (analyze_image, analyze_gif, generate_embedding) + registry — `ai/provider.py` (+error hierarchy, `normalize_analysis`, `create_provider`)
- [x] M3.2 OpenRouter provider (vision chat completion → structured JSON per PRD §15; embeddings endpoint; retries; strict JSON parse with error recording) — `ai/openrouter.py` (OpenRouter exposes OpenAI-compatible /embeddings, per-model; 429/5xx backoff, response_format fallback)
- [x] M3.3 Mock provider (deterministic, used in tests/demo without key) — `ai/mock.py` (384-dim configurable)
- [x] M3.4 GIF frame sampling (first/middle/last for short; interval sampling for long) per PRD §13 — `ai/frames.py` (≤4 → first/mid/last, else even spacing ≤6, malformed → [] + warn)
- [x] M3.5 AI queue worker: statuses `DOWNLOADED→ANALYZING→ANALYZED→EMBEDDING→READY→FAILED`, async, concurrency limit, per-item failure never stops the queue — `ai/queue.py` + `jobs/ai_job.py` (float32 LE embedding BLOB, claim/resume, pause/cancel)
- [x] M3.6 Verify: pytest (AI tests run against mock; OpenRouter tests mocked HTTP) — 68 tests green; AI stage wired into scrape pipeline + `POST /api/media/{id}/reanalyze` (T1 backend wave); live-verified: mock queue processed 27 real items → READY in 2.1s

## Milestone 4 — Search

- [x] M4.1 FTS5 keyword search (description/tags/filename/source; ranking, prefix) — `search/keyword.py` (rebuild_fts_index + index_media + sanitizer: adversarial FTS input never raises; bm25)
- [x] M4.2 Embedding store + semantic search (FAISS or NumPy fallback per AGENTS.md §2) — **faiss-cpu 1.15.1 installs & imports on Python 3.14** → `FaissSemanticIndex` primary; `NumpySemanticIndex` fallback behind same `SemanticIndex` protocol (search/semantic.py docstring documents the swap)
- [x] M4.3 Hybrid ranking with configurable weights (PRD §22) — `search/hybrid.py` async; modes hybrid/keyword_only/filters_only with weight renormalization; cosine→(1+cos)/2, bm25 normalized
- [x] M4.4 `/api/search` + search UI in Next.js (query box, filters: type/site/emotion/format/date/chapter/AI status/**dup status**) — `api/routes_search.py` (exact contract incl. mode+weights envelope, emotion via json1) + frontend SearchView/FiltersBar; live-verified: `q=confused reaction` → 27-hit hybrid result through UI proxy
- [x] M4.5 Verify: pytest search suite (exact tags, partial, semantic, empty query, no results) — 41 tests (search core 18, API 10, index job 3, + filters/adversarial cases)

## Milestone 5 — Jobs, progress, polish

- [x] M5.1 Job queue + `/api/jobs` (status/progress/error, pause/cancel) — crawl, thumbnail, AI, dedup, index rebuild — all 5 job types create jobs rows; scrape pipeline chains crawl → thumbnail → dup_scan → ai_analysis (keyless skip logged, never fails pipeline) → index_rebuild; concurrent-AI guard; pause/resume/cancel wired
- [x] M5.2 Jobs UI panel (live progress) — frontend JobsList/JobCard/JobControls/ProgressBar/ScanCounters, auto-polling
- [x] M5.3 Add Source flow in UI (URL → supported/unsupported feedback → scope selection → start scan + live counters per PRD §5.2) — AddSourceForm + JobProgressView (key=value counter parsing, progress bar, pause/resume/cancel); unsupported site → 400 detail shown verbatim (PRD §0)
- [x] M5.4 Settings page (crawl limits, AI provider/key indicator, storage paths, cloud-AI privacy notice + copyright notice per PRD §43) — `api/routes_settings.py` (GET/PATCH, key_present only — key never exposed, weight-sum 422, YAML round-trip allowlist) + SettingsForm; live-verified GET via UI
- [x] M5.5 Structured logging to `logs/` — `backend/logging_config.py`: console + rotating logs/memevault.log (5MB×3), idempotent, MEME_LOG_LEVEL, PRD §53 format
- [x] M5.6 README: setup, run, config, troubleshooting — backend+frontend run docs, mock/offline mode, API table incl. /api/search, /api/settings, reanalyze, .env.local

## Final validation

- [x] Full `pytest` green — **359 passed, 1 skipped** (chromium-guard skip expected)
- [x] Frontend type check + production build green — `tsc --noEmit` 0 errors; `npm run build` exit 0 (Next 16.4)
- [x] End-to-end smoke: start app → add fixture/demo source → scan → download → dup scan → thumbnails → AI (mock) → search "confused reaction" → open detail → favorite/edit — **run against the LIVE site**: asurascans chapter crawl (52 comments → 29 media → 27 new, 2 dup-skipped, 0 failed) → thumbnails 27/27 → dup scan → mock AI queue (27 → READY, 2.1s) → `GET /api/search?q=confused reaction` returns hits in hybrid mode through the UI proxy; gallery/settings/jobs pages all 200 on the production server (:3100 → :8000)
- [x] TODO.md reconciled; docs match implementation

---

## Change log

- 2026-10-08: **Resilient background AI** — free OpenRouter models as defaults (dots-3-note-preview:free vision, nemotron-3-embed-1b:free embeddings, benchmarked live); per-item retry with exponential backoff (`ai_attempts`/`ai_next_retry_at`, migration 003) so 429/5xx/timeouts/malformed responses defer instead of failing terminally; `AISupervisor` background loop (startup recovery, on_hold/idle/unavailable states, shared queue gate with the scrape pipeline, FTS reindex after runs); `GET /api/ai/status` + live AiStatusCard on the Jobs page; fixed a race in the status-API on_hold test. 410 passed + 1 skip; tsc/build green.
- 2026-10-07: Backend wave (search/settings/AI-wiring) landed after one rate-limited attempt — M4 + M5.1/5.4/5.5/5.6 complete; faiss-cpu 1.15.1 adopted on Python 3.14. Live collector demo (asurascans ch.1) + mock AI over 27 real items + hybrid search verified end-to-end. Frontend deps reinstalled (node_modules was missing), production build re-verified.
- 2026-10-08: `.env.local` support — loader now reads `.env.local` then `.env` (real env > `.env.local` > `.env` > YAML); created `.env.local` (gitignored) for the OpenRouter key. Verified: full suite green with a key present.
- 2026-10-08: Created tracker from PRD v1.1 (added §0 decisions, §12.1 dup lifecycle, 4 target sites, OpenRouter, Next.js).
- 2026-10-08: M0 verified (39 pytest tests green). Deviation from original M0.3 wording: `media_fts` is a standalone FTS5 table maintained by explicit index code (M4.1) instead of content sync triggers — indexed columns span media/ai_metadata/source, so a JOIN-based reindex pass is simpler; documented in the migrations module docstring. Also: `crawl_history.url` is UNIQUE to support PRD §38 rescan upserts; `favorites` are `media.is_favorite` (no separate table).
- 2026-10-08: M1 core done (M1.1, M1.7–M1.12; 107 pytest tests green). Config additions to `CrawlerSettings` (env-overridable): `retry_attempts` (default 3, page fetch + download attempts) and `request_timeout_seconds` (default 30). Interface decisions: adapters receive HTML text in `find_comments` (crawler backfills `CommentMeta.page_url/chapter/page_number` from the `PageRef`); per-adapter `render: bool` selects Playwright vs httpx fetching; `discover_pages` runs via `asyncio.to_thread`; downloader writes only files (media/source rows remain M2). Site adapters M1.2–M1.6 still pending.
