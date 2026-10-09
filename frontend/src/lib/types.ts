/**
 * Types mirroring the FastAPI/pydantic models served by the backend.
 *
 * Source of truth: `backend/api/routes_media.py`, `routes_scraper.py`,
 * `routes_jobs.py`, `schemas.py`. The `/api/search` and `/api/settings`
 * shapes follow the documented contracts for their (parallel) builds.
 */

// ---------------------------------------------------------------------------
// Media (routes_media.py)
// ---------------------------------------------------------------------------

/** PRD §19 processing states. */
export type ProcessingStatus =
  | "DOWNLOADED"
  | "ANALYZING"
  | "ANALYZED"
  | "EMBEDDING"
  | "READY"
  | "FAILED";

/** PRD §12.1 duplicate flags. */
export type DupStatus = "dup" | "nondup" | "unflagged";

/** `media.type` values the collector accepts (PRD §5.3). */
export type MediaKind = "image" | "gif" | "video";

/** `/api/media/random` scopes (PRD §30). */
export type RandomScope = "everything" | "favorites" | "gifs";

/** One provenance row (PRD §31). */
export interface MediaSource {
  id: number;
  media_id: number;
  site: string;
  page_url: string;
  chapter: string | null;
  page_number: number | null;
  comment_id: string | null;
  media_url: string;
  author_name: string | null;
  collected_at: string;
}

/** AI analysis record (PRD §15). */
export interface AiMetadata {
  description: string | null;
  tags: string[];
  emotions: string[];
  subjects: string[];
  meme_context: string | null;
  suggested_search_phrases: string[];
  ai_provider: string | null;
  model: string | null;
  processed_at: string | null;
}

/** One gallery card (`MediaOut`). Storage paths are never exposed. */
export interface MediaItem {
  id: number;
  title: string | null;
  original_filename: string | null;
  mime_type: string | null;
  extension: string | null;
  file_size: number | null;
  width: number | null;
  height: number | null;
  duration: number | null;
  site: string | null;
  processing_status: string;
  dup_status: string;
  dup_flagged_at: string | null;
  dup_expires_at: string | null;
  is_favorite: boolean;
  created_at: string;
}

/** Detail payload (`MediaDetailOut`, PRD §26/§31). */
export interface MediaDetail extends MediaItem {
  sha256: string | null;
  phash: string | null;
  dup_of_media_id: number | null;
  user_description: string | null;
  user_tags: string[];
  sources: MediaSource[];
  ai_metadata: AiMetadata | null;
}

/** PRD §19 header breakdown. `status_counts` is added by the backend in parallel. */
export interface StatusCounts {
  ready: number;
  processing: number;
  failed: number;
}

/** Paginated gallery page (`MediaListResponse`, PRD §19/§50). */
export interface MediaListResponse {
  items: MediaItem[];
  page: number;
  page_size: number;
  total: number;
  /** Counts keyed by `dup` / `nondup` / `unflagged`. */
  dup_counts: Record<string, number>;
  status_counts?: StatusCounts;
}

/** PATCH body (`MediaUpdateRequest`) — only fields present are applied (PRD §27). */
export interface MediaUpdate {
  title?: string;
  user_description?: string;
  user_tags?: string[];
  is_favorite?: boolean;
}

export interface MediaDeleteResponse {
  id: number;
  deleted: boolean;
}

/** Query params accepted by `GET /api/media` (type alias for index-signature use). */
export type MediaQueryParams = {
  type?: string;
  site?: string;
  format?: string;
  chapter?: string;
  date_from?: string;
  date_to?: string;
  processing_status?: string;
  dup_status?: string;
  is_favorite?: string;
  emotion?: string;
  page?: number;
  page_size?: number;
};

// ---------------------------------------------------------------------------
// Scraper (routes_scraper.py)
// ---------------------------------------------------------------------------

/** PRD §5.1 scope selection. */
export type ScrapeScope =
  | "current_page"
  | "current_chapter"
  | "multiple_chapters"
  | "entire_comic"
  | "custom_urls";

export interface ScrapeRequest {
  url: string;
  scope: ScrapeScope;
  force_rescan: boolean;
  urls: string[];
}

export interface ScrapeStarted {
  job_id: number;
  status: string;
}

// ---------------------------------------------------------------------------
// Jobs (schemas.py — JobOut)
// ---------------------------------------------------------------------------

export type JobStatus = "running" | "completed" | "cancelled" | "failed";
export type JobAction = "pause" | "resume" | "cancel";

