/**
 * The single typed API client (AGENTS.md §4 — every fetch goes through here).
 *
 * All paths are same-origin `/api/...`; `next.config.ts` rewrites them to the
 * FastAPI backend on 127.0.0.1:8000. Non-2xx responses are turned into
 * `ApiError` carrying the FastAPI `detail` message.
 */

import type {
  AiStatus,
  BackfillActionResponse,
  BackfillDetailOut,
  BackfillItemsResponse,
  BackfillListResponse,
  BackfillOut,
  Job,
  JobActionResponse,
  JobListResponse,
  MediaDeleteResponse,
  MediaDetail,
  MediaListResponse,
  MediaQueryParams,
  MediaUpdate,
  RandomScope,
  ScrapeRequest,
  ScrapeStarted,
  SearchQueryParams,
  SearchResponse,
  SettingsPatch,
  SettingsResponse,
} from "./types";

/** Error carrying the user-facing message extracted from the API response. */
export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/** Pull a readable message out of a FastAPI error body (`detail`, or validation issues). */
async function errorDetail(response: Response): Promise<string> {
  const fallback =
    response.status >= 500 && response.status <= 504
      ? `Backend at 127.0.0.1:8000 unreachable (HTTP ${response.status}) — is the API server running?`
      : `Request failed (HTTP ${response.status})`;
  try {
    const body: unknown = await response.json();
    if (isRecord(body)) {
      const detail = body["detail"];
      if (typeof detail === "string" && detail.trim() !== "") return detail;
      if (Array.isArray(detail)) {
        // FastAPI request-validation errors: [{ loc, msg, type }, ...]
        const messages = detail
          .map((issue: unknown) =>
            isRecord(issue) && typeof issue["msg"] === "string" ? issue["msg"] : null,
          )
          .filter((msg): msg is string => msg !== null);
        if (messages.length > 0) return messages.join("; ");
      }
    }
  } catch {
    // Body was not JSON — fall through to the generic message.
  }
  return fallback;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, init);
  } catch {
    throw new ApiError("Cannot reach the backend at 127.0.0.1:8000 — is it running?", 0);
  }
  if (!response.ok) throw new ApiError(await errorDetail(response), response.status);
  const text = await response.text();
  if (text.trim() === "") return undefined as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ApiError("Backend returned an unreadable response", response.status);
  }
}

/** Build a query string, dropping empty/undefined values. */
export function buildQuery(values: Record<string, string | number | boolean | undefined>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value === undefined || value === null || value === "") continue;
    params.set(key, String(value));
  }
  const query = params.toString();
  return query === "" ? "" : `?${query}`;
}

function jsonInit(method: string, body?: unknown): RequestInit {
  return {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  };
}

/** Human-readable message for any thrown value. */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return String(error);
}

// --- Media -----------------------------------------------------------------

export function listMedia(params: MediaQueryParams): Promise<MediaListResponse> {
  return request<MediaListResponse>(`/api/media${buildQuery({ ...params })}`);
}

export function getRandomMedia(scope: RandomScope = "everything"): Promise<MediaDetail> {
  return request<MediaDetail>(`/api/media/random${buildQuery({ scope })}`);
}

export function getMedia(id: number): Promise<MediaDetail> {
  return request<MediaDetail>(`/api/media/${id}`);
}

export function updateMedia(id: number, patch: MediaUpdate): Promise<MediaDetail> {
  return request<MediaDetail>(`/api/media/${id}`, jsonInit("PATCH", patch));
}

export function deleteMedia(id: number): Promise<MediaDeleteResponse> {
  return request<MediaDeleteResponse>(`/api/media/${id}`, jsonInit("DELETE"));
}

export function unflagDup(id: number): Promise<MediaDetail> {
  return request<MediaDetail>(`/api/media/${id}/unflag-dup`, jsonInit("POST"));
}

/** Kick off a fresh AI analysis for a FAILED item; the body is not relied upon. */
export function reanalyzeMedia(id: number): Promise<void> {
  return request<void>(`/api/media/${id}/reanalyze`, jsonInit("POST"));
}

/** Same-origin URL for a stored file (`file` | `thumbnail` | `preview`). */
export function mediaFileUrl(id: number, target: "file" | "thumbnail" | "preview"): string {
  return `/api/media/${id}/${target}`;
}

// --- Scraper ---------------------------------------------------------------

export function startScrape(body: ScrapeRequest): Promise<ScrapeStarted> {
  return request<ScrapeStarted>("/api/scrape", jsonInit("POST", body));
}

export function getScrapeStatus(jobId: number): Promise<Job> {
  return request<Job>(`/api/scrape/${jobId}`);
}

// --- Jobs ------------------------------------------------------------------

export function listJobs(params: { status?: string; limit?: number } = {}): Promise<JobListResponse> {
  return request<JobListResponse>(`/api/jobs${buildQuery(params)}`);
}

export function jobAction(jobId: number, action: "pause" | "resume" | "cancel"): Promise<JobActionResponse> {
  return request<JobActionResponse>(`/api/jobs/${jobId}/${action}`, jsonInit("POST"));
}

// --- Background AI ---------------------------------------------------------

/** Live state of the background AI supervisor (`processing`/`on_hold`/`idle`/…). */
export function getAiStatus(): Promise<AiStatus> {
  return request<AiStatus>("/api/ai/status");
}

// --- Search ----------------------------------------------------------------

export function searchMedia(params: SearchQueryParams): Promise<SearchResponse> {
  return request<SearchResponse>(`/api/search${buildQuery({ ...params })}`);
}

// --- Settings --------------------------------------------------------------

export function getSettings(): Promise<SettingsResponse> {
  return request<SettingsResponse>("/api/settings");
}

export function updateSettings(patch: SettingsPatch): Promise<SettingsResponse> {
  return request<SettingsResponse>("/api/settings", jsonInit("PATCH", patch));
}

// --- Site-wide backfill -----------------------------------------------------

/** Backfill runs, newest first. */
export function listBackfills(limit = 10): Promise<BackfillListResponse> {
  return request<BackfillListResponse>(`/api/backfill${buildQuery({ limit })}`);
}

/** One run with per-status item counts. */
export function getBackfill(id: number): Promise<BackfillDetailOut> {
  return request<BackfillDetailOut>(`/api/backfill/${id}`);
}

/** Comics queued in a run (paginated; newest state first). */
export function listBackfillItems(
  id: number,
  params: { limit?: number; offset?: number } = {},
): Promise<BackfillItemsResponse> {
  return request<BackfillItemsResponse>(`/api/backfill/${id}/items${buildQuery(params)}`);
}

/** Start a run (202). Throws `ApiError` 400 when disabled/unsupported, 409 when one is running. */
export function startBackfill(): Promise<BackfillOut> {
  return request<BackfillOut>("/api/backfill/start", jsonInit("POST", {}));
}

/** Pause / resume / cancel / retry-failed on one run. */
export function backfillAction(
  id: number,
  action: "pause" | "resume" | "cancel" | "retry-failed",
): Promise<BackfillActionResponse> {
  return request<BackfillActionResponse>(`/api/backfill/${id}/${action}`, jsonInit("POST"));
}
