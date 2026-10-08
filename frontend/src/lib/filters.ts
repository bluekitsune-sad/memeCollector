/** Filter model shared by the gallery and the search results views (PRD §24). */

/** URL-backed filter values (everything is a plain string). */
export interface FilterValues {
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
}

export const FILTER_KEYS = [
  "type",
  "site",
  "format",
  "chapter",
  "date_from",
  "date_to",
  "processing_status",
  "dup_status",
  "is_favorite",
  "emotion",
] as const;

/** Hostnames of the MVP adapters (AGENTS.md §5) — merged with observed sites. */
export const SUPPORTED_SITES = ["asurascans.com", "mangadex.org", "mangapark.net", "comix.to"];

export const TYPE_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "All types" },
  { value: "image", label: "Image" },
  { value: "gif", label: "GIF" },
  { value: "video", label: "Video" },
];

export const DUP_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "All dup states" },
  { value: "dup", label: "Dup" },
  { value: "nondup", label: "Non-dup" },
  { value: "unflagged", label: "Unflagged" },
];

export const FORMAT_OPTIONS = [
  "jpg",
  "jpeg",
  "png",
  "gif",
  "webp",
  "bmp",
  "avif",
  "mp4",
  "webm",
];

/** PRD §24 emotion filters. */
export const EMOTION_OPTIONS = ["Angry", "Happy", "Confused", "Sad", "Surprised"];

/** Exact `processing_status` values (PRD §19) grouped for the select control. */
export const PROCESSING_GROUPS: { label: string; options: { value: string; label: string }[] }[] = [
  { label: "AI status", options: [{ value: "", label: "Any status" }] },
  {
    label: "Ready",
    options: [{ value: "READY", label: "Ready" }],
  },
  {
    label: "Processing",
    options: [
      { value: "DOWNLOADED", label: "Downloaded (queued)" },
      { value: "ANALYZING", label: "Analyzing" },
      { value: "ANALYZED", label: "Analyzed" },
      { value: "EMBEDDING", label: "Embedding" },
    ],
  },
  { label: "Failed", options: [{ value: "FAILED", label: "Failed" }] },
];

/** Read the filter keys out of the current URL. */
export function readFilters(params: URLSearchParams): FilterValues {
  return {
    type: params.get("type") ?? undefined,
    site: params.get("site") ?? undefined,
    format: params.get("format") ?? undefined,
    chapter: params.get("chapter") ?? undefined,
    date_from: params.get("date_from") ?? undefined,
    date_to: params.get("date_to") ?? undefined,
    processing_status: params.get("processing_status") ?? undefined,
    dup_status: params.get("dup_status") ?? undefined,
    is_favorite: params.get("is_favorite") ?? undefined,
    emotion: params.get("emotion") ?? undefined,
  };
}

/** Query-string view of the filters (empty values are dropped by `buildQuery`). */
export function filtersToQuery(filters: FilterValues): Record<string, string | undefined> {
  return { ...filters };
}

/** Is any filter set? Used for the "clear filters" affordance. */
export function hasActiveFilters(filters: FilterValues): boolean {
  return Object.values(filters).some((value) => value !== undefined && value !== "");
}

/** Site select options: supported hosts plus every site seen in the current page. */
export function siteOptions(observed: (string | null)[]): string[] {
  const merged = new Set<string>(SUPPORTED_SITES);
  for (const site of observed) {
    if (site) merged.add(site);
  }
  return [...merged].sort((a, b) => a.localeCompare(b));
}