export interface Job {
  id: number;
  job_type: string;
  status: string;
  progress: number;
  message: string | null;
  error: string | null;
  params: Record<string, unknown> | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export interface JobListResponse {
  items: Job[];
}

export interface JobActionResponse {
  job: Job;
  applied: boolean;
}

// ---------------------------------------------------------------------------
// Background AI status (routes_ai.py — GET /api/ai/status)
// ---------------------------------------------------------------------------

/** Supervisor loop states behind the live AI card (PRD §18). */
export type AiStatusState = "processing" | "on_hold" | "idle" | "unavailable" | "stopped";

/** Counters of the newest `ai_analysis` job (`null` before any run). */
export interface AiJobStatus {
  id: number;
  done: number;
  total: number;
  ready: number;
  failed: number;
  deferred: number;
}

/** Live background-analysis state (PRD §35) — never carries the API key. */
export interface AiStatus {
  state: AiStatusState;
  /** Populated only in the `unavailable` state (why the provider cannot be built). */
  reason: string | null;
  provider: string;
  model: string;
  embedding_model: string;
  /** Whether an API key is configured — the key itself is never sent (PRD §41). */
  key_present: boolean;
  /** Seconds until deferred items are retried (live countdown in `on_hold`). */
  retry_in_seconds: number | null;
  next_retry_at: string | null;
  last_error: string | null;
  job: AiJobStatus | null;
  updated_at: string;
}

// ---------------------------------------------------------------------------
// Search (GET /api/search — documented contract)
// ---------------------------------------------------------------------------

export type SearchMode = "hybrid" | "keyword_only" | "filters_only";

export interface SearchWeights {
  keyword: number;
  semantic: number;
  tag: number;
  metadata: number;
}

/** Gallery item + ranking payload. Optional fields guard against older payloads. */
export interface SearchItem extends MediaItem {
  description?: string | null;
  tags?: string[];
  score?: number;
}

export interface SearchResponse {
  items: SearchItem[];
  page: number;
  page_size: number;
  total: number;
  mode: SearchMode;
  weights: SearchWeights;
}

/** Query params accepted by `GET /api/search`. */
export type SearchQueryParams = MediaQueryParams & { q?: string };

// ---------------------------------------------------------------------------
// Settings (GET/PATCH /api/settings — documented contract)
// ---------------------------------------------------------------------------

export interface ServerSettings {
  host: string;
  port: number;
}

export interface StorageSettings {
  media_directory: string;
  thumbnail_directory: string;
  preview_directory: string;
  database_path: string;
}

export interface CrawlerSettings {
  delay_seconds: number;
  concurrency: number;
  max_pages: number;
  download_limit: number;
  max_file_size_mb: number;
  headless?: boolean;
  debug?: boolean;
  retry_attempts?: number;
  request_timeout_seconds?: number;
}

export interface AiSettings {
  provider: string;
  model: string;
  embedding_model: string;
  /** Whether an API key is configured — the key itself is never sent. */
  key_present: boolean;
  /** True when analysis leaves this machine (PRD §42). */
  external_provider: boolean;
}

export interface SearchSettings {
  keyword_weight: number;
  semantic_weight: number;
  tag_weight: number;
  metadata_weight: number;
}

export interface SettingsNotices {
  privacy: string;
  copyright: string;
}

export interface SettingsResponse {
  server: ServerSettings;
  storage: StorageSettings;
  ai: AiSettings;
  crawler: CrawlerSettings;
  search: SearchSettings;
  notices: SettingsNotices;
}

/** PATCH body — only the sections present are applied. */
export interface SettingsPatch {
  crawler?: Partial<CrawlerSettings>;
  ai?: Partial<Pick<AiSettings, "provider" | "model" | "embedding_model">>;
  search?: Partial<SearchSettings>;
}

// ---------------------------------------------------------------------------
// Site-wide backfill (routes_backfill.py — GET/POST /api/backfill)
// ---------------------------------------------------------------------------

/** Lifecycle of a site-wide backfill run. */
export type BackfillStatus = "running" | "completed" | "cancelled" | "failed";

/** Per-comic state inside one backfill run. */
export type BackfillItemStatus = "pending" | "running" | "done" | "failed";

/** One backfill run summary (`BackfillOut`). */
export interface BackfillOut {
  id: number;
  site_url: string;
  adapter_site: string;
  status: BackfillStatus;
  total: number;
  done: number;
  failed: number;
  current_url: string | null;
  current_title: string | null;
  message: string | null;
  error: string | null;
  started_at: string;
  completed_at: string | null;
  /** True while the worker process is actively driving this run. */
  live: boolean;
}

/** Run summary plus per-status item counts (`BackfillDetailOut`). */
export interface BackfillDetailOut extends BackfillOut {
  counts: { pending: number; running: number; done: number; failed: number };
}

/** One comic queued in a backfill run (`BackfillItemOut`). */
export interface BackfillItemOut {
  id: number;
  url: string;
  title: string | null;
  status: BackfillItemStatus;
  error: string | null;
  finished_at: string | null;
}

export interface BackfillListResponse {
  items: BackfillOut[];
}

export interface BackfillItemsResponse {
  items: BackfillItemOut[];
  total: number;
}

export interface BackfillActionResponse {
  backfill: BackfillOut;
  applied: boolean;
}
